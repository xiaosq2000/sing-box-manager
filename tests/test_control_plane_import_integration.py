"""Real PostgreSQL tests with synthetic snapshots and isolated subprocesses."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.control_plane.import_schema import (
    context_identity,
    secret_identity,
)
from sing_box_manager.settings import load_settings
from tests.test_control_plane_import import fixture_payload
from tests.test_control_plane_integration import (
    ROOT,
    cli,
    config,
    postgres,
    psql,
    python,
)


@pytest.fixture
def case(postgres, tmp_path):
    database = "sbm_import_" + uuid4().hex[:12]
    result = psql(postgres, "postgres", provision_sql(database))
    assert result.returncode == 0, result.stderr
    path, _, env = config(postgres, tmp_path, database)
    runtime = yaml.safe_load(path.read_text())
    example = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    example.update(runtime)
    example["web"]["subscription_secret"] = "synthetic-subscription-key"
    # Decoy config rosters must never be imported or used as comparison data.
    for name in ("web_portal", "trojan", "hysteria2", "naive"):
        example[name]["users"] = []
    path.write_text(yaml.safe_dump(example))
    assert cli(path, env, "migrate").returncode == 0
    setup = python(
        env,
        "import django; django.setup(); from sing_box_manager.control_plane.models import ImportTarget; assert not ImportTarget.objects.get(pk=1).fixture_enabled; ImportTarget.objects.filter(pk=1).update(fixture_enabled=True); print('ok')",
    )
    assert setup.strip() == "ok"
    payload = fixture_payload()
    portal = payload["access"]["portal"][0]
    payload["access"]["portal"] += [
        {**portal, "username": "Alice", "admin": False},
        {**portal, "username": "bob", "admin": False},
        {**portal, "username": "delta", "enabled": False},
    ]
    payload["access"]["trojan"] += [
        {"username": "Alice", "password": "synthetic-uppercase-value", "enabled": True},
        {
            "username": "carol",
            "password": "synthetic-protocol-only-value",
            "enabled": True,
        },
        {"username": "delta", "password": "synthetic-disabled-value", "enabled": False},
    ]
    payload["access"]["hysteria2"] = [
        {"username": "alice", "password": "synthetic-hysteria-value", "enabled": True}
    ]
    payload["access"]["naive"] = [
        {"username": "alice", "password": "synthetic-naive-value", "enabled": True}
    ]
    payload["counters"].append({"username": "orphan", "counter": 5})
    settings = load_settings(path)
    payload.update(
        context_identity=context_identity(settings),
        secret_identity=secret_identity(settings),
    )
    return {
        "pg": postgres,
        "path": path,
        "env": env,
        "root": tmp_path,
        "payload": payload,
        "database": database,
    }


def bundle(case, payload=None, name="snapshot.json"):
    path = case["root"] / name
    path.write_text(json.dumps(payload or case["payload"]))
    path.chmod(0o600)
    return path


def invoke(case, action, snapshot, report):
    flag = "--report" if action == "preview" else "--approved-report"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sing_box_manager",
            "--config",
            str(case["path"]),
            "control-plane-import",
            "--action",
            action,
            "--snapshot",
            str(snapshot),
            flag,
            str(report),
        ],
        cwd=ROOT,
        env=case["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def copied(case):
    return json.loads(
        python(
            case["env"],
            "import django,json; django.setup(); from sing_box_manager.control_plane.import_service import database_state; from sing_box_manager.control_plane.models import ImportSource,ImportRun; source=ImportSource.objects.first(); print(json.dumps({'state':database_state(source),'runs':ImportRun.objects.count()}))",
        )
    )


def import_once(case, payload=None, tag="first"):
    source = bundle(case, payload, tag + ".json")
    report = case["root"] / (tag + "-report.json")
    preview = invoke(case, "preview", source, report)
    assert preview.returncode == 0, preview.stderr
    applied = invoke(case, "apply", source, report)
    assert applied.returncode == 0, applied.stderr
    return source, report


def test_full_copy_preview_no_writes_retry_ids_and_operator_separation(case):
    snapshot = bundle(case)
    report = case["root"] / "report.json"
    assert copied(case)["state"]["head"] is None
    preview = invoke(case, "preview", snapshot, report)
    assert preview.returncode == 0, preview.stderr
    assert copied(case)["state"]["head"] is None
    assert report.stat().st_mode & 0o777 == 0o600
    for secret in (
        "synthetic-access-value",
        case["payload"]["access"]["portal"][0]["password_hash"],
    ):
        assert secret not in report.read_text() + preview.stdout + preview.stderr
    result = invoke(case, "apply", snapshot, report)
    assert result.returncode == 0, result.stderr
    before = copied(case)
    assert {row["username"] for row in before["state"]["subjects"]} == {
        "alice",
        "Alice",
        "bob",
        "carol",
        "delta",
    }
    assert (
        next(row for row in before["state"]["legacy"] if row["username"] == "orphan")[
            "subject"
        ]
        is None
    )
    assert before["runs"] == 1
    for _ in range(2):
        assert invoke(case, "apply", snapshot, report).returncode == 0
        assert copied(case) == before
    second_report = case["root"] / "repeat-report.json"
    assert invoke(case, "preview", snapshot, second_report).returncode == 0
    assert invoke(case, "apply", snapshot, second_report).returncode == 0
    acknowledged = copied(case)
    assert acknowledged["state"] == before["state"]
    assert acknowledged["runs"] == before["runs"] + 1
    assert invoke(case, "apply", snapshot, second_report).returncode == 0
    assert copied(case) == acknowledged
    assert (
        python(
            case["env"],
            "import django; django.setup(); from django.contrib.auth import get_user_model; from django.contrib import admin; from sing_box_manager.control_plane.models import ProxySubject; assert get_user_model().objects.count()==0; assert ProxySubject not in admin.site._registry; print('ok')",
        ).strip()
        == "ok"
    )


@pytest.mark.parametrize("confirmed_noop", [True, False])
def test_noop_receipt_survives_supersession_without_replay(case, confirmed_noop):
    snapshot, _ = import_once(case)
    repeat_report = case["root"] / "repeat-report.json"
    before = copied(case)
    assert invoke(case, "preview", snapshot, repeat_report).returncode == 0
    assert copied(case) == before
    if confirmed_noop:
        assert invoke(case, "apply", snapshot, repeat_report).returncode == 0
        acknowledged = copied(case)
        assert acknowledged["state"] == before["state"]
        assert acknowledged["runs"] == before["runs"] + 1
        assert invoke(case, "apply", snapshot, repeat_report).returncode == 0
        assert copied(case) == acknowledged
    newer = deepcopy(case["payload"])
    newer["sequence"] = 2
    newer["access"]["portal"][0]["enabled"] = False
    import_once(case, newer, "newer")
    after_newer = copied(case)
    expected = 0 if confirmed_noop else 2
    assert invoke(case, "apply", snapshot, repeat_report).returncode == expected
    assert copied(case) == after_newer


@pytest.mark.parametrize(
    "kind,invalid,enabled",
    [
        ("trojan", "\x00", True),
        ("hysteria2", "\x00", False),
        ("naive", "\x00", True),
        ("trojan", "\ud800", True),
        ("naive", "\udfff", False),
    ],
)
def test_unrepresentable_credentials_never_get_ready_preview(
    case, kind, invalid, enabled
):
    payload = deepcopy(case["payload"])
    payload["access"][kind][0].update(
        password="synthetic-" + invalid + "-value", enabled=enabled
    )
    snapshot = bundle(case, payload)
    report = case["root"] / "invalid-report.json"
    before = copied(case)
    for action in ("preview", "apply"):
        result = invoke(case, action, snapshot, report)
        assert result.returncode == 2
        assert "snapshot_rejected" in result.stderr
        assert "synthetic-" + invalid + "-value" not in result.stdout + result.stderr
        assert not report.exists()
        assert copied(case) == before


def test_noop_receipt_failure_rollback_and_concurrent_retries(case):
    snapshot, _ = import_once(case)
    report = case["root"] / "noop-report.json"
    assert invoke(case, "preview", snapshot, report).returncode == 0
    before = copied(case)
    script = """
