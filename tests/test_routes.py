"""The single Django portal: database authority, permissions, and compatibility."""

import pytest
import yaml

from tests.test_control_plane_integration import (
    ROOT,
    cli,
    config,
    provision_sql,
    psql,
    python,
)
from tests.test_control_plane_integration import (
    postgres as postgres_fixture,
)

postgres = postgres_fixture


@pytest.fixture(scope="module")
def portal_runtime(postgres, tmp_path_factory):
    root = tmp_path_factory.mktemp("portal")
    database = "sbm_portal"
    assert psql(postgres, "postgres", provision_sql(database)).returncode == 0
    path, _, env = config(postgres, root, database)
    runtime = yaml.safe_load(path.read_text())
    example = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    for name in ("deployment", "web_portal", "trojan", "hysteria2", "naive"):
        runtime[name] = example[name]
    runtime["deployment"].pop("snell_port", None)
    runtime["control_plane"]["authoritative"] = True
    runtime["web"] = {
        "subscription_secret": "synthetic-subscription-secret-" + "2" * 24,
        "session_secret": "synthetic-session-secret-" + "3" * 24,
        "allowed_hosts": ["testserver"],
    }
    runtime["releases_root"] = str(root / "releases")
    runtime["traffic_stats"] = {"enabled": True, "database_path": str(root / "traffic.sqlite3")}
    path.write_text(yaml.safe_dump(runtime))
    for action in ("migrate", "collect-static"):
        result = cli(path, env, action)
        assert result.returncode == 0, result.stderr
    return env


SETUP = """
import base64
import json
from pathlib import Path
from unittest.mock import Mock, patch
import django
django.setup()
from django.test import Client, override_settings
from django.contrib.auth.models import Permission
from sing_box_manager.control_plane import django_settings, services, portal, views, compat
from sing_box_manager.control_plane.models import Account, Credential, User
from sing_box_manager.settings import ARGON2ID_VERIFIER
from sing_box_manager.subscription import subscription_token
from sing_box_manager.web.auth import MachineTokenManager
runtime = django_settings.runtime
Account.objects.all().delete()
User.objects.all().delete()
alice = Account.objects.create(username='db_alice', portal_password_hash=ARGON2ID_VERIFIER.hash('synthetic-legacy-password'))
bob = Account.objects.create(username='db_bob')
for account in (alice, bob):
    for protocol in ('trojan', 'hysteria2', 'naive'):
        Credential.objects.create(account=account, protocol=protocol, secret=f'synthetic-{account.username}-{protocol}')
token = subscription_token(runtime.web.subscription_secret, alice.username, alice.subscription_counter)
url = f'/sub/{token}'
client = Client(HTTP_HOST='127.0.0.1', enforce_csrf_checks=True)
basic = 'Basic ' + base64.b64encode(b'db_alice:synthetic-legacy-password').decode()
bearer = 'Bearer ' + MachineTokenManager(runtime.web.session_secret).create_token(alice.username)
query = {'format': 'sbc', 'os': 'linux', 'sing-box': '1.14.2'}
"""


def test_db_only_accounts_keep_links_protocols_and_user_pages(portal_runtime):
    python(portal_runtime, SETUP + """
first = client.get(url, query)
assert first.status_code == 200
assert 'synthetic-db_alice-trojan' in first.content.decode()
assert 'synthetic-db_bob' not in first.content.decode()
assert first.json()['nodes'][0]['protocols'] == ['trojan', 'hysteria2', 'naive']
assert first.json()['default_node'] == 'vpn.example.com'
assert first.json()['nodes'][0]['region'] == {
    'timezone': 'Asia/Shanghai',
    'language': 'zh-CN',
}
page = client.get(url + '/')
assert page.status_code == 200, page.content
text = page.content.decode()
assert 'db_alice' in text and 'db_bob' not in text
assert '/install.sh' in text and '/install.ps1' in text
assert '{%' not in text and '{{' not in text
assert 'format=mobile' in text
assert '/login' not in text and '/subscription/reset' not in text
assert client.get(url, query, HTTP_IF_NONE_MATCH=first['ETag']).status_code == 304
assert client.get(url, {**query, 'os':'darwin'}, HTTP_IF_NONE_MATCH=first['ETag']).status_code == 200
mobile = client.get(url, {'format':'mobile'})
assert mobile.status_code == 200
assert mobile.json()['outbounds'][0]['outbounds'] == ['trojan', 'hysteria2', 'naive']
assert client.get(url, {'format':'mobile', 'protocol':'naive'}).status_code == 400
assert client.get(url, {**query, 'sing-box':'1.13.0'}).status_code == 409
assert client.get(url, {**query, 'os':'freebsd'}).status_code == 400
assert client.head(url, query).content == b''
bob_url = '/sub/' + subscription_token(runtime.web.subscription_secret, bob.username)
assert bob.portal_password_hash == ''
assert client.get(bob_url).status_code == 200
""")


