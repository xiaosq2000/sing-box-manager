"""Isolate Django tests; use PostgreSQL only for database-dependent behavior."""

import os

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


@pytest.fixture
def unit_runtime(tmp_path):
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "PYTHONPATH")
        if key in os.environ
    }
    path, _, env = config(
        {"sockets": tmp_path / "unused-socket", "env": env}, tmp_path, "unused_db"
    )
    return path, env


UNIT_SETUP = """
import json
from unittest.mock import Mock, patch
import django
django.setup()
from sing_box_manager.control_plane import django_settings, node_sync
runtime = django_settings.runtime
"""


@pytest.fixture
def runtime_config(postgres, tmp_path):
    database = "sbm_reliability"
    assert (
        psql(postgres, "postgres", f"DROP DATABASE IF EXISTS {database}").returncode
        == 0
    )
    assert psql(postgres, "postgres", provision_sql(database)).returncode == 0
    path, _, env = config(postgres, tmp_path, database)
    example = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    settings = yaml.safe_load(path.read_text())
    for name in ("deployment", "web_portal", "trojan", "hysteria2", "naive"):
        settings[name] = example[name]
    # This fixture tests inventories without Snell infrastructure.
    settings["deployment"].pop("snell_port", None)
    settings["web_portal"]["users"] = example["web_portal"]["users"][:1]
    for name in ("trojan", "hysteria2", "naive"):
        settings[name]["users"] = [
            {"username": "alice", "password": f"fixture-{name}-secret", "enabled": True}
        ]
    settings["releases_root"] = str(tmp_path / "releases")
    settings["web"] = {
        "subscription_secret": "synthetic-subscription-secret-" + "2" * 24,
        "subscription_database_path": str(tmp_path / "counters.sqlite3"),
    }
    path.write_text(yaml.safe_dump(settings))
    return path, env


@pytest.fixture
def account_runtime(runtime_config):
    path, env = runtime_config
    result = cli(path, env, "migrate")
    assert result.returncode == 0, result.stderr
    return path, env


SETUP = """
import json
from pathlib import Path
from unittest.mock import Mock, patch
import django
django.setup()
from django.contrib import admin
from django.db import transaction, connection
from django.test import Client, RequestFactory
from sing_box_manager.control_plane import (
    account_services,
    django_settings,
    node_sync,
    projections,
)
from sing_box_manager.control_plane.admin import AccountAdmin
from sing_box_manager.control_plane.models import Account, Credential
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.subscription import subscription_token
runtime = django_settings.runtime
account_services.import_inventory(runtime)
account = Account.objects.get(username=runtime.web_portal.users[0].username)
client = Client(HTTP_HOST='127.0.0.1')
def roster():
    return {i['type']: i['users'] for i in build_server_config(projections.build_inventory_from_db(runtime))['inbounds']}
"""


def test_account_disable_reenable_and_protocol_switches(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + """
original = roster()
assert all(original.values())
token = subscription_token(runtime.web.subscription_secret, account.username, account.subscription_counter)
assert client.get(f'/sub/{token}').status_code == 200
account.enabled = False
account.save()
assert not any(roster().values()), 'disabled account still has proxy access'
assert client.get(f'/sub/{token}').status_code == 404
assert client.get(f'/sub/{token}/').status_code == 404
assert client.get(f'/sub/{token}/files/manifest.json').status_code == 404
assert projections.build_account_mobile_profile(account, runtime) is None
Credential.objects.filter(account=account, protocol='trojan').update(enabled=False)
account.enabled = True
account.save()
assert roster()['trojan'] == []
assert roster()['hysteria2'] == original['hysteria2']
assert client.get(f'/sub/{token}').status_code == 200
account.portal_enabled = False
account.save()
assert client.get(f'/sub/{token}').status_code == 404
assert roster()['hysteria2'] == original['hysteria2']
""",
    )