import django
from pathlib import Path
from unittest.mock import patch
django.setup()
from sing_box_manager.control_plane.import_service import apply
from sing_box_manager.control_plane.models import ImportRun
from sing_box_manager.settings import load_settings
with patch.object(ImportRun.objects,'create',side_effect=RuntimeError('synthetic-receipt-failure')):
    try: apply(Path(SNAPSHOT_PATH),Path(REPORT_PATH),load_settings(CONFIG_PATH))
    except RuntimeError: pass
    else: raise AssertionError('no-op receipt failure was accepted')
print('ok')
"""
    script = (
        script.replace("SNAPSHOT_PATH", repr(str(snapshot)))
        .replace("REPORT_PATH", repr(str(report)))
        .replace("CONFIG_PATH", repr(str(case["path"])))
    )
    assert python(case["env"], script).strip() == "ok"
    assert copied(case) == before
    args = [
        sys.executable,
        "-m",
        "sing_box_manager",
        "--config",
        str(case["path"]),
        "control-plane-import",
        "--action",
        "apply",
        "--snapshot",
        str(snapshot),
        "--approved-report",
        str(report),
    ]
    workers = [
        subprocess.Popen(
            args,
            cwd=ROOT,
            env=case["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for worker in workers:
        _, error = worker.communicate(timeout=60)
        assert worker.returncode == 0, error
    acknowledged = copied(case)
    assert acknowledged["state"] == before["state"]
    assert acknowledged["runs"] == before["runs"] + 1
    assert invoke(case, "apply", snapshot, report).returncode == 0
    assert copied(case) == acknowledged


def test_unicode_credentials_round_trip_through_postgres(case):
    payload = deepcopy(case["payload"])
    for kind in ("trojan", "hysteria2", "naive"):
        payload["access"][kind][0]["password"] = "synthetic-café-😀-value"
    import_once(case, payload)
    state = copied(case)["state"]
    for kind in ("trojan", "hysteria2", "naive"):
        row = next(
            row
            for row in state["access"]
            if row["username"] == "alice" and row["kind"] == kind
        )
        assert row["payload"]["password"] == "synthetic-café-😀-value"


def test_old_flags_equal_counters_and_invalidated_preview(case):
    old = bundle(case)
    old_report = case["root"] / "old-report.json"
    assert invoke(case, "preview", old, old_report).returncode == 0
    newer = deepcopy(case["payload"])
    newer["sequence"] = 2
    newer["access"]["portal"][0]["enabled"] = False
    import_once(case, newer, "new")
    before = copied(case)
    assert invoke(case, "apply", old, old_report).returncode == 2
    assert copied(case) == before
    stale_report = case["root"] / "stale-report.json"
    assert invoke(case, "preview", old, stale_report).returncode == 2
    assert json.loads(stale_report.read_text())["body"]["status"] == "rejected"
    assert invoke(case, "apply", old, stale_report).returncode == 2
    assert copied(case) == before


def test_exact_retry_after_newer_import_does_not_replay(case):
    old, report = import_once(case)
    new = deepcopy(case["payload"])
    new["sequence"] = 2
    new["access"]["trojan"][0]["enabled"] = False
    import_once(case, new, "new")
    before = copied(case)
    assert invoke(case, "apply", old, report).returncode == 0
    assert copied(case) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["counters"][0].update(counter=2),
        lambda p: p["counters"].pop(0),
        lambda p: p.update(namespace=str(uuid4())),
        lambda p: p.update(source_identity=str(uuid4())),
        lambda p: p["evidence"].update(identity_changes="rename"),
        lambda p: p.update(secret_identity="f" * 64),
        lambda p: p.update(purpose="production"),
        lambda p: (
            p.update(sequence=1),
            p["access"]["portal"][0].update(enabled=False),
        ),
        lambda p: p["access"]["trojan"].append(
            {
                "username": "orphan",
                "password": "synthetic-orphan-value",
                "enabled": True,
            }
        ),
    ],
)
def test_rejected_updates_leave_all_state_unchanged(case, change):
    import_once(case)
    before = copied(case)
    new = deepcopy(case["payload"])
    new["sequence"] = 2
    change(new)
    result = invoke(
        case,
        "preview",
        bundle(case, new, "changed.json"),
        case["root"] / "changed-report.json",
    )
    assert result.returncode == 2, result.stderr
    assert copied(case) == before


def test_removals_and_retired_name_reappearance(case):
    import_once(case)
    new = deepcopy(case["payload"])
    new["sequence"] = 2
    new["access"]["naive"] = []
    for kind in new["access"]:
        new["access"][kind] = [
            row for row in new["access"][kind] if row["username"] != "delta"
        ]
    import_once(case, new, "remove")
    state = copied(case)["state"]
    assert not next(row for row in state["subjects"] if row["username"] == "delta")[
        "present"
    ]
    removed = next(row for row in state["access"] if row["kind"] == "naive")
    assert (
        not removed["present"]
        and removed["payload"]["password"] == "synthetic-naive-value"
    )
    replay = deepcopy(case["payload"])
    replay["sequence"] = 3
    assert (
        invoke(
            case,
            "preview",
            bundle(case, replay, "reappear.json"),
            case["root"] / "reappear-report.json",
        ).returncode
        == 2
    )


def test_changed_report_bundle_context_and_unexpected_db_change(case):
    snapshot = bundle(case)
    report = case["root"] / "report.json"
    assert invoke(case, "preview", snapshot, report).returncode == 0
    original = report.read_bytes()
    report.write_bytes(original + b" ")
    assert invoke(case, "apply", snapshot, report).returncode == 2
    report.write_bytes(original)
    data = json.loads(original)
    data["body"]["expected_revision"] = 99
    report.write_text(json.dumps(data))
    assert invoke(case, "apply", snapshot, report).returncode == 2
    report.write_bytes(original)
    content = snapshot.read_bytes()
    snapshot.write_bytes(content + b" ")
    assert invoke(case, "apply", snapshot, report).returncode == 2
    snapshot.write_bytes(content)
    cfg = case["path"].read_text()
    raw = yaml.safe_load(cfg)
    raw["default_protocol"] = "naive"
    case["path"].write_text(yaml.safe_dump(raw))
    assert invoke(case, "apply", snapshot, report).returncode == 2
    case["path"].write_text(cfg)
    assert invoke(case, "apply", snapshot, report).returncode == 0
    next_payload = deepcopy(case["payload"])
    next_payload["sequence"] = 2
    next_snapshot = bundle(case, next_payload, "next.json")
    next_report = case["root"] / "next-report.json"
    assert invoke(case, "preview", next_snapshot, next_report).returncode == 0
    python(
        case["env"],
        "import django; django.setup(); from sing_box_manager.control_plane.models import ImportedAccess; row=ImportedAccess.objects.filter(kind='trojan').first(); row.payload={**row.payload,'enabled':False}; row.save(); print('ok')",
    )
    before = copied(case)
    assert invoke(case, "apply", next_snapshot, next_report).returncode == 2
    assert copied(case) == before


def test_transaction_rollback_and_database_backed_parity(case):
    snapshot = bundle(case)
    report = case["root"] / "report.json"
    assert invoke(case, "preview", snapshot, report).returncode == 0
    output = python(
        case["env"],
        f"""
