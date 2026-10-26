"""Compose the real Go counter exporter with the real PostgreSQL ingest service.

Each check drives the actual `accounting.Store` and `exporter.Client` against a
private loopback TLS process. That process performs fixture bearer
authentication, then calls the actual `ingest_page` service against an isolated
real PostgreSQL database. It is not a production route, resolver, registration
or binding operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sing_box_manager.control_plane import traffic_contract as tc
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

NODE_ID = uuid.UUID("a1111111-1111-4111-8111-111111111111")
SOURCE_ID = uuid.UUID("b2222222-2222-4222-8222-222222222222")
CREDENTIAL_A = uuid.UUID("c3333333-3333-4333-8333-333333333333")
CREDENTIAL_B = uuid.UUID("d4444444-4444-4444-8444-444444444444")
DAY = "2026-10-23"
TOKEN = "fixture-counter-export-bearer-token"
REJECTED_TOKEN = "fixture-counter-export-wrong-bearer-token"
GO_TEST = "^TestPostgresCounterExportComposition$"

SEED_IDENTITIES = f"""
import uuid

import django
django.setup()
from sing_box_manager.control_plane.models import (
    Account,
    Credential,
    Node,
    TrafficSource,
)

node = Node.objects.create(
    id=uuid.UUID("{NODE_ID}"),
    slug="counter-export-fixture",
    name="Counter export fixture",
    token_digest="a" * 64,
)
first = Account.objects.create(username="counter_export_a")
Credential.objects.create(
    account=first,
    protocol="trojan",
    secret="fixture-counter-export-a",
    uuid=uuid.UUID("{CREDENTIAL_A}"),
    node=node,
    wire_name="trojan:counter-export-a",
)
second = Account.objects.create(username="counter_export_b")
Credential.objects.create(
    account=second,
    protocol="hysteria2",
    secret="fixture-counter-export-b",
    uuid=uuid.UUID("{CREDENTIAL_B}"),
    node=node,
    wire_name="hysteria2:counter-export-b",
)
TrafficSource.objects.create(id=uuid.UUID("{SOURCE_ID}"), node=node)
"""


def fixture_rows() -> list[dict[str, Any]]:
    return [
        {
            "credential_id": str(CREDENTIAL_A),
            "upload": 1024,
            "download": 2048,
            "updated_upload": 1536,
            "updated_download": 3072,
            "changed": True,
        },
        {
            "credential_id": str(CREDENTIAL_B),
            "upload": 4096,
            "download": 8192,
            "updated_upload": 4608,
            "updated_download": 8704,
            "changed": False,
        },
    ]


def _require_go() -> str:
    go = shutil.which("go")
    if go is not None:
        return go
    reason = "The Go toolchain is required for counter export composition"
    if (
        os.environ.get("SBM_REQUIRE_NODE_G0") == "1"
        or os.environ.get("SBM_REQUIRE_POSTGRES") == "1"
    ):
        pytest.fail(reason)
    pytest.skip(reason)


def _require_openssl() -> str:
    openssl = shutil.which("openssl")
    if openssl is not None:
        return openssl
    reason = "openssl is required for the private counter export TLS fixture"
    if (
        os.environ.get("SBM_REQUIRE_NODE_G0") == "1"
        or os.environ.get("SBM_REQUIRE_POSTGRES") == "1"
    ):
        pytest.fail(reason)
    pytest.skip(reason)


def _private_write(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def _make_tls(root: Path) -> tuple[Path, Path]:
    openssl = _require_openssl()
    cert = root / "fixture-server.crt"
    key = root / "fixture-server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


@dataclass
class CounterExportServer:
    process: subprocess.Popen[str]
    port: int
    cert: Path
    body_dir: Path
    evidence_path: Path
    stderr_path: Path

    def events(self) -> list[dict[str, Any]]:
        if not self.evidence_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.evidence_path.read_text().splitlines()
            if line
        ]

    def request_bytes(self, index: int) -> bytes:
        return (self.body_dir / f"request-{index:03d}.json").read_bytes()

    def response_bytes(self, index: int) -> bytes | None:
        path = self.body_dir / f"response-{index:03d}.json"
        return path.read_bytes() if path.exists() else None


def _start_server(
    env: dict[str, str], root: Path, mode: str, cert: Path, key: Path
) -> CounterExportServer:
    body_dir = root / "server-bodies"
    body_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence_path = root / "server-evidence.jsonl"
    stderr_path = root / "server.stderr"
    server_env = {
        **env,
        "SBM_COUNTER_FIXTURE_TOKEN": TOKEN,
        "SBM_COUNTER_FIXTURE_NODE_ID": str(NODE_ID),
        "SBM_COUNTER_FIXTURE_MODE": mode,
        "SBM_COUNTER_FIXTURE_BODY_DIR": str(body_dir),
        "SBM_COUNTER_FIXTURE_EVIDENCE": str(evidence_path),
        "SBM_COUNTER_FIXTURE_CERT": str(cert),
        "SBM_COUNTER_FIXTURE_KEY": str(key),
    }
    stderr_file = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.node_counter_export.server"],
            cwd=ROOT,
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
        )
    finally:
        stderr_file.close()
    ready = ""
    try:
        if process.stdout is not None:
            readable, _, _ = select.select([process.stdout], [], [], 30)
            if readable:
                ready = process.stdout.readline()
        if not ready:
            diagnostics = stderr_path.read_text(errors="replace")
            pytest.fail(
                f"The private PostgreSQL/TLS fixture did not start: {diagnostics}"
            )
        port = json.loads(ready)["port"]
    except BaseException:
        _abort_server_start(process)
        raise
    return CounterExportServer(
        process=process,
        port=port,
        cert=cert,
        body_dir=body_dir,
        evidence_path=evidence_path,
        stderr_path=stderr_path,
    )


def _abort_server_start(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _stop_server(server: CounterExportServer) -> None:
    if server.process.poll() is None:
        server.process.terminate()
    try:
        server.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.process.kill()
        server.process.wait()
    if server.process.stdout is not None:
        server.process.stdout.close()


@contextmanager
def _running_server(
    env: dict[str, str], root: Path, mode: str, cert: Path, key: Path
):
    server = _start_server(env, root, mode, cert, key)
    try:
        yield server
    finally:
        _stop_server(server)


@pytest.fixture
def counter_export_runtime(postgres, tmp_path: Path) -> dict[str, str]:
    database = f"sbm_counter_export_{uuid.uuid4().hex[:12]}"
    assert (
        psql(postgres, "postgres", f"DROP DATABASE IF EXISTS {database}").returncode
        == 0
    )
    assert psql(postgres, "postgres", provision_sql(database)).returncode == 0
    path, _, env = config(postgres, tmp_path, database)
    result = cli(path, env, "migrate")
    assert result.returncode == 0, result.stderr
    python(env, SEED_IDENTITIES)
    return env


def _run_go(
    env: dict[str, str],
    root: Path,
    server: CounterExportServer,
    rows: list[dict[str, Any]],
    scenario: str,
    failure_mode: str = "",
) -> dict[str, Any]:
    go = _require_go()
    descriptor = root / "go-fixture.json"
    evidence = root / "go-evidence.json"
    _private_write(
        descriptor,
        json.dumps(
            {
                "origin": f"https://127.0.0.1:{server.port}",
                "ca_file": str(server.cert),
                "token": TOKEN,
                "rejected_token": REJECTED_TOKEN,
                "sqlite_path": str(root / "traffic.sqlite3"),
                "evidence_path": str(evidence),
                "source_id": str(SOURCE_ID),
                "day": DAY,
                "page_size": 100,
                "scenario": scenario,
                "failure_mode": failure_mode,
                "rows": rows,
            },
            sort_keys=True,
        ).encode(),
    )
    go_env = {
        **env,
        "GOPROXY": "off",
        "CGO_ENABLED": "0",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=readonly",
        "SBM_COUNTER_EXPORT_FIXTURE": str(descriptor),
    }
    result = subprocess.run(
        [
            go,
            "-C",
            "node-agent",
            "test",
            "-count=1",
            "-run",
            GO_TEST,
            "-v",
            "./internal/exporter",
        ],
        cwd=ROOT,
        env=go_env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"go test failed:\n{result.stdout}\n{result.stderr}"
    assert "--- SKIP" not in result.stdout, result.stdout
    assert "--- PASS: TestPostgresCounterExportComposition" in result.stdout, (
        result.stdout
    )
    assert evidence.is_file(), result.stdout
    return json.loads(evidence.read_text())


def _postgres_rows(env: dict[str, str]) -> list[dict[str, Any]]:
    output = python(
        env,
        """
