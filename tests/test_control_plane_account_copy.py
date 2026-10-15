"""Only synthetic sources. No operational config, PostgreSQL, or network access."""

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from sing_box_manager import cli
from sing_box_manager.account_copy.command import AccountCopyCommand
from sing_box_manager.account_copy.export import export_snapshot
from sing_box_manager.account_copy.files import (
    publish_private,
    read_private,
    read_source,
)
from sing_box_manager.account_copy.schema import AccountSnapshot, copy_context_identity
from sing_box_manager.control_plane.import_schema import (
    ImportRejected,
    Snapshot,
    canonical,
)
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import load_settings
from tests.test_control_plane_import import fixture_payload

NS = UUID("00000000-0000-0000-0000-000000000011")
SOURCE = UUID("00000000-0000-0000-0000-000000000012")
ROOT = Path(__file__).parents[1]


def source_files(root, overrides=None):
    value = yaml.safe_load((ROOT / "config/inventory/example.yaml").read_text())
    access = deepcopy(fixture_payload()["access"])
    portal = access["portal"][0]
    access["portal"] += [
        {**portal, "username": "Alice", "admin": False},
        {**portal, "username": "portal_only", "admin": False},
        {**portal, "username": "disabled", "enabled": False},
    ]
    access["trojan"] += [
        {"username": "Alice", "enabled": True, "password": "synthetic-case-credential"},
        {
            "username": "proxy_only",
            "enabled": True,
            "password": "synthetic-protocol-credential",
        },
        {
            "username": "disabled",
            "enabled": False,
            "password": "synthetic-disabled-credential",
        },
    ]
    for protocol in ("hysteria2", "naive"):
        access[protocol] = [
            {
                "username": "alice",
                "enabled": True,
                "password": "synthetic-extra-protocol",
            },
            {
                "username": "disabled",
                "enabled": False,
                "password": "synthetic-extra-disabled",
            },
        ]
    for kind, rows in access.items():
        value["web_portal" if kind == "portal" else kind]["users"] = rows
    value["control_plane"] = {
        "enabled": True,
        "secret_key": "synthetic-account-copy-key-" + "x" * 64,
    }
    value["web"]["subscription_secret"] = "synthetic-old-link-key-do-not-export"
    value["web"]["subscription_database_path"] = str(
        root / "must-not-be-created.sqlite3"
    )
    if overrides:
        value.update(overrides)
    path = root / "source.yaml"
    path.write_text(yaml.safe_dump(value))
    path.chmod(0o600)
    runtime = load_settings(path)
    auth = root / "auth.json"
    auth.write_bytes(
        canonical(
            {"users": [row.model_dump(mode="json") for row in runtime.web_portal.users]}
        )
    )
    auth.chmod(0o600)
    proxy = root / "proxy.json"
    proxy.write_bytes(canonical(build_server_config(runtime, runtime.traffic_stats)))
    proxy.chmod(0o600)
    return runtime, auth, proxy


@pytest.fixture
def source(tmp_path):
    return source_files(tmp_path)


def export(source, path, sequence=1):
    return export_snapshot(
        *[source[0], path, source[1], source[2]],
        namespace=NS,
        source_identity=SOURCE,
        sequence=sequence,
    )


@pytest.fixture
def payload(source, tmp_path):
    path = tmp_path / "snapshot.json"
    export(source, path)
    return json.loads(path.read_bytes())


def test_export_complete_accounts_no_signing_secrets_or_legacy_state(source, tmp_path):
    path = tmp_path / "snapshot.json"
    before = [p.read_bytes() for p in (source[0].config_path, source[1], source[2])]
    result = export(source, path)
    raw = path.read_bytes()
    parsed = AccountSnapshot.model_validate(json.loads(raw))
    assert result == {"exported": True, "subjects": 5}
    assert parsed.legacy_counters == "not-imported"
    assert parsed.identity_history == "not-established"
    assert parsed.link_policy == "fresh-links-at-cutover"
    assert raw == canonical(parsed.model_dump(mode="json")) + b"\n"
    assert source[0].web.subscription_secret.encode() not in raw
    assert source[0].control_plane.secret_key.encode() not in raw
    assert b"password_hash" in raw and b"synthetic-disabled-credential" in raw
    assert not source[0].web.subscription_database_path.exists()
    assert before == [
        p.read_bytes() for p in (source[0].config_path, source[1], source[2])
    ]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-disabled-credential" not in repr(parsed) + str(parsed)
    with pytest.raises(ImportRejected, match="report_output_exists"):
        export(source, path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("version", 2),
        ("sequence", True),
        ("sequence", 0),
        ("sequence", 2**63),
        ("sequence", "1"),
        ("purpose", "synthetic-fixture"),
        ("link_policy", "preserve-links"),
        ("legacy_counters", "initial-zero"),
        ("identity_history", "verified"),
        ("namespace", "00000000000000000000000000000011"),
        ("source_identity", None),
        ("context_identity", "x" * 64),
        ("captured_at", "2026-10-15T00:00:00Z"),
        ("captured_at", "٢٠٢٦-10-15T00:00:00.000000Z"),
        ("captured_at", "2026-02-30T00:00:00.000000Z"),
        ("counters", []),
        ("secret_identity", "0" * 64),
    ],
)
def test_strict_snapshot(payload, field, value):
    payload[field] = value
    with pytest.raises(ValidationError):
        AccountSnapshot.model_validate(payload)