import django
from pathlib import Path
from unittest.mock import patch
django.setup()
from sing_box_manager.control_plane import import_service as service
from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.control_plane.models import ImportSource,ImportRun,ProxySubject,ImportedAccess
from sing_box_manager.settings import load_settings
runtime=load_settings({str(case["path"])!r})
with patch.object(service, 'verify_parity', side_effect=ImportRejected('injected_failure')):
    try: service.apply(Path({str(snapshot)!r}),Path({str(report)!r}),runtime)
    except ImportRejected: pass
    else: raise AssertionError('injection did not fail')
assert ImportSource.objects.count()==ImportRun.objects.count()==ProxySubject.objects.count()==0
with patch.object(ImportRun.objects, 'create', side_effect=RuntimeError('synthetic-db-detail')):
    try: service.apply(Path({str(snapshot)!r}),Path({str(report)!r}),runtime)
    except RuntimeError: pass
    else: raise AssertionError('receipt failure did not roll back')
assert ImportSource.objects.count()==ImportRun.objects.count()==ProxySubject.objects.count()==ImportedAccess.objects.count()==0
print('ok')
""",
    )
    assert output.strip() == "ok"
    assert invoke(case, "apply", snapshot, report).returncode == 0
    output = python(
        case["env"],
        f"""
import django
django.setup()
from sing_box_manager.control_plane.import_service import database_state, load_snapshot
from sing_box_manager.control_plane.import_parity import verify_parity
from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.control_plane.models import ImportSource,ImportedAccess,LegacyAccess,ProxySubject
from sing_box_manager.settings import load_settings
from pathlib import Path
runtime=load_settings({str(case["path"])!r}); snapshot,_=load_snapshot(Path({str(snapshot)!r}),runtime)
row=ImportedAccess.objects.get(kind='trojan',subject__username='delta')
assert not row.payload['enabled']
row.payload={{**row.payload,'password':'synthetic-mutated-disabled-value'}}; row.save()
try: verify_parity(snapshot,runtime,database_state(ImportSource.objects.first()))
except ImportRejected: pass
else: raise AssertionError('disabled credential loss was not detected')
row.payload=snapshot.access.memberships()['delta']['trojan']; row.save()
verify_parity(snapshot,runtime,database_state(ImportSource.objects.first()))
bob=ProxySubject.objects.get(username='bob')
mutations=[
    (ImportedAccess.objects.get(kind='trojan',subject__username='alice'),'subject_id',bob.pk),
    (ImportedAccess.objects.get(kind='trojan',subject__username='alice'),'present',False),
    (LegacyAccess.objects.get(token_username='alice'),'subject_id',bob.pk),
]
for record,attribute,value in mutations:
    original=getattr(record,attribute); setattr(record,attribute,value); record.save()
    try: verify_parity(snapshot,runtime,database_state(ImportSource.objects.first()))
    except ImportRejected: pass
    else: raise AssertionError('membership binding loss was not detected')
    setattr(record,attribute,original); record.save()
    verify_parity(snapshot,runtime,database_state(ImportSource.objects.first()))