import json

import django
django.setup()
from sing_box_manager.control_plane.models import TrafficSnapshot

rows = []
for row in TrafficSnapshot.objects.order_by(
    "source_id", "credential_id", "kind", "accounting_date"
):
    payload = bytes(row.payload)
    rows.append(
        {
            "source_id": str(row.source_id),
            "credential_id": str(row.credential_id),
            "kind": row.kind,
            "accounting_date": row.accounting_date.isoformat(),
            "version": row.version,
            "digest": row.digest,
            "upload": row.upload,
            "download": row.download,
            "detail": row.detail,
            "payload_hex": payload.hex(),
        }
    )
print(json.dumps(rows, sort_keys=True))
""",
    )
    rows = json.loads(output)
    for row in rows:
        payload = bytes.fromhex(row["payload_hex"])
        row["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    return rows


def _counter(row: dict[str, Any], *, updated: bool = False) -> tc.CounterSnapshot:
    upload = row["updated_upload"] if updated else row["upload"]
    download = row["updated_download"] if updated else row["download"]
    version = 2 if updated else 1
    return tc.make_counter_snapshot(
        source_id=str(SOURCE_ID),
        credential_id=row["credential_id"],
        accounting_date=DAY,
        version=version,
        upload=upload,
        download=download,
    )


def _initial_counters(rows: list[dict[str, Any]]) -> list[tc.CounterSnapshot]:
    return [_counter(row) for row in rows]


def _updated_counters(rows: list[dict[str, Any]]) -> list[tc.CounterSnapshot]:
    return [_counter(row, updated=row["changed"]) for row in rows]


def _changed_counters(rows: list[dict[str, Any]]) -> list[tc.CounterSnapshot]:
    return [_counter(row, updated=True) for row in rows if row["changed"]]


def _request_bytes(snapshots: list[tc.CounterSnapshot]) -> bytes:
    return tc.encode_request(tc.make_request(snapshots))


def _receipt_response(snapshots: list[tc.CounterSnapshot]) -> tc.ReceiptResponse:
    return tc.ReceiptResponse(
        schema=1,
        receipts=tuple(
            tc.Receipt(
                source_id=snapshot.source_id,
                credential_id=snapshot.credential_id,
                kind="counter",
                accounting_date=snapshot.accounting_date,
                version=snapshot.version,
                digest=snapshot.digest,
            )
            for snapshot in snapshots
        ),
    )


def _response_bytes(snapshots: list[tc.CounterSnapshot]) -> bytes:
    request = tc.make_request(snapshots)
    return tc.encode_receipt_response(_receipt_response(snapshots), request)


def _stored_rows(snapshots: list[tc.CounterSnapshot]) -> list[dict[str, Any]]:
    rows = []
    for snapshot in snapshots:
        payload = tc.canonical_payload_bytes(snapshot)
        rows.append(
            {
                "source_id": snapshot.source_id,
                "credential_id": snapshot.credential_id,
                "kind": "counter",
                "accounting_date": snapshot.accounting_date.isoformat(),
                "version": snapshot.version,
                "digest": snapshot.digest,
                "upload": snapshot.upload,
                "download": snapshot.download,
                "detail": None,
                "payload_hex": payload.hex(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return sorted(rows, key=lambda row: row["credential_id"])


def _local_usage(snapshots: list[tc.CounterSnapshot]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "credential_id": snapshot.credential_id,
                "day": snapshot.accounting_date.isoformat(),
                "version": snapshot.version,
                "upload": snapshot.upload,
                "download": snapshot.download,
            }
            for snapshot in snapshots
        ],
        key=lambda row: row["credential_id"],
    )


def _local_acks(snapshots: list[tc.CounterSnapshot]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "credential_id": snapshot.credential_id,
                "day": snapshot.accounting_date.isoformat(),
                "version": snapshot.version,
                "digest": snapshot.digest,
            }
            for snapshot in snapshots
        ],
        key=lambda row: row["credential_id"],
    )


def _pending(snapshots: list[tc.CounterSnapshot]) -> list[dict[str, Any]]:
    return [
        {
            "credential_id": snapshot.credential_id,
            "accounting_date": snapshot.accounting_date.isoformat(),
            "version": snapshot.version,
            "upload": snapshot.upload,
            "download": snapshot.download,
            "digest": snapshot.digest,
        }
        for snapshot in snapshots
    ]


def _assert_step(
    actual: dict[str, Any],
    name: str,
    selected: int,
    acknowledged: int,
    pending: list[tc.CounterSnapshot],
    error: str = "",
) -> None:
    assert actual["name"] == name
    assert actual["selected"] == selected
    assert actual["acknowledged"] == acknowledged
    assert actual["pending"] == _pending(pending)
    if error:
        assert error in actual.get("error", "")
    else:
        assert actual.get("error", "") == ""


def _assert_request(
    server: CounterExportServer, index: int, snapshots: list[tc.CounterSnapshot]
) -> None:
    raw = server.request_bytes(index)
    assert raw == _request_bytes(snapshots)
    parsed = tc.parse_request(raw)
    assert [
        (
            snapshot.credential_id,
            snapshot.version,
            snapshot.digest,
            snapshot.upload,
            snapshot.download,
        )
        for snapshot in parsed.snapshots
    ] == [
        (
            snapshot.credential_id,
            snapshot.version,
            snapshot.digest,
            snapshot.upload,
            snapshot.download,
        )
        for snapshot in snapshots
    ]


def _assert_response(
    server: CounterExportServer, index: int, snapshots: list[tc.CounterSnapshot]
) -> None:
    raw = server.response_bytes(index)
    assert raw is not None
    assert raw == _response_bytes(snapshots)


def _receipt_error(
    server: CounterExportServer, index: int, snapshots: list[tc.CounterSnapshot]
) -> str:
    raw = server.response_bytes(index)
    assert raw is not None
    with pytest.raises(tc.TrafficContractError) as error:
        tc.parse_receipt_response(raw, tc.make_request(snapshots))
    return error.value.code.value


def _assert_event_rows(
    event: dict[str, Any], snapshots: list[tc.CounterSnapshot]
) -> None:
    actual = sorted(
        [
            {key: value for key, value in row.items() if key != "updated_at"}
            for row in event["rows"]
        ],
        key=lambda row: row["credential_id"],
    )
    assert actual == _stored_rows(snapshots)


def _assert_postgres_rows(
    actual: list[dict[str, Any]], snapshots: list[tc.CounterSnapshot]
) -> None:
    normalized = sorted(
        [
            {key: value for key, value in row.items() if key != "updated_at"}
            for row in actual
        ],
        key=lambda row: row["credential_id"],
    )
    assert normalized == _stored_rows(snapshots)


def _assert_local_state(
    evidence: dict[str, Any], snapshots: list[tc.CounterSnapshot]
) -> None:
    assert evidence["usage"] == _local_usage(snapshots)
    assert evidence["acks"] == _local_acks(snapshots)


def test_closed_loop_durable_ack_and_local_update(
    counter_export_runtime: dict[str, str], tmp_path: Path
) -> None:
    rows = fixture_rows()
    initial = _initial_counters(rows)
    updated = _updated_counters(rows)
    changed = _changed_counters(rows)
    cert, key = _make_tls(tmp_path)
    with _running_server(
        counter_export_runtime, tmp_path, "normal", cert, key
    ) as server:
        evidence = _run_go(
            counter_export_runtime, tmp_path, server, rows, "closed_loop"
        )
    assert evidence["scenario"] == "closed_loop"
    assert len(evidence["steps"]) == 3
    _assert_step(evidence["steps"][0], "initial export", 2, 2, [])
    _assert_step(evidence["steps"][1], "reopened export", 0, 0, [])
    _assert_step(evidence["steps"][2], "updated export", 1, 1, [])
    _assert_local_state(evidence, updated)

    events = server.events()
    assert len(events) == 2
    assert [event["authorization_ok"] for event in events] == [True, True]
    assert [event["committed"] for event in events] == [True, True]
    assert [event["status"] for event in events] == [200, 200]
    _assert_event_rows(events[0], initial)
    _assert_event_rows(events[1], updated)
    _assert_request(server, 1, initial)
    _assert_request(server, 2, changed)
    _assert_response(server, 1, initial)
    _assert_response(server, 2, changed)
    _assert_postgres_rows(_postgres_rows(counter_export_runtime), updated)


def test_lost_reply_retry_is_exactly_idempotent(
    counter_export_runtime: dict[str, str], tmp_path: Path
) -> None:
    rows = fixture_rows()
    initial = _initial_counters(rows)
    cert, key = _make_tls(tmp_path)
    with _running_server(
        counter_export_runtime, tmp_path, "lost_reply", cert, key
    ) as server:
        evidence = _run_go(
            counter_export_runtime, tmp_path, server, rows, "lost_reply_retry"
        )
    assert evidence["scenario"] == "lost_reply_retry"
    assert len(evidence["steps"]) == 2
    _assert_step(
        evidence["steps"][0],
        "lost reply",
        2,
        0,
        initial,
        "HTTPS request failed",
    )
    _assert_step(evidence["steps"][1], "retry", 2, 2, [])
    _assert_local_state(evidence, initial)

    events = server.events()
    assert len(events) == 2
    assert events[0]["authorization_ok"] is True
    assert events[0]["committed"] is True
    assert events[0]["status"] is None
    assert events[0]["response_file"] is None
    assert events[1]["authorization_ok"] is True
    assert events[1]["committed"] is True
    assert events[1]["status"] == 200
    _assert_event_rows(events[0], initial)
    _assert_event_rows(events[1], initial)
    assert events[0]["rows"] == events[1]["rows"]
    _assert_request(server, 1, initial)
    _assert_request(server, 2, initial)
    _assert_response(server, 2, initial)
    _assert_postgres_rows(_postgres_rows(counter_export_runtime), initial)


def test_stale_selection_cannot_clear_a_newer_local_version(
    counter_export_runtime: dict[str, str], tmp_path: Path
) -> None:
    rows = fixture_rows()
    initial = _initial_counters(rows)
    changed = _changed_counters(rows)
    final = _updated_counters(rows)
    cert, key = _make_tls(tmp_path)
    with _running_server(
        counter_export_runtime, tmp_path, "normal", cert, key
    ) as server:
        evidence = _run_go(
            counter_export_runtime, tmp_path, server, rows, "stale_selection"
        )
    assert evidence["scenario"] == "stale_selection"
    assert len(evidence["steps"]) == 2
    _assert_step(evidence["steps"][0], "older receipt", 2, 1, changed)
    _assert_step(evidence["steps"][1], "newer export", 1, 1, [])
    _assert_local_state(evidence, final)

    events = server.events()
    assert len(events) == 2
    _assert_event_rows(events[0], initial)
    _assert_event_rows(events[1], final)
    _assert_request(server, 1, initial)
    _assert_request(server, 2, changed)
    _assert_response(server, 1, initial)
    _assert_response(server, 2, changed)
    _assert_postgres_rows(_postgres_rows(counter_export_runtime), final)


@pytest.mark.parametrize(
    "failure_mode", ["wrong_coverage", "truncated", "error_status"]
)
def test_rejected_response_cannot_acknowledge_a_partial_page(
    counter_export_runtime: dict[str, str], tmp_path: Path, failure_mode: str
) -> None:
    rows = fixture_rows()
    initial = _initial_counters(rows)
    cert, key = _make_tls(tmp_path)
    with _running_server(
        counter_export_runtime, tmp_path, failure_mode, cert, key
    ) as server:
        evidence = _run_go(
            counter_export_runtime,
            tmp_path,
            server,
            rows,
            "rejected_response",
            failure_mode,
        )
    assert evidence["scenario"] == "rejected_response"
    assert len(evidence["steps"]) == 2
    expected_error = (
        "status 500"
        if failure_mode == "error_status"
        else (
            "receipt_key_mismatch"
            if failure_mode == "wrong_coverage"
            else "invalid_request"
        )
    )
    _assert_step(
        evidence["steps"][0],
        "rejected response",
        2,
        0,
        initial,
        expected_error,
    )
    _assert_step(evidence["steps"][1], "retry", 2, 2, [])
    _assert_local_state(evidence, initial)

    events = server.events()
    assert len(events) == 2
    _assert_request(server, 1, initial)
    _assert_request(server, 2, initial)
    _assert_response(server, 2, initial)
    if failure_mode == "error_status":
        assert events[0]["committed"] is False
        assert events[0]["status"] == 500
        assert events[0]["rows"] == []
        assert events[0]["response_file"] is None
        assert events[1]["committed"] is True
        _assert_event_rows(events[1], initial)
    else:
        assert events[0]["committed"] is True
        assert events[0]["status"] == 200
        assert events[1]["committed"] is True
        assert events[1]["status"] == 200
        _assert_event_rows(events[0], initial)
        _assert_event_rows(events[1], initial)
        assert events[0]["rows"] == events[1]["rows"]
        if failure_mode == "wrong_coverage":
            assert events[0]["receipt_count"] == 1
            assert _receipt_error(server, 1, initial) == "receipt_key_mismatch"
        else:
            assert events[0]["receipt_count"] is None
            assert _receipt_error(server, 1, initial) == "invalid_request"
    _assert_postgres_rows(_postgres_rows(counter_export_runtime), initial)


def test_fixture_bearer_is_checked_before_ingest(
    counter_export_runtime: dict[str, str], tmp_path: Path
) -> None:
    rows = fixture_rows()
    initial = _initial_counters(rows)
    cert, key = _make_tls(tmp_path)
    with _running_server(
        counter_export_runtime, tmp_path, "normal", cert, key
    ) as server:
        evidence = _run_go(
            counter_export_runtime, tmp_path, server, rows, "auth_rejected"
        )
    assert evidence["scenario"] == "auth_rejected"
    assert len(evidence["steps"]) == 2
    _assert_step(
        evidence["steps"][0],
        "fixture auth rejected",
        2,
        0,
        initial,
        "status 401",
    )
    _assert_step(evidence["steps"][1], "authorized retry", 2, 2, [])
    _assert_local_state(evidence, initial)

    events = server.events()
    assert len(events) == 2
    assert events[0]["authorization_ok"] is False
    assert events[0]["committed"] is False
    assert events[0]["status"] == 401
    assert events[0]["rows"] == []
    assert events[0]["response_file"] is None
    assert events[1]["authorization_ok"] is True
    assert events[1]["committed"] is True
    assert events[1]["status"] == 200
    _assert_event_rows(events[1], initial)
    _assert_request(server, 1, initial)
    _assert_request(server, 2, initial)
    _assert_response(server, 2, initial)
    _assert_postgres_rows(_postgres_rows(counter_export_runtime), initial)


class _FixtureStartupStdout:
    def __init__(
        self, *, line: str = "", read_error: Exception | None = None
    ) -> None:
        self.line = line
        self.read_error = read_error
        self.closed = False

    def readline(self) -> str:
        if self.read_error is not None:
            raise self.read_error
        return self.line

    def close(self) -> None:
        self.closed = True


class _FixtureStartupProcess:
    def __init__(self, stdout: _FixtureStartupStdout) -> None:
        self.stdout = stdout
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.parametrize(
    ("readable", "line", "read_error", "expected"),
    [
        (True, '{"status":"ready"}\n', None, KeyError),
        (True, "{malformed\n", None, json.JSONDecodeError),
        (True, "", RuntimeError("fixture readiness read failed"), RuntimeError),
        (False, "", None, pytest.fail.Exception),
    ],
)
def test_start_server_cleans_up_on_failed_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    readable: bool,
    line: str,
    read_error: Exception | None,
    expected: type[BaseException],
) -> None:
    stdout = _FixtureStartupStdout(line=line, read_error=read_error)
    process = _FixtureStartupProcess(stdout)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        select,
        "select",
        lambda *args, **kwargs: ([stdout], [], []) if readable else ([], [], []),
    )
    with pytest.raises(expected):
        _start_server(
            {},
            tmp_path,
            "normal",
            tmp_path / "fixture-server.crt",
            tmp_path / "fixture-server.key",
        )
    assert process.killed is True
    assert process.waited is True
    assert stdout.closed is True


def test_missing_mandatory_tooling_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("SBM_REQUIRE_NODE_G0", "1")
    with pytest.raises(pytest.fail.Exception):
        _require_go()
    with pytest.raises(pytest.fail.Exception):
        _require_openssl()
