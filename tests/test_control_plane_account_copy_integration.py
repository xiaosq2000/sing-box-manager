"""Account-only production-path checks using disposable PostgreSQL and fake sources."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
import yaml

from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.control_plane.import_schema import canonical
from tests.test_control_plane_account_copy import NS, SOURCE, source_files
from tests.test_control_plane_integration import ROOT, config, postgres, psql, python


@pytest.fixture
def case(postgres, tmp_path):
    name = "sbm_account_" + uuid4().hex[:12]
    result = psql(postgres, "postgres", provision_sql(name))
    assert result.returncode == 0, result.stderr
    path, _, env = config(postgres, tmp_path, name)
    controls = yaml.safe_load(path.read_text())["control_plane"]
    runtime, auth, proxy = source_files(tmp_path, {"control_plane": controls})
    target = yaml.safe_load(runtime.config_path.read_text())
    for section in ("web_portal", "trojan", "hysteria2", "naive"):
        target[section]["users"] = []
    path.write_text(yaml.safe_dump(target))
    case = {
        "path": path,
        "env": env,
        "root": tmp_path,
        "source": runtime.config_path,
        "auth": auth,
        "proxy": proxy,
        "pg": postgres,
        "database": name,
    }
    # Deliberately do not apply the deferred capture migration.
    python(
        env,
        "import django; django.setup(); from django.core.management import call_command; call_command('migrate','account_copy',verbosity=0)",
    )
    snap = tmp_path / "snapshot.json"
    exported = invoke(
        case,
        "export",
        config_path=runtime.config_path,
        snapshot=snap,
        auth_file=auth,
        proxy_config=proxy,
        namespace=NS,
        source_identity=SOURCE,
        sequence=1,
    )
    assert exported.returncode == 0, exported.stderr
    case["snapshot"] = snap
    case["payload"] = json.loads(snap.read_text())
    return case


def invoke(case, action, config_path=None, **options):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sing_box_manager",
            "--config",
            str(config_path or case["path"]),
            "account-copy",
            "--action",
            action,
            *[
                piece
                for key, value in options.items()
                for piece in ("--" + key.replace("_", "-"), str(value))
            ],
        ],
        cwd=ROOT,
        env=case["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def authorize(case):
    result = invoke(case, "authorize", namespace=NS, source_identity=SOURCE)
    assert result.returncode == 0, result.stderr


def bundle(case, payload, name):
    path = case["root"] / name
    path.write_bytes(canonical(payload) + b"\n")
    path.chmod(0o600)
    return path


def apply_once(case, snapshot=None, tag="copy"):
    snapshot = snapshot or case["snapshot"]
    report = case["root"] / (tag + "-report.json")
    preview = invoke(case, "preview", snapshot=snapshot, report=report)
    assert preview.returncode == 0, preview.stderr
    applied = invoke(case, "apply", snapshot=snapshot, approved_report=report)
    assert applied.returncode == 0, applied.stderr
    return report


def state(case):
    return json.loads(
        python(
            case["env"],
            """
import django,json; django.setup()
from sing_box_manager.control_plane.import_service import database_state
from sing_box_manager.control_plane.models import ImportSource,ImportRun,LegacyAccess,User
from sing_box_manager.account_copy.models import AccountCopyAdmission
print(json.dumps({'state':database_state(ImportSource.objects.first()),'runs':ImportRun.objects.count(),
 'legacy_count':LegacyAccess.objects.count(),'operators':User.objects.count(),
 'admissions':list(AccountCopyAdmission.objects.values('enabled','closed'))}))
