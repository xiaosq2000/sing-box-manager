"""Real-PostgreSQL contracts for durable node traffic ingestion."""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_control_plane_integration import (
    ROOT,
    cli,
    config,
    provision_sql,
    psql,
)
from tests.test_control_plane_integration import postgres as postgres_fixture

postgres = postgres_fixture

NODE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SOURCE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
RETIRED_SOURCE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
CREDENTIAL_UUID = uuid.UUID("44444444-4444-4444-8444-444444444444")
NODE2_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
SOURCE2_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
CREDENTIAL2_UUID = uuid.UUID("77777777-7777-4777-8777-777777777777")
UNBOUND_CREDENTIAL_UUID = uuid.UUID("88888888-8888-4888-8888-888888888888")
CREDENTIAL3_UUID = uuid.UUID("99999999-9999-4999-8999-999999999999")

IDENTITY_CONSTANTS = f"""
NODE_ID = uuid.UUID("{NODE_ID}")
SOURCE_ID = uuid.UUID("{SOURCE_ID}")
RETIRED_SOURCE_ID = uuid.UUID("{RETIRED_SOURCE_ID}")
CREDENTIAL_UUID = uuid.UUID("{CREDENTIAL_UUID}")
NODE2_ID = uuid.UUID("{NODE2_ID}")
SOURCE2_ID = uuid.UUID("{SOURCE2_ID}")
CREDENTIAL2_UUID = uuid.UUID("{CREDENTIAL2_UUID}")
UNBOUND_CREDENTIAL_UUID = uuid.UUID("{UNBOUND_CREDENTIAL_UUID}")
CREDENTIAL3_UUID = uuid.UUID("{CREDENTIAL3_UUID}")
"""

SEED_IDENTITIES = f"""
import uuid

import django
django.setup()
from django.utils import timezone

from sing_box_manager.control_plane.models import (
    Account,
    Credential,
    Node,
    TrafficSource,
)

{IDENTITY_CONSTANTS}
node = Node.objects.create(
    id=NODE_ID,
    slug="ingest-node",
    name="Ingest node",
    token_digest="a" * 64,
)
account = Account.objects.create(username="ingest-alpha")
credential = Credential.objects.create(
    account=account,
    protocol="trojan",
    secret="fixture-ingest-alpha-trojan",
    uuid=CREDENTIAL_UUID,
    node=node,
    wire_name="trojan:ingest-alpha",
)
TrafficSource.objects.create(id=SOURCE_ID, node=node)
TrafficSource.objects.create(
    id=RETIRED_SOURCE_ID,
    node=node,
    retired_at=timezone.now(),
)
node2 = Node.objects.create(
    id=NODE2_ID,
    slug="ingest-node-two",
    name="Ingest node two",
    token_digest="b" * 64,
)
account2 = Account.objects.create(username="ingest-bravo")
Credential.objects.create(
    account=account2,
    protocol="hysteria2",
    secret="fixture-bravo-secret",
    uuid=CREDENTIAL2_UUID,
    node=node2,
    wire_name="hysteria2:ingest-bravo",
)
TrafficSource.objects.create(id=SOURCE2_ID, node=node2)
account3 = Account.objects.create(username="ingest-charlie")
Credential.objects.create(
    account=account3,
    protocol="trojan",
    secret="fixture-ingest-charlie-trojan",
    uuid=CREDENTIAL3_UUID,
    node=node,
    wire_name="trojan:ingest-charlie",
)
unbound_account = Account.objects.create(username="ingest-unbound")
Credential.objects.create(
    account=unbound_account,
    protocol="naive",
    secret="fixture-ingest-unbound-naive",
    uuid=UNBOUND_CREDENTIAL_UUID,
)
"""


