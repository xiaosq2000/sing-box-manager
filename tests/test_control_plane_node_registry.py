"""Dormant node/source identity metadata and its PostgreSQL migration seam."""

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
from tests.test_control_plane_integration import postgres as postgres_fixture

postgres = postgres_fixture

DATABASE = "sbm_node_registry"


@pytest.fixture
def registry_runtime(postgres, tmp_path):
    assert (
        psql(postgres, "postgres", f"DROP DATABASE IF EXISTS {DATABASE}").returncode
        == 0
    )
    assert psql(postgres, "postgres", provision_sql(DATABASE)).returncode == 0
    path, _, env = config(postgres, tmp_path, DATABASE)
    example = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    settings = yaml.safe_load(path.read_text())
    for name in ("deployment", "web_portal", "trojan", "hysteria2", "naive"):
        settings[name] = example[name]
    settings["snell"] = {"psk": "synthetic-shared-snell-psk", "users": []}
    settings["releases_root"] = str(tmp_path / "releases")
    settings["web"] = {
        "subscription_secret": "synthetic-subscription-secret-" + "2" * 24,
        "subscription_database_path": str(tmp_path / "counters.sqlite3"),
    }
    path.write_text(yaml.safe_dump(settings))
    path.chmod(0o600)
    return path, env


def migrate_runtime(env, target=None):
    python(
        env,
        f"""
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
target = {target!r}
if target is None:
    target = [
        node for node in executor.loader.graph.leaf_nodes()
        if node[0] == "control_plane"
    ]
executor.migrate(target)
""",
    )