""",
        )
    )


def test_export_authorization_preview_apply_and_retry_are_separate(case):
    before = state(case)
    assert before == {
        "state": {"head": None, "subjects": [], "access": [], "legacy": []},
        "runs": 0,
        "legacy_count": 0,
        "operators": 0,
        "admissions": [],
    }
    report = case["root"] / "report.json"
    rejected = invoke(case, "preview", snapshot=case["snapshot"], report=report)
    assert rejected.returncode == 2 and not report.exists()
    authorize(case)
    authorized = state(case)
    assert authorized["admissions"] == [{"enabled": True, "closed": False}]
    authorize(case)
    assert state(case) == authorized
    result = invoke(case, "preview", snapshot=case["snapshot"], report=report)
    assert result.returncode == 0, result.stderr
    assert state(case) == authorized
    assert report.stat().st_mode & 0o777 == 0o600
    assert (
        "synthetic-access-value"
        not in report.read_text() + result.stdout + result.stderr
    )
    assert "synthetic-old-link-key-do-not-export" not in case["snapshot"].read_text()
    assert (
        invoke(
            case, "apply", snapshot=case["snapshot"], approved_report=report
        ).returncode
        == 0
    )
    copied = state(case)
    assert len(copied["state"]["subjects"]) == 5
    assert (
        copied["runs"] == 1 and copied["legacy_count"] == 0 and copied["operators"] == 0
    )
    assert copied["state"]["legacy"] == []
    assert {row["username"] for row in copied["state"]["subjects"]} == {
        "alice",
        "Alice",
        "disabled",
        "proxy_only",
        "portal_only",
    }
    for _ in range(2):
        assert (
            invoke(
                case, "apply", snapshot=case["snapshot"], approved_report=report
            ).returncode
            == 0
        )
        assert state(case) == copied
    assert not (case["root"] / "must-not-be-created.sqlite3").exists()


def test_independent_migration_defaults_no_admin_and_no_prototype_dependency(case):
    output = python(
        case["env"],
        """
import django; django.setup()
from django.db import connection,transaction,IntegrityError
from django.core.management import call_command
from django.contrib import admin
from django.db.migrations.executor import MigrationExecutor
from sing_box_manager.account_copy.models import AccountCopyAdmission
from sing_box_manager.control_plane.models import ImportTarget
assert not ImportTarget.objects.get(pk=1).fixture_enabled
assert not AccountCopyAdmission._meta.get_field('enabled').default
assert not AccountCopyAdmission._meta.get_field('closed').default
assert AccountCopyAdmission not in admin.site._registry
assert not AccountCopyAdmission.objects.exists()
executor=MigrationExecutor(connection)
assert ('control_plane','0003_capture_records') not in executor.loader.applied_migrations
assert ('account_copy','0001_initial') in executor.loader.applied_migrations
assert not executor.migration_plan(executor.loader.graph.leaf_nodes('account_copy'))
call_command('makemigrations',check=True,dry_run=True)
try:
 with transaction.atomic():
  AccountCopyAdmission.objects.create(target_id=1,namespace='00000000-0000-0000-0000-000000000011',source_identity='00000000-0000-0000-0000-000000000012',enabled=True,closed=True)
except IntegrityError: pass
else: raise AssertionError('constraint_missing')
print('ok')
""",
    )
    assert "No changes detected" in output and output.rstrip().endswith("ok")


def test_fixture_and_production_gates_are_mutually_exclusive(case):
    python(
        case["env"],
        "import django; django.setup(); from sing_box_manager.control_plane.models import ImportTarget; ImportTarget.objects.filter(pk=1).update(fixture_enabled=True)",
    )
    assert (
        invoke(case, "authorize", namespace=NS, source_identity=SOURCE).returncode == 2
    )
    python(
        case["env"],
        "import django; django.setup(); from sing_box_manager.control_plane.models import ImportTarget; ImportTarget.objects.filter(pk=1).update(fixture_enabled=False)",
    )
    authorize(case)
    # Even an accidental later fixture flag cannot admit the fixture service.
    output = python(
        case["env"],
        """