print('ok')
""",
    )
    assert output.strip() == "ok"


@pytest.mark.parametrize(
    "fault", ["absent_payload", "absent_row", "existing_id", "absent_subject"]
)
def test_post_copy_history_and_identity_corruption_rolls_back(case, fault):
    import_once(case)
    payload = deepcopy(case["payload"])
    payload["sequence"] = 2
    for kind in payload["access"]:
        payload["access"][kind] = [
            row for row in payload["access"][kind] if row["username"] != "delta"
        ]
    snapshot = bundle(case, payload, "remove.json")
    report = case["root"] / "remove-report.json"
    assert invoke(case, "preview", snapshot, report).returncode == 0
    before = copied(case)
    script = """
import django
from pathlib import Path
from unittest.mock import patch
django.setup()
from sing_box_manager.control_plane import import_service as service
from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.control_plane.models import ProxySubject,ImportedAccess,LegacyAccess
from sing_box_manager.settings import load_settings
original_copy=service._copy
fault=FAULT

def corrupt(snapshot,source):
    original_copy(snapshot,source)
    if fault in ('absent_payload','absent_row'):
        row=ImportedAccess.objects.get(subject__source=source,subject__username='delta',kind='trojan')
        assert not row.present
        if fault=='absent_payload':
            row.payload={**row.payload,'password':'synthetic-corrupted-history'}; row.save()
        else: row.delete()
    elif fault=='existing_id':
        old=ProxySubject.objects.get(source=source,username='alice')
        ProxySubject.objects.filter(pk=old.pk).update(username='synthetic_temporary')
        replacement=ProxySubject.objects.create(source=source,username='alice')
        ImportedAccess.objects.filter(subject=old).update(subject=replacement)
        LegacyAccess.objects.filter(subject=old).update(subject=replacement)
        ProxySubject.objects.filter(pk=old.pk).delete()
    elif fault=='absent_subject':
        old=ProxySubject.objects.get(source=source,username='delta')
        assert not old.present
        ImportedAccess.objects.filter(subject=old).delete()
        LegacyAccess.objects.filter(subject=old).delete()
        old.delete()
    else: raise AssertionError('unknown fixture fault')

