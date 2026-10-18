"""Shared artifacts no longer publish inventory account snapshots or client archives."""

import base64
import json
import os
from pathlib import Path

import pytest

from sing_box_manager.release import builder as builder_module
from sing_box_manager.release.artifacts import read_release_info
from sing_box_manager.release.builder import ReleaseBuilder
from sing_box_manager.release.errors import ReleaseStageError
from sing_box_manager.release.manifest import Manifest
from sing_box_manager.settings import ControlPlaneSettings, TrafficStatsSettings
from tests.test_release_downloads import (
    CERTIFICATE_LINES,
    KEY_LINES,
    PREPARE_RULE_SETS,
    _build_relative_settings,
    _fake_linux_download,
    _fixed_release_info,
    _linux_platform,
    _write_inventory,
    _write_pem,
)
from tests.test_release_downloads import (
    _build_settings as _build_settings,
)
from tests.test_release_downloads import (
    fixture_rule_snapshots as fixture_rule_snapshots,
)


def _builder(settings, monkeypatch, *, root=None):
    builder = ReleaseBuilder(settings, project_root=root)
    monkeypatch.setattr(builder_module, "PLATFORMS", [_linux_platform()])
    monkeypatch.setattr(builder, "_extract_archives", lambda: 0)
    monkeypatch.setattr(builder, "_download_binaries", lambda: _fake_linux_download(builder))
    return builder


