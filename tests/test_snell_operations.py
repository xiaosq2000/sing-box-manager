"""Snell installation and management operations use scratch state only."""

import json
import shlex

import pytest
import yaml

from sing_box_manager.cli import main
from sing_box_manager.provision import _configure_ufw, _derive_remote_config
from sing_box_manager.release.builder import ReleaseBuilder
from sing_box_manager.settings import Settings
from tests.snell_fixtures import with_snell
from tests.test_install_sh import _run
from tests.test_m3_profiles import profile_inventory
from tests.test_server_install import run_installer, server_fixture


def test_release_validates_snell_for_every_client_without_account_keys(
    tmp_path, monkeypatch
):
    inventory = with_snell(profile_inventory())
    inventory.default_protocol = "snell"
    inventory.releases_root = tmp_path / "releases"
    inventory.config_root = tmp_path / "config"
    inventory.upstream_cache_root = tmp_path / "upstream"
    builder = ReleaseBuilder(inventory)
    checked = []
    monkeypatch.setattr(
        builder, "_check_config", lambda config, label: checked.append((config, label))
    )
    builder._validate_client_configs(inventory)
    assert {label for _, label in checked} == {
        "validation/linux/sbc",
        "validation/darwin/sbc",
        "validation/windows/sbc",
        "validation/linux-tun",
        "validation/mobile",
    }
    for config, _ in checked:
        outbound = next(o for o in config["outbounds"] if o["type"] == "snell")
        assert outbound["userkey"] == "synthetic-validation-only"
        assert "synthetic-snell-alice" not in str(config)
        assert config["outbounds"][0]["default"] == "snell"


def test_runtime_publication_keeps_shared_psk_but_not_account_secrets():
    inventory = with_snell(profile_inventory())
    inventory.control_plane.enabled = True
    inventory.control_plane.authoritative = True
    data = yaml.safe_load(_derive_remote_config(inventory, inventory))
    assert data["snell"] == {"psk": inventory.snell.psk, "users": []}
    assert data["deployment"]["snell_port"] == 10443
    assert "synthetic-snell-alice" not in str(data)
    assert "client_signing_key" not in data
    Settings.model_validate(data)


def test_bootstrap_opens_tcp_only_for_snell(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sing_box_manager.provision._ssh", lambda *args, **kwargs: calls.append(args)
    )
    inventory = with_snell(profile_inventory())
    _configure_ufw("fixture-host", inventory.deployment, dry_run=False)
    command = calls[0][1]
    assert "ufw allow 10443/tcp comment Snell" in command
    assert "10443/udp" not in command


def test_snell_only_server_install(server_fixture):
    root, package, state, _ = server_fixture
    path = package / "config.json"
    config = json.loads(path.read_text())
    config["inbounds"].append({"type": "snell", "tag": "snell"})
    path.write_text(json.dumps(config))
    result = run_installer(server_fixture, "-p", "snell")
    assert result.returncode == 0, result.stderr
    assert json.loads((root / "usr/local/etc/sing-box/config.json").read_text())[
        "inbounds"
    ] == [{"type": "snell", "tag": "snell"}]


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
def test_unix_installer_passes_snell_choice(tmp_path, platform):
    result, log = _run(tmp_path, "-p", "snell", system=platform)
    assert result.returncode == 0, result.stderr
    assert "--protocol snell" in log


@pytest.mark.parametrize("remote", [False, True])
def test_backfill_preview_cli_is_read_only_and_forwards_remote_flag(
    monkeypatch, remote
):
    calls = []
    runtime = profile_inventory()
    monkeypatch.setattr("sing_box_manager.settings.load_settings", lambda _: runtime)
    monkeypatch.setattr(
        "sing_box_manager.control_plane.runner.run",
        lambda action, settings, **kwargs: calls.append((action, kwargs)),
    )
    monkeypatch.setattr(
        "sing_box_manager.deploy.run_remote_command",
        lambda host, command, error: calls.append(
            (host, shlex.split(command.split(" && ", 1)[1]))
        ),
    )
    main(
        [
            "--config",
            "/fixture/runtime.yaml",
            "control-plane",
            "backfill-snell",
            "--dry-run",
            *(["--remote", "fixture-host"] if remote else []),
        ]
    )
    if remote:
        host, argv = calls[0]
        assert host == "fixture-host"
        assert argv[:3] == ["sudo", "-u", "sbm"]
        assert argv[-2:] == ["backfill-snell", "--dry-run"]
    else:
        assert calls == [("backfill-snell", {"dry_run": True})]


def test_dry_run_is_not_silently_ignored_by_other_actions(monkeypatch):
    monkeypatch.setattr(
        "sing_box_manager.settings.load_settings", lambda _: profile_inventory()
    )
    monkeypatch.setattr(
        "sing_box_manager.control_plane.runner.run",
        lambda *args, **kwargs: pytest.fail("must not run"),
    )
    with pytest.raises(SystemExit) as error:
        main(
            ["--config", "/fixture/runtime.yaml", "control-plane", "sync", "--dry-run"]
        )
    assert error.value.code == 2
