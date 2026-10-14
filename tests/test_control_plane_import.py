"""Synthetic schema, private file, and CLI checks. No database or live inventory."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError

from sing_box_manager.cli import main
from sing_box_manager.control_plane.import_files import (
    decode_json,
    publish_private,
    read_private,
)
from sing_box_manager.control_plane.import_parity import verify_projection
from sing_box_manager.control_plane.import_schema import ImportRejected, Snapshot
from sing_box_manager.settings import Settings


def fixture_payload():
    return {
        "version": 1,
        "purpose": "synthetic-fixture",
        "namespace": str(uuid4()),
        "source_identity": str(uuid4()),
        "sequence": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "evidence": {
            "loaded_runtime": "synthetic-match",
            "loaded_auth": "synthetic-match",
            "proxy_config": "synthetic-match",
            "source_order": "synthetic-coordinated",
            "username_continuity": "synthetic-confirmed",
            "identity_changes": "none",
        },
        "context_identity": "0" * 64,
        "secret_identity": "1" * 64,
        "counter_state": "complete-table",
        "counters": [{"username": "alice", "counter": 3}],
        "access": {
            "portal": [
                {
                    "username": "alice",
                    "password_hash": PasswordHasher(
                        time_cost=1, memory_cost=1024, parallelism=1
                    ).hash("synthetic-login-value"),
                    "enabled": True,
                    "admin": True,
                }
            ],
            "trojan": [
                {
                    "username": "alice",
                    "password": "synthetic-access-value",
                    "enabled": True,
                }
            ],
            "hysteria2": [],
            "naive": [],
        },
    }


def test_snapshot_retains_fields_but_repr_hides_credentials():
    payload = fixture_payload()
    snapshot = Snapshot.model_validate(payload)
    assert (
        snapshot.access.memberships()["alice"]["portal"]
        == payload["access"]["portal"][0]
    )
    assert "synthetic-access-value" not in repr(snapshot)
    assert payload["access"]["portal"][0]["password_hash"] not in repr(snapshot)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(purpose="production"),
        lambda p: p.update(version=True),
        lambda p: p.update(sequence=0),
        lambda p: p.update(sequence=True),
        lambda p: p.update(captured_at="2026-10-14T00:00:00"),
        lambda p: p["evidence"].update(username_continuity="unknown"),
        lambda p: p["evidence"].update(identity_changes="rename"),
        lambda p: p["evidence"].update(loaded_runtime="mixed"),
        lambda p: p.update(counter_state="missing"),
        lambda p: p.update(counter_state="initial-zero", counters=[]),
        lambda p: p["counters"][0].update(counter=-1),
        lambda p: p["counters"][0].update(counter=True),
        lambda p: p["counters"][0].update(counter=2**63),
        lambda p: p["counters"].append(dict(p["counters"][0])),
        lambda p: p["access"]["portal"].append(dict(p["access"]["portal"][0])),
        lambda p: p["access"]["portal"][0].pop("admin"),
        lambda p: p["access"]["portal"][0].update(enabled="false"),
        lambda p: p["access"]["trojan"][0].update(extra="secret-value"),
    ],
)
def test_invalid_snapshots_are_rejected(change):
    payload = fixture_payload()
    change(payload)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize("kind", ["trojan", "hysteria2", "naive"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("invalid", ["\x00", "\ud800", "\udfff"])
def test_snapshot_rejects_postgres_unrepresentable_credentials(kind, enabled, invalid):
    payload = fixture_payload()
    payload["access"][kind] = [
        {
            "username": "alice",
            "password": "synthetic-" + invalid + "-value",
            "enabled": enabled,
        }
    ]
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


def test_snapshot_preserves_valid_unicode_credentials():
    payload = fixture_payload()
    for kind in ("trojan", "hysteria2", "naive"):
        payload["access"][kind] = [
            {
                "username": "alice",
                "password": "synthetic-café-😀-value",
                "enabled": True,
            }
        ]
    snapshot = Snapshot.model_validate(payload)
    assert (
        snapshot.access.memberships()["alice"]["trojan"]["password"]
        == "synthetic-café-😀-value"
    )


def test_explicit_initial_zero_and_case_sensitive_names():
    payload = fixture_payload()
    payload.update(counter_state="initial-zero", counters=[])
    payload["evidence"]["initial_zero"] = "synthetic-verified"
    payload["access"]["trojan"].append(
        {"username": "Alice", "password": "synthetic-other-value", "enabled": False}
    )
    snapshot = Snapshot.model_validate(payload)
    assert set(snapshot.access.memberships()) == {"alice", "Alice"}


@pytest.mark.parametrize(
    "content", [b'{"key":1,"key":2}', b'{"key":NaN}', b"not-json", b"\xff"]
)
def test_json_is_strict_and_errors_are_generic(content):
    with pytest.raises(ImportRejected) as error:
        decode_json(content)
    assert str(error.value) == "invalid_json"


def projection_fixture():
    subjects = [
        {"username": "alice", "id": "existing-alice", "present": True},
        {"username": "bob", "id": "new", "present": True},
        {"username": "carol", "id": "new", "present": True},
        {"username": "retired", "id": "existing-retired", "present": False},
    ]
    expected = {
        "subjects": subjects,
        "access": [
            {
                "username": "retired",
                "kind": "trojan",
                "present": False,
                "payload": {
                    "username": "retired",
                    "password": "synthetic-retained-value",
                    "enabled": True,
                },
            }
        ],
        "legacy": [
            {
                "username": row["username"],
                "subject": row["id"],
                "counter": 0,
                "secret_identity": "1" * 64,
            }
            for row in subjects
        ]
        + [
            {
                "username": "orphan",
                "subject": None,
                "counter": 5,
                "secret_identity": "1" * 64,
            }
        ],
    }
    actual = deepcopy(expected)
    generated = {"bob": str(uuid4()), "carol": str(uuid4())}
    for row in actual["subjects"]:
        if row["id"] == "new":
            row["id"] = generated[row["username"]]
    for row in actual["legacy"]:
        if row["subject"] == "new":
            row["subject"] = generated[row["username"]]
    return expected, actual


def test_projection_resolves_new_ids_without_mutating_expectation():
    expected, actual = projection_fixture()
    original = deepcopy(expected)
    for rows in actual.values():
        rows.reverse()
    verify_projection(expected, actual)
    assert expected == original


@pytest.mark.parametrize(
    "fault",
    [
        "absent_payload",
        "absent_row",
        "absent_subject",
        "existing_id",
        "new_link",
        "counter_only",
        "extra_subject",
    ],
)
def test_projection_checks_all_retained_rows_and_exact_links(fault):
    expected, actual = projection_fixture()
    if fault == "absent_payload":
        actual["access"][0]["payload"]["password"] = "synthetic-corrupted-value"
    elif fault == "absent_row":
        actual["access"].clear()
    elif fault == "absent_subject":
        actual["subjects"].pop()
    elif fault == "existing_id":
        actual["subjects"][0]["id"] = "replacement-alice"
        actual["legacy"][0]["subject"] = "replacement-alice"
    elif fault == "new_link":
        actual["legacy"][1]["subject"] = actual["subjects"][2]["id"]
    elif fault == "counter_only":
        actual["legacy"].pop()
    elif fault == "extra_subject":
        actual["subjects"].append(
            {"username": "unexpected", "id": str(uuid4()), "present": False}
        )
    with pytest.raises(ImportRejected, match="projection_parity_failed"):
        verify_projection(expected, actual)


def test_private_atomic_files_do_not_overwrite_or_follow_symlinks(tmp_path):
    tmp_path.chmod(0o700)
    output = tmp_path / "report.json"
    publish_private(output, b"original")
    assert output.stat().st_mode & 0o777 == 0o600
    assert read_private(output) == b"original"
    with pytest.raises(ImportRejected, match="report_output_exists"):
        publish_private(output, b"replacement")
    assert output.read_bytes() == b"original"
    alias = tmp_path / "alias"
    alias.symlink_to(output)
    with pytest.raises(ImportRejected, match="unsafe_input_path"):
        read_private(alias)
    with pytest.raises(ImportRejected, match="report_output_exists"):
        publish_private(alias, b"replacement")
    assert output.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["alias", "report.json"]
    hard_link = tmp_path / "hard-link"
    hard_link.hardlink_to(output)
    with pytest.raises(ImportRejected, match="unsafe_input_file"):
        read_private(output)
    hard_link.unlink()
    output.chmod(0o644)
    with pytest.raises(ImportRejected):
        read_private(output)
    output.chmod(0o600)
    tmp_path.chmod(0o755)
    with pytest.raises(ImportRejected):
        read_private(output)


@pytest.mark.parametrize(
    "args",
    [
        ["--snapshot", "SNAPSHOT", "--report", "REPORT"],
        ["--action", "preview", "--report", "REPORT"],
        ["--action", "preview", "--snapshot", "SNAPSHOT"],
        ["--action", "apply", "--snapshot", "SNAPSHOT", "--report", "REPORT"],
        [
            "--action",
            "preview",
            "--snapshot",
            "SNAPSHOT",
            "--approved-report",
            "REPORT",
        ],
        [
            "--action",
            "apply",
            "--snapshot",
            "SNAPSHOT",
            "--approved-report",
            "REPORT",
            "--report",
            "OTHER",
        ],
        [
            "--action",
            "apply",
            "--snapshot",
            "SNAPSHOT",
            "--approved-report",
            "REPORT",
            "--force",
        ],
    ],
)
def test_cli_rejects_invalid_arguments_before_config_or_database(monkeypatch, args):
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings",
        lambda _: pytest.fail("config was opened"),
    )
    with pytest.raises(SystemExit) as error:
        main(["--config", "CONFIG", "control-plane-import", *args])
    assert error.value.code == 2


def test_import_help_needs_no_config(monkeypatch):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings",
        lambda _: pytest.fail("config was opened"),
    )
    with pytest.raises(SystemExit) as error:
        main(["control-plane-import", "--help"])
    assert error.value.code == 0


def test_missing_import_config_and_bad_action_are_redacted(monkeypatch, capsys):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "control-plane-import",
                "--action",
                "preview",
                "--snapshot",
                "SNAPSHOT",
                "--report",
                "REPORT",
            ]
        )
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        main(
            [
                "control-plane-import",
                "--action",
                "synthetic-access-value",
                "--snapshot",
                "SNAPSHOT",
                "--report",
                "REPORT",
            ]
        )
    assert error.value.code == 2
    assert "synthetic-access-value" not in capsys.readouterr().err


def test_import_operational_errors_are_redacted(monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("synthetic-access-value")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", lambda _: Settings())
    monkeypatch.setattr("sing_box_manager.control_plane.import_runner.run", fail)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--config",
                "CONFIG",
                "control-plane-import",
                "--action",
                "apply",
                "--snapshot",
                "SNAPSHOT",
                "--approved-report",
                "REPORT",
            ]
        )
    assert error.value.code == 1
    assert "synthetic-access-value" not in capsys.readouterr().err


def test_import_config_errors_are_redacted(monkeypatch, capsys):
    def fail(_):
        raise ValueError("synthetic-access-value")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", fail)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--config",
                "CONFIG",
                "control-plane-import",
                "--action",
                "preview",
                "--snapshot",
                "SNAPSHOT",
                "--report",
                "REPORT",
            ]
        )
    assert error.value.code == 2
    assert "synthetic-access-value" not in capsys.readouterr().err