import django; django.setup()
from sing_box_manager.control_plane.models import ImportTarget
from sing_box_manager.control_plane.import_service import _target
from sing_box_manager.control_plane.import_schema import ImportRejected
ImportTarget.objects.filter(pk=1).update(fixture_enabled=True)
try: _target()
except ImportRejected: print('rejected')
else: raise AssertionError('fixture_gate_open')
""",
    )
    assert output.strip() == "rejected"
    assert (
        invoke(
            case,
            "preview",
            snapshot=case["snapshot"],
            report=case["root"] / "blocked.json",
        ).returncode
        == 2
    )


def test_authorization_requires_empty_target_and_exact_source(case):
    python(
        case["env"],
        f"import django; django.setup(); from sing_box_manager.control_plane.models import ImportSource; ImportSource.objects.create(namespace='{NS}',source_identity='{SOURCE}')",
    )
    assert (
        invoke(case, "authorize", namespace=NS, source_identity=SOURCE).returncode == 2
    )
    assert state(case)["admissions"] == []


def test_close_is_permanent_and_never_removes_copied_accounts(case):
    authorize(case)
    report = apply_once(case)
    before = state(case)
    assert (
        invoke(case, "close", namespace=uuid4(), source_identity=SOURCE).returncode == 2
    )
    assert state(case) == before
    for _ in range(2):
        assert (
            invoke(case, "close", namespace=NS, source_identity=SOURCE).returncode == 0
        )
    after = state(case)
    assert after["state"] == before["state"] and after["runs"] == before["runs"]
    assert after["admissions"] == [{"enabled": False, "closed": True}]
    assert (
        invoke(case, "authorize", namespace=NS, source_identity=SOURCE).returncode == 2
    )
    assert (
        invoke(
            case,
            "preview",
            snapshot=case["snapshot"],
            report=case["root"] / "closed.json",
        ).returncode
        == 2
    )
    assert (
        invoke(
            case, "apply", snapshot=case["snapshot"], approved_report=report
        ).returncode
        == 2
    )
    assert state(case) == after


def test_updates_noop_supersession_and_retired_names(case):
    authorize(case)
    first_report = apply_once(case)
    initial = state(case)
    noop_report = apply_once(case, tag="noop")
    noop = state(case)
    assert noop["state"] == initial["state"] and noop["runs"] == 2
    newer = deepcopy(case["payload"])
    newer["sequence"] = 2
    # Keep disabled credentials but remove a whole subject and one membership.
    newer["access"]["portal"] = [
        r for r in newer["access"]["portal"] if r["username"] != "portal_only"
    ]
    newer["access"]["naive"] = [
        r for r in newer["access"]["naive"] if r["username"] != "disabled"
    ]
    newer["access"]["trojan"][0]["enabled"] = False
    second = bundle(case, newer, "second.json")
    apply_once(case, second, "second")
    current = state(case)
    ids_before = {r["username"]: r["id"] for r in initial["state"]["subjects"]}
    assert ids_before == {r["username"]: r["id"] for r in current["state"]["subjects"]}
    assert not next(
        r for r in current["state"]["subjects"] if r["username"] == "portal_only"
    )["present"]
    assert not next(
        r
        for r in current["state"]["access"]
        if r["username"] == "disabled" and r["kind"] == "naive"
    )["present"]
    for report in (first_report, noop_report):
        assert (
            invoke(
                case, "apply", snapshot=case["snapshot"], approved_report=report
            ).returncode
            == 0
        )
        assert state(case) == current
    assert (
        invoke(
            case,
            "preview",
            snapshot=case["snapshot"],
            report=case["root"] / "stale.json",
        ).returncode
        == 2
    )
    renamed = deepcopy(case["payload"])
    renamed["sequence"] = 3
    assert (
        invoke(
            case,
            "preview",
            snapshot=bundle(case, renamed, "retired.json"),
            report=case["root"] / "retired-report.json",
        ).returncode
        == 2
    )
    assert state(case) == current


@pytest.mark.parametrize(
    "mutation",
    ["bytes", "report", "purpose", "context", "state", "admission", "database"],
)
def test_report_bindings_reject_changes(case, mutation):
    authorize(case)
    report = case["root"] / "report.json"
    assert (
        invoke(case, "preview", snapshot=case["snapshot"], report=report).returncode
        == 0
    )
    snapshot = case["snapshot"]
    config_path = case["path"]
    if mutation == "bytes":
        snapshot.write_bytes(snapshot.read_bytes() + b" ")
    elif mutation in {"report", "purpose"}:
        body = json.loads(report.read_bytes())
        body["body"]["purpose" if mutation == "purpose" else "sequence"] = (
            "synthetic-fixture" if mutation == "purpose" else 5
        )
        report.write_bytes(canonical(body) + b"\n")
    elif mutation in {"context", "database"}:
        value = yaml.safe_load(config_path.read_text())
        if mutation == "context":
            value["default_protocol"] = "naive"
        else:
            # A second real fixture database with the same schema and admission.
            new_name = "sbm_account_" + uuid4().hex[:12]
            assert psql(case["pg"], "postgres", provision_sql(new_name)).returncode == 0
            value["control_plane"]["database_name"] = new_name
        config_path = case["root"] / "changed-config.yaml"
        config_path.write_text(yaml.safe_dump(value))
        config_path.chmod(0o600)
        if mutation == "database":
            env = {**case["env"], "SBM_CONFIG": str(config_path)}
            python(
                env,
                "import django; django.setup(); from django.core.management import call_command; call_command('migrate','account_copy',verbosity=0)",
            )
            assert (
                invoke(
                    case,
                    "authorize",
                    config_path=config_path,
                    namespace=NS,
                    source_identity=SOURCE,
                ).returncode
                == 0
            )
    elif mutation == "state":
        python(
            case["env"],
            f"import django; django.setup(); from sing_box_manager.control_plane.models import ImportSource; ImportSource.objects.create(namespace='{NS}',source_identity='{SOURCE}')",
        )
    else:
        python(
            case["env"],
            f"import django; django.setup(); from sing_box_manager.account_copy.models import AccountCopyAdmission; AccountCopyAdmission.objects.update(source_identity='{uuid4()}')",
        )
    before = state(case)
    assert (
        invoke(
            case,
            "apply",
            config_path=config_path,
            snapshot=snapshot,
            approved_report=report,
        ).returncode
        == 2
    )
    assert state(case) == before


@pytest.mark.parametrize(
    "fault", ["payload", "missing", "retained", "legacy", "receipt"]
)
def test_post_copy_faults_roll_back_all_writes(case, fault):
    authorize(case)
    apply_once(case)
    newer = deepcopy(case["payload"])
    newer["sequence"] = 2
    newer["access"]["naive"] = [
        r for r in newer["access"]["naive"] if r["username"] != "disabled"
    ]
    second = bundle(case, newer, "second.json")
    apply_once(case, second, "second")
    newer["sequence"] = 3
    third = bundle(case, newer, "third.json")
    report = case["root"] / "third-report.json"
    assert invoke(case, "preview", snapshot=third, report=report).returncode == 0
    before = state(case)
    output = python(
        case["env"],
        f"""