def test_mobile_import_buttons_and_qr_share_authorized_remote_profiles(portal_runtime):
    python(portal_runtime, SETUP + """
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit, unquote
from io import BytesIO
from PIL import Image
import zxingcpp
class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
page = client.get(url + '/', secure=True, HTTP_HOST='testserver')
assert page.status_code == 200
assert page['Cache-Control'] == 'private, no-store'
assert page['Referrer-Policy'] == 'no-referrer'
parser = Elements()
parser.feed(page.content.decode())
imports = [a['href'] for tag, a in parser.elements if tag == 'a' and a.get('href', '').startswith('sing-box:')]
images = [a['src'] for tag, a in parser.elements if tag == 'img' and a.get('class') == 'mobile-qr']
assert len(imports) == len(images) == 1
uri, image = imports[0], images[0]
parts = urlsplit(uri)
assert (parts.scheme, parts.netloc) == ('sing-box', 'import-remote-profile')
remote = parse_qs(parts.query)['url'][0]
assert remote == 'https://testserver' + url + '?format=mobile'
assert unquote(parts.fragment) == 'db_alice - sing-box'
decoded = zxingcpp.read_barcode(Image.open(BytesIO(base64.b64decode(image.split(',', 1)[1]))).convert('RGB'))
assert decoded is not None and decoded.text == uri
response = client.get(remote)
assert response.status_code == 200
assert response['Cache-Control'] == 'private, no-store'
assert response['Referrer-Policy'] == 'no-referrer'
assert 'subscription-userinfo' in response
assert response.json()['outbounds'][0]['outbounds'] == ['trojan', 'hysteria2', 'naive']
for protocol in ('trojan', 'hysteria2', 'naive'):
    assert 'synthetic-db_alice-' + protocol in response.content.decode()
assert 'synthetic-db_bob' not in response.content.decode()
assert client.get(remote, HTTP_IF_NONE_MATCH=response['ETag']).status_code == 304
assert client.head(remote).content == b''
inputs = [attrs for tag, attrs in parser.elements if tag == 'input']
assert len(inputs) == 2 and all('readonly' in attrs for attrs in inputs)
assert all(attrs['value'].startswith('https://testserver/sub/') for attrs in inputs)
buttons = [attrs for tag, attrs in parser.elements if tag == 'button']
statuses = {attrs['id']: attrs for tag, attrs in parser.elements if attrs.get('role') == 'status'}
assert len(buttons) == len(statuses) == 2
assert len({button['data-copy-status'] for button in buttons}) == 2
for button in buttons:
    assert button['data-copy-target'] in {attrs['id'] for attrs in inputs}
    assert statuses[button['data-copy-status']]['aria-live'] == 'polite'
    button_index = next(index for index, (_, attrs) in enumerate(parser.elements) if attrs == button)
    status_index = next(index for index, (_, attrs) in enumerate(parser.elements) if attrs.get('id') == button['data-copy-status'])
    assert 0 < status_index - button_index <= 2
assert 'Protocols:' not in page.content.decode()
downloads = [attrs for tag, attrs in parser.elements if tag == 'a' and attrs.get('download')]
assert len(downloads) == 1 and downloads[0]['href'] == remote
assert all(attrs['src'].startswith('/static/') for tag, attrs in parser.elements if tag == 'script')
alice.subscription_counter += 1
alice.save()
assert client.get(url + '/').status_code == 404
for uri in imports:
    assert client.get(parse_qs(urlsplit(uri).query)['url'][0]).status_code == 404
new_token = subscription_token(runtime.web.subscription_secret, alice.username, alice.subscription_counter)
assert client.get('/sub/' + new_token + '/').status_code == 200
""")


def test_readiness_requires_the_subscription_script(portal_runtime):
    python(portal_runtime, SETUP + """
assert client.get('/health/').status_code == 200
with patch.object(views.staticfiles_storage, 'exists', side_effect=lambda name: not name.endswith('.js')):
    assert client.get('/health/').status_code == 503
""")