def test_migration_from_0005_preserves_rows_bindings_and_legacy_access(
    registry_runtime,
):
    path, env = registry_runtime
    assert cli(path, env, "collect-static").returncode == 0
    python(
        env,
        """
import uuid

import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from sing_box_manager.control_plane import django_settings

runtime = django_settings.runtime
old_target = [("control_plane", "0005_snell")]
executor = MigrationExecutor(connection)
executor.migrate(old_target)
apps = executor.loader.project_state(old_target).apps
Account = apps.get_model("control_plane", "Account")
Credential = apps.get_model("control_plane", "Credential")

account_rows = [
    {
        "username": "alice",
        "enabled": True,
        "portal_enabled": True,
        "is_portal_admin": True,
        "portal_password_hash": "",
        "subscription_counter": 7,
    },
    {
        "username": "bob",
        "enabled": False,
        "portal_enabled": True,
        "is_portal_admin": False,
        "portal_password_hash": "",
        "subscription_counter": 0,
    },
    {
        "username": "carol",
        "enabled": True,
        "portal_enabled": False,
        "is_portal_admin": False,
        "portal_password_hash": "",
        "subscription_counter": 11,
    },
]
for row in account_rows:
    Account.objects.create(**row)
accounts = {account.username: account for account in Account.objects.all()}

credential_rows = [
    ("alice", "trojan", "fixture-alice-trojan", True),
    ("alice", "hysteria2", "fixture-alice-hysteria2", False),
    ("alice", "naive", "fixture-alice-naive", True),
    ("alice", "snell", "fixture-alice-snell", True),
    ("bob", "trojan", "fixture-bob-trojan", True),
    ("bob", "hysteria2", "fixture-bob-hysteria2", False),
    ("bob", "naive", "fixture-bob-naive", True),
    ("carol", "trojan", "fixture-carol-trojan", True),
]
for username, protocol, secret, enabled in credential_rows:
    Credential.objects.create(
        account=accounts[username],
        protocol=protocol,
        secret=secret,
        enabled=enabled,
    )

account_fields = (
    "id",
    "username",
    "enabled",
    "portal_enabled",
    "is_portal_admin",
    "portal_password_hash",
    "subscription_counter",
    "created_at",
    "updated_at",
)
credential_fields = ("id", "account_id", "protocol", "secret", "enabled")
accounts_before = list(Account.objects.order_by("id").values_list(*account_fields))
credentials_before = list(
    Credential.objects.order_by("id").values_list(*credential_fields)
)
assert len(accounts_before) == 3
assert len(credentials_before) == 8

from sing_box_manager.release.profiles import counter_username
from sing_box_manager.subscription import subscription_token

account_enabled = {row["username"]: row["enabled"] for row in account_rows}
expected_roster = {}
for protocol in ("trojan", "hysteria2", "naive", "snell"):
    users = sorted(
        (counter_username(protocol, username), secret)
        for username, held, secret, enabled in credential_rows
        if held == protocol and enabled and account_enabled[username]
    )
    if users or protocol not in {"naive", "snell"}:
        expected_roster[protocol] = users
expected_tokens = {
    row["username"]: subscription_token(
        runtime.web.subscription_secret,
        row["username"],
        row["subscription_counter"],
    )
    for row in account_rows
}

leaf_target = [("control_plane", "0006_node_source_registry")]
executor = MigrationExecutor(connection)
executor.migrate(leaf_target)
assert MigrationExecutor(connection).migration_plan(leaf_target) == []
apps_0006 = executor.loader.project_state(leaf_target).apps
Node0006 = apps_0006.get_model("control_plane", "Node")
Source0006 = apps_0006.get_model("control_plane", "TrafficSource")
Account0006 = apps_0006.get_model("control_plane", "Account")
Credential0006 = apps_0006.get_model("control_plane", "Credential")

assert Node0006.objects.count() == 0
assert Source0006.objects.count() == 0
assert list(
    Account0006.objects.order_by("id").values_list(*account_fields)
) == accounts_before
assert list(
    Credential0006.objects.order_by("id").values_list(*credential_fields)
) == credentials_before
credentials_0006 = list(Credential0006.objects.order_by("id"))
assert len({credential.uuid for credential in credentials_0006}) == 8
assert all(isinstance(credential.uuid, uuid.UUID) for credential in credentials_0006)
assert all(credential.node_id is None for credential in credentials_0006)
assert all(credential.wire_name is None for credential in credentials_0006)

uuid_before_repeat = dict(
    Credential0006.objects.values_list("id", "uuid")
)
executor = MigrationExecutor(connection)
assert executor.migration_plan(leaf_target) == []
executor.migrate(leaf_target)
assert dict(Credential0006.objects.values_list("id", "uuid")) == uuid_before_repeat

# Current runtime/label/HMAC checks use the actual graph leaf. The explicit
# 0005->0006 assertions above remain at the historical boundary.
current_target = [
    node
    for node in executor.loader.graph.leaf_nodes()
    if node[0] == "control_plane"
]
executor = MigrationExecutor(connection)
executor.migrate(current_target)
assert MigrationExecutor(connection).migration_plan(current_target) == []

from django.test import Client

from sing_box_manager.control_plane import projections
from sing_box_manager.control_plane.models import (
    Account as CurrentAccount,
    Credential as CurrentCredential,
    Node,
    TrafficSource,
)
from sing_box_manager.release.profiles import build_server_config

assert Node.objects.count() == 0
assert TrafficSource.objects.count() == 0
assert list(
    CurrentAccount.objects.order_by("id").values_list(*account_fields)
) == accounts_before
assert list(
    CurrentCredential.objects.order_by("id").values_list(*credential_fields)
) == credentials_before
credentials = list(CurrentCredential.objects.order_by("id"))
assert len({credential.uuid for credential in credentials}) == 8
assert all(isinstance(credential.uuid, uuid.UUID) for credential in credentials)
assert all(credential.node_id is None for credential in credentials)
assert all(credential.wire_name is None for credential in credentials)

inventory = projections.build_inventory_from_db(runtime)
rendered = build_server_config(inventory)
actual_roster = {}
for inbound in rendered["inbounds"]:
    protocol = inbound["type"]
    name_key = "username" if protocol == "naive" else "name"
    secret_key = "userkey" if protocol == "snell" else "password"
    actual_roster[protocol] = sorted(
        (user[name_key], user[secret_key]) for user in inbound["users"]
    )
assert actual_roster == expected_roster

client = Client(HTTP_HOST="127.0.0.1")
alice_response = client.get(
    f"/sub/{expected_tokens['alice']}?format=sbc&os=linux&sing-box=1.14.3"
)
assert alice_response.status_code == 200
assert alice_response.json()["nodes"][0]["protocols"] == [
    "trojan",
    "naive",
    "snell",
]
mobile_response = client.get(f"/sub/{expected_tokens['alice']}")
assert mobile_response.status_code == 200
assert mobile_response.json()["outbounds"][0]["outbounds"] == [
    "trojan",
    "naive",
    "snell",
]
assert client.get(f"/sub/{expected_tokens['alice']}/").status_code == 200
assert client.get(f"/sub/{expected_tokens['bob']}").status_code == 404
assert client.get(f"/sub/{expected_tokens['carol']}").status_code == 404

""",
    )