INGEST_PRELUDE = (
    "import uuid\n"
    "from datetime import date\n\n"
    "import django\n"
    "django.setup()\n\n"
    "from sing_box_manager.control_plane import traffic_contract as tc\n"
    "from sing_box_manager.control_plane.models import (\n"
    "    Account,\n"
    "    Credential,\n"
    "    Node,\n"
    "    TrafficSnapshot,\n"
    "    TrafficSource,\n"
    ")\n"
    "from sing_box_manager.control_plane.traffic_ingest import (\n"
    "    TrafficIngestError,\n"
    "    ingest_page,\n"
    ")\n\n"
    + IDENTITY_CONSTANTS
    + """

def counter(
    version=1,
    upload=10,
    download=20,
    *,
    source=SOURCE_ID,
    credential=CREDENTIAL_UUID,
    day="2026-10-23",
):
    return tc.make_counter_snapshot(
        source_id=str(source),
        credential_id=str(credential),
        accounting_date=day,
        version=version,
        upload=upload,
        download=download,
    )


def entry(name, upload=0, download=0, connections=0):
    return tc.AttributionEntry(
        name=name, upload=upload, download=download, connections=connections
    )


def attribution(
    version=1,
    detail="exact",
    domains=(),
    destinations=(),
    *,
    source=SOURCE_ID,
    credential=CREDENTIAL_UUID,
    day="2026-10-23",
):
    return tc.make_attribution_snapshot(
        source_id=str(source),
        credential_id=str(credential),
        accounting_date=day,
        version=version,
        detail=detail,
        domains=tuple(domains),
        destinations=tuple(destinations),
    )


def wire(*snapshots):
    request = tc.make_request(snapshots)
    return request, tc.encode_request(request)


def ingest(*snapshots, node=NODE_ID):
    request, raw = wire(*snapshots)
    return request, ingest_page(node, raw)


def expect_error(code, *snapshots, node=NODE_ID):
    request, raw = wire(*snapshots)
    try:
        ingest_page(node, raw)
    except TrafficIngestError as exc:
        assert exc.code == code, (exc.code, code)
        assert str(exc) == code
    else:
        raise AssertionError(f"expected {code}")


def parse_receipts(request, raw):
    return tc.parse_receipt_response(raw, request)


def row_for(snapshot):
    return TrafficSnapshot.objects.get(
        source_id=uuid.UUID(snapshot.source_id),
        credential_id=uuid.UUID(snapshot.credential_id),
        kind=snapshot.kind,
        accounting_date=snapshot.accounting_date,
    )
"""
)