def test_unified_mobile_profile_omits_disabled_protocols_and_rejects_old_links(portal_runtime):
    python(portal_runtime, SETUP + """
Credential.objects.filter(account=alice).exclude(protocol='trojan').update(enabled=False)
page = client.get(url + '/')
assert page.status_code == 200
text = page.content.decode()
assert text.count('class="mobile-qr"') == 1
assert 'protocol=' not in text and 'mobile-title-trojan' not in text
profile = client.get(url, {'format': 'mobile'})
assert profile.status_code == 200
assert profile.json()['outbounds'][0]['outbounds'] == ['trojan']
assert profile.json()['outbounds'][0]['default'] == 'trojan'
assert 'synthetic-db_alice-naive' not in profile.content.decode()
assert 'synthetic-db_alice-hysteria2' not in profile.content.decode()
for protocol in ('trojan', 'hysteria2', 'naive', 'unknown', ''):
    for query in ({'protocol': protocol}, {'format': 'mobile', 'protocol': protocol}):
        rejected = client.get(url, query)
        assert rejected.status_code == 400
        assert 'unified profile' in rejected.json()['detail']
assert client.get(url + '/', HTTP_HOST='attacker.example.com').status_code == 400
assert client.get(url, {'format':'mobile'}, HTTP_HOST='attacker.example.com').status_code == 400
runtime.default_protocol = 'naive'
with patch.object(views, '_runtime', return_value=runtime):
    assert client.get(url).json()['outbounds'][0]['default'] == 'trojan'
Credential.objects.filter(account=alice, protocol='trojan').update(enabled=False)
assert client.get(url).status_code == 404
assert client.get(url + '/').status_code == 404
""")


def test_legacy_exchange_uses_current_postgres_counter_and_credentials(portal_runtime):
    python(portal_runtime, SETUP + """
for authorization in (basic, bearer):
    response = client.post('/api/sub', HTTP_AUTHORIZATION=authorization)
    assert response.status_code == 200, response.content
    assert response.json() == {'url': 'http://127.0.0.1' + url, 'username': alice.username}
assert client.get('/auth', HTTP_AUTHORIZATION=bearer).json() == {'authenticated': True}
machine = client.post('/api/token', HTTP_AUTHORIZATION=basic)
assert machine.status_code == 200
assert MachineTokenManager(runtime.web.session_secret).validate_token(machine.json()['token']) == alice.username
assert client.post('/api/token', HTTP_AUTHORIZATION=bearer).status_code == 401
alice.subscription_counter = 4
alice.save()
new_url = '/sub/' + subscription_token(runtime.web.subscription_secret, alice.username, 4)
assert client.get(url).status_code == 404
assert client.post('/api/sub', HTTP_AUTHORIZATION=bearer).json()['url'].endswith(new_url)
alice.portal_password_hash = ARGON2ID_VERIFIER.hash('changed-synthetic-password')
alice.save()
assert client.post('/api/sub', HTTP_AUTHORIZATION=basic).status_code == 401
assert client.get('/auth', HTTP_AUTHORIZATION=bearer).status_code == 200
""")


@pytest.mark.parametrize("field", ["enabled", "portal_enabled"])
def test_disable_revokes_all_portal_entry_points(portal_runtime, field):
    python(portal_runtime, SETUP + f"""
setattr(alice, {field!r}, False)
alice.save()
for path in (url, url + '/', url + '/files/manifest.json'):
    assert client.get(path).status_code == 404
for authorization in (basic, bearer):
    assert client.post('/api/sub', HTTP_AUTHORIZATION=authorization).status_code == 401
    assert client.get('/auth', HTTP_AUTHORIZATION=authorization).status_code == 401
    assert client.get('/api/usage', HTTP_AUTHORIZATION=authorization).status_code == 401
setattr(alice, {field!r}, True)
alice.save()
assert client.get(url).status_code == 200
assert client.post('/api/sub', HTTP_AUTHORIZATION=bearer).status_code == 200
""")


@pytest.mark.parametrize("authorization", ["", "Basic ???", "Basic YWxpY2U=", "Bearer invalid", "Basic /w=="])
def test_malformed_or_missing_authentication_fails_closed(portal_runtime, authorization):
    python(portal_runtime, SETUP + f"""
assert client.post('/api/sub', HTTP_AUTHORIZATION={authorization!r}).status_code == 401
""")


def test_browser_cookies_and_forged_csrf_cannot_manage_links(portal_runtime):
    python(portal_runtime, SETUP + """
client.cookies['session'] = MachineTokenManager(runtime.web.session_secret).create_token(alice.username)
assert client.post('/api/sub').status_code == 401
assert client.post('/subscription/reset').status_code == 404
assert client.post('/login', {'username':'db_alice','password':'synthetic-legacy-password'}).status_code == 404
assert client.post(url + '/').status_code == 403
alice.refresh_from_db()
assert alice.subscription_counter == 0
assert client.get('/download?platform=android-arm64').status_code == 410
assert client.get('/').status_code == 410
""")


