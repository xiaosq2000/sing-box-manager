"""PostgreSQL-only accounts, safe backfill, subscriptions and account revocation."""

import pytest

from tests.test_control_plane_integration import python
from tests.test_control_plane_reliability import (
    SETUP,
    account_runtime,
    postgres,
    runtime_config,
)

CONFIGURE = """
from sing_box_manager.settings import Settings
runtime.control_plane.authoritative = True
data = runtime.model_dump()
data.pop('default_protocol')
data['deployment']['snell_port'] = 10443
data['snell'] = {'psk': 'synthetic-shared-snell-psk'}
runtime = django_settings.runtime = Settings.model_validate(data)
import os, yaml
Path(os.environ['SBM_CONFIG']).write_text(yaml.safe_dump(runtime.model_dump(mode='json')))
"""


def test_backfill_is_explicit_idempotent_and_preserves_all_existing_state(
    account_runtime,
):
    _, env = account_runtime
    python(
        env,
        SETUP
        + CONFIGURE
        + """
from sing_box_manager.control_plane.runner import _run_django_action, ControlPlaneError
from django.core.management import call_command
from django.db import IntegrityError
call_command('makemigrations', 'control_plane', check=True, dry_run=True, verbosity=0)
import contextlib, io
inactive = Account.objects.create(username='inactive', enabled=False, subscription_counter=7)
db_only = Account.objects.create(username='database_only')
portal_off = Account.objects.create(username='portal_off', portal_enabled=False)
existing = Account.objects.create(username='existing')
Credential.objects.create(account=existing, protocol='snell', secret='synthetic-retained-key', enabled=False)
accounts_before = list(Account.objects.values())
credentials_before = list(Credential.objects.values())
with patch.object(services, 'request_node_sync') as trigger:
    preview = services.backfill_snell_credentials(runtime, dry_run=True)
    assert preview == {'accounts': 5, 'existing': 1, 'missing': 4}, preview
    assert list(Account.objects.values()) == accounts_before
    assert list(Credential.objects.values()) == credentials_before
    trigger.assert_not_called()
    result = services.backfill_snell_credentials(runtime)
    assert result == {'accounts': 5, 'existing': 1, 'created': 4}, result
    trigger.assert_called_once_with(runtime)
    trigger.reset_mock()
    keys = list(Credential.objects.values())
    assert services.backfill_snell_credentials(runtime)['created'] == 0
    assert list(Credential.objects.values()) == keys
    trigger.assert_not_called()
assert list(Account.objects.values()) == accounts_before
assert all(Credential.objects.filter(**row).exists() for row in credentials_before)
assert not Credential.objects.get(account=existing, protocol='snell').enabled
assert len(set(Credential.objects.filter(protocol='snell').values_list('secret', flat=True))) == 5
projected = services.build_inventory_from_db(runtime)
active = {user.username for user in projected.snell.users if user.enabled}
assert active == {'alice', 'database_only', 'portal_off'}, active
assert services.account_protocols(inactive, runtime).snell is None
assert services.account_protocols(existing, runtime).snell is None
assert services.account_protocols(portal_off, runtime).snell is not None
# Duplicate keys cannot be stored, even through a direct ORM write.
try:
    with transaction.atomic():
        Credential.objects.create(account=Account.objects.create(username='duplicate'), protocol='snell', secret='synthetic-retained-key')
except IntegrityError:
    pass
else:
    raise AssertionError('duplicate userkey accepted')
output = io.StringIO()
with contextlib.redirect_stdout(output):
    _run_django_action('backfill-snell', runtime, dry_run=True)
assert 'preview' in output.getvalue()
assert 'synthetic' not in output.getvalue()
assert 'alice' not in output.getvalue()
""",
    )