def test_import_refuses_authoritative_mode_before_any_side_effect(account_runtime):
    path, env = account_runtime
    python(
        env,
        SETUP
        + """
account.enabled = False
account.subscription_counter = 5
account.save()
Credential.objects.filter(account=account).update(secret='fixture-admin-edited-secret')
Account.objects.create(username='db_only', portal_password_hash=account.portal_password_hash)
before = list(Account.objects.values()), list(Credential.objects.values())
runtime.control_plane.authoritative = True
with patch.object(account_services, 'read_legacy_reset_counters', side_effect=AssertionError('must not read SQLite')):
    try:
        account_services.import_inventory(runtime)
    except account_services.ControlPlaneError as error:
        assert 'authoritative' in str(error)
    else:
        raise AssertionError('authoritative import was accepted')
assert before == (list(Account.objects.values()), list(Credential.objects.values()))
""",
    )
    cfg = yaml.safe_load(path.read_text())
    cfg["control_plane"]["authoritative"] = True
    path.write_text(yaml.safe_dump(cfg))
    for action in ("import", "import-inventory"):
        result = cli(path, env, action)
        assert result.returncode != 0
        assert "authoritative" in result.stderr


def test_admin_sync_waits_for_commit_and_reports_trigger_failure(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + """
from sing_box_manager.control_plane import admin as account_admin
runtime.control_plane.authoritative = True
model_admin = AccountAdmin(Account, admin.site)
model_admin.message_user = Mock()
request = RequestFactory().post('/admin/control_plane/account/')
with patch.object(account_admin, 'request_node_sync') as sync:
    with transaction.atomic():
        model_admin._maybe_trigger_node_sync(request)
        sync.assert_not_called()
    sync.assert_called_once_with(runtime)
    sync.reset_mock()
    try:
        with transaction.atomic():
            model_admin._maybe_trigger_node_sync(request)
            raise ValueError('fixture rollback')
    except ValueError:
        pass
    sync.assert_not_called()
with patch.object(account_admin, 'request_node_sync', side_effect=PermissionError('fixture')):
    with transaction.atomic():
        account.enabled = False
        account.save()
        model_admin._maybe_trigger_node_sync(request)
account.refresh_from_db()
assert not account.enabled
assert 'sync' in model_admin.message_user.call_args.args[1].lower()
assert model_admin.message_user.call_args.kwargs['level'] == 'error'
""",
    )


def test_admin_account_lifecycle_and_link_reset(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + """
from sing_box_manager.control_plane.models import User
from sing_box_manager.control_plane import admin as account_admin
runtime.control_plane.authoritative = True
operator = User.objects.create_superuser('fixture_operator', password='fixture-password')
client.force_login(operator)
data = {
    'username': 'new_user', 'enabled': 'on', 'portal_enabled': 'on',
    'subscription_counter': '0', 'credentials-TOTAL_FORMS': '0',
    'credentials-INITIAL_FORMS': '0', 'credentials-MAX_NUM_FORMS': '4',
    'credentials-MIN_NUM_FORMS': '0', '_save': 'Save',
}
with patch.object(account_admin, 'request_node_sync') as sync:
    response = client.post('/admin/control_plane/account/add/', data)
    assert response.status_code == 302, response.content.decode()
    sync.assert_called_once()
new = Account.objects.get(username='new_user')
assert new.portal_password_hash == ''
assert new.credentials.count() == 4
assert len({c.secret for c in new.credentials.all()}) == 4
before = list(new.credentials.values('protocol', 'secret', 'enabled'))
token = subscription_token(runtime.web.subscription_secret, new.username, 0)
assert client.get(f'/sub/{token}').status_code == 200
response = client.post('/admin/control_plane/account/', {
    'action': 'reset_subscription_links', '_selected_action': [str(new.pk)]
})
assert response.status_code == 302
new.refresh_from_db()
assert new.subscription_counter == 1
assert client.get(f'/sub/{token}').status_code == 404
fresh = subscription_token(runtime.web.subscription_secret, new.username, 1)
assert client.get(f'/sub/{fresh}').status_code == 200
assert before == list(new.credentials.values('protocol', 'secret', 'enabled'))
# Removing every credential must not silently generate replacements on edit.
data.update({'username': new.username, 'subscription_counter': '0',
             'portal_password_hash': new.portal_password_hash,
             'credentials-TOTAL_FORMS': '4', 'credentials-INITIAL_FORMS': '4'})
for index, credential in enumerate(new.credentials.all()):
    data.update({f'credentials-{index}-id': str(credential.pk),
                 f'credentials-{index}-account': str(new.pk),
                 f'credentials-{index}-protocol': credential.protocol,
                 f'credentials-{index}-secret': credential.secret,
                 f'credentials-{index}-DELETE': 'on'})
with patch.object(account_admin, 'request_node_sync') as sync:
    response = client.post(f'/admin/control_plane/account/{new.pk}/change/', data)
    assert response.status_code == 302, response.content.decode()
    sync.assert_called_once()
assert new.credentials.count() == 0
new.refresh_from_db()
assert new.subscription_counter == 1, 'admin edit restored a revoked subscription'
with patch.object(account_admin, 'request_node_sync') as sync:
    response = client.post(f'/admin/control_plane/account/{new.pk}/delete/', {'post': 'yes'})
    assert response.status_code == 302
    sync.assert_called_once()
assert not Account.objects.filter(pk=new.pk).exists()
""",
    )


