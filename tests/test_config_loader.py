"""Tests for runtime inventory and auth snapshot loading."""

import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from sing_box_manager import config_loader, runtime_env
from sing_box_manager.config_loader import (
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    TlsConfig,
    load_auth_snapshot,
    load_runtime_inventory,
    write_auth_snapshot,
)

VALID_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHQxMjM0NTY$"
    "c29tZWhhc2h2YWx1ZXNvbWVoaXN0b3J5"
)

DEFAULT_TLS_BLOCK = (
    "  tls:\n"
    "    enabled: true\n"
    "    key_path: /etc/letsencrypt/live/vpn.example.com/privkey.pem\n"
    "    certificate_path: /etc/letsencrypt/live/vpn.example.com/fullchain.pem\n"
)

SELF_SIGNED_TLS_BLOCK = (
    "  tls:\n"
    "    enabled: true\n"
    "    server_name: vpn.example.com\n"
    "    key_path: /etc/ssl/self-signed.key\n"
    "    certificate_path: /etc/ssl/self-signed.crt\n"
    "    self_signed_cert: true\n"
)


def _write_inventory(
    path: Path,
    web_portal_users: str,
    trojan_users: str,
    hysteria2_users: str,
    tls_block: str = DEFAULT_TLS_BLOCK,
    naive_users: str = "    - username: naive-user\n      password: naive-secret\n      enabled: true\n",
) -> None:
    path.write_text(
        "deployment:\n"
        "  host: vpn.example.com\n"
        "  ip: 203.0.113.10\n"
        "  trojan_port: 8443\n"
        "  hysteria2_port: 4443\n"
        "  naive_port: 9443\n"
        f"{tls_block}"
        "web_portal:\n"
        "  users:\n"
        f"{web_portal_users}"
        "trojan:\n"
        "  users:\n"
        f"{trojan_users}"
        "hysteria2:\n"
        "  obfs_password: hy2-obfs-secret\n"
        "  users:\n"
        f"{hysteria2_users}"
        "naive:\n"
        "  users:\n"
        f"{naive_users}",
        encoding="utf-8",
    )