def test_snell_subscriptions_respect_protocol_account_and_portal_switches(
    account_runtime,
):
    _, env = account_runtime
    python(
        env,
        SETUP
        + CONFIGURE
        + """
with patch.object(services, 'request_node_sync'):
    services.backfill_snell_credentials(runtime)
credential = Credential.objects.get(account=account, protocol='snell')
original = list(account.credentials.values())
token = subscription_token(runtime.web.subscription_secret, account.username, 0)
for os_name in ('linux', 'darwin', 'windows'):
    response = client.get(f'/sub/{token}?format=sbc&os={os_name}&sing-box=1.14.3')
    assert response.status_code == 200, response.content
    body = response.json()
    assert body['default_protocol'] == 'snell'
    assert body['nodes'][0]['protocols'] == ['trojan', 'hysteria2', 'naive', 'snell']
    for name in ('config', 'config_tun'):
        assert body[name]['outbounds'][0]['default'] == 'snell'
        outbound = next(o for o in body[name]['outbounds'] if o['type'] == 'snell')
        assert outbound['userkey'] == credential.secret
        assert outbound['psk'] == runtime.snell.psk
    assert client.get(f'/sub/{token}?format=sbc&os={os_name}&sing-box=1.14.3', HTTP_IF_NONE_MATCH=response['ETag']).status_code == 304
mobile = client.get(f'/sub/{token}?format=mobile')
assert mobile.status_code == 200
assert mobile.json()['outbounds'][0]['outbounds'][-1] == 'snell'
assert mobile.json()['outbounds'][0]['default'] == 'snell'
assert mobile['Cache-Control'] == 'private, no-store'
credential.enabled = False
credential.save()
response = client.get(f'/sub/{token}?format=mobile')
assert response.status_code == 200
assert response['ETag'] != mobile['ETag']
assert 'snell' not in response.json()['outbounds'][0]['outbounds']
assert response.json()['outbounds'][0]['default'] == 'trojan'
for os_name in ('linux', 'darwin', 'windows'):
    body = client.get(f'/sub/{token}?format=sbc&os={os_name}&sing-box=1.14.3').json()
    assert body['default_protocol'] == 'trojan'
    assert body['config']['outbounds'][0]['default'] == 'trojan'
    assert body['config_tun']['outbounds'][0]['default'] == 'trojan'
assert 'snell' not in roster()
credential.enabled = True
credential.save()
account.enabled = False
account.save()
assert client.get(f'/sub/{token}?format=mobile').status_code == 404
assert not roster().get('snell')
account.enabled = True
account.save()
assert client.get(f'/sub/{token}?format=mobile').status_code == 200
assert list(account.credentials.values()) == original
# Link rotation is distinct from protocol revocation.
account.subscription_counter += 1
account.save()
assert client.get(f'/sub/{token}?format=mobile').status_code == 404
assert list(account.credentials.values()) == original
""",
    )


@pytest.mark.parametrize("protocol", ["trojan", "hysteria2", "naive"])
def test_explicit_default_wins_over_snell_for_every_subscription(
    account_runtime, protocol
):
    _, env = account_runtime
    python(
        env,
        SETUP
        + CONFIGURE
        + f"runtime.default_protocol = {protocol!r}\n"
        + """
with patch.object(services, 'request_node_sync'):
    services.backfill_snell_credentials(runtime)
for os_name in ('linux', 'darwin', 'windows'):
    body = services.build_account_envelope(account, runtime, os_name=os_name)
    assert body['default_protocol'] == runtime.default_protocol
    for name in ('config', 'config_tun'):
        assert body[name]['outbounds'][0]['default'] == runtime.default_protocol
assert services.build_account_mobile_profile(account, runtime)['outbounds'][0]['default'] == runtime.default_protocol
""",
    )


def test_snell_credentials_stay_dormant_without_infrastructure(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + """
from sing_box_manager.control_plane.runner import ControlPlaneError
new = Account.objects.create(username='new_user')
assert services.ensure_default_credentials(new) == ['trojan', 'hysteria2', 'naive', 'snell']
assert services.account_protocols(new, runtime).snell is None
assert services.build_inventory_from_db(runtime).snell.users == []
assert 'snell' not in roster()
try:
    services.backfill_snell_credentials(runtime)
except ControlPlaneError:
    pass
else:
    raise AssertionError('unconfigured backfill accepted')
""",
    )


def test_snell_backfill_sync_failure_keeps_saved_keys_for_retry(account_runtime):
    _, env = account_runtime
    python(
        env,
        SETUP
        + CONFIGURE
        + """
from sing_box_manager.control_plane.runner import ControlPlaneError
with patch.object(services, 'request_node_sync', side_effect=OSError('synthetic-failure')):
    try:
        services.backfill_snell_credentials(runtime)
    except ControlPlaneError as error:
        assert 'saved' in str(error) and 'sudo sbm control-plane sync' in str(error)
    else:
        raise AssertionError('sync request failure was hidden')
assert Credential.objects.filter(protocol='snell').count() == 1
with patch.object(services, 'request_node_sync') as trigger:
    assert services.backfill_snell_credentials(runtime)['created'] == 0
    trigger.assert_not_called()
""",
    )