def run_python(env, content: str) -> str:
    result = subprocess.run(
        [sys.executable, "-"],
        input=content,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def migrate_to(env, target=None):
    run_python(
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


@pytest.fixture
def missing_snapshot_runtime(postgres, tmp_path: Path):
    database = f"sbm_traffic_missing_{uuid.uuid4().hex[:12]}"
    assert (
        psql(postgres, "postgres", f"DROP DATABASE IF EXISTS {database}").returncode
        == 0
    )
    assert psql(postgres, "postgres", provision_sql(database)).returncode == 0
    _path, _, env = config(postgres, tmp_path, database)
    migrate_to(env)
    run_python(
        env,
        """
import django
django.setup()
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("DROP TABLE control_plane_trafficsnapshot")
""",
    )
    run_python(env, SEED_IDENTITIES)
    return env


@pytest.fixture
def ingest_runtime(postgres, tmp_path: Path):
    database = f"sbm_traffic_ingest_{uuid.uuid4().hex[:12]}"
    assert (
        psql(postgres, "postgres", f"DROP DATABASE IF EXISTS {database}").returncode
        == 0
    )
    assert psql(postgres, "postgres", provision_sql(database)).returncode == 0
    path, _, env = config(postgres, tmp_path, database)
    result = cli(path, env, "migrate")
    assert result.returncode == 0, result.stderr
    run_python(env, SEED_IDENTITIES)
    return path, env


def test_migration_schema_constraints_and_no_new_route(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        """
import django
django.setup()

from django.contrib import admin
from django.core.management import get_commands
from django.db import connection, models
from django.urls import get_resolver

from sing_box_manager.control_plane.models import TrafficSnapshot

field = TrafficSnapshot._meta.get_field("credential")
assert field.target_field.name == "uuid"
assert field.remote_field.on_delete is models.PROTECT
assert TrafficSnapshot._meta.get_field("source").remote_field.on_delete is models.PROTECT
assert not TrafficSnapshot._meta.get_field("source").has_default()
assert not field.has_default()
assert not admin.site.is_registered(TrafficSnapshot)
assert "traffic_ingest" not in get_commands()
patterns = [str(pattern.pattern) for pattern in get_resolver().url_patterns]
assert not any("node/v1/traffic" in pattern for pattern in patterns)

with connection.cursor() as cursor:
    cursor.execute(
        "SELECT data_type, udt_name FROM information_schema.columns "
        "WHERE table_name = 'control_plane_trafficsnapshot'"
    )
    columns = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.execute(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_name = 'control_plane_trafficsnapshot'"
    )
    column_types = dict(cursor.fetchall())
    assert column_types["payload"] == "bytea"
    assert column_types["version"] == "int8"
    assert column_types["upload"] == "int8"
    assert column_types["download"] == "int8"
    assert column_types["source_id"] == "uuid"
    assert column_types["credential_id"] == "uuid"
    cursor.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'control_plane_trafficsnapshot'::regclass"
    )
    constraints = {row[0] for row in cursor.fetchall()}
    assert {
        "unique_traffic_snapshot_key",
        "traffic_snapshot_version_positive",
        "traffic_snapshot_digest_lower_hex",
        "traffic_snapshot_payload_bounds",
        "traffic_snapshot_kind_valid",
        "traffic_snapshot_kind_shape",
    } <= constraints
print("schema-ok")
""",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "django",
            "makemigrations",
            "--check",
            "--dry-run",
            "control_plane",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_codec_validation_errors_propagate_before_database_work(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
snapshot = counter(version=1, upload=1, download=2)
request, raw = wire(snapshot)
try:
    ingest_page(NODE_ID, raw[:-1])
except tc.TrafficContractError as exc:
    assert exc.code == "invalid_request"
    assert str(exc) == "invalid_request"
else:
    raise AssertionError("malformed wire body reached persistence")
assert TrafficSnapshot.objects.count() == 0
print("codec-error-ok")
""",
    )


def test_counter_insert_and_actual_codec_receipt_round_trip(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
snapshot = counter(version=1, upload=1024, download=2048)
request, raw = wire(snapshot)
receipt_bytes = ingest_page(NODE_ID, raw)
response = parse_receipts(request, receipt_bytes)
assert len(response.receipts) == 1
receipt = response.receipts[0]
assert receipt.source_id == snapshot.source_id
assert receipt.credential_id == snapshot.credential_id
assert receipt.kind == "counter"
assert receipt.accounting_date == snapshot.accounting_date
assert receipt.version == 1
assert receipt.digest == snapshot.digest
row = row_for(snapshot)
assert row.version == 1
assert row.digest == snapshot.digest
assert row.upload == 1024
assert row.download == 2048
assert row.detail is None
assert row.payload == tc.canonical_payload_bytes(snapshot)
print("counter-ok")
""",
    )


def test_database_constraints_reject_invalid_shapes(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

source = TrafficSource.objects.get(pk=SOURCE_ID)
credential = Credential.objects.get(uuid=CREDENTIAL_UUID)


def model_instance(**overrides):
    values = {
        "source": source,
        "credential": credential,
        "kind": "counter",
        "accounting_date": date(2026, 1, 14),
        "version": 1,
        "digest": "a" * 64,
        "payload": b"{}",
        "upload": 1,
        "download": 2,
        "detail": None,
    }
    values.update(overrides)
    return TrafficSnapshot(**values)


model_instance().full_clean()
for invalid_payload in (b"", b"x" * (768 * 1024 + 1)):
    try:
        model_instance(payload=invalid_payload).full_clean()
    except ValidationError:
        pass
    else:
        raise AssertionError("full_clean accepted an invalid payload bound")

base = {
    "source": source,
    "credential": credential,
    "kind": "counter",
    "accounting_date": date(2026, 1, 1),
    "version": 1,
    "digest": "a" * 64,
    "payload": b"{}",
    "upload": 1,
    "download": 2,
    "detail": None,
}
TrafficSnapshot.objects.create(**base)


def rejected(**overrides):
    values = {**base, **overrides}
    try:
        with transaction.atomic():
            TrafficSnapshot.objects.create(**values)
    except IntegrityError:
        return
    raise AssertionError(f"database accepted {overrides}")


rejected()
rejected(accounting_date=date(2026, 1, 2), version=0)
rejected(accounting_date=date(2026, 1, 3), digest="A" * 64)
rejected(accounting_date=date(2026, 1, 4), digest="a" * 63)
rejected(accounting_date=date(2026, 1, 5), payload=b"")
rejected(
    accounting_date=date(2026, 1, 6),
    payload=b"x" * (768 * 1024 + 1),
)
max_payload = TrafficSnapshot.objects.create(
    **{
        **base,
        "accounting_date": date(2026, 1, 13),
        "payload": b"x" * (768 * 1024),
    }
)
assert max_payload.payload == b"x" * (768 * 1024)
rejected(accounting_date=date(2026, 1, 7), detail="exact")
rejected(accounting_date=date(2026, 1, 8), upload=None)
rejected(accounting_date=date(2026, 1, 9), upload=-1)
rejected(
    accounting_date=date(2026, 1, 10),
    kind="attribution",
    detail="exact",
    upload=1,
    download=None,
)
rejected(
    accounting_date=date(2026, 1, 11),
    kind="attribution",
    detail=None,
    upload=None,
    download=None,
)
rejected(accounting_date=date(2026, 1, 12), kind="other")
for obj in (source, credential):
    try:
        with transaction.atomic():
            obj.delete()
    except ProtectedError:
        pass
    else:
        raise AssertionError("PROTECT did not retain a referenced identity")
assert TrafficSnapshot.objects.count() == 2
print("constraints-ok")
""",
    )


UNICODE_NAMES = [
    "nul\x00name",
    "control\x01\b\f\n\r\t",
    'he said "hi"\\ok',
    "café.example",
    "cafe\u0301.example",
    "line\u2028sep\u2029end",
    "a&b<c>d.example",
]


def test_unicode_payload_is_stored_as_exact_bytea(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + f"""
names = {UNICODE_NAMES!r}
snapshot = attribution(
    version=1,
    detail="exact",
    domains=[entry(name, upload=1, download=2, connections=3) for name in names],
    destinations=[entry("2001:db8::1", upload=4, download=5, connections=6)],
)
request, raw = wire(snapshot)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].digest == snapshot.digest
row = row_for(snapshot)
expected = tc.canonical_payload_bytes(snapshot)
assert row.payload == expected
assert row.digest == snapshot.digest
assert row.kind == "attribution"
assert row.detail == "exact"
assert row.upload is None
assert row.download is None
text = expected.decode("utf-8")
assert "nul\\\\u0000name" in text
assert "control\\\\u0001\\\\b\\\\f\\\\n\\\\r\\\\t" in text
assert 'he said \\\\"hi\\\\"\\\\\\\\ok' in text
assert names[3] in text
assert names[4] in text
assert "line\\\\u2028sep\\\\u2029end" in text
assert "\\\\u003c" not in text
assert "\\\\u003e" not in text
assert "\\\\u0026" not in text
print("unicode-ok")
""",
    )


def test_counter_version_matrix_no_op_timestamps_and_regressions(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
s1 = counter(version=1, upload=10, download=20)
request, raw = wire(s1)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 1
assert response.receipts[0].digest == s1.digest
row = row_for(s1)
assert row.version == 1
assert row.payload == tc.canonical_payload_bytes(s1)
updated1 = row.updated_at

# Equal version and digest is a no-op, including the timestamp.
request, raw = wire(s1)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 1
assert response.receipts[0].digest == s1.digest
row.refresh_from_db()
assert row.updated_at == updated1
assert row.payload == tc.canonical_payload_bytes(s1)

# Equal version with another digest is a whole-page conflict.
conflict = counter(version=1, upload=11, download=20)
expect_error("version_conflict", conflict)
row.refresh_from_db()
assert row.version == 1
assert row.digest == s1.digest
assert row.updated_at == updated1

# A higher version replaces the row and its complete payload.
s2 = counter(version=2, upload=15, download=25)
request, raw = wire(s2)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 2
assert response.receipts[0].digest == s2.digest
row.refresh_from_db()
assert row.version == 2
assert row.payload == tc.canonical_payload_bytes(s2)
updated2 = row.updated_at

# An older version returns the stored receipt and does not touch timestamps.
request, raw = wire(s1)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 2
assert response.receipts[0].digest == s2.digest
row.refresh_from_db()
assert row.version == 2
assert row.updated_at == updated2

# Same version, different digest again conflicts.
conflict2 = counter(version=2, upload=16, download=25)
expect_error("version_conflict", conflict2)
row.refresh_from_db()
assert row.version == 2
assert row.updated_at == updated2

# Either counter component decreasing on a higher version regresses.
expect_error(
    "counter_regression", counter(version=3, upload=14, download=25)
)
expect_error(
    "counter_regression", counter(version=3, upload=16, download=24)
)
row.refresh_from_db()
assert row.version == 2
assert row.updated_at == updated2

s3 = counter(version=3, upload=16, download=26)
request, raw = wire(s3)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 3
assert response.receipts[0].digest == s3.digest
row.refresh_from_db()
assert row.version == 3
assert row.payload == tc.canonical_payload_bytes(s3)
assert TrafficSnapshot.objects.count() == 1
print("versions-ok")
""",
    )


def test_attribution_replacement_can_shrink_after_compaction(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
first = attribution(
    version=1,
    detail="compacted",
    domains=[
        entry("a.example.com", upload=10, download=20, connections=1),
        entry("b.example.com", upload=30, download=40, connections=2),
    ],
    destinations=[entry("198.51.100.7", upload=50, download=60, connections=3)],
)
request, raw = wire(first)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 1
row = row_for(first)
assert row.detail == "compacted"
assert row.payload == tc.canonical_payload_bytes(first)
updated1 = row.updated_at

second = attribution(
    version=2,
    detail="compacted",
    domains=[entry("a.example.com", upload=10, download=20, connections=1)],
    destinations=[],
)
request, raw = wire(second)
response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 2
assert response.receipts[0].digest == second.digest
row.refresh_from_db()
assert row.version == 2
assert row.detail == "compacted"
assert row.upload is None and row.download is None
assert row.payload == tc.canonical_payload_bytes(second)
assert b"b.example.com" not in row.payload
assert b"198.51.100.7" not in row.payload
assert TrafficSnapshot.objects.count() == 1
print("attribution-ok")
""",
    )


def test_whole_page_rollback_and_receipt_coverage(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
existing = counter(
    version=1, upload=10, download=20, day="2026-01-01"
)
request, raw = wire(existing)
parse_receipts(request, ingest_page(NODE_ID, raw))

new = counter(version=1, upload=1, download=2, day="2026-01-02")
conflict = counter(
    version=1, upload=99, download=99, day="2026-01-01"
)
page_request, page_raw = wire(new, conflict)
try:
    ingest_page(NODE_ID, page_raw)
except TrafficIngestError as exc:
    assert exc.code == "version_conflict"
else:
    raise AssertionError("conflicting page succeeded")

assert not TrafficSnapshot.objects.filter(
    accounting_date=date(2026, 1, 2)
).exists()
existing_row = row_for(existing)
assert existing_row.version == 1
assert existing_row.digest == existing.digest
assert existing_row.payload == tc.canonical_payload_bytes(existing)
assert TrafficSnapshot.objects.count() == 1

# A valid page returns one exact codec receipt per request key.
page_request, page_raw = wire(new, existing)
response = parse_receipts(page_request, ingest_page(NODE_ID, page_raw))
assert {receipt.kind for receipt in response.receipts} == {"counter"}
assert {receipt.accounting_date for receipt in response.receipts} == {
    date(2026, 1, 1),
    date(2026, 1, 2),
}
assert {receipt.version for receipt in response.receipts} == {1}
assert TrafficSnapshot.objects.count() == 2
print("rollback-ok")
""",
    )


def test_ownership_rules_and_history_do_not_filter_operational_state(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
identity_counts = (
    Node.objects.count(),
    TrafficSource.objects.count(),
    Credential.objects.count(),
    Account.objects.count(),
)
missing_source = uuid.uuid4()
missing_credential = uuid.uuid4()
expect_error(
    "ownership_mismatch",
    counter(source=missing_source, credential=CREDENTIAL_UUID),
)
expect_error(
    "ownership_mismatch",
    counter(source=SOURCE_ID, credential=missing_credential),
)
expect_error(
    "ownership_mismatch",
    counter(source=SOURCE2_ID, credential=CREDENTIAL_UUID),
)
expect_error(
    "ownership_mismatch",
    counter(source=SOURCE_ID, credential=CREDENTIAL2_UUID),
)
expect_error(
    "ownership_mismatch",
    counter(source=SOURCE2_ID, credential=CREDENTIAL2_UUID),
)
expect_error(
    "ownership_mismatch",
    counter(source=SOURCE_ID, credential=UNBOUND_CREDENTIAL_UUID),
)
assert TrafficSnapshot.objects.count() == 0
assert (
    Node.objects.count(),
    TrafficSource.objects.count(),
    Credential.objects.count(),
    Account.objects.count(),
) == identity_counts

account = Account.objects.get(username="ingest-alpha")
credential = Credential.objects.get(uuid=CREDENTIAL_UUID)
account.enabled = False
account.portal_enabled = False
account.save(update_fields=["enabled", "portal_enabled"])
credential.enabled = False
credential.save(update_fields=["enabled"])

disabled_snapshot = counter(
    version=1, upload=7, download=9, day="2026-02-01"
)
request, raw = wire(disabled_snapshot)
parse_receipts(request, ingest_page(NODE_ID, raw))
assert row_for(disabled_snapshot).version == 1

retired_source_snapshot = counter(
    version=1,
    source=RETIRED_SOURCE_ID,
    credential=CREDENTIAL_UUID,
    day="2026-02-02",
)
request, raw = wire(retired_source_snapshot)
parse_receipts(request, ingest_page(NODE_ID, raw))
assert row_for(retired_source_snapshot).version == 1
assert TrafficSnapshot.objects.count() == 2
assert (
    Node.objects.count(),
    TrafficSource.objects.count(),
    Credential.objects.count(),
    Account.objects.count(),
) == identity_counts
print("ownership-ok")
""",
    )


def test_no_op_comparison_does_not_fetch_stored_payload(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
from django.db import connection
from django.test.utils import CaptureQueriesContext

snapshot = counter(version=1, upload=10, download=20)
request, raw = wire(snapshot)
parse_receipts(request, ingest_page(NODE_ID, raw))
request, raw = wire(snapshot)
with CaptureQueriesContext(connection) as captured:
    response = parse_receipts(request, ingest_page(NODE_ID, raw))
assert response.receipts[0].version == 1
selects = [
    query["sql"]
    for query in captured.captured_queries
    if query["sql"].lstrip().upper().startswith("SELECT")
]
assert selects
assert not any(
    "payload" in query.lower() or "secret" in query.lower()
    for query in selects
), selects
assert TrafficSnapshot.objects.count() == 1
print("deferred-ok")
""",
    )


def test_nested_transaction_and_commit_failure_cannot_return_success(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
from unittest.mock import patch

from django.db import connection, transaction

from sing_box_manager.control_plane import traffic_ingest as service

snapshot = counter(version=1, upload=1, download=2, day="2026-03-01")
request, raw = wire(snapshot)
nested = False
with transaction.atomic():
    try:
        ingest_page(NODE_ID, raw)
    except RuntimeError:
        nested = True
    else:
        raise AssertionError("nested transaction returned a receipt")
assert nested
assert TrafficSnapshot.objects.count() == 0

with patch.object(
    service.transaction, "atomic", wraps=service.transaction.atomic
) as atomic:
    result = ingest_page(NODE_ID, raw)
assert atomic.call_args.kwargs.get("durable") is True
assert parse_receipts(request, result).receipts[0].version == 1

commit_snapshot = counter(
    version=1, upload=3, download=4, day="2026-03-02"
)
commit_request, commit_raw = wire(commit_snapshot)
commit_failed = False
with patch.object(
    connection, "commit", side_effect=RuntimeError("synthetic commit failure")
):
    try:
        ingest_page(NODE_ID, commit_raw)
    except Exception:
        commit_failed = True
    else:
        raise AssertionError("commit failure returned a receipt")
assert commit_failed
connection.close()
assert not TrafficSnapshot.objects.filter(
    accounting_date=date(2026, 3, 2)
).exists()
print("atomicity-ok")
""",
    )


def test_autocommit_disabled_without_atomic_rejected(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
from django.db import connection

snapshot = counter(version=1, upload=5, download=6, day="2026-03-03")
request, raw = wire(snapshot)
original_autocommit = connection.get_autocommit()
connection.set_autocommit(False)
try:
    assert connection.in_atomic_block is False
    try:
        ingest_page(NODE_ID, raw)
    except RuntimeError:
        pass
    else:
        raise AssertionError("disabled autocommit returned a receipt")
    connection.rollback()
finally:
    connection.set_autocommit(original_autocommit)
assert connection.get_autocommit() == original_autocommit
assert not TrafficSnapshot.objects.filter(
    accounting_date=date(2026, 3, 3)
).exists()
print("autocommit-ok")
""",
    )


def test_real_concurrency_same_key_and_reversed_overlapping_pages(ingest_runtime):
    _path, env = ingest_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
import threading

from django.db import close_old_connections


def run_pair(first_page, second_page):
    results = []
    barrier = threading.Barrier(2)

    def worker(label, page):
        close_old_connections()
        try:
            request, raw = wire(*page)
        except Exception as exc:
            results.append((label, type(exc).__name__, str(exc)))
            return
        try:
            barrier.wait(timeout=10)
            receipts = ingest_page(NODE_ID, raw)
            response = parse_receipts(request, receipts)
            results.append(
                (
                    label,
                    "ok",
                    tuple(
                        (receipt.version, receipt.digest)
                        for receipt in response.receipts
                    ),
                )
            )
        except TrafficIngestError as exc:
            results.append((label, exc.code, None))
        except Exception as exc:
            results.append((label, type(exc).__name__, str(exc)))
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=worker, args=(label, page), daemon=True)
        for label, page in (
            ("first", first_page),
            ("second", second_page),
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(not thread.is_alive() for thread in threads), "bounded completion"
    return results


# Equal same-key requests both succeed; the no-op path leaves updated_at alone.
equal = counter(version=1, upload=10, download=20, day="2026-04-01")
request, raw = wire(equal)
parse_receipts(request, ingest_page(NODE_ID, raw))
equal_row = row_for(equal)
equal_updated = equal_row.updated_at
results = run_pair((equal,), (equal,))
assert sorted(result[1] for result in results) == ["ok", "ok"], results
assert all(result[2] == ((1, equal.digest),) for result in results)
equal_row.refresh_from_db()
assert equal_row.updated_at == equal_updated
assert TrafficSnapshot.objects.count() == 1

# Same version with different digests: exactly one writer wins.
conflict_a = counter(version=2, upload=30, download=40, day="2026-04-02")
conflict_b = counter(version=2, upload=31, download=40, day="2026-04-02")
results = run_pair((conflict_a,), (conflict_b,))
assert sorted(result[1] for result in results) == [
    "ok",
    "version_conflict",
], results
successful = [result for result in results if result[1] == "ok"]
assert len(successful) == 1
conflict_row = row_for(conflict_a)
assert conflict_row.version == 2
assert conflict_row.digest in {conflict_a.digest, conflict_b.digest}
assert successful[0][2] == ((2, conflict_row.digest),)
assert TrafficSnapshot.objects.filter(
    accounting_date=date(2026, 4, 2)
).count() == 1

# A concurrent higher and lower version both succeed; the higher version wins.
initial = counter(version=1, upload=1, download=2, day="2026-04-03")
request, raw = wire(initial)
parse_receipts(request, ingest_page(NODE_ID, raw))
lower = counter(version=2, upload=20, download=30, day="2026-04-03")
higher = counter(version=3, upload=30, download=40, day="2026-04-03")
results = run_pair((lower,), (higher,))
assert sorted(result[1] for result in results) == ["ok", "ok"], results
by_label = {result[0]: result for result in results}
assert by_label["first"][2] in {
    ((2, lower.digest),),
    ((3, higher.digest),),
}
assert by_label["second"][2] == ((3, higher.digest),)
version_row = row_for(higher)
assert version_row.version == 3
assert version_row.digest == higher.digest

# Reversed overlapping pages use the same sorted source/credential lock order.
page_a = (
    counter(
        version=1,
        source=SOURCE_ID,
        credential=CREDENTIAL_UUID,
        day="2026-04-04",
    ),
    counter(
        version=1,
        source=RETIRED_SOURCE_ID,
        credential=CREDENTIAL3_UUID,
        day="2026-04-04",
    ),
)
page_b = tuple(reversed(page_a))
results = run_pair(page_a, page_b)
assert sorted(result[1] for result in results) == ["ok", "ok"], results
by_label = {result[0]: result for result in results}
assert by_label["first"][2] == tuple(
    (1, snapshot.digest) for snapshot in page_a
)
assert by_label["second"][2] == tuple(
    (1, snapshot.digest) for snapshot in page_b
)
assert TrafficSnapshot.objects.filter(
    accounting_date=date(2026, 4, 4)
).count() == 2
assert TrafficSnapshot.objects.count() == 5
print("concurrency-ok")
""",
    )


def test_unapplied_snapshot_table_is_not_treated_as_empty(missing_snapshot_runtime):
    env = missing_snapshot_runtime
    run_python(
        env,
        INGEST_PRELUDE
        + """
from django.db import DatabaseError, connection

try:
    list(TrafficSnapshot._base_manager.all())
except DatabaseError as exc:
    assert "control_plane_trafficsnapshot" in str(exc)
else:
    raise AssertionError("missing snapshot table returned an empty queryset")
connection.close()

credential = Credential.objects.get(uuid=CREDENTIAL_UUID)
try:
    credential.delete()
except DatabaseError as exc:
    assert "control_plane_trafficsnapshot" in str(exc)
else:
    raise AssertionError("delete dependency inspection ignored the missing table")
connection.close()
assert Credential.objects.filter(uuid=CREDENTIAL_UUID).exists()
assert TrafficSource.objects.filter(pk=SOURCE_ID).exists()
print("missing-table-error-ok")
""",
    )