def test_release_has_shared_server_files_but_no_auth_or_user_archives(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    settings.control_plane = ControlPlaneSettings(enabled=True, authoritative=True)
    settings.default_protocol = "naive"  # Not all legacy users hold this protocol.
    builder = _builder(settings, monkeypatch)
    before = settings.config_path.read_bytes()
    result = builder.build()
    manifest = Manifest.from_json((result.release_dir / "manifest.json").read_text())
    assert result.release_id == manifest.release_id
    assert manifest.archives == () and manifest.prefixes == ()
    assert manifest.payload_digests
    assert not (result.release_dir / "users").exists()
    assert not (settings.generated_root / "auth-users.json").exists()
    node = json.loads((result.server_package_dir / "config.json").read_text())
    assert all(not inbound.get("users") for inbound in node["inbounds"])
    assert "alice-trojan-runtime-password" not in json.dumps(node)
    assert "hy2-obfs-runtime-secret" in json.dumps(node)
    assert (result.server_package_dir / "sing-box").is_symlink()
    assert (result.server_package_dir / "sing-box").read_text() == "binary"
    assert {path.name for path in result.server_package_dir.iterdir()} >= {
        "config.json", "server-install.sh", "server-uninstall.sh", "sing-box.service", "lib",
    }
    assert settings.config_path.read_bytes() == before
    assert read_release_info(settings.release_info_path).release_id == result.release_id


def test_initial_inventory_backed_node_package_remains_available(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    result = _builder(settings, monkeypatch).build()
    node = json.loads((result.server_package_dir / "config.json").read_text())
    assert {i["type"] for i in node["inbounds"]} == {"trojan", "hysteria2", "naive"}
    assert "alice-trojan-runtime-password" in json.dumps(node)
    assert not (settings.generated_root / "auth-users.json").exists()


def test_self_signed_tls_is_in_server_package_not_client_private_keys(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    _write_pem(certificate, CERTIFICATE_LINES)
    _write_pem(key, KEY_LINES)
    _write_inventory(settings.config_path, self_signed_cert=True, key_path=str(key), certificate_path=str(certificate))
    builder = _builder(settings, monkeypatch)
    checked = []
    monkeypatch.setattr(builder, "_check_config", lambda cfg, label, **kw: checked.append((label, cfg)))
    result = builder.build()
    node = json.loads((result.server_package_dir / "config.json").read_text())
    for inbound in node["inbounds"]:
        assert inbound["tls"]["key"] == KEY_LINES
        assert inbound["tls"]["certificate"] == CERTIFICATE_LINES
    clients = [cfg for label, cfg in checked if label.startswith("validation/")]
    assert len(clients) == 6
    assert all(KEY_LINES[1] not in json.dumps(cfg) for cfg in clients)
    assert all(CERTIFICATE_LINES[1] in json.dumps(cfg) for cfg in clients)


def test_stats_build_keeps_the_custom_server_binary(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    settings.traffic_stats = TrafficStatsSettings(enabled=True)
    builder = _builder(settings, monkeypatch)
    def custom():
        path = builder._staging_dir / "custom"
        path.write_bytes(b"custom binary")
        return path
    monkeypatch.setattr(builder, "_build_custom_server_binary", custom)
    result = builder.build()
    assert (result.server_package_dir / "sing-box").read_bytes() == b"custom binary"
    node = json.loads((result.server_package_dir / "config.json").read_text())
    assert node["experimental"]["v2ray_api"]["listen"] == settings.traffic_stats.api_listen
    assert len(node["services"]) == 1


def test_unchanged_build_and_inventory_password_changes_reuse_authoritative_release(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    settings.control_plane = ControlPlaneSettings(enabled=True, authoritative=True)
    first = _builder(settings, monkeypatch).build()
    path = settings.config_path
    path.write_text(path.read_text().replace("alice-trojan-runtime-password", "synthetic-rotated-password"))
    second = _builder(settings, monkeypatch).build()
    assert first.release_id == second.release_id
    assert second.reused_existing_release
    assert first.release_dir == second.release_dir


def test_failed_validation_leaves_published_artifacts_and_inventory_unchanged(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    first = _builder(settings, monkeypatch).build()
    before = settings.release_info_path.read_bytes(), settings.config_path.read_bytes()
    builder = _builder(settings, monkeypatch)
    def fail(*args, **kwargs):
        raise ValueError("fixture validation failure")
    monkeypatch.setattr(builder, "_check_config", fail)
    with pytest.raises(ValueError, match="validation failure"):
        builder.build()
    assert (settings.release_info_path.read_bytes(), settings.config_path.read_bytes()) == before
    assert first.release_dir.is_dir()


def test_failed_rules_leave_published_metadata_unchanged(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    first = _builder(settings, monkeypatch).build()
    before = settings.release_info_path.read_bytes()
    builder = _builder(settings, monkeypatch)
    def fail():
        raise ValueError("Rule source unavailable")
    monkeypatch.setattr(builder, "_prepare_rule_sets", fail)
    with pytest.raises(ReleaseStageError, match="Rule source unavailable"):
        builder.build()
    assert settings.release_info_path.read_bytes() == before
    assert first.release_dir.is_dir()


def test_relative_paths_and_owner_only_node_config_survive_hostile_umask(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    settings = _build_relative_settings(root)
    monkeypatch.setattr(builder_module, 'build_release_info', lambda version, **kwargs: _fixed_release_info(version))
    old_cwd, old_umask = Path.cwd(), os.umask(0o077)
    try:
        result = _builder(settings, monkeypatch, root=root).build()
    finally:
        os.umask(old_umask)
    assert Path.cwd() == old_cwd
    assert result.release_dir.parent == root / "releases"
    assert (result.server_package_dir / "config.json").stat().st_mode & 0o777 == 0o600
    assert not (root / "config/generated/auth-users.json").exists()


def test_rule_preparation_covers_every_desktop_route(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path).model_copy(update={"client_signing_key":"fixture-key"})
    builder = _builder(settings, monkeypatch)
    _fake_linux_download(builder)
    calls = []
    def prepare(rules, destination, binary, token):
        calls.append(rules)
        assert len(rules) == 7
        assert {rule["tag"] for rule in rules.values()} >= {"AI-Anthropic", "AI-Geosite"}
        assert destination == builder._staging_dir / "rules"
        assert binary == builder._upstream_payload_dir(_linux_platform()) / "sing-box"
        assert token is None
        return [destination / name for name in (*rules, "files.txt")]
    monkeypatch.setattr(builder, "_github_token", lambda: None)
    monkeypatch.setattr(builder_module, "prepare_rule_snapshots", prepare)
    assert PREPARE_RULE_SETS(builder) == 7
    assert len(calls) == 1


def test_changed_rule_bytes_change_signed_shared_artifacts(tmp_path, monkeypatch):
    settings = _build_settings(tmp_path)
    settings.client_signing_key = base64.b64encode(bytes(range(32))).decode()
    def sbc(root, platform, version, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture sbc")
    monkeypatch.setattr(builder_module, "build_sbc", sbc)
    first = _builder(settings, monkeypatch).build()
    original = ReleaseBuilder._prepare_rule_sets
    def refreshed(builder):
        count = original(builder)
        builder._rule_snapshot_paths[0].write_bytes(b"changed fixture rules")
        return count
    monkeypatch.setattr(ReleaseBuilder, "_prepare_rule_sets", refreshed)
    second = _builder(settings, monkeypatch).build()
    assert first.release_id != second.release_id
    assert (first.release_dir / "client/manifest.json").read_bytes() != (second.release_dir / "client/manifest.json").read_bytes()


def test_report_describes_shared_artifacts_not_account_counts(tmp_path, monkeypatch, capsys):
    result = _builder(_build_settings(tmp_path), monkeypatch).build()
    output = capsys.readouterr().out
    assert "Release complete." in output and result.release_id in output
    assert "Auth snapshot" not in output and "release users" not in output
