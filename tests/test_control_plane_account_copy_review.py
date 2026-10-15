"""Synthetic regressions for the independent account-copy source review."""

import json
import os
import subprocess
import sys
from base64 import b64encode

import pytest
import yaml
from argon2 import PasswordHasher
from argon2.low_level import Type, hash_secret
from pydantic import ValidationError

from sing_box_manager import cli
from sing_box_manager.account_copy.schema import AccountSnapshot
from sing_box_manager.control_plane.import_schema import ImportRejected, canonical
from sing_box_manager.settings import WebPortalUser
from tests.test_control_plane_account_copy import NS, SOURCE, export, source_files


def export_args(root, config):
    return [
        "--config",
        str(config),
        "account-copy",
        "--action",
        "export",
        "--snapshot",
        str(root / "snapshot.json"),
        "--auth-file",
        str(root / "auth.json"),
        "--proxy-config",
        str(root / "proxy.json"),
        "--namespace",
        str(NS),
        "--source-identity",
        str(SOURCE),
        "--sequence",
        "1",
    ]


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized", "public", "ancestor"])
def test_export_cli_checks_source_before_generic_config_loader(
    tmp_path, monkeypatch, capsys, kind
):
    source = tmp_path / "source.yaml"
    if kind == "fifo":
        os.mkfifo(source, 0o600)
    elif kind == "symlink":
        source.symlink_to(tmp_path / "missing")
    elif kind == "ancestor":
        parent = tmp_path / "shared"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        source = parent / "source.yaml"
        source.write_text("synthetic-private-value")
        source.chmod(0o600)
    else:
        source.write_text("synthetic-private-value")
        source.chmod(0o644 if kind == "public" else 0o600)
        if kind == "oversized":
            with source.open("ab") as stream:
                stream.truncate(8 * 1024 * 1024 + 1)
    calls = []

    def forbidden(path):
        calls.append(path)
        raise AssertionError("generic_config_loader_must_not_run")

    monkeypatch.setattr("sing_box_manager.settings.load_settings", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(export_args(tmp_path, source))
    assert error.value.code == 2
    assert calls == []
    assert not (tmp_path / "snapshot.json").exists()
    output = capsys.readouterr()
    assert "synthetic-private-value" not in output.out + output.err


def test_export_cli_fifo_rejects_without_blocking(tmp_path):
    source = tmp_path / "source.yaml"
    os.mkfifo(source, 0o600)
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "LANG", "LD_LIBRARY_PATH")
        if name in os.environ
    }
    result = subprocess.run(
        [sys.executable, "-m", "sing_box_manager", *export_args(tmp_path, source)],
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert not (tmp_path / "snapshot.json").exists()


@pytest.mark.parametrize("syntax", ["alias", "merge", "merge_aliases"])
def test_export_rejects_yaml_expansion_before_flattening(tmp_path, monkeypatch, syntax):
    source = source_files(tmp_path)
    path = source[0].require_config_path()
    extra = {
        "alias": "extra: &item [1]\nother: *item\n",
        "merge": "extra: {<<: {one: 1}}\n",
        "merge_aliases": "first: &a {one: 1}\nsecond: &b {<<: [*a, *a]}\nthird: {<<: [*b, *b]}\n",
    }[syntax]
    path.write_text(path.read_text() + "\n" + extra)
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError("yaml_merge_expansion_must_not_run")

    monkeypatch.setattr(yaml.SafeLoader, "flatten_mapping", forbidden)
    with pytest.raises(ImportRejected):
        export(source, tmp_path / "snapshot.json")
    assert calls == []
    assert not (tmp_path / "snapshot.json").exists()


def test_cli_first_yaml_parse_has_no_expansion_or_generic_load(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "source.yaml"
    source.write_text("first: &a {one: 1}\nsecond: {<<: [*a, *a]}\n")
    source.chmod(0o600)
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError("unprotected_yaml_load")

    monkeypatch.setattr(yaml.SafeLoader, "flatten_mapping", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(export_args(tmp_path, source))
    assert error.value.code == 2
    assert calls == []
    assert not (tmp_path / "snapshot.json").exists()
    assert "first:" not in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("enabled", 1), ("listen_port", 8443.0)])
def test_export_proxy_comparison_preserves_json_types(tmp_path, field, value):
    source = source_files(tmp_path)
    config = json.loads(source[2].read_bytes())
    inbound = config["inbounds"][0]
    if field == "enabled":
        assert inbound["multiplex"][field] is True
        inbound["multiplex"][field] = value
    else:
        inbound[field] = float(inbound[field])
    source[2].write_bytes(canonical(config))
    with pytest.raises(ImportRejected, match="source_proxy_mismatch"):
        export(source, tmp_path / "snapshot.json")
    assert not (tmp_path / "snapshot.json").exists()


def verifier(*, parameters="m=65536,t=3,p=4", version="v=19$", salt=None, digest=None):
    # Arbitrary synthetic bytes, not an operational verifier or password.
    salt = b64encode(b"synthetic-salt").decode().rstrip("=") if salt is None else salt
    digest = (
        b64encode(b"synthetic-digest" * 2).decode().rstrip("=")
        if digest is None
        else digest
    )
    return f"$argon2id${version}{parameters}${salt}${digest}"


@pytest.mark.parametrize(
    "parameters",
    ["m=65536,t=4294967295,p=4", "m=4294967295,t=1,p=1", "m=134217728,t=1,p=16777215"],
)
def test_verifier_validation_never_executes_input_selected_cost(
    monkeypatch, parameters
):
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError("password_kdf_must_not_run")

    monkeypatch.setattr(PasswordHasher, "verify", forbidden)
    encoded = verifier(parameters=parameters)
    assert (
        WebPortalUser(username="alice", password_hash=encoded).password_hash == encoded
    )
    assert calls == []


@pytest.mark.parametrize(
    "encoded",
    [
        verifier(parameters="m=0,t=3,p=4"),
        verifier(parameters="m=31,t=3,p=4"),
        verifier(parameters="m=65536,t=0,p=4"),
        verifier(parameters="m=65536,t=3,p=0"),
        verifier(parameters="m=4294967296,t=3,p=4"),
        verifier(parameters="m=65536,t=4294967296,p=4"),
        verifier(parameters="m=4294967295,t=3,p=16777216"),
        verifier(parameters="m=-1,t=3,p=4"),
        verifier(parameters="m=65536,t=3,p=4,p=4"),
        verifier(parameters="m=65536,t=٣,p=4"),
        verifier(version="v=18$"),
        verifier(version="v=999$"),
        verifier(salt="@@@"),
        verifier(salt="YQ"),
        verifier(digest="YQ"),
        verifier(salt="c3ludGhldGljLXNhbHQ="),
        verifier(salt="c3ludGhldGljLXNhbHR"),
        verifier().replace("argon2id", "argon2i"),
    ],
)
def test_malformed_verifiers_reject_without_password_work(monkeypatch, encoded):
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError("password_kdf_must_not_run")

    monkeypatch.setattr(PasswordHasher, "verify", forbidden)
    with pytest.raises(ValidationError, match="password_hash"):
        WebPortalUser(username="alice", password_hash=encoded)
    assert calls == []


@pytest.mark.parametrize("version", ["v=16$", "v=19$", ""])
def test_structural_verifier_validation_retains_supported_encoding(version):
    encoded = verifier(version=version)
    assert (
        WebPortalUser(username="alice", password_hash=encoded).password_hash == encoded
    )


@pytest.mark.parametrize("version,explicit", [(16, True), (19, True), (16, False)])
def test_actual_low_cost_argon2_encodings_remain_usable(version, explicit):
    encoded = hash_secret(
        b"synthetic-password",
        b"synthetic-salt",
        time_cost=1,
        memory_cost=8,
        parallelism=1,
        hash_len=4,
        type=Type.ID,
        version=version,
    ).decode("ascii")
    if not explicit:
        encoded = encoded.replace("$v=16$", "$")
    assert (
        WebPortalUser(username="alice", password_hash=encoded).password_hash == encoded
    )
    assert PasswordHasher().verify(encoded, "synthetic-password")


def test_export_and_snapshot_validation_preserve_verifier_without_kdf(
    tmp_path, monkeypatch, capsys
):
    source = source_files(tmp_path)
    encoded = source[0].web_portal.users[0].password_hash
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise AssertionError("password_kdf_must_not_run")

    monkeypatch.setattr(PasswordHasher, "verify", forbidden)
    # Exercise the first CLI settings parse, source/auth parse, and wire validation.
    cli.main(export_args(tmp_path, source[0].require_config_path()))
    snapshot = AccountSnapshot.model_validate(
        json.loads((tmp_path / "snapshot.json").read_bytes())
    )
    assert snapshot.access.portal[0].password_hash == encoded
    assert calls == []
    output = capsys.readouterr()
    assert encoded not in output.out + output.err
