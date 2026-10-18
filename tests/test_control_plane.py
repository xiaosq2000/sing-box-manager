"""Fixture-only settings, lifecycle, and deployment checks."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sing_box_manager.cli import main
from sing_box_manager.control_plane import deployment
from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.control_plane.runner import run
from sing_box_manager.provision import _derive_remote_config
from sing_box_manager.remote_config import publish_runtime_config
from sing_box_manager.settings import (
    ControlPlaneSettings,
    Settings,
    load_runtime_inventory,
)


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "template0",
        "template1",
        "x;DROP TABLE users",
        "../data",
        "MixedCase",
    ],
)
def test_rejects_unsafe_database_names(name):
    with pytest.raises(ValidationError):
        ControlPlaneSettings(database_name=name)
    with pytest.raises(ValidationError):
        provision_sql(name)


def test_defaults_are_disabled_and_loopback_port_is_separate():
    settings = Settings()
    assert not settings.control_plane.enabled
    assert settings.control_plane.port != settings.web.port
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            control_plane=ControlPlaneSettings(enabled=True, port=settings.web.port)
        )
    with pytest.raises(ValidationError):
        ControlPlaneSettings(secret_key="short")
    with pytest.raises(ValidationError):
        ControlPlaneSettings(database_socket_dir=Path("relative"))
    with pytest.raises(ValidationError, match="requires control_plane.enabled"):
        ControlPlaneSettings(enabled=False, authoritative=True)


@pytest.mark.parametrize(
    "settings", [Settings(), Settings(control_plane=ControlPlaneSettings(enabled=True))]
)
def test_disabled_or_unconfigured_operations_fail_before_loading_django(settings):
    with pytest.raises(ValueError):
        run("migrate", settings)


def test_control_plane_cli_requires_config(monkeypatch):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    with pytest.raises(SystemExit) as exit:
        main(["control-plane", "--action", "check"])
    assert exit.value.code == 1


def test_remote_config_generates_and_preserves_separate_key(tmp_path, monkeypatch):
    inventory = load_runtime_inventory(
        Path(__file__).parents[1] / "config/inventory/example.yaml"
    )
    enabled = Settings(control_plane=ControlPlaneSettings(enabled=True))
    incoming = yaml.safe_load(_derive_remote_config(enabled, inventory))
    key = incoming["control_plane"]["secret_key"]
    assert len(key) == 64
    assert key != incoming["web"]["session_secret"]
    target = tmp_path / "runtime.yaml"
    staged = tmp_path / "staged.yaml"
    target.write_text(yaml.safe_dump(incoming))
    monkeypatch.setattr("sing_box_manager.remote_config.os.chown", lambda *_: None)
    for settings in (Settings(), enabled):
        staged.write_text(_derive_remote_config(settings, inventory))
        publish_runtime_config(staged, target, tmp_path)
        current = yaml.safe_load(target.read_text())
        assert current["control_plane"]["secret_key"] == key
        assert current["control_plane"]["enabled"] == settings.control_plane.enabled
        assert current["trojan"] == incoming["trojan"]


def record_deployment(monkeypatch, *, fail_action=None):
    events = []
    monkeypatch.setattr(
        deployment, "_require_ubuntu_debian", lambda _: events.append(("os",))
    )
    monkeypatch.setattr(
        deployment,
        "_ssh_write",
        lambda host, path, content, **kwargs: events.append(
            ("write", path, content, kwargs)
        ),
    )
    monkeypatch.setattr(
        deployment,
        "_install_unit",
        lambda host, content, name, **kwargs: events.append(
            ("unit", name, content, kwargs)
        ),
    )

    def command(host, command, error, **kwargs):
        events.append(("command", command, kwargs))
        if fail_action and f"--action {fail_action}" in command:
            from sing_box_manager.deploy import DeploymentError

            raise DeploymentError(error)

    monkeypatch.setattr(deployment, "_ssh", command)
    return events


def test_enabled_deployment_provisions_migrates_starts_and_checks(monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.deploy(
        "fixture-host",
        "/opt/sing-box-manager",
        Settings(control_plane=ControlPlaneSettings(enabled=True)),
    )
    commands = [event[1] for event in events if event[0] == "command"]
    assert "postgresql-client" in commands[0]
    assert "ON_ERROR_STOP=1" in commands[1]
    assert "sudo -u sbm" in commands[2]
    actions = [
        command.split("--action ", 1)[1].split()[0]
        for command in commands
        if "--action " in command
    ]
    assert actions == ["check", "migrate", "collect-static", "health"]
    unit = next(event[2] for event in events if event[0] == "unit")
    assert "User=sbm\n" in unit
    assert "NoNewPrivileges=true\n" in unit
    assert "ProtectSystem=strict\n" in unit
    assert "PYTHONDONTWRITEBYTECODE=1" in unit
    assert "--action serve" in unit
    assert "systemctl restart" in commands[-2]


def test_authoritative_deployment_syncs_node_config_from_database(monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.deploy(
        "fixture-host",
        "/opt/sing-box-manager",
        Settings(control_plane=ControlPlaneSettings(enabled=True, authoritative=True)),
    )
    commands = [event[1] for event in events if event[0] == "command"]
    actions = [
        command.split("--action ", 1)[1].split()[0]
        for command in commands
        if "--action " in command
    ]
    assert actions == ["check", "migrate", "collect-static", "health", "sync-node"]
    units = [event[1] for event in events if event[0] == "unit"]
    assert units == [
        "sing-box-manager-control-plane.service",
        "sing-box-manager-sync.service",
        "sing-box-manager-sync.path",
    ]
    assert "update_nginx_site_text" in commands[-1]
    assert "nginx -t" in commands[-1]


def test_update_nginx_site_text_is_idempotent_and_reversible():
    original = (
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name vpn.example.com;\n"
        "\n"
        "    location / {\n"
        "        proxy_pass http://127.0.0.1:8080;\n"
        "    }\n"
        "}\n"
    )
    routed = deployment.update_nginx_site_text(original, 47071)
    assert "proxy_pass http://127.0.0.1:47071;" in routed
    assert "location ~ ^/(sub/|install\\.sh$|install\\.ps1$|install/)" in routed
    assert deployment.update_nginx_site_text(routed, 47071) == routed
    reverted = deployment.update_nginx_site_text(routed, None)
    assert reverted == original


def test_disabled_deployment_only_disables_its_unit_and_retains_database(monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.deploy("fixture-host", "/opt/sing-box-manager", Settings())
    assert len(events) == 1
    assert "disable --now sing-box-manager-control-plane.service" in events[0][1]
    assert "postgres" not in events[0][1]


def test_failed_migration_never_starts_control_plane(monkeypatch):
    from sing_box_manager.deploy import DeploymentError

    events = record_deployment(monkeypatch, fail_action="migrate")
    with pytest.raises(DeploymentError, match="migrate failed"):
        deployment.deploy(
            "fixture-host",
            "/opt/sing-box-manager",
            Settings(control_plane=ControlPlaneSettings(enabled=True)),
        )
    assert not any(event[0] == "unit" for event in events)


def test_dry_run_marks_every_mutating_step(monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.deploy(
        "fixture-host",
        "/opt/sing-box-manager",
        Settings(control_plane=ControlPlaneSettings(enabled=True)),
        dry_run=True,
    )
    for event in events:
        if event[0] != "os":
            assert event[-1]["dry_run"] is True