import django; django.setup()
from unittest.mock import patch
from sing_box_manager.account_copy import service
from sing_box_manager.control_plane.models import ImportedAccess,LegacyAccess
from sing_box_manager.control_plane.import_schema import ImportRejected
from sing_box_manager.settings import load_settings
from pathlib import Path
runtime=load_settings(Path({str(case["path"])!r}))
original=service.shared.copy_accounts
fault={fault!r}
def corrupt(records,source):
 original(records,source)
 if fault=='payload':
  row=ImportedAccess.objects.filter(kind='trojan').first(); row.payload['password']='synthetic-corruption'; row.save()
 elif fault=='missing': ImportedAccess.objects.filter(kind='trojan').first().delete()
 elif fault=='retained':
  row=ImportedAccess.objects.get(kind='naive',subject__username='disabled'); assert not row.present; row.payload['password']='synthetic-retained-corruption'; row.save()
 elif fault=='legacy': LegacyAccess.objects.create(source=source,token_username='unexpected',counter=0,secret_identity='0'*64)
def receipt(*args): raise ImportRejected('injected_receipt_failure')
with patch.object(service.shared,'copy_accounts',side_effect=corrupt), patch.object(service,'_receipt',side_effect=receipt if fault=='receipt' else service._receipt):
 try: service.apply(Path({str(third)!r}),Path({str(report)!r}),runtime)
 except ImportRejected: print('rejected')
 else: raise AssertionError('fault_not_rejected')
""",
    )
    assert output.strip() == "rejected"
    assert state(case) == before


def test_exact_concurrent_apply_records_one_copy(case):
    authorize(case)
    report = case["root"] / "report.json"
    assert (
        invoke(case, "preview", snapshot=case["snapshot"], report=report).returncode
        == 0
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: invoke(
                    case, "apply", snapshot=case["snapshot"], approved_report=report
                ),
                range(2),
            )
        )
    assert [r.returncode for r in results] == [0, 0]
    assert state(case)["runs"] == 1
    assert state(case)["state"]["head"]["revision"] == 1


def test_different_concurrent_copies_cannot_both_use_one_preview_state(case):
    authorize(case)
    first = case["root"] / "first-report.json"
    assert (
        invoke(case, "preview", snapshot=case["snapshot"], report=first).returncode == 0
    )
    payload = deepcopy(case["payload"])
    payload["sequence"] = 2
    payload["access"]["trojan"][0]["enabled"] = False
    second = bundle(case, payload, "second.json")
    report = case["root"] / "second-report.json"
    assert invoke(case, "preview", snapshot=second, report=report).returncode == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(
            invoke, case, "apply", snapshot=case["snapshot"], approved_report=first
        )
        b = pool.submit(invoke, case, "apply", snapshot=second, approved_report=report)
        assert sorted([a.result().returncode, b.result().returncode]) == [0, 2]
    assert state(case)["runs"] == 1


def test_full_fixture_suite_migration_can_follow_independent_copy_schema(case):
    output = python(
        case["env"],
        "import django; django.setup(); from django.core.management import call_command; call_command('migrate',verbosity=0); call_command('makemigrations',check=True,dry_run=True)",
    )
    assert "No changes detected" in output
