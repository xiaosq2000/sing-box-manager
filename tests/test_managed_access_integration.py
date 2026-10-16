"""Synthetic live-access checks on disposable PostgreSQL only."""

import json

import pytest
import yaml

from tests.test_control_plane_account_copy import NS, SOURCE
from tests.test_control_plane_account_copy_integration import (
    apply_once,
    authorize,
    case,
    invoke,
)
from tests.test_control_plane_integration import postgres, python


@pytest.fixture
def copied(case):
    authorize(case)
    apply_once(case)
    assert invoke(case, "close", namespace=NS, source_identity=SOURCE).returncode == 0
    data = yaml.safe_load(case["source"].read_text())
    data["config_root"] = str(case["root"] / "synthetic-config")
    data["control_plane"].update(
        {
            "subscription_secret": "synthetic-independent-live-signing-state-"
            + "x" * 32,
            "portal_enabled": True,
            "portal_origin": "https://vpn.example.test",
        }
    )
    case["source"].write_text(yaml.safe_dump(data))
    case["env"] = {**case["env"], "SBM_CONFIG": str(case["source"])}
    python(
        case["env"],
        "import django; django.setup(); from django.core.management import call_command; call_command('migrate',verbosity=0); call_command('check'); call_command('makemigrations',check=True,dry_run=True,verbosity=0)",
    )
    return case


def run(case, script):
    return python(
        case["env"],
        """
import django; django.setup()
from uuid import UUID
from pathlib import Path
from django.conf import settings
from sing_box_manager.control_plane.django_settings import runtime
from sing_box_manager.control_plane.models import ProxySubject,ImportedAccess,LegacyAccess,ImportRun,User
from sing_box_manager.managed_access.models import Ownership,LinkGeneration
from sing_box_manager.managed_access import service,storage
from sing_box_manager.control_plane.import_schema import canonical,AccessRecords
from sing_box_manager.account_copy.schema import copy_context_identity
storage.FENCE = Path(runtime.require_config_path()).parent / 'owner.json'
"""
        + script,
    )


def prepare_activate():
    return f"""
service.prepare(runtime, UUID('{NS}'))
value = service.owner(runtime)
storage.FENCE.write_bytes(canonical({{'version':1,'identity':str(value.identity)}})+b'\\n')
storage.FENCE.chmod(0o600)
service.begin_application(runtime,value.identity,value.revision,copy_context_identity(runtime))
service.record_application(runtime,value.identity,value.revision,copy_context_identity(runtime),'a'*64,failed=False)
service.activate(runtime)
value=service.owner(runtime,active=True)
"""


def test_prepare_is_not_activation_and_preserves_copy(copied):
    output = run(
        copied,
        f"""
before=list(ProxySubject.objects.order_by('username').values())
assert not Ownership.objects.exists()
result=service.prepare(runtime,UUID('{NS}'))
assert result['prepared'] and not result['noop']
value=service.owner(runtime)
assert not value.active and value.applied_revision==0 and value.revision==1
assert LinkGeneration.objects.count()==len(before)
assert list(ProxySubject.objects.order_by('username').values())==before
assert ImportRun.objects.count()==1 and not LegacyAccess.objects.exists()
assert service.prepare(runtime,UUID('{NS}'))['noop']
try: service.activate(runtime)
except service.AccessRejected: pass
else: raise AssertionError('activation without apply')
print('prepared-only')
""",
    )
    assert output.strip() == "prepared-only"


