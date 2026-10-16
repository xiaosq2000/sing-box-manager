"""Public/private boundary tests with synthetic accounts and disposable PostgreSQL."""

from tests.test_control_plane_account_copy_integration import case
from tests.test_control_plane_integration import postgres
from tests.test_managed_access_integration import copied, prepare_activate, run


def test_sparse_successful_logins_do_not_accumulate_global_lockouts(copied):
    run(
        copied,
        prepare_activate()
        + """
from types import SimpleNamespace
from sing_box_manager.managed_access import auth
from sing_box_manager.web import auth as legacy_auth
clock=[1000.0]
legacy_auth.time=SimpleNamespace(time=lambda:clock[0])
auth.time=SimpleNamespace(monotonic=lambda:clock[0])
auth._verify_with_capacity=lambda *args: True
for attempt in range(75):
    assert auth.authenticate(runtime,value,'alice','synthetic-login-value','192.0.2.1') is not None, f'Unexpected lockout after {attempt+1} spaced logins'
    clock[0]+=61
budget=auth._WindowLimiter(max_entries=2)
for attempt in range(30): assert budget.check('portal','192.0.2.2')
assert not budget.check('portal','192.0.2.2')
assert budget.check('portal','192.0.2.3')
assert not budget.check('portal','192.0.2.4')
clock[0]+=61
assert budget.check('portal','192.0.2.4')
assert budget.check('portal','192.0.2.2')
assert len(budget._attempts)<=2
""",
    )


def public(case, script):
    selected = {
        **case,
        "env": {
            **case["env"],
            "DJANGO_SETTINGS_MODULE": "sing_box_manager.managed_access.public_settings",
        },
    }
    return run(
        selected,
        prepare_activate()
        + """
from django.test import Client,override_settings
from django.core.management import call_command
call_command('check')
client=Client(enforce_csrf_checks=True,HTTP_HOST='vpn.example.test',HTTP_X_FORWARDED_PROTO='https')
"""
        + script,
    )


def test_csrf_password_sessions_and_operator_isolation(copied):
    public(
        copied,
        """
import base64
assert client.get('/',HTTP_X_FORWARDED_PROTO='http').status_code==400
assert client.get('/').status_code==200
assert client.cookies['sbm_portal_csrf']['secure']
csrf=client.cookies['sbm_portal_csrf'].value
fields={'username':'alice','password':'synthetic-login-value','csrfmiddlewaretoken':csrf}
assert client.post('/',fields,HTTP_ORIGIN='null').status_code==403
assert client.post('/',fields,HTTP_ORIGIN='https://attacker.example').status_code==403
response=client.post('/',fields,HTTP_ORIGIN='https://vpn.example.test')
assert response.status_code==302
assert client.cookies['sbm_portal_csrf'].value != csrf
csrf=client.cookies['sbm_portal_csrf'].value
assert response['Referrer-Policy']=='same-origin'
assert client.cookies['sbm_portal_session']['secure']
assert client.cookies['sbm_portal_session']['httponly']
assert client.cookies['sbm_portal_session']['samesite']=='Strict'
page=client.get('/files')
assert page.status_code==200 and b'https://vpn.example.test/sub/' in page.content
assert b'password_hash' not in page.content and b'synthetic-access-value' not in page.content
assert client.get('/admin/').status_code==404
assert client.get('/subscription/reset').status_code==405
assert client.post('/subscription/reset',{},HTTP_ORIGIN='https://vpn.example.test').status_code==403
subject=ProxySubject.objects.get(username='alice')
link=LinkGeneration.objects.get(subject=subject)
old=service.token(runtime,subject.pk,link.generation)
assert client.post('/subscription/reset',{'csrfmiddlewaretoken':csrf},HTTP_ORIGIN='https://vpn.example.test').status_code==302
assert service.resolve(runtime,value,old) is None
assert client.get('/sub/'+old,{'format':'sbc','os':'linux','sing-box':'1.14.2'}).status_code==404
# A portal session does not become an operator session if its cookie is renamed.
portal_cookie=client.cookies['sbm_portal_session'].value
private=Client(HTTP_HOST='127.0.0.1')
private.cookies['sbm_control_session']=portal_cookie
from sing_box_manager.control_plane import django_settings as private_settings
with override_settings(ROOT_URLCONF=private_settings.ROOT_URLCONF,MIDDLEWARE=private_settings.MIDDLEWARE,ALLOWED_HOSTS=private_settings.ALLOWED_HOSTS,SESSION_COOKIE_NAME=private_settings.SESSION_COOKIE_NAME):
    assert private.get('/admin/').status_code==302
operator=User.objects.create_superuser(username='synthetic_operator',password='synthetic-operator-password')
other=Client(HTTP_HOST='vpn.example.test',HTTP_X_FORWARDED_PROTO='https')
other.force_login(operator)
assert other.get('/files').status_code==302
assert other.get('/admin/').status_code==404
assert client.post('/api/sub').status_code==401
header='Basic '+base64.b64encode(b'alice:synthetic-login-value').decode()
exchange=client.post('/api/sub',HTTP_AUTHORIZATION=header)
assert exchange.status_code==200 and exchange.json()['username']=='alice'
assert client.post('/api/sub',HTTP_AUTHORIZATION='Bearer '+old).status_code==401
assert client.get('/api/token').status_code==404
# Changing the verifier invalidates an existing portal session.
from argon2 import PasswordHasher
row=ImportedAccess.objects.get(subject=subject,kind='portal')
row.payload['password_hash']=PasswordHasher(time_cost=1,memory_cost=1024,parallelism=1).hash('synthetic-replacement-login')
row.save(update_fields=['payload'])
assert client.get('/files').status_code==302
""",
    )