def test_uuid_backfill_direct_replay_preserves_assigned_identities(
    registry_runtime,
):
    _, env = registry_runtime
    python(
        env,
        """
import uuid
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

migration = import_module(
    "sing_box_manager.control_plane.migrations.0006_node_source_registry"
)
old_target = [("control_plane", "0005_snell")]
executor = MigrationExecutor(connection)
executor.migrate(old_target)
old_state = executor.loader.project_state(old_target)
Account = old_state.apps.get_model("control_plane", "Account")
Credential = old_state.apps.get_model("control_plane", "Credential")
account = Account.objects.create(username="replay_fixture")
assigned_credential = Credential.objects.create(
    account=account, protocol="trojan", secret="fixture-replay-trojan"
)
unassigned_credential = Credential.objects.create(
    account=account, protocol="naive", secret="fixture-replay-naive"
)

# Build the historical state at the nullable-uuid seam without a generic
# migration harness: apply only the three operations before the data callback.
state = old_state
with connection.schema_editor() as schema_editor:
    for operation in migration.Migration.operations[:3]:
        previous = state.clone()
        operation.state_forwards("control_plane", state)
        operation.database_forwards(
            "control_plane", schema_editor, previous, state
        )
Credentials = state.apps.get_model("control_plane", "Credential")
assigned = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
fresh = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
with connection.cursor() as cursor:
    cursor.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'control_plane_credential' AND column_name = 'uuid'"
    )
    assert cursor.fetchone() == ("YES",)
    cursor.execute(
        "UPDATE control_plane_credential SET uuid = %s WHERE id = %s",
        [assigned, assigned_credential.pk],
    )

before = dict(Credentials.objects.using(connection.alias).values_list("id", "uuid"))
assert before == {
    assigned_credential.pk: assigned,
    unassigned_credential.pk: None,
}
with patch.object(migration.uuid, "uuid4", side_effect=[assigned, fresh]):
    migration.allocate_credential_uuids(
        state.apps, SimpleNamespace(connection=connection)
    )
after = dict(Credentials.objects.using(connection.alias).values_list("id", "uuid"))
assert after == {
    assigned_credential.pk: assigned,
    unassigned_credential.pk: fresh,
}

migration.allocate_credential_uuids(
    state.apps, SimpleNamespace(connection=connection)
)
assert dict(
    Credentials.objects.using(connection.alias).values_list("id", "uuid")
) == after
""",
    )


def test_reverse_migration_is_irreversible_and_keeps_identity_columns(
    registry_runtime,
):
    _, env = registry_runtime
    python(
        env,
        """
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError

old_target = [("control_plane", "0005_snell")]
executor = MigrationExecutor(connection)
executor.migrate(old_target)
apps = executor.loader.project_state(old_target).apps
Account = apps.get_model("control_plane", "Account")
Credential = apps.get_model("control_plane", "Credential")
account = Account.objects.create(username="reverse_fixture")
Credential.objects.create(
    account=account, protocol="trojan", secret="fixture-reverse-trojan"
)

leaf_target = [("control_plane", "0006_node_source_registry")]
executor = MigrationExecutor(connection)
executor.migrate(leaf_target)
try:
    MigrationExecutor(connection).migrate(old_target)
except IrreversibleError:
    pass
else:
    raise AssertionError("reverse migration discarded identity metadata")

with connection.cursor() as cursor:
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'control_plane_credential'"
    )
    columns = {row[0] for row in cursor.fetchall()}
    assert {"uuid", "node_id", "wire_name"} <= columns
    cursor.execute(
        "SELECT to_regclass('control_plane_node'), "
        "to_regclass('control_plane_trafficsource')"
    )
    assert all(cursor.fetchone())
    cursor.execute("SELECT uuid FROM control_plane_credential")
    values = [row[0] for row in cursor.fetchall()]
    assert len(values) == 1 and values[0] is not None
    cursor.execute(
        "SELECT count(*) FROM django_migrations "
        "WHERE app = 'control_plane' AND name = '0006_node_source_registry'"
    )
    assert cursor.fetchone()[0] == 1
""",
    )


