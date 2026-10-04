"""Tests for bootstrap provisioning."""

import subprocess
from pathlib import Path

import pytest
import yaml

from sing_box_manager.config_loader import RuntimeInventory
from sing_box_manager.deploy import DeploymentError
from sing_box_manager.provision import (
    REMOTE_INVENTORY_FILENAME,
    SERVICE_USER,
    SSH_PORTS_ALLOW_COMMAND,
    _certbot_service_unit,
    _certbot_timer_unit,
    _derive_remote_config,
    _traffic_stats_service_unit,
    _traffic_stats_stream_unit,
    _traffic_stats_timer_unit,
    _web_service_unit,
    install_managed_units,
    provision_remote,
)
from sing_box_manager.settings import (
    ClientUpgradeSettings,
    Settings,
    TrafficStatsSettings,
    WebSettings,
)

HOST = "vpn-host"
REMOTE_ROOT = "/opt/sing-box-manager"


def _settings(*, traffic_stats_enabled: bool = True) -> Settings:
    return Settings(
        sing_box_version="1.13.15",
        traffic_stats=TrafficStatsSettings(enabled=traffic_stats_enabled),
    )


def _inventory_data() -> dict:
    return {
        "deployment": {
            "host": "vpn.example.com",
            "ip": "1.2.3.4",
            "trojan_port": 8443,
            "hysteria2_port": 4443,
            "naive_port": 9443,
            "tls": {
                "enabled": True,
                "key_path": "/etc/letsencrypt/live/vpn.example.com/privkey.pem",
                "certificate_path": "/etc/letsencrypt/live/vpn.example.com/fullchain.pem",
                "self_signed_cert": False,
            },
        },
        "web_portal": {
            "users": [
                {
                    "username": "alice",
                    "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQxMjM0NTY$c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5",
                }
            ]
        },
        "trojan": {
            "users": [{"username": "alice", "password": "change-me", "enabled": True}]
        },
        "hysteria2": {
            "obfs_password": "change-me-obfs",
            "users": [{"username": "alice", "password": "change-me", "enabled": True}],
        },
        "naive": {
            "users": [{"username": "alice", "password": "change-me", "enabled": True}]
        },
    }


def _inventory() -> RuntimeInventory:
    return RuntimeInventory.model_validate(_inventory_data())


def _inventory_with_port_hopping() -> RuntimeInventory:
    data = _inventory_data()
    data["deployment"]["hysteria2_port_range"] = "20000:50000"
    return RuntimeInventory.model_validate(data)