@pytest.mark.parametrize("field", list(AccountSnapshot.model_fields))
def test_all_fields_required(payload, field):
    del payload[field]
    with pytest.raises(ValidationError):
        AccountSnapshot.model_validate(payload)


def test_production_and_fixture_formats_are_not_interchangeable(payload):
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)
    with pytest.raises(ValidationError):
        AccountSnapshot.model_validate(fixture_payload())


@pytest.mark.parametrize(
    "mutation",
    ["auth_flag", "auth_verifier", "auth_extra", "proxy_password", "proxy_extra"],
)
def test_export_rejects_disagreement(source, tmp_path, mutation):
    runtime, auth, proxy = source
    if mutation.startswith("auth"):
        value = json.loads(auth.read_bytes())
        if mutation == "auth_flag":
            value["users"][0]["enabled"] = not value["users"][0]["enabled"]
        elif mutation == "auth_verifier":
            value["users"][0]["password_hash"] = "not-a-verifier"
        else:
            value["users"].append({**value["users"][0], "username": "additional"})
        auth.write_bytes(canonical(value))
    else:
        value = json.loads(proxy.read_bytes())
        if mutation == "proxy_password":
            value["inbounds"][0]["users"][0]["password"] = "synthetic-mismatch"
        else:
            value["unexpected"] = True
        proxy.write_bytes(canonical(value))
    with pytest.raises(ImportRejected):
        export(source, tmp_path / "rejected.json")
    assert not (tmp_path / "rejected.json").exists()
    assert not runtime.web.subscription_database_path.exists()


def test_export_refuses_changed_config_and_duplicate_yaml(source, tmp_path):
    path = source[0].config_path
    original = path.read_text()
    path.write_text(original + "\ndefault_protocol: naive\n")
    with pytest.raises(ImportRejected):
        export(source, tmp_path / "snapshot.json")
    path.write_text(original)
    value = yaml.safe_load(original)
    value["default_protocol"] = "naive"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ImportRejected, match="source_changed"):
        export(source, tmp_path / "snapshot.json")


def test_export_rereads_all_source_bytes_before_publication(
    source, tmp_path, monkeypatch
):
    from sing_box_manager.account_copy import export as module

    original = module.read_source
    calls = 0

    def change(path):
        nonlocal calls
        calls += 1
        raw = original(path)
        return raw + b" " if calls == 4 else raw

    monkeypatch.setattr(module, "read_source", change)
    with pytest.raises(ImportRejected, match="source_changed"):
        export(source, tmp_path / "snapshot.json")
    assert not (tmp_path / "snapshot.json").exists()


def test_context_ignores_rosters_and_old_link_secret_but_binds_render_settings(source):
    runtime = source[0]
    before = copy_context_identity(runtime)
    runtime.web.subscription_secret = "synthetic-different-old-key"
    runtime.trojan.users.clear()
    assert copy_context_identity(runtime) == before
    runtime.traffic_stats.connection_api_secret = "synthetic-changed-render-setting"
    assert copy_context_identity(runtime) != before
    runtime.deployment.tls.self_signed_cert = True
    with pytest.raises(ImportRejected, match="account_copy_context_rejected"):
        copy_context_identity(runtime)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666, 0o400])
def test_source_permissions(source, mode):
    source[1].chmod(mode)
    with pytest.raises(ImportRejected, match="unsafe_source_file"):
        read_source(source[1])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_source_unsafe_files(source, tmp_path, kind):
    path = tmp_path / "unsafe"
    if kind == "symlink":
        path.symlink_to(source[1])
    elif kind == "hardlink":
        os.link(source[1], path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises((ImportRejected, OSError)):
        read_source(path)


def test_unsafe_ancestors_and_private_output(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    private = shared / "private"
    private.mkdir(mode=0o700)
    path = private / "file"
    path.write_bytes(b"{}")
    path.chmod(0o600)
    for operation in (read_source, read_private):
        with pytest.raises(ImportRejected, match="unsafe_directory"):
            operation(path)
    with pytest.raises(ImportRejected, match="unsafe_directory"):
        publish_private(private / "output", b"{}")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--action", "apply"],
        ["--action", "preview", "--snapshot", "secret-value"],
        ["--action", "authorize", "--namespace", str(NS)],
        [
            "--action",
            "close",
            "--namespace",
            str(NS),
            "--source-identity",
            str(SOURCE),
            "--snapshot",
            "secret-value",
        ],
        ["--action", "export", "--snapshot", "secret-value"],
        ["--action", "unknown", "--snapshot", "secret-value"],
        [
            "--action",
            "preview",
            "--snapshot",
            "secret-value",
            "--report",
            "out",
            "--sequence",
            "1",
        ],
    ],
)
def test_cli_invalid_arguments_open_nothing(args, monkeypatch, capsys):
    def fail(_):
        raise AssertionError("configuration_must_not_open")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["account-copy", *args])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "secret-value" not in output.out + output.err