@pytest.mark.parametrize(
    "failure", ["check", "restart", "health", "recovery", "missing_previous"]
)
def test_node_sync_failure_preserves_config_and_recovers_service(unit_runtime, failure):
    _, env = unit_runtime
    python(
        env,
        UNIT_SETUP
        + f"\nfailure = {failure!r}\n"
        + """
import subprocess
patch.object(node_sync, '_build_inventory_as_db_user', return_value=runtime).start()
patch.object(node_sync, 'build_server_config', return_value={'inbounds': [{'tag': 'trojan'}]}).start()
node = runtime.config_root / 'node' / 'config.json'
node.parent.mkdir(parents=True)
previous = b'{"fixture": "previous-good-config"}\\n'
if failure != 'missing_previous':
    node.write_bytes(previous)
    node.chmod(0o600)
binary = runtime.config_root / 'sing-box'
binary.touch()
restarts = []
def run(command, **kwargs):
    if command[1] == 'check':
        assert node.read_bytes() == previous if node.exists() else failure == 'missing_previous'
        return subprocess.CompletedProcess(command, int(failure == 'check'), '', 'fixture-secret-output')
    if command[1] == 'restart':
        restarts.append(node.read_bytes())
        if len(restarts) == 1:
            assert json.loads(restarts[0])['inbounds']
        else:
            assert restarts[-1] == previous
        code = int(failure == 'recovery' or (len(restarts) == 1 and failure != 'health'))
    else:
        assert command[1] == 'is-active'
        code = int(failure == 'health' and len(restarts) == 1)
    return subprocess.CompletedProcess(command, code, '', 'fixture-secret-output')
with patch.object(node_sync.subprocess, 'run', side_effect=run), patch.object(node_sync.time, 'sleep'):
    try:
        node_sync.sync_node_config(runtime, config_path=node, binary_path=binary)
    except RuntimeError as error:
        assert 'fixture-secret-output' not in str(error)
        if failure == 'recovery':
            assert 'recovery failed' in str(error).lower()
    else:
        raise AssertionError('failed sync was reported as successful')
if failure == 'missing_previous':
    assert not node.exists()
else:
    assert node.read_bytes() == previous
    assert node.stat().st_mode & 0o777 == 0o600
assert len(restarts) == (0 if failure == 'check' else 1 if failure == 'missing_previous' else 2)
assert not list(node.parent.glob('.config-*.json.tmp'))
""",
    )