def test_node_and_source_require_supplied_identities_and_valid_metadata(
    registry_runtime,
):
    _, env = registry_runtime
    migrate_runtime(env)
    python(
        env,
        """
import uuid

import django
django.setup()
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from sing_box_manager.control_plane.models import Node, TrafficSource


def rejected(operation, exception=IntegrityError):
    try:
        with transaction.atomic():
            operation()
    except exception:
        return
    raise AssertionError("database accepted invalid metadata")


node_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
node = Node.objects.create(
    id=node_id,
    slug="edge-original",
    name="Edge original",
    token_digest="a" * 64,
)
assert node.pk == node_id
assert node.token_updated_at is not None
assert node.created_at is not None

rejected(
    lambda: Node.objects.create(
        slug="missing-id", name="Missing id", token_digest="b" * 64
    )
)
rejected(
    lambda: Node.objects.create(
        id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        slug="edge-original",
        name="Duplicate slug",
        token_digest="b" * 64,
    )
)
rejected(
    lambda: Node.objects.create(
        id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        slug="",
        name="Empty slug",
        token_digest="b" * 64,
    )
)
invalid_slug = Node(
    id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
    slug="not a slug",
    name="Invalid slug",
    token_digest="b" * 64,
)
try:
    invalid_slug.full_clean()
except ValidationError:
    pass
else:
    raise AssertionError("model accepted an invalid slug")

rejected(
    lambda: Node.objects.create(
        id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        slug="duplicate-digest",
        name="Duplicate digest",
        token_digest="a" * 64,
    )
)
rejected(
    lambda: Node.objects.create(
        id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
        slug="uppercase-digest",
        name="Uppercase digest",
        token_digest="A" * 64,
    )
)
rejected(
    lambda: Node.objects.create(
        id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
        slug="short-digest",
        name="Short digest",
        token_digest="a" * 63,
    )
)

rejected(
    lambda: TrafficSource.objects.create(
        node=node,
    )
)
rejected(
    lambda: TrafficSource.objects.create(
        id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        node_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
    )
)

source = TrafficSource.objects.create(
    id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), node=node
)
rejected(
    lambda: TrafficSource.objects.create(
        id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), node=node
    )
)
source.retired_at = timezone.now()
source.save()
replacement = TrafficSource.objects.create(
    id=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), node=node
)
assert replacement.retired_at is None
assert TrafficSource.objects.filter(
    node=node, retired_at__isnull=True
).get() == replacement
""",
    )


def test_credential_uuid_pair_constraint_and_protect_direction(registry_runtime):
    _, env = registry_runtime
    migrate_runtime(env)
    python(
        env,
        """
import uuid

import django
django.setup()
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from sing_box_manager.control_plane.models import (
    Account,
    Credential,
    Node,
    TrafficSource,
)


def rejected(operation, exception=IntegrityError):
    try:
        with transaction.atomic():
            operation()
    except exception:
        return
    raise AssertionError("database accepted invalid credential metadata")


node = Node.objects.create(
    id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
    slug="edge-credentials",
    name="Edge credentials",
    token_digest="a" * 64,
)
account = Account.objects.create(username="unbound")
first = Credential.objects.create(
    account=account, protocol="trojan", secret="fixture-unbound-trojan"
)
second = Credential.objects.create(
    account=account, protocol="naive", secret="fixture-unbound-naive"
)
assert first.uuid is not None and second.uuid is not None
assert first.uuid != second.uuid
assert first.node_id is None and first.wire_name is None
assert second.node_id is None and second.wire_name is None

bound_account = Account.objects.create(username="bound")
bound = Credential.objects.create(
    account=bound_account,
    protocol="trojan",
    secret="fixture-bound-trojan",
    node=node,
    wire_name="trojan:bound",
)
assert bound.node_id == node.pk
assert bound.wire_name == "trojan:bound"

node_only = Account.objects.create(username="node_only")
rejected(
    lambda: Credential.objects.create(
        account=node_only,
        protocol="trojan",
        secret="fixture-node-only",
        node=node,
    )
)
wire_only = Account.objects.create(username="wire_only")
rejected(
    lambda: Credential.objects.create(
        account=wire_only,
        protocol="trojan",
        secret="fixture-wire-only",
        wire_name="trojan:wire-only",
    )
)
empty_wire = Account.objects.create(username="empty_wire")
rejected(
    lambda: Credential.objects.create(
        account=empty_wire,
        protocol="trojan",
        secret="fixture-empty-wire",
        node=node,
        wire_name="",
    )
)
duplicate_uuid = Account.objects.create(username="duplicate_uuid")
rejected(
    lambda: Credential.objects.create(
        account=duplicate_uuid,
        protocol="trojan",
        secret="fixture-duplicate-uuid",
        uuid=first.uuid,
    )
)

# The node is protected by both dependent rows, but neither child row is
# itself protected from deletion.
credential_node = Node.objects.create(
    id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    slug="edge-credential-protect",
    name="Edge credential protect",
    token_digest="b" * 64,
)
protected_account = Account.objects.create(username="protected")
protected_credential = Credential.objects.create(
    account=protected_account,
    protocol="trojan",
    secret="fixture-protected-trojan",
    node=credential_node,
    wire_name="trojan:protected",
)
rejected(lambda: credential_node.delete(), ProtectedError)
protected_credential.delete()
assert Node.objects.filter(pk=credential_node.pk).exists()
credential_node.delete()
assert not Node.objects.filter(pk=credential_node.pk).exists()

source_node = Node.objects.create(
    id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
    slug="edge-source-protect",
    name="Edge source protect",
    token_digest="c" * 64,
)
source = TrafficSource.objects.create(
    id=uuid.UUID("44444444-4444-4444-8444-444444444444"), node=source_node
)
rejected(lambda: source_node.delete(), ProtectedError)
source.delete()
source_node.delete()
assert not Node.objects.filter(pk=source_node.pk).exists()

bound.delete()
assert not Credential.objects.filter(pk=bound.pk).exists()
assert Node.objects.filter(pk=node.pk).exists()
bound_account.delete()
assert not Account.objects.filter(pk=bound_account.pk).exists()
assert Node.objects.filter(pk=node.pk).exists()
""",
    )