def test_edits_retirement_and_durable_fresh_links(copied):
    run(
        copied,
        prepare_activate()
        + """
from sing_box_manager.subscription import subscription_token
subject=ProxySubject.objects.get(username='alice')
link=LinkGeneration.objects.get(subject=subject)
first=service.token(runtime,subject.pk,link.generation)
assert len(first)==22 and service.resolve(runtime,value,first)[0].pk==subject.pk
old=subscription_token(runtime.web.subscription_secret,'alice')
assert service.resolve(runtime,value,old) is None
service.reset_link(runtime,subject.pk)
assert service.resolve(runtime,value,first) is None
link.refresh_from_db()
second=service.token(runtime,subject.pk,link.generation)
assert service.resolve(runtime,value,second)[0].pk==subject.pk
records=service.accounts(value)
rows={k:[row.model_dump(mode='json') for row in getattr(records,k) if row.username=='alice'] for k in ('portal','trojan','hysteria2','naive')}
rows['trojan'][0]['password']='synthetic-updated-protocol'
changed=AccessRecords.model_validate(rows)
service.save_account(runtime,'alice',changed,subject_id=subject.pk,expected_revision=1)
value.refresh_from_db()
assert value.revision==2 and not service.ready(value,runtime)
assert ImportedAccess.objects.get(subject=subject,kind='trojan').payload['password']=='synthetic-updated-protocol'
try: service.save_account(runtime,'alice',changed,subject_id=subject.pk,expected_revision=1)
except service.AccessRejected as error: assert str(error)=='account_edit_stale'
else: raise AssertionError('stale edit allowed')
service.save_account(runtime,'alice',changed,subject_id=subject.pk,expected_revision=2)
assert Ownership.objects.get().revision==2
service.retire(runtime,subject.pk,expected_revision=2)
subject.refresh_from_db()
assert not subject.present and not ImportedAccess.objects.filter(subject=subject,present=True).exists()
assert service.resolve(runtime,value,second) is None
try: service.save_account(runtime,'alice',changed,subject_id=None,expected_revision=3)
except service.AccessRejected as error: assert str(error)=='username_already_reserved'
else: raise AssertionError('retired username reused')
assert ProxySubject.objects.filter(pk=subject.pk).exists()
assert ImportRun.objects.count()==1 and not LegacyAccess.objects.exists()
""",
    )


def test_application_failure_and_stale_receipts_do_not_claim_success(copied):
    run(
        copied,
        prepare_activate()
        + """
context=copy_context_identity(runtime)
Ownership.objects.filter(pk=1).update(revision=2)
service.begin_application(runtime,value.identity,2,context)
result=service.record_application(runtime,value.identity,2,context,'b'*64,failed=True)
assert result['pending'] and result['applied_revision']==1
assert not service.ready(service.owner(runtime),runtime)
service.begin_application(runtime,value.identity,2,context)
result=service.record_application(runtime,value.identity,2,context,'b'*64,failed=False)
assert not result['pending'] and result['applied_revision']==2
for failed in (True,False):
    try: service.record_application(runtime,value.identity,1,context,'a'*64,failed=failed)
    except ValueError as error: assert str(error)=='stale_node_receipt'
    else: raise AssertionError('stale receipt accepted')
assert service.ready(service.owner(runtime),runtime)
""",
    )


def test_public_subscription_rules_and_host_boundary(copied):
    run(
        copied,
        prepare_activate()
        + """
from django.test import Client,override_settings
from sing_box_manager.managed_access import public_settings as public
client=Client(HTTP_HOST='vpn.example.test',HTTP_X_FORWARDED_PROTO='https')
with override_settings(**{key:getattr(public,key) for key in ('ROOT_URLCONF','MIDDLEWARE','ALLOWED_HOSTS','SBM_RUNTIME','CSRF_TRUSTED_ORIGINS','SECURE_PROXY_SSL_HEADER')}):
    def url(name):
        subject=ProxySubject.objects.get(username=name)
        link=LinkGeneration.objects.get(subject=subject)
        return '/sub/'+service.token(runtime,subject.pk,link.generation)
    assert client.get(url('alice'),{'format':'sbc','os':'linux','sing-box':'1.14.2'}).status_code==200
    response=client.get(url('alice'),{'format':'sbc','os':'linux','sing-box':'1.14.2'})
    assert response['Cache-Control']=='private, no-store'
    assert 'synthetic-access-value' in response.content.decode()
    assert client.get(url('alice'),{'format':'sbc','os':'linux','sing-box':'1.14.2'},HTTP_IF_NONE_MATCH=response['ETag']).status_code==304
    assert client.get(url('alice'),{'format':'sbc','os':'linux','sing-box':'1.1.0'}).status_code==409
    assert client.get(url('alice'),{'format':'other','os':'linux','sing-box':'1.14.2'}).status_code==400
    for name in ('disabled','portal_only','proxy_only'):
        assert client.get(url(name),{'format':'sbc','os':'linux','sing-box':'1.14.2'}).status_code==404
    assert client.get('/admin/').status_code==404
    assert client.get('/api/token').status_code==404
    assert client.get('/files/archive.zip').status_code==404
    assert client.get('/',HTTP_HOST='attacker.example').status_code==400
    assert client.get('/files').status_code==302
    assert client.get('/install.sh').status_code==200
""",
    )
