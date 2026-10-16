"""Managed deployment and publication tests. SSH and services are mocked."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from sing_box_manager import deploy, remote_config
from sing_box_manager.deploy import DeploymentError, DeployOptions
from sing_box_manager.managed_access import deployment, storage
from sing_box_manager.settings import Settings
from tests.test_deploy import _deploy, _Recorder, _runtime_inventory


def test_changed_collector_listener_is_checked_after_node_application(monkeypatch):
    from sing_box_manager.control_plane import deployment as private

    events = []
    settings = Settings.from_dict(
        {
            **_runtime_inventory().model_dump(mode="json"),
            "sing_box_version": "1.14.2",
            "traffic_stats": {"enabled": True, "api_listen": "127.0.0.1:19181"},
        }
    )
    remote = SimpleNamespace(
        hostname="synthetic-host", root="/synthetic", server_dir="/synthetic/server"
    )

    def prepare(*args, **kwargs):
        events.append("private")

    def apply(*args, **kwargs):
        assert events == ["private"]
        events.append("node")

    def collect(remote, settings, options):
        assert settings.traffic_stats.api_listen == "127.0.0.1:19181"
        assert events == ["private", "node"], "new collector used the old node"
        events.append("collect")

    monkeypatch.setattr(private, "deploy", prepare)
    monkeypatch.setattr(deployment, "apply_node", apply)
    monkeypatch.setattr(deploy, "_verify_traffic_stats_collector", collect)
    monkeypatch.setattr(
        deployment, "finish", lambda *args, **kwargs: events.append("public")
    )
    deploy._finish_services(
        remote,
        settings,
        DeployOptions(managed_accounts=True, acknowledge_interrupt=True),
    )
    assert events == ["private", "node", "collect", "public"]


def test_managed_deploy_never_installs_inventory_access(tmp_path, monkeypatch):
    recorder = _deploy(
        tmp_path,
        monkeypatch,
        options=DeployOptions(managed_accounts=True, acknowledge_interrupt=True),
    )
    commands = recorder.remote_commands
    assert "--require-active" in commands[1]
    assert not any("./server-install.sh" in command for command in commands)
    assert not any(
        "mv -f" in command and "auth-users.json" in command for command in commands
    )
    assert not any(
        "enable --now sing-box-manager-web.service" in command for command in commands
    )
    assert any(
        "node-apply-current" in command and "--acknowledge-interrupt" in command
        for command in commands
    )
    assert any(
        "remote_config" in command and "--managed" in command for command in commands
    )
    assert any(
        "systemctl restart sing-box-manager-portal.service" in command
        for command in commands
    )
    assert any("managed-access --action health" in command for command in commands)
    assert recorder.index_of(
        "managed-access --action node-apply-current"
    ) < recorder.index_of("systemctl restart sing-box-manager-portal.service")
    assert not any(
        "prepare" in command
        and "managed-access" in command
        or "--action activate" in command
        for command in commands
    )
    assert not any(
        "auth-users.json.tmp" in command[-1] for command in recorder.rsync_commands
    )
    units = b"\n".join(recorder.inputs)
    for restriction in (
        b"CPUQuota=50%",
        b"MemoryMax=384M",
        b"ProtectSystem=strict",
        b"NoNewPrivileges=true",
    ):
        assert restriction in units


@pytest.mark.parametrize(
    "options",
    [
        DeployOptions(managed_accounts=True),
        DeployOptions(
            managed_accounts=True, acknowledge_interrupt=True, skip_code=True
        ),
        DeployOptions(
            managed_accounts=True, acknowledge_interrupt=True, bootstrap=True
        ),
    ],
)
def test_managed_deploy_requires_explicit_supported_mode(
    tmp_path, monkeypatch, options
):
    recorder = _Recorder()
    with pytest.raises(DeploymentError):
        _deploy(tmp_path, monkeypatch, options=options, recorder=recorder)
    assert recorder.commands == []


def test_ownership_guard_failure_precedes_every_mutation(tmp_path, monkeypatch):
    def reject(*args, **kwargs):
        raise DeploymentError("synthetic ownership rejection")

    recorder = _Recorder()
    monkeypatch.setattr(deployment, "_ssh", reject)
    with pytest.raises(DeploymentError, match="ownership rejection"):
        _deploy(tmp_path, monkeypatch, recorder=recorder)
    assert not recorder.commands


def test_dry_run_checks_ownership_but_does_not_run_node_or_service_commands(
    tmp_path, monkeypatch
):
    recorder = _deploy(
        tmp_path,
        monkeypatch,
        options=DeployOptions(managed_accounts=True, dry_run=True),
    )
    for command in recorder.remote_commands:
        assert "--action node-apply-current" not in command
        assert "systemctl restart" not in command
        assert "systemctl enable" not in command
        assert "remote_config" not in command
    assert any("--require-active" in command for command in recorder.remote_commands)


def config_pair(tmp_path, monkeypatch):
    marker = tmp_path / "owner.json"
    monkeypatch.setattr(storage, "FENCE", marker)
    current = Settings.from_dict(
        {
            **_runtime_inventory().model_dump(mode="json"),
            "sing_box_version": "1.14.2",
            "control_plane": {
                "enabled": True,
                "portal_enabled": True,
                "portal_origin": "https://vpn.example.test",
                "secret_key": "synthetic-django-key-" + "d" * 48,
                "subscription_secret": "synthetic-managed-key-" + "m" * 48,
            },
            "trojan": {
                "users": [
                    {
                        "username": "retained",
                        "password": "synthetic-retained-protocol",
                        "enabled": True,
                    }
                ]
            },
        }
    ).model_dump(mode="json")
    incoming = json.loads(json.dumps(current))
    incoming["trojan"]["users"][0]["password"] = (
        "synthetic-inventory-value-must-not-win"
    )
    incoming["control_plane"]["subscription_secret"] = (
        "synthetic-new-key-must-not-rotate-" + "n" * 48
    )
    paths = []
    for name, value in [("target.yaml", current), ("staged.yaml", incoming)]:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(value))
        path.chmod(0o600)
        paths.append(path)
    return marker, paths[0], paths[1], current


def test_managed_runtime_preserves_key_binding_and_frozen_roster(tmp_path, monkeypatch):
    marker, target, staged, current = config_pair(tmp_path, monkeypatch)
    marker.write_text("synthetic fence")
    remote_config.publish_runtime_config(staged, target, tmp_path, managed=True)
    result = yaml.safe_load(target.read_text())
    assert (
        result["control_plane"]["subscription_secret"]
        == current["control_plane"]["subscription_secret"]
    )
    assert result["trojan"] == current["trojan"]
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change", ["ordinary", "missing-fence", "database", "disabled"]
)
def test_runtime_publication_cannot_bypass_ownership(tmp_path, monkeypatch, change):
    marker, target, staged, current = config_pair(tmp_path, monkeypatch)
    if change != "missing-fence":
        marker.write_text("synthetic fence")
    if change in {"database", "disabled"}:
        incoming = yaml.safe_load(staged.read_text())
        incoming["control_plane"][
            "database_name" if change == "database" else "portal_enabled"
        ] = "sbm_other" if change == "database" else False
        staged.write_text(yaml.safe_dump(incoming))
    original = target.read_bytes()
    with pytest.raises(ValueError):
        remote_config.publish_runtime_config(
            staged, target, tmp_path, managed=change != "ordinary"
        )
    assert target.read_bytes() == original
    assert staged.exists()


def test_nginx_switch_preserves_tls_and_restores_rejected_file(tmp_path, monkeypatch):
    site = tmp_path / "site.conf"
    original = "server {\n    listen 443 ssl;\n    ssl_certificate /synthetic/fullchain.pem;\n    ssl_certificate_key /synthetic/key.pem;\n    access_log /synthetic/access.log old_format;\n    location / {\n        proxy_pass http://127.0.0.1:47070;\n    }\n}\n"
    site.write_text(original)
    site.chmod(0o644)
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    monkeypatch.setattr(deployment, "NGINX_SITE_PATH", str(site))
    monkeypatch.setattr(deployment, "RUNS", str(runs))
    real_stat = Path.stat

    def checked_stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if path in (site, runs):
            fields = list(value)
            fields[4] = 0
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "stat", checked_stat)
    monkeypatch.setattr(os, "fchown", lambda *args: None)
    calls = []

    def process(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", process)
    exec(
        compile(
            deployment.nginx_switch_script(47070, 47072),
            "synthetic-nginx-switch",
            "exec",
        ),
        {},
    )
    result = site.read_text()
    assert "ssl_certificate /synthetic/fullchain.pem" in result
    assert "ssl_certificate_key /synthetic/key.pem" in result
    assert "proxy_pass http://127.0.0.1:47072;" in result
    assert "access_log off;" in result and "error_log /dev/null;" in result
    assert "old_format" not in result
    assert calls == [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
    assert len(list(runs.iterdir())) == 1

    def failure(argv, **kwargs):
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", failure)
    with pytest.raises(SystemExit, match="validation failed"):
        exec(
            compile(
                deployment.nginx_switch_script(47070, 47072),
                "synthetic-nginx-switch",
                "exec",
            ),
            {},
        )
    assert site.read_text() == result
    assert len(list(runs.iterdir())) == 2