def test_cli_help_and_config_errors_are_safe(monkeypatch, capsys):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["account-copy", "--help"])
    assert error.value.code == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "account-copy",
                "--action",
                "authorize",
                "--namespace",
                str(NS),
                "--source-identity",
                str(SOURCE),
            ]
        )
    assert error.value.code == 2

    def fail(_):
        raise ValueError("synthetic-private-value")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "--config",
                "unused",
                "account-copy",
                "--action",
                "authorize",
                "--namespace",
                str(NS),
                "--source-identity",
                str(SOURCE),
            ]
        )
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "synthetic-private-value" not in output.out + output.err


@pytest.mark.parametrize("sequence", [0, True, 2**63])
def test_cli_sequence_bound(sequence):
    command = AccountCopyCommand(
        action="export",
        snapshot=Path("out"),
        auth_file=Path("auth"),
        proxy_config=Path("proxy"),
        namespace=NS,
        source_identity=SOURCE,
        sequence=sequence,
    )
    with pytest.raises(ValueError):
        command.options()


def test_account_parity_never_uses_tokens_or_counters(payload, source, monkeypatch):
    from sing_box_manager.control_plane import import_parity
    from sing_box_manager.control_plane.import_schema import AccessRecords

    records = AccessRecords.model_validate(payload["access"])
    memberships = records.memberships()
    state = {
        "subjects": [
            {"id": str(NS), "username": name, "present": True} for name in memberships
        ],
        "access": [
            {"username": name, "kind": kind, "present": True, "payload": row}
            for name, kinds in memberships.items()
            for kind, row in kinds.items()
        ],
        "legacy": [],
    }

    def fail(*args, **kwargs):
        raise AssertionError("token_or_counter_access")

    monkeypatch.setattr(import_parity, "subscription_token", fail)
    monkeypatch.setattr(import_parity, "find_subscriber", fail)
    monkeypatch.setattr(import_parity.ResetCounters, "current", fail)
    monkeypatch.setattr(import_parity.ResetCounters, "advance", fail)
    assert all(import_parity.verify_account_parity(records, source[0], state).values())
    row = next(
        r
        for r in state["access"]
        if r["username"] == "disabled" and r["kind"] == "trojan"
    )
    row["payload"] = {**row["payload"], "password": "synthetic-corruption"}
    with pytest.raises(ImportRejected, match="field_parity_failed"):
        import_parity.verify_account_parity(records, source[0], state)


def test_valid_cli_ids_and_operational_error_redaction(source, monkeypatch, capsys):
    from sing_box_manager.account_copy import runner

    def fail(action, runtime, options):
        assert action == "authorize" and options == {
            "namespace": NS,
            "source_identity": SOURCE,
        }
        raise OSError("synthetic-private-database-error")

    monkeypatch.setattr(runner, "run", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "--config",
                str(source[0].config_path),
                "account-copy",
                "--action",
                "authorize",
                "--namespace",
                str(NS),
                "--source-identity",
                str(SOURCE),
            ]
        )
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "synthetic-private-database-error" not in output.out + output.err


@pytest.mark.parametrize("fault", ["duplicate", "invalid_utf8", "extra_field"])
def test_export_rejects_bad_auth_json(source, tmp_path, fault):
    original = source[1].read_bytes()
    if fault == "duplicate":
        source[1].write_bytes(b'{"users":[],"users":[]}')
    elif fault == "invalid_utf8":
        source[1].write_bytes(b"\xff")
    else:
        value = json.loads(original)
        value["extra"] = "synthetic-extra"
        source[1].write_bytes(canonical(value))
    with pytest.raises(ImportRejected):
        export(source, tmp_path / "rejected.json")
    assert not (tmp_path / "rejected.json").exists()


def test_export_failed_directory_sync_is_not_success(source, tmp_path, monkeypatch):
    from sing_box_manager.control_plane import import_files

    original = import_files.os.fsync
    import stat

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-sync-failure")
        original(fd)

    monkeypatch.setattr(import_files.os, "fsync", fail_directory)
    output = tmp_path / "uncertain.json"
    with pytest.raises(OSError):
        export(source, output)
    # Retain uncertain published bytes. No automatic overwrite or cleanup.
    assert output.is_file()
    with pytest.raises(ImportRejected, match="report_output_exists"):
        export(source, output)