def test_sync_locks_before_reading_database(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + '''
import fcntl
import subprocess
import sys
import time
node = runtime.config_root / 'node' / 'config.json'
node.parent.mkdir(parents=True)
ready = node.parent / 'waiting'
queried = node.parent / 'queried'
code = """
import sys
from pathlib import Path
from unittest.mock import patch
import fcntl
import django
django.setup()
from sing_box_manager.control_plane import django_settings, node_sync
node, ready, queried = map(Path, sys.argv[1:])
real_lock = fcntl.flock
real_read = node_sync._build_inventory_as_db_user
def lock(fd, flags):
    ready.touch()
    real_lock(fd, flags)
def read(runtime):
    queried.touch()
    return real_read(runtime)
with patch.object(fcntl, 'flock', side_effect=lock), patch.object(node_sync, '_build_inventory_as_db_user', side_effect=read):
    node_sync.sync_node_config(django_settings.runtime, config_path=node, binary_path=node.parent / 'missing', restart_service=False)
"""
with (node.parent / '.sync.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    process = subprocess.Popen([sys.executable, '-c', code, str(node), str(ready), str(queried)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'sync did not attempt the shared lock'
        assert not queried.exists(), 'read database before acquiring the shared lock'
        account.enabled = False
        account.save()
        fcntl.flock(lock, fcntl.LOCK_UN)
        _, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors
        assert queried.exists()
        assert not any(i['users'] for i in json.loads(node.read_text())['inbounds'])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
''',
    )


def test_root_sync_restores_identity_after_database_failure(unit_runtime):
    _, env = unit_runtime
    python(
        env,
        UNIT_SETUP
        + """
import pwd
from types import SimpleNamespace
from django.db import connections
calls = []
with patch.object(node_sync.os, 'geteuid', return_value=0), \\
     patch.object(node_sync.os, 'getegid', return_value=123), \\
     patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=456, pw_gid=789)), \\
     patch.object(node_sync.os, 'seteuid', side_effect=lambda uid: calls.append(('uid', uid))), \\
     patch.object(node_sync.os, 'setegid', side_effect=lambda gid: calls.append(('gid', gid))), \\
     patch.object(connections, 'close_all'), \\
     patch.object(node_sync, 'build_inventory_from_db', side_effect=RuntimeError('fixture database failure')):
    try:
        node_sync._build_inventory_as_db_user(runtime)
    except RuntimeError:
        pass
    else:
        raise AssertionError('database failure was ignored')
assert calls == [('gid', 789), ('uid', 456), ('uid', 0), ('gid', 123)]
""",
    )


@pytest.mark.parametrize("active", [True, False])
def test_unchanged_sync_checks_health_without_restarting_healthy_service(
    unit_runtime, active
):
    _, env = unit_runtime
    python(
        env,
        UNIT_SETUP
        + f"\nactive = {active!r}\n"
        + """
import subprocess
node = runtime.config_root / 'node' / 'config.json'
node.parent.mkdir(parents=True)
expected = {'inbounds': [{'tag': 'trojan', 'users': []}], 'log': {'level': 'info'}}
# The installer writes compact JSON. Sync writes sorted, indented JSON.
node.write_text(json.dumps(expected))
node.chmod(0o600)
previous = node.read_bytes()
binary = node.parent / 'sing-box'
binary.touch()
calls = []
def run(command, **kwargs):
    calls.append(command[1])
    code = int(command[1] == 'is-active' and not active and 'restart' not in calls)
    return subprocess.CompletedProcess(command, code, '', '')
with patch.object(node_sync, '_build_inventory_as_db_user', return_value=runtime), \
     patch.object(node_sync, 'build_server_config', return_value=expected), \
     patch.object(node_sync.subprocess, 'run', side_effect=run), patch.object(node_sync.time, 'sleep'):
    node_sync.sync_node_config(runtime, config_path=node, binary_path=binary)
assert calls == (['check', 'is-active'] if active else ['check', 'is-active', 'restart', 'is-active'])
if active:
    assert node.read_bytes() == previous
assert json.loads(node.read_text()) == expected
assert not list(node.parent.glob('.config-*.json.tmp'))
""",
    )


