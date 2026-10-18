"""Fixture-only settings, lifecycle, and deployment checks."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

from sing_box_manager.cli import main
from sing_box_manager.control_plane import deployment, runner
from sing_box_manager.control_plane.database import provision_sql
from sing_box_manager.control_plane.runner import Action, normalize_action, run
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


def test_control_plane_port_checks_ignore_the_retired_web_listener():
    settings = Settings()
    assert not settings.control_plane.enabled
    Settings(control_plane=ControlPlaneSettings(enabled=True, port=settings.web.port))
    assert "port" not in settings.web.model_dump()
    assert "host" not in settings.web.model_dump()
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            control_plane=ControlPlaneSettings(
                enabled=True, port=settings.control_plane.database_port
            )
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


@pytest.mark.parametrize("args", [["check"], ["--action", "check"]])
def test_control_plane_cli_requires_config(monkeypatch, args):
    monkeypatch.delenv("SBM_CONFIG", raising=False)
    with pytest.raises(SystemExit) as exit:
        main(["control-plane", *args])
    assert exit.value.code == 1


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("createsuperuser", "create-admin"),
        ("import", "import-inventory"),
        ("sync", "sync-node"),
    ],
)
def test_action_aliases(alias, canonical):
    assert normalize_action(alias) == canonical
    assert normalize_action(canonical) == canonical


@pytest.mark.parametrize(
    "action",
    [action for action in get_args(Action) if action not in {"serve", "health"}],
)
def test_runner_normalizes_actions_before_django_dispatch(monkeypatch, action):
    import django

    calls = []
    settings = Settings(
        config_path=Path("/fixture/runtime.yaml"),
        control_plane=ControlPlaneSettings(enabled=True, secret_key="fixture-key-" * 6),
    )
    for name in ("SBM_CONFIG", "DJANGO_SETTINGS_MODULE", "PYTHONDONTWRITEBYTECODE"):
        monkeypatch.setenv(name, "fixture-original-value")
    monkeypatch.setattr(django, "setup", lambda: None)
    monkeypatch.setattr(
        runner,
        "_run_django_action",
        lambda action, runtime: calls.append((action, runtime)),
    )

    run(action, settings)

    assert calls == [(normalize_action(action), settings)]


@pytest.mark.parametrize("safe", [True, False])
def test_cli_reports_safe_sync_errors_but_hides_backend_details(tmp_path, monkeypatch, capsys, safe):
    path = tmp_path / "runtime.yaml"
    path.write_text("sing_box_version: 1.14.2\ncontrol_plane:\n  enabled: true\n")
    message = "Node start and service recovery failed." if safe else "fixture-backend-secret"

    def fail(*_):
        raise (runner.ControlPlaneError if safe else RuntimeError)(message)

    monkeypatch.setattr(runner, "run", fail)
    with pytest.raises(SystemExit) as result:
        main(["--config", str(path), "control-plane", "sync"])
    assert result.value.code == 1
    errors = capsys.readouterr().err
    assert (message in errors) == safe
    if not safe:
        assert "Check the configuration and local database" in errors


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
    wrapper = next(
        event for event in events if event[:2] == ("write", deployment.WRAPPER_PATH)
    )
    assert wrapper[2] == deployment.wrapper_script("/opt/sing-box-manager")
    assert wrapper[3] == {"mode": "0755", "dry_run": False}
    commands = [event[1] for event in events if event[0] == "command"]
    assert "disable --now sing-box-manager-sync.path" in commands[0]
    assert "postgresql-client" in commands[1]
    assert "ON_ERROR_STOP=1" in commands[2]
    assert "sudo -u sbm" in commands[3]
    assert "update_nginx_site_text" in commands[-2]
    assert "disable --now sing-box-manager-web.service" in commands[-1]
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
    assert "systemctl restart" in commands[-4]


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
    assert "update_nginx_site_text" in commands[-2]
    assert "nginx -t" in commands[-2]
    assert "disable --now sing-box-manager-web.service" in commands[-1]


def test_update_nginx_site_text_routes_the_whole_portal_and_disables_without_fallback():
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
    assert "location ^~ /admin/ { return 404; }" in routed
    assert "limit_req zone=sbm_auth" in routed
    assert "8080" not in routed
    assert deployment.update_nginx_site_text(routed, 47071) == routed
    disabled = deployment.update_nginx_site_text(routed, None)
    assert "return 503" in disabled
    assert "proxy_pass" not in disabled
    assert deployment.update_nginx_site_text(disabled, 47071) == routed


@pytest.mark.parametrize("port", [None, 47071])
@pytest.mark.parametrize("failure", [None, "check", "reload"])
def test_nginx_route_changes_restore_site_on_check_or_reload_failure(tmp_path, port, failure):
    python = tmp_path / deployment.REMOTE_PIXI_PYTHON_RELATIVE_PATH
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    site = tmp_path / "nginx-site"
    original = "server {\n    location / {\n    }\n}\n"
    if port is None:
        original = deployment.update_nginx_site_text(original, 47071)
    site.write_text(original)
    site.chmod(0o640)
    for name, phase in (("nginx", "check"), ("systemctl", "reload")):
        script = tmp_path / name
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$FAIL_PHASE" = {phase} ] && [ ! -f "$FAILED_ONCE" ]; then\n'
            '  touch "$FAILED_ONCE"; exit 1\nfi\nexit 0\n'
        )
        script.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", deployment.nginx_route_script(str(tmp_path), str(site), port)],
        env={
            "PATH": f"{tmp_path}:{os.defpath}",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "FAIL_PHASE": failure or "",
            "FAILED_ONCE": str(tmp_path / "failed"),
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == (1 if failure else 0), result.stderr
    assert site.read_text() == (original if failure else deployment.update_nginx_site_text(original, port))
    assert site.stat().st_mode & 0o777 == 0o640


def test_pause_node_sync_disables_watcher_and_waits_without_killing_sync(tmp_path, monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.pause_node_sync("fixture-host")
    command = events[0][1]
    assert "timeout 60" in command
    assert f"stop {deployment.SYNC_SERVICE_NAME}" not in command
    for name, content in {
        "sudo": 'exec "$@"',
        "sleep": "exit 0",
        "systemctl": (
            'echo "$*" >> "$CALLS"\n'
            'if [ "$1" = show ]; then\n'
            '  if [ -f "$WAITED" ]; then echo inactive; else touch "$WAITED"; echo activating; fi\n'
            "fi"
        ),
    }.items():
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\n{content}\n")
        path.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["sh", "-c", command],
        env={"PATH": f"{tmp_path}:{os.defpath}", "CALLS": str(calls), "WAITED": str(tmp_path / "waited")},
        capture_output=True, text=True, check=False, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert lines.index(f"disable --now {deployment.SYNC_PATH_NAME}") < lines.index(f"show -p ActiveState --value {deployment.SYNC_SERVICE_NAME}")
    assert lines.count(f"show -p ActiveState --value {deployment.SYNC_SERVICE_NAME}") == 2


def test_disabled_deployment_only_disables_its_unit_and_retains_database(monkeypatch):
    events = record_deployment(monkeypatch)
    deployment.deploy("fixture-host", "/opt/sing-box-manager", Settings())
    commands = [event[1] for event in events]
    assert len(commands) == 4
    assert "disable --now sing-box-manager-sync.path" in commands[0]
    assert "update_nginx_site_text" in commands[1]
    assert "nginx -t" in commands[1]
    assert "disable --now sing-box-manager-control-plane.service" in commands[2]
    assert "disable --now sing-box-manager-web.service" in commands[3]
    assert all("postgres" not in command for command in commands)


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


@pytest.mark.parametrize(
    "config_override", [None, "", "/fixture/alternate config.yaml"]
)
def test_wrapper_executes_in_deploy_root_and_preserves_config(
    tmp_path, config_override
):
    root = tmp_path / 'manager \'with spaces $and "quotes"'
    python = root / ".pixi/envs/default/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'config': os.environ['SBM_CONFIG'], "
        "'bytecode': os.environ['PYTHONDONTWRITEBYTECODE'], 'pid': os.getpid()}))\n"
        "sys.exit(7)\n"
    )
    python.chmod(0o755)
    wrapper = tmp_path / "sbm"
    wrapper.write_text(deployment.wrapper_script(str(root)))
    wrapper.chmod(0o755)
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "0"}
    if config_override is not None:
        env["SBM_CONFIG"] = config_override
    args = ["control-plane", "createsuperuser", "a b", "*", "$(exit 99)"]

    process = subprocess.Popen(
        [str(wrapper), *args],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 7, stderr
    assert json.loads(stdout) == {
        "argv": ["-m", "sing_box_manager", *args],
        "cwd": str(root),
        "config": str(root / "config/inventory/runtime.yaml")
        if config_override is None
        else config_override,
        "bytecode": "1",
        "pid": process.pid,
    }