class TestLoadRuntimeInventory:
    def test_loads_plaintext_yaml_inventory(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
        )

        inventory = load_runtime_inventory(inventory_file)

        assert inventory.deployment.host == "vpn.example.com"
        assert inventory.deployment.ip == "203.0.113.10"
        assert inventory.deployment.trojan_port == 8443
        assert inventory.deployment.hysteria2_port == 4443
        assert inventory.deployment.naive_port == 9443
        assert inventory.deployment.tls.enabled is True
        assert inventory.deployment.tls.server_name == "vpn.example.com"
        assert (
            inventory.deployment.tls.key_path
            == "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
        )
        assert (
            inventory.deployment.tls.certificate_path
            == "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
        )
        assert inventory.deployment.tls.self_signed_cert is False
        assert inventory.web_portal.users[0].username == "alice"
        assert inventory.web_portal.users[0].password_hash == VALID_PASSWORD_HASH
        assert inventory.trojan.users[0].password == "trojan-secret"
        assert inventory.hysteria2.users[0].password == "hysteria2-secret"
        assert inventory.hysteria2.obfs_password == "hy2-obfs-secret"
        assert inventory.naive.users[0].password == "naive-secret"

    def test_loads_sops_inventory_via_decrypt_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        inventory_file = tmp_path / "runtime.sops.yaml"
        inventory_file.write_text("encrypted", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], check: bool, capture_output: bool, text: bool):
            calls.append(cmd)
            assert check is True
            assert capture_output is True
            assert text is True
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=(
                    "deployment:\n"
                    "  host: vpn.example.com\n"
                    "  ip: 203.0.113.10\n"
                    "  trojan_port: 8443\n"
                    "  hysteria2_port: 4443\n"
                    "  naive_port: 9443\n"
                    f"{DEFAULT_TLS_BLOCK}"
                    "web_portal:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    f"      password_hash: {VALID_PASSWORD_HASH}\n"
                    "      enabled: true\n"
                    "trojan:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    "      password: trojan-secret\n"
                    "      enabled: true\n"
                    "hysteria2:\n"
                    "  obfs_password: hy2-obfs-secret\n"
                    "  users:\n"
                    "    - username: bob\n"
                    "      password: hysteria2-secret\n"
                    "      enabled: true\n"
                    "naive:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    "      password: naive-secret\n"
                    "      enabled: true\n"
                ),
            )

        monkeypatch.setattr("sing_box_manager.config_loader.subprocess.run", fake_run)

        inventory = load_runtime_inventory(inventory_file)

        assert calls == [["sops", "--decrypt", str(inventory_file)]]
        assert inventory.web_portal.users[0].username == "alice"
        assert inventory.hysteria2.users[0].username == "bob"
        assert inventory.deployment.tls.server_name == "vpn.example.com"
        assert (
            inventory.deployment.tls.key_path
            == "/etc/letsencrypt/live/vpn.example.com/privkey.pem"
        )
        assert (
            inventory.deployment.tls.certificate_path
            == "/etc/letsencrypt/live/vpn.example.com/fullchain.pem"
        )

    def test_loads_sops_age_key_from_repo_local_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        inventory_file = tmp_path / "runtime.sops.yaml"
        inventory_file.write_text("encrypted", encoding="utf-8")
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text(
            "SOPS_AGE_KEY=AGE-SECRET-KEY-LOCAL-DOTENV\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(runtime_env, "DEFAULT_DOTENV_PATH", dotenv_file)
        monkeypatch.delenv("SOPS_AGE_KEY", raising=False)

        def fake_run(cmd: list[str], check: bool, capture_output: bool, text: bool):
            assert cmd == ["sops", "--decrypt", str(inventory_file)]
            assert check is True
            assert capture_output is True
            assert text is True
            assert os.getenv("SOPS_AGE_KEY") == "AGE-SECRET-KEY-LOCAL-DOTENV"
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=(
                    "deployment:\n"
                    "  host: vpn.example.com\n"
                    "  ip: 203.0.113.10\n"
                    "  trojan_port: 8443\n"
                    "  hysteria2_port: 4443\n"
                    "  naive_port: 9443\n"
                    f"{DEFAULT_TLS_BLOCK}"
                    "web_portal:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    f"      password_hash: {VALID_PASSWORD_HASH}\n"
                    "      enabled: true\n"
                    "trojan:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    "      password: trojan-secret\n"
                    "      enabled: true\n"
                    "hysteria2:\n"
                    "  obfs_password: hy2-obfs-secret\n"
                    "  users:\n"
                    "    - username: bob\n"
                    "      password: hysteria2-secret\n"
                    "      enabled: true\n"
                    "naive:\n"
                    "  users:\n"
                    "    - username: alice\n"
                    "      password: naive-secret\n"
                    "      enabled: true\n"
                ),
            )

        monkeypatch.setattr("sing_box_manager.config_loader.subprocess.run", fake_run)

        inventory = load_runtime_inventory(inventory_file)

        assert inventory.web_portal.users[0].username == "alice"
        assert inventory.hysteria2.users[0].username == "bob"

    def test_rejects_duplicate_usernames_within_each_section(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n"
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: false\n",
            "    - username: trojan-user\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: hysteria2-user\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
        )

        with pytest.raises(ValidationError, match="Duplicate username"):
            load_runtime_inventory(inventory_file)

    def test_allows_same_username_across_different_sections(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            naive_users="    - username: alice\n"
            "      password: naive-secret\n"
            "      enabled: true\n",
        )

        inventory = load_runtime_inventory(inventory_file)

        assert inventory.web_portal.users[0].username == "alice"
        assert inventory.trojan.users[0].username == "alice"
        assert inventory.hysteria2.users[0].username == "alice"
        assert inventory.naive.users[0].username == "alice"

    def test_rejects_invalid_username(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: ../evil\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
        )

        with pytest.raises(ValidationError, match="username"):
            load_runtime_inventory(inventory_file)

    def test_rejects_invalid_password_hash(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            "      password_hash: not-an-argon2id-hash\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
        )

        with pytest.raises(ValidationError, match="password_hash"):
            load_runtime_inventory(inventory_file)

    def test_loads_self_signed_cert_flag_with_existing_tls_paths(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            tls_block=SELF_SIGNED_TLS_BLOCK,
        )

        inventory = load_runtime_inventory(inventory_file)

        assert inventory.deployment.tls.self_signed_cert is True
        assert inventory.deployment.tls.key_path == "/etc/ssl/self-signed.key"
        assert inventory.deployment.tls.certificate_path == "/etc/ssl/self-signed.crt"

    def test_rejects_unknown_inventory_keys(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        inventory_file.write_text(
            "deployment:\n"
            "  host: vpn.example.com\n"
            "  ip: 203.0.113.10\n"
            "  trojan_port: 8443\n"
            "  hysteria2_port: 4443\n"
            "  naive_port: 9443\n"
            "  tls:\n"
            "    enabled: true\n"
            "    server_name: vpn.example.com\n"
            "    cert_path: /etc/ssl/fullchain.pem\n"
            "    key_path: /etc/ssl/privkey.pem\n"
            "web_portal:\n"
            "  users:\n"
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n"
            "trojan:\n"
            "  users:\n"
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n"
            "hysteria2:\n"
            "  obfs_password: hy2-obfs-secret\n"
            "  users:\n"
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n"
            "naive:\n"
            "  users:\n"
            "    - username: alice\n"
            "      password: naive-secret\n"
            "      enabled: true\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="cert_path|key_path"):
            load_runtime_inventory(inventory_file)

    def test_requires_tls_server_name_when_self_signed_cert_is_true(
        self, tmp_path: Path
    ):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            tls_block=(
                "  tls:\n"
                "    enabled: true\n"
                "    key_path: /etc/ssl/privkey.pem\n"
                "    certificate_path: /etc/ssl/fullchain.pem\n"
                "    self_signed_cert: true\n"
            ),
        )

        with pytest.raises(ValidationError, match="server_name"):
            load_runtime_inventory(inventory_file)

    def test_rejects_mismatched_ca_signed_tls_server_name(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            tls_block=(
                "  tls:\n"
                "    enabled: true\n"
                "    server_name: wrong.example.com\n"
                "    key_path: /etc/ssl/privkey.pem\n"
                "    certificate_path: /etc/ssl/fullchain.pem\n"
            ),
        )

        with pytest.raises(ValidationError, match="must match deployment.host"):
            load_runtime_inventory(inventory_file)

    def test_requires_tls_key_path_when_enabled_for_ca_signed_cert(
        self, tmp_path: Path
    ):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            tls_block=(
                "  tls:\n"
                "    enabled: true\n"
                "    certificate_path: /etc/ssl/fullchain.pem\n"
            ),
        )

        with pytest.raises(ValidationError, match="key_path"):
            load_runtime_inventory(inventory_file)

    def test_requires_tls_certificate_path_when_enabled_for_ca_signed_cert(
        self, tmp_path: Path
    ):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n",
            tls_block=(
                "  tls:\n    enabled: true\n    key_path: /etc/ssl/privkey.pem\n"
            ),
        )

        with pytest.raises(ValidationError, match="certificate_path"):
            load_runtime_inventory(inventory_file)

    def test_requires_host_ip_and_both_protocol_ports(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        inventory_file.write_text(
            "deployment:\n"
            "  host: vpn.example.com\n"
            "  ip: 203.0.113.10\n"
            "  tls:\n"
            "    enabled: true\n"
            "    key_path: /etc/ssl/privkey.pem\n"
            "    certificate_path: /etc/ssl/fullchain.pem\n"
            "web_portal:\n"
            "  users:\n"
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n"
            "trojan:\n"
            "  users:\n"
            "    - username: alice\n"
            "      password: trojan-secret\n"
            "      enabled: true\n"
            "hysteria2:\n"
            "  obfs_password: hy2-obfs-secret\n"
            "  users:\n"
            "    - username: bob\n"
            "      password: hysteria2-secret\n"
            "      enabled: true\n"
            "naive:\n"
            "  users:\n"
            "    - username: alice\n"
            "      password: naive-secret\n"
            "      enabled: true\n",
            encoding="utf-8",
        )

        with pytest.raises(
            ValidationError,
            match="trojan_port|hysteria2_port|naive_port",
        ):
            load_runtime_inventory(inventory_file)


class TestAuthSnapshot:
    def test_auth_snapshot_round_trip_returns_only_enabled_users(self, tmp_path: Path):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n"
            "      admin: true\n"
            "    - username: bob\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: false\n",
            "    - username: alice\n"
            "      password: alice-trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: bob-hysteria2-secret\n"
            "      enabled: true\n",
        )
        inventory = load_runtime_inventory(inventory_file)
        snapshot_file = tmp_path / "auth-users.json"

        write_auth_snapshot(snapshot_file, inventory.web_portal.users)
        snapshot_users = load_auth_snapshot(snapshot_file)

        assert list(snapshot_users) == ["alice"]
        assert snapshot_users["alice"].username == "alice"
        assert snapshot_users["alice"].password_hash == VALID_PASSWORD_HASH
        assert snapshot_users["alice"].enabled is True
        assert snapshot_users["alice"].admin is True

    def test_load_auth_snapshot_rejects_unknown_keys(self, tmp_path: Path):
        snapshot_file = tmp_path / "auth-users.json"
        snapshot_file.write_text(
            "{\n"
            '  "users": [\n'
            "    {\n"
            '      "username": "alice",\n'
            f'      "password_hash": "{VALID_PASSWORD_HASH}",\n'
            '      "enabled": true,\n'
            '      "unexpected": "nope"\n'
            "    }\n"
            "  ]\n"
            "}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="unexpected"):
            load_auth_snapshot(snapshot_file)

    def test_write_auth_snapshot_keeps_existing_file_if_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        inventory_file = tmp_path / "runtime.yaml"
        _write_inventory(
            inventory_file,
            "    - username: alice\n"
            f"      password_hash: {VALID_PASSWORD_HASH}\n"
            "      enabled: true\n",
            "    - username: alice\n"
            "      password: alice-trojan-secret\n"
            "      enabled: true\n",
            "    - username: bob\n"
            "      password: bob-hysteria2-secret\n"
            "      enabled: true\n",
        )
        inventory = load_runtime_inventory(inventory_file)
        snapshot_file = tmp_path / "auth-users.json"
        original_content = '{"users": []}\n'
        snapshot_file.write_text(original_content, encoding="utf-8")

        def fake_replace(src: str, dst: str) -> None:
            raise RuntimeError("replace failed")

        monkeypatch.setattr(config_loader.os, "replace", fake_replace)

        with pytest.raises(RuntimeError, match="replace failed"):
            write_auth_snapshot(snapshot_file, inventory.web_portal.users)

        assert snapshot_file.read_text(encoding="utf-8") == original_content


def _deployment(**overrides):
    params = {
        "host": "vpn.example.com",
        "ip": "203.0.113.10",
        "trojan_port": 8443,
        "hysteria2_port": 4443,
        "naive_port": 9443,
        "tls": TlsConfig(enabled=False),
    }
    params.update(overrides)
    return DeploymentConfig(**params)


def _hysteria2_inventory(**overrides):
    params = {
        "obfs_password": "hy2-obfs-secret",
        "users": [
            Hysteria2User(username="bob", password="bob-secret", enabled=True)
        ],
    }
    params.update(overrides)
    return Hysteria2Inventory(**params)


class TestHysteria2PortRange:
    def test_defaults_to_none(self):
        assert _deployment().hysteria2_port_range is None

    def test_accepts_and_normalizes_valid_range(self):
        deployment = _deployment(hysteria2_port_range=" 20000:50000 ")
        assert deployment.hysteria2_port_range == "20000:50000"

    @pytest.mark.parametrize(
        "value",
        [
            "50000:20000",  # start > end
            "abc",  # not numeric
            "1000",  # missing end
            "70000:80000",  # above 65535
            "0:100",  # below 1
            "20000:50000:1",  # too many parts
        ],
    )
    def test_rejects_invalid_range(self, value):
        with pytest.raises(ValidationError):
            _deployment(hysteria2_port_range=value)


class TestHysteria2Bandwidth:
    def test_defaults_to_none(self):
        inventory = _hysteria2_inventory()
        assert inventory.up_mbps is None
        assert inventory.down_mbps is None

    def test_accepts_positive_values(self):
        inventory = _hysteria2_inventory(up_mbps=50, down_mbps=200)
        assert inventory.up_mbps == 50
        assert inventory.down_mbps == 200

    @pytest.mark.parametrize("field", ["up_mbps", "down_mbps"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_values(self, field, value):
        with pytest.raises(ValidationError):
            _hysteria2_inventory(**{field: value})