def test_bad_passwords_are_limited_without_hashing_after_lockout(portal_runtime):
    python(portal_runtime, SETUP + """
wrong = 'Basic ' + base64.b64encode(b'db_alice:wrong').decode()
for _ in range(5):
    assert client.get('/auth', HTTP_AUTHORIZATION=wrong).status_code == 401
with patch.object(compat, 'ARGON2ID_VERIFIER') as verifier:
    assert client.get('/auth', HTTP_AUTHORIZATION=basic).status_code == 401
    verifier.verify.assert_not_called()
assert client.get('/auth', HTTP_AUTHORIZATION=bearer).status_code == 200
""")


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/traffic/", "/admin/traffic/connections/", "/admin/traffic/users/db_alice/"])
def test_all_admin_paths_are_private_even_for_authenticated_operators(portal_runtime, path):
    python(portal_runtime, SETUP + f"""
operator = User.objects.create_superuser('operator', 'operator@example.com', 'synthetic-operator-password')
client.force_login(operator)
assert client.get({path!r}, HTTP_HOST='vpn.example.com').status_code == 404
for header in ('HTTP_X_FORWARDED_FOR', 'HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_PROTO'):
    assert client.get({path!r}, **{{header: 'fixture'}}).status_code == 404
assert client.get({path!r}, HTTP_HOST='attacker.example.com').status_code == 400
""")


def test_operator_traffic_permission_is_separate_from_account_management(portal_runtime):
    python(portal_runtime, SETUP + """
operator = User.objects.create_user('viewer', is_staff=True)
client.force_login(operator)
assert client.get('/admin/traffic/').status_code == 403
operator.user_permissions.add(Permission.objects.get(codename='view_traffic'))
for path in ('/admin/traffic/', '/admin/traffic/users/db_alice/', '/admin/traffic/connections/'):
    response = client.get(path)
    assert response.status_code == 200
    assert b'{%' not in response.content and b'{{' not in response.content
assert client.get('/admin/control_plane/account/').status_code == 403
assert client.get('/admin/traffic/users/missing/').status_code == 404
operator.is_active = False
operator.save()
assert client.get('/admin/traffic/').status_code == 302
alice.is_portal_admin = True
alice.save()
client.logout()
client.cookies['session'] = bearer.removeprefix('Bearer ')
assert client.get('/admin/traffic/').status_code == 302
""")


def test_traffic_pages_chart_rendering_and_domain_privacy(portal_runtime):
    python(portal_runtime, SETUP + """
from datetime import date, datetime, UTC
from sing_box_manager.traffic_models import UserTrafficSummary, DomainUsage, DomainUsageBreakdown
provider = Mock()
provider.get_user_summary.return_value = UserTrafficSummary('db_alice', '2026-10', date(2026,10,1), date(2026,10,31), 'Asia/Shanghai', 12, 3456, datetime.now(UTC))
provider.get_domain_breakdown.return_value = DomainUsageBreakdown('db_alice', 'Asia/Shanghai', 30, (DomainUsage('<script>example.com</script>', 1, 2),), 3)
with patch.object(portal, 'create_traffic_stats_provider', return_value=provider), patch.object(compat, 'create_traffic_stats_provider', return_value=provider), patch.object(views, 'create_traffic_stats_provider', return_value=provider):
    page = client.get(url + '/')
    assert page.status_code == 200 and b'3456' not in page.content
    assert b'2026-10-31' in page.content
    assert b'{%' not in page.content and b'{{' not in page.content
    provider.get_domain_breakdown.assert_not_called()
    response = client.get(url, query)
    assert response['subscription-userinfo'] == 'upload=12; download=3456'
    payload = client.get('/api/usage', HTTP_AUTHORIZATION=bearer).json()
    assert payload['upload_bytes'] == 12 and payload['download_bytes'] == 3456
    assert payload['top_domains'] == []
    visible = runtime.model_copy(update={'traffic_stats':runtime.traffic_stats.model_copy(update={'show_user_domains':True})})
    with patch.object(views, '_runtime', return_value=visible):
        page = client.get(url + '/')
        assert b'&lt;script&gt;example.com&lt;/script&gt;' in page.content
        assert b'<script>example.com</script>' not in page.content
operator = User.objects.create_superuser('operator', 'operator@example.com', 'synthetic-operator-password')
client.force_login(operator)
page = client.get('/admin/traffic/?days=bad')
assert page.status_code == 200 and b'sbm-chart' in page.content
assert b'db_alice' in page.content and b'db_bob' in page.content
assert b'/admin/traffic/' in client.get('/admin/').content
""")