class _Recorder:
    """Records subprocess.run calls for assertion."""

    def __init__(
        self,
        *,
        os_id: str = "ubuntu",
        existing_files: set[str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self._os_id = os_id
        self._existing_files = existing_files or set()

    def __call__(self, command, **kwargs):
        self.commands.append(command)

        if command[0] != "ssh":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        remote_cmd = command[-1]

        if "grep ^ID=" in remote_cmd:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{self._os_id}\n", stderr=""
            )

        if remote_cmd.startswith("test -f "):
            path = remote_cmd.split("test -f ", 1)[1].strip("'\"")
            returncode = 0 if path in self._existing_files else 1
            return subprocess.CompletedProcess(
                command, returncode, stdout="", stderr=""
            )

        if kwargs.get("input") is not None:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @property
    def ssh_commands(self) -> list[str]:
        return [cmd[-1] for cmd in self.commands if cmd[0] == "ssh"]


# ---------------------------------------------------------------------------
# Config derivation tests
# ---------------------------------------------------------------------------


class TestDeriveRemoteConfig:
    def test_contains_sing_box_version(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert config["sing_box_version"] == "1.13.15"

    def test_does_not_contain_inventory_path(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert "inventory_path" not in config

    def test_generates_a_session_secret(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        secret = config["web"]["session_secret"]
        assert len(secret) == 64
        assert all(c in "0123456789abcdef" for c in secret)

    def test_uses_the_configured_session_secret(self):
        settings = Settings(
            sing_box_version="1.13.15",
            web=WebSettings(session_secret="configured-secret"),
        )

        config = yaml.safe_load(_derive_remote_config(settings, _inventory()))

        assert config["web"]["session_secret"] == "configured-secret"

    def test_session_secrets_differ_between_calls(self):
        a = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        b = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert a["web"]["session_secret"] != b["web"]["session_secret"]

    def test_generates_a_separate_subscription_secret(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        secret = config["web"]["subscription_secret"]
        assert len(secret) == 64
        assert all(c in "0123456789abcdef" for c in secret)
        assert secret != config["web"]["session_secret"]

    def test_the_client_signing_key_never_reaches_the_server(self):
        settings = Settings(sing_box_version="1.13.15", client_signing_key="SECRET-KEY")

        config = _derive_remote_config(settings, _inventory())

        assert "SECRET-KEY" not in config
        assert "client_signing_key" not in config

    def test_uses_the_configured_subscription_secret(self):
        settings = Settings(
            sing_box_version="1.13.15",
            web=WebSettings(subscription_secret="configured-secret"),
        )

        config = yaml.safe_load(_derive_remote_config(settings, _inventory()))

        assert config["web"]["subscription_secret"] == "configured-secret"

    def test_includes_deployment_host_in_allowed_hosts(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert "vpn.example.com" in config["web"]["allowed_hosts"]

    def test_web_binds_to_loopback(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert config["web"]["host"] == "127.0.0.1"

    def test_carries_traffic_stats_settings(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert config["traffic_stats"]["enabled"] is True
        assert config["traffic_stats"]["timezone"] == "Asia/Shanghai"

    def test_carries_every_traffic_stats_field(self):
        """A dropped field is invisible: the remote just takes its default.

        The inventory has no `traffic_stats` section to merge on top, so this
        block is the only thing that carries the operator's settings across.
        `relay_multiplier` shipped once without being here, which silently
        doubled every user's reported plan usage on hosts that had set it to 1.
        """
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))

        assert set(config["traffic_stats"]) == set(TrafficStatsSettings.model_fields)

    def test_carries_the_relay_multiplier(self):
        settings = Settings(
            sing_box_version="1.13.15",
            traffic_stats=TrafficStatsSettings(relay_multiplier=1.0),
        )

        config = yaml.safe_load(_derive_remote_config(settings, _inventory()))

        assert config["traffic_stats"]["relay_multiplier"] == 1.0

    def test_serializes_the_database_path_as_a_string(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))

        assert isinstance(config["traffic_stats"]["database_path"], str)

    def test_carries_client_upgrade_settings(self):
        settings = Settings(
            sing_box_version="1.13.15",
            client_upgrade=ClientUpgradeSettings(
                policy="required", message="Routing format changed"
            ),
        )

        config = yaml.safe_load(_derive_remote_config(settings, _inventory()))

        assert config["client_upgrade"] == {
            "policy": "required",
            "message": "Routing format changed",
        }

    def test_includes_inventory_data(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert config["deployment"]["host"] == "vpn.example.com"
        assert config["trojan"]["users"][0]["username"] == "alice"

    def test_includes_vps_info_credentials(self):
        data = _inventory_data()
        data["vps_info"] = {
            "kiwi_veid": "test-veid",
            "kiwi_api_key": "test-api-key",
        }
        inventory = RuntimeInventory.model_validate(data)

        config = yaml.safe_load(_derive_remote_config(_settings(), inventory))

        assert config["vps_info"] == {
            "kiwi_veid": "test-veid",
            "kiwi_api_key": "test-api-key",
        }

    def test_excludes_none_values(self):
        config = yaml.safe_load(_derive_remote_config(_settings(), _inventory()))
        assert "hysteria2_port_range" not in yaml.dump(config)


# ---------------------------------------------------------------------------
# Systemd unit tests
# ---------------------------------------------------------------------------


class TestSystemdUnits:
    def test_web_service_uses_remote_root(self):
        unit = _web_service_unit("/srv/sbm")
        assert "WorkingDirectory=/srv/sbm" in unit
        assert f"User={SERVICE_USER}" in unit

    def test_traffic_stats_service_uses_remote_root(self):
        unit = _traffic_stats_service_unit("/srv/sbm")
        assert "WorkingDirectory=/srv/sbm" in unit
        assert "stats-collect" in unit

    def test_units_declare_a_state_directory_outside_the_deploy_root(self):
        """The deploy root is disposable; the usage database must outlive it.

        systemd creates /var/lib/sing-box-manager owned by the service user and
        leaves it alone across restarts, so wiping and reprovisioning /opt does
        not take the traffic history with it.
        """
        for unit in (
            _web_service_unit("/opt/sing-box-manager"),
            _traffic_stats_service_unit("/opt/sing-box-manager"),
            _traffic_stats_stream_unit("/opt/sing-box-manager"),
        ):
            assert "StateDirectory=sing-box-manager" in unit
            assert "StateDirectoryMode=0750" in unit

    def test_the_oneshot_collector_has_no_install_section(self):
        """Its timer is what is enabled, so the unit has nothing to be wanted by."""
        assert "[Install]" not in _traffic_stats_service_unit("/srv/sbm")
        assert "[Install]" in _traffic_stats_stream_unit("/srv/sbm")

    def test_traffic_stats_timer_runs_every_5_minutes(self):
        unit = _traffic_stats_timer_unit()
        assert "OnCalendar=*:0/5" in unit

    def test_traffic_stream_unit_is_long_running_and_always_restarted(self):
        """sing-box drops connection events while nobody is subscribed.

        A oneshot or an on-failure restart would silently lose traffic for the
        whole window the daemon is down, so this one has to stay up.
        """
        unit = _traffic_stats_stream_unit("/srv/sbm")

        assert "Type=simple" in unit
        assert "Restart=always" in unit
        assert "stats-stream" in unit
        assert "WorkingDirectory=/srv/sbm" in unit
        assert "StateDirectory=sing-box-manager" in unit

    def test_traffic_stream_unit_starts_after_every_protocol_service(self):
        unit = _traffic_stats_stream_unit("/srv/sbm")

        assert "sing-box.service" in unit

    def test_certbot_service_restarts_protocol_services(self):
        unit = _certbot_service_unit()
        assert "try-restart" in unit
        assert "try-restart sing-box.service" in unit

    def test_certbot_timer_runs_twice_daily(self):
        unit = _certbot_timer_unit()
        assert "00,12:00:00" in unit


# ---------------------------------------------------------------------------
# Provision flow tests
# ---------------------------------------------------------------------------


class TestProvisionRemote:
    def test_rejects_non_ubuntu_debian(self, monkeypatch):
        recorder = _Recorder(os_id="centos")
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        with pytest.raises(DeploymentError, match="Ubuntu or Debian"):
            provision_remote(
                hostname=HOST,
                remote_root=REMOTE_ROOT,
                settings=_settings(),
                inventory=_inventory(),
            )

    def test_creates_service_user_and_directory(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        assert any("useradd --system" in cmd for cmd in recorder.ssh_commands)
        assert any(
            "mkdir -p" in cmd and REMOTE_ROOT in cmd for cmd in recorder.ssh_commands
        )

    def test_installs_apt_packages(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        assert any(
            "apt-get install" in cmd and "nginx" in cmd and "certbot" in cmd
            for cmd in recorder.ssh_commands
        )

    def test_skips_existing_config_file(self, monkeypatch):
        existing = {
            f"{REMOTE_ROOT}/config/inventory/{REMOTE_INVENTORY_FILENAME}",
        }
        recorder = _Recorder(existing_files=existing)
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert not any(REMOTE_INVENTORY_FILENAME in cmd for cmd in tee_commands)

    def test_ships_config_when_absent(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert any(REMOTE_INVENTORY_FILENAME in cmd for cmd in tee_commands)

    def test_configures_ufw_with_protocol_ports(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        ufw_commands = [cmd for cmd in recorder.ssh_commands if "ufw allow" in cmd]
        ports_opened = " ".join(ufw_commands)
        assert "80/tcp" in ports_opened
        assert "443/tcp" in ports_opened
        assert "8443/tcp" in ports_opened
        assert "4443/udp" in ports_opened
        assert "9443/tcp" in ports_opened

    def test_enables_ufw(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        assert any("ufw --force enable" in cmd for cmd in recorder.ssh_commands)

    def test_allows_the_ssh_ports_before_enabling_ufw(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        [command] = [
            cmd for cmd in recorder.ssh_commands if "ufw --force enable" in cmd
        ]
        assert command.index(SSH_PORTS_ALLOW_COMMAND) < command.index(
            "ufw --force enable"
        )
        assert "22/tcp" not in command

    def test_installs_nat_redirect_for_port_hopping(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory_with_port_hopping(),
        )

        ufw_commands = [cmd for cmd in recorder.ssh_commands if "ufw allow" in cmd]
        assert any("20000:50000/udp" in cmd for cmd in ufw_commands)

        nat_commands = [
            cmd
            for cmd in recorder.ssh_commands
            if "PREROUTING" in cmd and "REDIRECT" in cmd
        ]
        assert len(nat_commands) == 1  # batched into one SSH call
        assert "before.rules" in nat_commands[0]
        assert "before6.rules" in nat_commands[0]

    def test_no_nat_redirect_without_port_hopping(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        nat_commands = [cmd for cmd in recorder.ssh_commands if "PREROUTING" in cmd]
        assert len(nat_commands) == 0

    def test_installs_nginx_site(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        assert any("nginx -t" in cmd for cmd in recorder.ssh_commands)
        assert any("systemctl reload nginx" in cmd for cmd in recorder.ssh_commands)

    def test_runs_certbot_with_email(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
            certbot_email="admin@example.com",
        )

        certbot_commands = [cmd for cmd in recorder.ssh_commands if "certbot" in cmd]
        assert any(
            "--email" in cmd and "admin@example.com" in cmd for cmd in certbot_commands
        )

    def test_runs_certbot_without_email(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
        )

        certbot_commands = [cmd for cmd in recorder.ssh_commands if "certbot" in cmd]
        assert any(
            "--register-unsafely-without-email" in cmd for cmd in certbot_commands
        )

    def test_dry_run_issues_no_real_commands(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        provision_remote(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
            inventory=_inventory(),
            dry_run=True,
        )

        # Only the distro check should be a real SSH call
        real_ssh = [
            cmd for cmd in recorder.commands if cmd[0] == "ssh" and "-t" not in cmd
        ]
        # The distro check uses _ssh_stdout (no -t)
        assert len(real_ssh) == 1
        assert "grep ^ID=" in real_ssh[0][-1]


class TestInstallManagedUnits:
    def test_installs_web_service(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert any("sing-box-manager-web.service" in cmd for cmd in tee_commands)

    def test_installs_traffic_stats_when_enabled(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        settings = _settings()
        assert settings.traffic_stats.enabled is True

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=settings,
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert any("traffic-stats.service" in cmd for cmd in tee_commands)
        assert any("traffic-stats.timer" in cmd for cmd in tee_commands)
        assert any("traffic-stream.service" in cmd for cmd in tee_commands)

    def test_enables_the_traffic_stream_daemon(self, monkeypatch):
        """Installing the unit is not enough; nothing else ever starts it."""
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )

        enable_commands = [
            cmd for cmd in recorder.ssh_commands if "systemctl enable" in cmd
        ]
        assert any(
            "sing-box-manager-traffic-stream.service" in cmd for cmd in enable_commands
        )

    def test_skips_traffic_stats_when_disabled(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(traffic_stats_enabled=False),
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert not any("traffic-stats" in cmd for cmd in tee_commands)

    def test_installs_certbot_renewal_timer(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )

        tee_commands = [cmd for cmd in recorder.ssh_commands if "tee" in cmd]
        assert any("certbot-renew.service" in cmd for cmd in tee_commands)
        assert any("certbot-renew.timer" in cmd for cmd in tee_commands)

    def test_enables_services(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )

        enable_commands = [
            cmd for cmd in recorder.ssh_commands if "systemctl enable --now" in cmd
        ]
        enabled_units = " ".join(enable_commands)
        assert "sing-box-manager-web.service" in enabled_units
        assert "certbot-renew.timer" in enabled_units

    def test_reloads_systemd_daemon(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

        install_managed_units(
            hostname=HOST,
            remote_root=REMOTE_ROOT,
            settings=_settings(),
        )

        assert any("systemctl daemon-reload" in cmd for cmd in recorder.ssh_commands)


@pytest.mark.parametrize(
    "filename,render",
    [
        ("sing-box-manager-web.service", _web_service_unit),
        ("sing-box-manager-traffic-stats.service", _traffic_stats_service_unit),
        ("sing-box-manager-traffic-stream.service", _traffic_stats_stream_unit),
    ],
)
def test_example_and_generated_units_select_the_same_runtime_config(filename, render):
    import shlex

    example = Path(__file__).parent.parent / "docs" / "examples" / "systemd" / filename
    for unit in (example.read_text(), render("/opt/sing-box-manager")):
        command = next(
            line.removeprefix("ExecStart=")
            for line in unit.splitlines()
            if line.startswith("ExecStart=")
        )
        args = shlex.split(command)
        assert args[args.index("--config") + 1] == "config/inventory/runtime.yaml"


def _run_ssh_allow(tmp_path: Path, *, sshd_output: str | None, connection: str | None):
    """Run the allow fragment in a real shell, with sudo, sshd and ufw faked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ufw.log"
    fakes = {
        "sudo": 'exec "$@"',
        "ufw": f'printf "%s\\n" "$*" >> {log}',
        "sshd": "exit 1" if sshd_output is None else f"printf '{sshd_output}'",
    }
    for name, body in fakes.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if connection is not None:
        env["SSH_CONNECTION"] = connection
    subprocess.run(["sh", "-c", SSH_PORTS_ALLOW_COMMAND], env=env, check=True)
    return log.read_text().splitlines()


def test_ssh_allow_opens_every_port_sshd_listens_on(tmp_path):
    allowed = _run_ssh_allow(
        tmp_path,
        sshd_output="port 27484\\nport 2222\\nlistenaddress 0.0.0.0\\n",
        connection="203.0.113.9 51234 198.51.100.7 27484",
    )

    assert allowed == ["allow 2222/tcp comment SSH", "allow 27484/tcp comment SSH"]


def test_ssh_allow_falls_back_to_the_current_session_port(tmp_path):
    allowed = _run_ssh_allow(
        tmp_path,
        sshd_output=None,
        connection="203.0.113.9 51234 198.51.100.7 27484",
    )

    assert allowed == ["allow 27484/tcp comment SSH"]


def test_ssh_allow_falls_back_to_port_22(tmp_path):
    allowed = _run_ssh_allow(tmp_path, sshd_output=None, connection=None)

    assert allowed == ["allow 22/tcp comment SSH"]


@pytest.mark.parametrize(
    "render",
    [
        _web_service_unit,
        _traffic_stats_service_unit,
        _traffic_stats_stream_unit,
    ],
)
def test_units_run_the_environment_interpreter(render):
    """The service account cannot write the tree, so nothing may need to."""
    unit = render("/opt/sing-box-manager")
    command = next(line for line in unit.splitlines() if line.startswith("ExecStart="))

    assert command.startswith(
        "ExecStart=/opt/sing-box-manager/.pixi/envs/default/bin/python "
        "-m sing_box_manager "
    )
    assert "pixi run" not in unit


def test_project_root_is_root_owned_and_read_by_the_service(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("sing_box_manager.provision.subprocess.run", recorder)

    provision_remote(
        hostname=HOST,
        remote_root=REMOTE_ROOT,
        settings=_settings(),
        inventory=_inventory(),
    )

    commands = " ".join(recorder.ssh_commands)
    assert f"chown root:sbm {REMOTE_ROOT}" in commands
    assert f"chmod 0750 {REMOTE_ROOT}" in commands
    assert "sbm:sbm" not in commands


CERTBOT_SITE = """server {
    server_name vpn.example.com;
    location / { proxy_pass http://127.0.0.1:47070; }
    listen 443 ssl; # managed by Certbot
}
server {
    listen 80;
    server_name vpn.example.com;
    return 404; # managed by Certbot
}
"""


def _run_nginx_redaction(tmp_path, *, nginx_test_passes: bool, old_conf: str | None):
    """Run the redaction script in a real shell with nginx and systemctl faked."""
    from sing_box_manager.provision import NGINX_LOG_CONFIG, nginx_log_redaction_script

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    for name, body in {
        "nginx": f'echo "nginx $*" >> {calls}; exit {0 if nginx_test_passes else 1}',
        "systemctl": f'echo "systemctl $*" >> {calls}',
    }.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    site = tmp_path / "site.conf"
    conf = tmp_path / "log.conf"
    if not site.exists():
        site.write_text(CERTBOT_SITE)
    if old_conf is not None:
        conf.write_text(old_conf)
    (tmp_path / "log.conf.new").write_text(NGINX_LOG_CONFIG)
    result = subprocess.run(
        ["sh", "-c", nginx_log_redaction_script(str(site), str(conf))],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return result, site, conf, calls


def test_nginx_redaction_adds_the_log_to_each_server_block_once(tmp_path):
    from sing_box_manager.provision import NGINX_LOG_CONFIG

    for _ in range(2):
        result, site, conf, calls = _run_nginx_redaction(
            tmp_path, nginx_test_passes=True, old_conf=None
        )
        assert result.returncode == 0, result.stderr

    lines = site.read_text().splitlines()
    log_line = "    access_log /var/log/nginx/access.log sbm_redacted;"
    assert lines.count(log_line) == 2
    for index, line in enumerate(lines):
        if line.strip().startswith("server_name "):
            assert lines[index + 1] == log_line
    assert conf.read_text() == NGINX_LOG_CONFIG
    assert (
        calls.read_text().splitlines()
        == [
            "nginx -t",
            "systemctl reload nginx",
        ]
        * 2
    )


@pytest.mark.parametrize("old_conf", [None, "# the previous log format\n"])
def test_nginx_redaction_restores_both_files_when_the_test_fails(tmp_path, old_conf):
    result, site, conf, calls = _run_nginx_redaction(
        tmp_path, nginx_test_passes=False, old_conf=old_conf
    )

    assert result.returncode == 1
    assert site.read_text() == CERTBOT_SITE
    if old_conf is None:
        assert not conf.exists()
    else:
        assert conf.read_text() == old_conf
    assert calls.read_text().splitlines() == ["nginx -t"]
    assert not list(tmp_path.glob("tmp.*"))