with patch.object(service,'_copy',side_effect=corrupt):
    try: service.apply(Path(SNAPSHOT_PATH),Path(REPORT_PATH),load_settings(CONFIG_PATH))
    except ImportRejected as error: assert str(error)=='projection_parity_failed'
    else: raise AssertionError('post-copy corruption was accepted')
print('ok')
"""
    script = (
        script.replace("FAULT", repr(fault))
        .replace("SNAPSHOT_PATH", repr(str(snapshot)))
        .replace("REPORT_PATH", repr(str(report)))
        .replace("CONFIG_PATH", repr(str(case["path"])))
    )
    assert python(case["env"], script).strip() == "ok"
    assert copied(case) == before
    assert invoke(case, "apply", snapshot, report).returncode == 0
    after = copied(case)
    assert {row["username"]: row["id"] for row in after["state"]["subjects"]} == {
        row["username"]: row["id"] for row in before["state"]["subjects"]
    }


def test_import_models_match_checked_in_migrations(case):
    output = python(
        case["env"],
        "import django; django.setup(); from django.core.management import call_command; call_command('makemigrations', 'control_plane', check=True, dry_run=True)",
    )
    assert "No changes detected" in output


def test_fixture_gate_and_cross_database_report_binding(case):
    snapshot, report = import_once(case)
    other_db = "sbm_other_import_" + uuid4().hex[:8]
    from sing_box_manager.control_plane.database import provision_sql

    assert psql(case["pg"], "postgres", provision_sql(other_db)).returncode == 0
    original = case["path"].read_text()
    cfg = yaml.safe_load(original)
    cfg["control_plane"]["database_name"] = other_db
    case["path"].write_text(yaml.safe_dump(cfg))
    assert cli(case["path"], case["env"], "migrate").returncode == 0
    result = invoke(case, "preview", snapshot, case["root"] / "blocked.json")
    assert result.returncode == 2 and "fixture_target_required" in result.stderr
    assert not (case["root"] / "blocked.json").exists()
    python(
        case["env"],
        "import django; django.setup(); from sing_box_manager.control_plane.models import ImportTarget; ImportTarget.objects.filter(pk=1).update(fixture_enabled=True); print('ok')",
    )
    assert invoke(case, "apply", snapshot, report).returncode == 2
    assert copied(case)["state"]["head"] is None
    case["path"].write_text(original)


def test_database_loaded_http_contract_and_signed_downloads(case):
    import_once(case)
    script = """