def test_existing_constraints_and_dormant_admin_boundary(registry_runtime):
    _, env = registry_runtime
    migrate_runtime(env)
    python(
        env,
        """
import django
django.setup()
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.test import RequestFactory

from sing_box_manager.control_plane.admin import CredentialInline
from sing_box_manager.control_plane.models import (
    Account,
    Credential,
    Node,
    TrafficSource,
    User,
)

assert not admin.site.is_registered(Node)
assert not admin.site.is_registered(TrafficSource)
assert {field.name for field in Node._meta.local_fields} == {
    "id",
    "slug",
    "name",
    "token_digest",
    "token_updated_at",
    "created_at",
    "updated_at",
}
assert {field.name for field in TrafficSource._meta.local_fields} == {
    "id",
    "node",
    "created_at",
    "retired_at",
}
assert not Node._meta.get_field("id").has_default()
assert not TrafficSource._meta.get_field("id").has_default()
assert Credential._meta.get_field("uuid").has_default()
for name in ("uuid", "node", "wire_name"):
    assert Credential._meta.get_field(name).editable is False
assert Node._meta.get_field("slug").unique
assert Node._meta.get_field("token_digest").unique
assert TrafficSource._meta.get_field("retired_at").null

request = RequestFactory().get("/admin/control_plane/account/add/")
request.user = User(is_superuser=True, is_staff=True, is_active=True)
formset = CredentialInline(Account, admin.site).get_formset(request)
fields = set(formset.form.base_fields)
assert {"protocol", "secret", "enabled"} <= fields
assert not {"uuid", "node", "wire_name"} & fields

account = Account.objects.create(username="constraints")
Credential.objects.create(
    account=account, protocol="trojan", secret="fixture-constraint-trojan"
)
try:
    with transaction.atomic():
        Credential.objects.create(
            account=account, protocol="trojan", secret="fixture-duplicate-protocol"
        )
except IntegrityError:
    pass
else:
    raise AssertionError("duplicate account/protocol accepted")

Credential.objects.create(
    account=account, protocol="snell", secret="fixture-constraint-snell"
)
other = Account.objects.create(username="other_constraints")
try:
    with transaction.atomic():
        Credential.objects.create(
            account=other, protocol="snell", secret="fixture-constraint-snell"
        )
except IntegrityError:
    pass
else:
    raise AssertionError("duplicate Snell userkey accepted")

credential_ids = set(
    Credential.objects.filter(account=account).values_list("id", flat=True)
)
assert len(credential_ids) == 2
account.delete()
assert not Credential.objects.filter(id__in=credential_ids).exists()
assert Account.objects.filter(pk=other.pk).exists()
""",
    )