def test_authoritative_traffic_collection_uses_database_accounts(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + """
from datetime import UTC, datetime
from sing_box_manager import traffic_stats as stats
runtime.control_plane.authoritative = True
runtime.traffic_stats.enabled = True
runtime.traffic_stats.database_path = runtime.config_root / 'traffic.sqlite3'
account.enabled = False
account.save()
user = Account.objects.create(username='db_only', portal_enabled=False)
Credential.objects.create(account=user, protocol='trojan', secret='fixture-secret')
Credential.objects.create(account=user, protocol='naive', secret='fixture-secret', enabled=False)
seen = []
def query(target):
    seen.append((target.protocol, target.usernames))
    return stats._ServiceSnapshot(100, {'db_only': stats.UserTrafficCounters(100, 200)})
now = datetime(2026, 4, 1, tzinfo=UTC)
with patch.object(stats, 'load_local_env'), \
     patch.object(stats, 'load_runtime_inventory', side_effect=AssertionError('must not load YAML users')), \
     patch.object(stats, '_refresh_billing_day', return_value=1), \
     patch.object(stats, '_query_service_snapshot', side_effect=query):
    result = stats.collect_traffic_stats(runtime, now=now)
    assert result.successful_services == ('trojan',)
    assert seen == [('trojan', ('db_only',))]
    user.enabled = False
    user.save()
    assert stats.collect_traffic_stats(runtime, now=now).successful_services == ()
    # A database failure must not silently use the obsolete YAML roster.
    with patch.object(node_sync, 'build_inventory_from_db', side_effect=RuntimeError('fixture-backend-secret')):
        try:
            stats.collect_traffic_stats(runtime, now=now)
        except stats.TrafficStatsError as error:
            assert 'fixture-backend-secret' not in str(error)
        else:
            raise AssertionError('database failure was ignored')
summary = stats.TrafficStatsProvider(runtime.traffic_stats.database_path, 'UTC').get_user_summary('db_only', now=now)
assert summary.upload_bytes == 100
assert summary.download_bytes == 200
""",
    )
    python(
        {key: value for key, value in env.items() if key != "DJANGO_SETTINGS_MODULE"},
        """
import os, sys
from unittest.mock import patch
from sing_box_manager.settings import load_settings
from sing_box_manager import traffic_stats as stats
runtime = load_settings(os.environ['SBM_CONFIG'])
runtime.control_plane.authoritative = True
assert 'django' not in sys.modules
with patch.object(stats, 'load_local_env'):
    inventory = stats._load_inventory(runtime)
assert 'db_only' in {user.username for user in inventory.trojan.users}
assert not any(user.enabled for user in inventory.trojan.users)
""",
    )


def test_migration_preserves_independent_legacy_access(runtime_config):
    _, env = runtime_config
    python(
        env,
        """
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
old_target = [('control_plane', '0002_accounts')]
executor.migrate(old_target)
apps = executor.loader.project_state(old_target).apps
Account = apps.get_model('control_plane', 'Account')
Credential = apps.get_model('control_plane', 'Credential')
for name, portal, protocol in [('portal_only', True, False), ('proxy_only', False, True), ('disabled', False, False)]:
    account = Account.objects.create(username=name, enabled=portal, subscription_counter=7)
    Credential.objects.create(account=account, protocol='trojan', enabled=protocol, secret='fixture-secret')
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
from sing_box_manager.control_plane.models import Account, Credential
assert Account.objects.get(username='portal_only').portal_enabled
assert Account.objects.get(username='portal_only').enabled
assert not Account.objects.get(username='proxy_only').portal_enabled
assert Account.objects.get(username='proxy_only').enabled
assert not Account.objects.get(username='disabled').enabled
assert not Account.objects.get(username='disabled').portal_enabled
assert set(Account.objects.values_list('subscription_counter', flat=True)) == {7}
assert set(Credential.objects.values_list('secret', flat=True)) == {'fixture-secret'}
""",
    )