import base64,hashlib
import django
django.setup()
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sing_box_manager.control_plane.import_service import database_state
from sing_box_manager.control_plane.import_parity import records_from_state,inventory_from_records
from sing_box_manager.control_plane.models import ImportSource
from sing_box_manager.release.client_downloads import ClientDownloads,client_manifest,generate_key,sign,public_key
from sing_box_manager.settings import load_settings
from sing_box_manager.subscription import SubscriptionService,subscription_token,ResetCounters
from sing_box_manager.web.auth import SessionManager,MachineTokenManager,RateLimiter
from sing_box_manager.web.subscriptions import router
runtime=load_settings(CONFIG_PATH)
state=database_state(ImportSource.objects.first())
records=records_from_state(state)
class FixtureCounters(ResetCounters):
    def __init__(self): self.values={row['username']:row['counter'] for row in state['legacy']}
    def current(self): return self.values.copy()
    def advance(self,username):
        self.values[username]=self.values.get(username,0)+1
        return self.values[username]
counters=FixtureCounters()
service=SubscriptionService(runtime.web.subscription_secret,inventory_from_records(runtime,records),runtime.default_protocol,runtime.sing_box_version,counters)
root=Path(FILE_DIR)/'downloads'; root.mkdir()
binary=b'synthetic-fixture-binary'; (root/'sbc').write_bytes(binary)
manifest=client_manifest([('sbc/linux-amd64/sbc',len(binary),hashlib.sha256(binary).hexdigest())],sbc='fixture-sbc',sing_box=runtime.sing_box_version)
key=generate_key(); signature=sign(key,manifest)
(root/'manifest.json').write_bytes(manifest); (root/'manifest.json.sig').write_text(signature)
app=FastAPI(); app.include_router(router)
app.state.subscriptions=service
app.state.users={user.username:user for user in records.portal if user.enabled}
app.state.client_downloads=ClientDownloads(root,{'manifest.json':'manifest.json','manifest.json.sig':'manifest.json.sig','sbc/linux-amd64/sbc':'sbc'},'fixture-sbc',hashlib.sha256(manifest).hexdigest())
app.state.traffic_stats_provider=None
app.state.session_manager=SessionManager('synthetic-http-session')
app.state.machine_token_manager=MachineTokenManager('synthetic-http-machine')
app.state.rate_limiter=RateLimiter()
query={'format':'sbc','os':'linux','sing-box':'1.14.2'}
with TestClient(app) as client:
    urls={name:'/sub/'+service.token_for(name) for name in ('alice','Alice','bob','carol','delta')}
    response=client.get(urls['alice'],params=query); assert response.status_code==200
    assert response.json()['latest']['sbc']=='fixture-sbc'
    assert client.get(urls['alice'],params=query,headers={'If-None-Match':response.headers['etag']}).status_code==304
    assert client.get(urls['alice'],params={**query,'sing-box':'1.13.0'}).status_code==409
    assert client.get(urls['alice'],params={**query,'os':'unsupported'}).status_code==400
    for name in ('bob','carol','delta'): assert client.get(urls[name],params=query).status_code==404
    assert client.get(urls['Alice'],params=query).status_code==200
    served=client.get(urls['bob']+'/files/manifest.json'); assert served.status_code==200
    served_sig=client.get(urls['bob']+'/files/manifest.json.sig'); assert served_sig.status_code==200
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key(key))).verify(base64.b64decode(served_sig.text),served.content)
    assert client.get(urls['bob']+'/files/sbc/linux-amd64/sbc').content==binary
    for name in ('carol','delta'): assert client.get(urls[name]+'/files/manifest.json').status_code==404
    for path in ('unlisted','%2e%2e/outside','sbc/linux-amd64/%2e%2e/sbc'):
        assert client.get(urls['alice']+'/files/'+path).status_code==404
    old='/sub/'+subscription_token(runtime.web.subscription_secret,'alice',2)
    assert client.get(old,params=query).status_code==404
    assert client.get(old+'/files/manifest.json').status_code==404
    assert client.post('/api/sub',auth=('alice','synthetic-login-value')).status_code==200
    assert client.post('/api/sub',auth=('bob','synthetic-login-value')).status_code==404
    assert client.post('/api/sub',auth=('delta','synthetic-login-value')).status_code==401
    assert client.post('/api/sub',headers={'Authorization':'Bearer '+service.token_for('alice')}).status_code==401
    client.cookies.set('session',app.state.session_manager.create_token('bob'))
    assert client.post('/subscription/reset',follow_redirects=False).status_code==404
    client.cookies.set('session',app.state.session_manager.create_token('alice'))
    assert client.post('/subscription/reset',follow_redirects=False).status_code==303
    assert client.get(urls['alice'],params=query).status_code==404
    assert counters.current()['alice']==4