@pytest.mark.parametrize("path", ["/", "/auth", "/api/usage", "/api/sub", "/install.sh", "/install.ps1", "/install/windows.ps1", "/sub/invalid", "/missing"])
def test_public_dynamic_responses_are_not_cacheable(portal_runtime, path):
    python(portal_runtime, SETUP + f"""
response = client.get({path!r})
assert 'no-store' in response['Cache-Control']
assert response['Referrer-Policy'] == 'no-referrer'
assert 'Access-Control-Allow-Origin' not in response
""")


def test_signed_downloads_and_update_metadata_keep_their_contract(portal_runtime):
    python(portal_runtime, SETUP + """
from sing_box_manager.release.client_downloads import ClientDownloads
from sing_box_manager.release.artifacts import ReleaseInfo, write_release_info
release = runtime.releases_root / 'fixture-release'
(release / 'client').mkdir(parents=True, exist_ok=True)
(release / 'client' / 'manifest.json').write_text('{"sbc":"abc"}\\n')
(release / 'client' / 'sbc').write_bytes(b'binary')
write_release_info(runtime.release_info_path, ReleaseInfo(release_dir_name=release.name, commit_sha='abcd1234', dirty=False, date='20261018', upstream_version='1.14.2'))
downloads = ClientDownloads(release_dir=release, files={'manifest.json':'client/manifest.json', 'sbc/linux-amd64/sbc':'client/sbc'}, sbc_version='abc', build_id='c'*64)
with patch.object(views, '_client_downloads', return_value=downloads):
    base = url + '/files/'
    binary = client.get(base + 'sbc/linux-amd64/sbc')
    assert binary.status_code == 200 and b''.join(binary.streaming_content) == b'binary'
    assert binary['Content-Length'] == '6'
    assert binary['Content-Type'] == 'application/octet-stream'
    assert client.get(base + 'manifest.json')['Content-Type'] == 'application/json'
    for name in ('../../runtime.yaml', 'client/sbc', '/etc/passwd', 'server/config.json'):
        assert client.get(base + name).status_code == 404
    assert client.get('/sub/' + 'a'*22 + '/files/manifest.json').status_code == 404
    assert client.get(url, query).json()['latest'] == {'sing_box':'1.14.2', 'sbc':'abc'}
    update = client.get('/api/client-update')
    assert update.status_code == 200 and f"client_build_id={'c'*64}\\n" in update.content.decode()
    alice.enabled = False
    alice.save()
    assert client.get(base + 'manifest.json').status_code == 404
""")


def test_every_installer_alias_serves_the_unchanged_script(portal_runtime):
    python(portal_runtime, SETUP + """
for path in ('/install.sh', '/install/linux.sh', '/install/macos.sh'):
    response = client.get(path)
    assert response.status_code == 200
    assert response['Content-Type'].startswith('text/plain')
    assert response.content.decode() == views.INSTALL_SCRIPT.read_text(encoding='utf-8')
for path in ('/install.ps1', '/install/windows.ps1'):
    response = client.get(path)
    assert response.status_code == 200
    assert response.content.decode() == views.INSTALL_SCRIPT.with_suffix('.ps1').read_text(encoding='utf-8-sig')
""")


def test_vps_info_requires_authentication_and_keeps_the_unavailable_shape(portal_runtime):
    python(portal_runtime, SETUP + """
assert client.get('/api/vps-info').status_code == 401
with patch.object(compat, 'vps_provider', return_value=None):
    response = client.get('/api/vps-info', HTTP_AUTHORIZATION=bearer)
    assert response.status_code == 200
    assert response.json() == {'hostname': 'vpn.example.com', 'relay_multiplier': 2.0, 'error': 'unavailable'}
""")


def test_database_error_never_falls_back_to_inventory(portal_runtime):
    python(portal_runtime, SETUP + """
from django.db import DatabaseError
client.raise_request_exception = False
with patch.object(Account.objects, 'filter', side_effect=DatabaseError('fixture-db-failure')):
    for path in (url, url + '/', url + '/files/manifest.json', '/auth'):
        response = client.get(path, HTTP_AUTHORIZATION=bearer)
        assert response.status_code == 500
        assert b'fixture-db-failure' not in response.content
""")