def test_download_rules_paths_revocation_and_pending_state(copied):
    public(
        copied,
        """
from types import SimpleNamespace
from sing_box_manager.managed_access import views
from sing_box_manager.release.client_downloads import CLIENT_MANIFEST_NAME
artifact=Path(runtime.require_config_path()).parent/'synthetic-manifest.json'
artifact.write_text('{"synthetic":true}')
views.downloads=lambda runtime:SimpleNamespace(sbc_version='1.0.0',path_for=lambda name:artifact if name==CLIENT_MANIFEST_NAME else None)
def url(name):
    subject=ProxySubject.objects.get(username=name)
    link=LinkGeneration.objects.get(subject=subject)
    return '/sub/'+service.token(runtime,subject.pk,link.generation)
for name in ('alice','portal_only'):
    result=client.get(url(name)+'/files/'+CLIENT_MANIFEST_NAME)
    assert result.status_code==200 and b''.join(result.streaming_content)==b'{"synthetic":true}'
for name in ('proxy_only','disabled'):
    assert client.get(url(name)+'/files/'+CLIENT_MANIFEST_NAME).status_code==404
for name in ('../source.yaml','..%2fsource.yaml','unknown','client-manifest.json/../source.yaml'):
    assert client.get(url('alice')+'/files/'+name).status_code==404
assert client.get('/sub/'+'A'*22+'/files/'+CLIENT_MANIFEST_NAME).status_code==404
for platform in ('linux','darwin','windows'):
    result=client.get(url('alice'),{'format':'sbc','os':platform,'sing-box':'1.14.2'})
    assert result.status_code==200 and result['Referrer-Policy']=='no-referrer'
    assert result.json()['latest']['sbc']=='1.0.0'
old=url('alice')
service.reset_link(runtime,ProxySubject.objects.get(username='alice').pk)
assert client.get(old+'/files/'+CLIENT_MANIFEST_NAME).status_code==404
Ownership.objects.filter(pk=1).update(last_error='node_application_failed')
assert client.get(url('alice'),{'format':'sbc','os':'linux','sing-box':'1.14.2'}).status_code==503
assert client.get(url('alice')+'/files/'+CLIENT_MANIFEST_NAME).status_code==200
for path in ('/install.sh','/install/linux.sh','/install/macos.sh','/install.ps1','/install/windows.ps1'):
    assert client.get(path).status_code==200
""",
    )


def test_private_admin_preserves_passwords_and_rejects_stale_edits(copied):
    run(
        copied,
        prepare_activate()
        + """
from django.test import Client,override_settings
from django.contrib.admin.models import LogEntry
operator=User.objects.create_superuser(username='synthetic_operator',password='synthetic-operator-password')
client=Client(HTTP_HOST='127.0.0.1')
client.force_login(operator)
subject=ProxySubject.objects.get(username='alice')
rows={row.kind:row.payload.copy() for row in ImportedAccess.objects.filter(subject=subject,present=True)}
path='/admin/control_plane/proxysubject/'+str(subject.pk)+'/change/'
fields={'username':'attempted_rename','expected_revision':'1','portal_admin':'on','_save':'Save'}
for kind in ('portal','trojan','hysteria2','naive'):
    fields[kind+'_member']='on'
    fields[kind+'_enabled']='on'
fields.pop('trojan_enabled')
with override_settings(STORAGES={'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}}):
    page=client.get(path)
    assert page.status_code==200 and b'https://vpn.example.test/sub/' in page.content
    assert 'no-store' in page['Cache-Control']
    for payload in rows.values():
        assert payload.get('password',payload.get('password_hash')).encode() not in page.content
    response=client.post(path,fields)
    assert response.status_code==302
    subject.refresh_from_db()
    assert subject.username=='alice' and subject.present
    saved={row.kind:row.payload for row in ImportedAccess.objects.filter(subject=subject,present=True)}
    expected={**rows,'trojan':{**rows['trojan'],'enabled':False}}
    assert saved==expected
    assert Ownership.objects.get().revision==2
    assert 'password_hash' not in str(list(LogEntry.objects.values('change_message')))
    response=client.post(path,fields)
    assert response.status_code==200 and b'Accounts changed' in response.content
    assert Ownership.objects.get().revision==2
    fields.update(expected_revision='2',revoke_link='on')
    assert client.post(path,fields).status_code==302
    assert Ownership.objects.get().revision==2
    assert LinkGeneration.objects.get(subject=subject).generation==2
    fields.update(expected_revision='2',retire_account='on')
    assert client.post(path,fields).status_code==302
    subject.refresh_from_db()
    assert not subject.present and ProxySubject.objects.filter(pk=subject.pk).exists()
    assert User.objects.count()==1
""",
    )