print('ok')
"""
    script = script.replace("CONFIG_PATH", repr(str(case["path"]))).replace(
        "FILE_DIR", repr(str(case["root"]))
    )
    assert python(case["env"], script).strip() == "ok"


def test_concurrent_identical_and_different_snapshots(case):
    first = bundle(case)
    first_report = case["root"] / "first-report.json"
    assert invoke(case, "preview", first, first_report).returncode == 0
    args = [
        sys.executable,
        "-m",
        "sing_box_manager",
        "--config",
        str(case["path"]),
        "control-plane-import",
        "--action",
        "apply",
        "--snapshot",
        str(first),
        "--approved-report",
        str(first_report),
    ]
    workers = [
        subprocess.Popen(
            args,
            cwd=ROOT,
            env=case["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for worker in workers:
        _, err = worker.communicate(timeout=60)
        assert worker.returncode == 0, err
    assert copied(case)["runs"] == 1
    variants = []
    for sequence in (2, 3):
        payload = deepcopy(case["payload"])
        payload["sequence"] = sequence
        payload["access"]["portal"][0]["enabled"] = sequence == 2
        snap = bundle(case, payload, f"capture-{sequence}.json")
        report = case["root"] / f"report-{sequence}.json"
        assert invoke(case, "preview", snap, report).returncode == 0
        variants.append(
            [
                *args[: args.index("--snapshot")],
                "--snapshot",
                str(snap),
                "--approved-report",
                str(report),
            ]
        )
    workers = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=case["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for command in variants
    ]
    codes = []
    for worker in workers:
        worker.communicate(timeout=60)
        codes.append(worker.returncode)
    assert sorted(codes) == [0, 2]
    assert copied(case)["runs"] == 2