def test_prepared_link_is_private_and_view_only_operators_cannot_read_it(copied):
    run(
        copied,
        prepare_activate()
        + """
from django.test import Client,override_settings
from django.contrib.auth.models import Permission
Ownership.objects.filter(pk=1).update(active=False)
editor=User.objects.create_superuser(username='synthetic_editor',password='synthetic-editor-password')
viewer=User.objects.create_user(username='synthetic_viewer',password='synthetic-viewer-password',is_staff=True)
viewer.user_permissions.add(Permission.objects.get(codename='view_proxysubject'))
subject=ProxySubject.objects.get(username='alice')
path='/admin/control_plane/proxysubject/'+str(subject.pk)+'/change/'
with override_settings(STORAGES={'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}}):
    client=Client(HTTP_HOST='127.0.0.1')
    client.force_login(editor)
    result=client.get(path)
    assert result.status_code==200 and b'https://vpn.example.test/sub/' in result.content
    assert b'name="_save"' not in result.content
    client.force_login(viewer)
    result=client.get(path)
    assert result.status_code==200 and b'https://vpn.example.test/sub/' not in result.content
assert not Ownership.objects.get().active
""",
    )


def test_exchange_cannot_get_a_replacement_url_with_a_replaced_password(copied):
    public(
        copied,
        """
import base64
from django.db import transaction
from argon2 import PasswordHasher
from sing_box_manager.managed_access import auth
replacement=PasswordHasher(time_cost=1,memory_cost=1024,parallelism=1).hash('synthetic-replacement-password')
def verify_then_change(verifier,password):
    with transaction.atomic():
        service.owner(runtime,active=True,lock=True)
        subject=ProxySubject.objects.get(username='alice')
        row=ImportedAccess.objects.get(subject=subject,kind='portal')
        row.payload['password_hash']=replacement
        row.save(update_fields=['payload'])
        service.revoke_link(runtime,subject.pk)
    return True
# Model a credential change that commits while the old password is checked.
auth._verify_password=verify_then_change
header='bAsIc '+base64.b64encode(b'alice:synthetic-login-value').decode()
response=client.post('/api/sub',HTTP_AUTHORIZATION=header)
assert response.status_code==401
assert b'/sub/' not in response.content
""",
    )


def test_password_capacity_is_shared_between_database_connections(copied):
    run(
        copied,
        prepare_activate()
        + """
from concurrent.futures import ThreadPoolExecutor
from django.db import connection,connections,transaction
from sing_box_manager.managed_access import auth
def forbidden(*args): raise AssertionError('A second password KDF must not start')
auth._verify_password=forbidden
def authenticate():
    try: return auth.authenticate(runtime,value,'alice','synthetic-login-value','192.0.2.1')
    finally: connections.close_all()
with transaction.atomic(),connection.cursor() as cursor:
    cursor.execute('SELECT pg_advisory_xact_lock(%s)',[auth._PASSWORD_LOCK])
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(authenticate).result(timeout=15) is None
""",
    )


def test_concurrent_reset_is_durable_and_does_not_touch_legacy_state(copied):
    run(
        copied,
        prepare_activate()
        + """
from concurrent.futures import ThreadPoolExecutor
from django.db import connections
subject=ProxySubject.objects.get(username='alice')
def reset():
    try: service.reset_link(runtime,subject.pk)
    finally: connections.close_all()
with ThreadPoolExecutor(max_workers=2) as pool:
    results=[pool.submit(reset) for _ in range(2)]
    for future in results: future.result(timeout=20)
assert LinkGeneration.objects.get(subject=subject).generation==3
for generation in (1,2):
    assert service.resolve(runtime,value,service.token(runtime,subject.pk,generation)) is None
assert service.resolve(runtime,value,service.token(runtime,subject.pk,3)) is not None
assert not LegacyAccess.objects.exists() and ImportRun.objects.count()==1
assert not runtime.web.subscription_database_path.exists()
""",
    )
