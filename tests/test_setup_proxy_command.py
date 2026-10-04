"""Tests for setup.sh proxy command behavior."""

from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


def _require_shell(name: str) -> str:
    shell_path = shutil.which(name)
    if shell_path is None:
        pytest.skip(f"{name} is not available")

    return shell_path


def _run_bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_require_shell("bash"), "--noprofile", "--norc", "-ic", script],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def version_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "FORCE_LANG": "en",
        "SBM_SKIP_UPDATE_CHECK": "1",
    }


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize(
    ("metadata", "upstream", "manager"),
    [
        (
            "upstream_version=1.14.0\ncommit_sha=9237d372\ndirty=false\n",
            "1.14.0",
            "9237d372",
        ),
        (
            "upstream_version=1.14.0\ncommit_sha=9237d372\ndirty=true\n",
            "1.14.0",
            "9237d372-dirty",
        ),
        (None, "unknown", "unknown"),
        ("upstream_version=1.14.0\ndirty=true\n", "1.14.0", "unknown"),
        ("commit_sha=9237d372\n", "unknown", "9237d372"),
        ("upstream_version=\ncommit_sha=\ndirty=true\n", "unknown", "unknown"),
        (
            'upstream_version=$(touch "$HOME/executed")\n',
            '$(touch "$HOME/executed")',
            "unknown",
        ),
    ],
)
def test_proxy_version_reads_package_metadata(
    version_env: dict[str, str],
    shell: str,
    metadata: str | None,
    upstream: str,
    manager: str,
) -> None:
    data_root = Path(version_env["XDG_DATA_HOME"]) / "sing-box"
    data_root.mkdir(parents=True)
    if metadata is not None:
        (data_root / "client-version").write_text(metadata, encoding="utf-8")
    result = subprocess.run(
        [
            _require_shell(shell),
            "-f",
            "-c",
            textwrap.dedent(f'''\
            source "{SETUP_SCRIPT}"
            curl() {{ echo 'unexpected network call' >&2; return 99; }}
            git() {{ echo 'unexpected git call' >&2; return 99; }}
            systemctl() {{ echo 'unexpected service call' >&2; return 99; }}
            proxy version
        '''),
        ],
        capture_output=True,
        text=True,
        check=True,
        env=version_env,
    )
    assert [line.strip() for line in result.stdout.splitlines()] == [
        f"sing-box: {upstream}",
        f"sing-box-manager: {manager}",
    ]
    assert result.stderr == ""
    assert not (Path(version_env["HOME"]) / "executed").exists()
    assert not Path(version_env["XDG_STATE_HOME"]).exists()


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_proxy_version_uses_default_data_fallback(
    version_env: dict[str, str], shell: str
) -> None:
    default_root = Path(version_env["HOME"]) / ".local/share/sing-box"
    default_root.mkdir(parents=True)
    (default_root / "client-version").write_text(
        "commit_sha=9237d372\n", encoding="utf-8"
    )
    result = subprocess.run(
        [_require_shell(shell), "-f", "-c", f'source "{SETUP_SCRIPT}"; proxy version'],
        capture_output=True,
        text=True,
        check=True,
        env=version_env,
    )
    assert "sing-box-manager: 9237d372" in result.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("argument", ["extra", "--json"])
def test_proxy_version_rejects_arguments(
    version_env: dict[str, str], shell: str, argument: str
) -> None:
    result = subprocess.run(
        [
            _require_shell(shell),
            "-f",
            "-c",
            f'source "{SETUP_SCRIPT}"; proxy version {argument}',
        ],
        capture_output=True,
        text=True,
        env=version_env,
    )
    assert result.returncode == 1
    assert f"Unknown argument: {argument}" in result.stdout + result.stderr
    assert "sing-box-manager:" not in result.stdout


@pytest.mark.parametrize("language", ["en_US", "zh_CN"])
def test_proxy_version_appears_in_help(
    version_env: dict[str, str], language: str
) -> None:
    version_env["FORCE_LANG"] = language
    result = _run_bash(f'source "{SETUP_SCRIPT}"; proxy help', env=version_env)
    assert "proxy version" in result.stdout
    assert (
        "Show bundled sing-box and manager versions"
        if language == "en_US"
        else "显示随包提供的 sing-box 和管理器版本"
    ) in result.stdout


def test_proxy_docker_on_updates_daemon_and_client_config(tmp_path: Path) -> None:
    daemon_dir = tmp_path / "docker.service.d"
    daemon_conf = daemon_dir / "http-proxy.conf"
    docker_config_dir = tmp_path / "docker-config"
    docker_config_dir.mkdir()
    docker_config_file = docker_config_dir / "config.json"
    docker_config_file.write_text(
        json.dumps(
            {
                "auths": {"registry.example.com": {}},
                "credsStore": "pass",
                "proxies": {
                    "tcp://builder": {"noProxy": "internal.example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    systemctl_log = tmp_path / "systemctl.log"

    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(docker_config_dir)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_prepare_proxy_config() {{ printf '127.0.0.1 1080\\n'; }}
            _proxy_docker_daemon_service_dir() {{ printf '%s\\n' "{daemon_dir}"; }}
            _proxy_docker_daemon_proxy_file() {{ printf '%s\\n' "{daemon_conf}"; }}
            sudo() {{ "$@"; }}
            systemctl() {{ printf '%s\\n' "$*" >> "{systemctl_log}"; }}
            docker() {{ return 0; }}
            proxy docker on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert daemon_conf.read_text(encoding="utf-8") == textwrap.dedent(
        """\
        [Service]
        Environment="http_proxy=http://127.0.0.1:1080"
        Environment="https_proxy=http://127.0.0.1:1080"
        Environment="no_proxy=localhost,127.0.0.0/8,::1,host.docker.internal"
        Environment="HTTP_PROXY=http://127.0.0.1:1080"
        Environment="HTTPS_PROXY=http://127.0.0.1:1080"
        Environment="NO_PROXY=localhost,127.0.0.0/8,::1,host.docker.internal"
        """
    )

    docker_config = json.loads(docker_config_file.read_text(encoding="utf-8"))
    assert docker_config["auths"] == {"registry.example.com": {}}
    assert docker_config["credsStore"] == "pass"
    assert docker_config["proxies"]["tcp://builder"] == {
        "noProxy": "internal.example.com"
    }
    assert docker_config["proxies"]["default"] == {
        "httpProxy": "http://127.0.0.1:1080",
        "httpsProxy": "http://127.0.0.1:1080",
        "ftpProxy": "ftp://127.0.0.1:1080",
        "noProxy": "localhost,127.0.0.0/8,::1,host.docker.internal",
    }
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "daemon-reload",
        "restart docker",
    ]


def test_proxy_docker_off_removes_managed_proxy_keys_only(tmp_path: Path) -> None:
    daemon_dir = tmp_path / "docker.service.d"
    daemon_dir.mkdir()
    daemon_conf = daemon_dir / "http-proxy.conf"
    daemon_conf.write_text("existing", encoding="utf-8")
    docker_config_dir = tmp_path / "docker-config"
    docker_config_dir.mkdir()
    docker_config_file = docker_config_dir / "config.json"
    docker_config_file.write_text(
        json.dumps(
            {
                "auths": {"registry.example.com": {}},
                "proxies": {
                    "default": {
                        "httpProxy": "http://127.0.0.1:1080",
                        "httpsProxy": "http://127.0.0.1:1080",
                        "ftpProxy": "ftp://127.0.0.1:1080",
                        "noProxy": "localhost,127.0.0.0/8,::1,host.docker.internal",
                    },
                    "tcp://builder": {"noProxy": "internal.example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    systemctl_log = tmp_path / "systemctl.log"

    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(docker_config_dir)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_docker_daemon_service_dir() {{ printf '%s\\n' "{daemon_dir}"; }}
            _proxy_docker_daemon_proxy_file() {{ printf '%s\\n' "{daemon_conf}"; }}
            sudo() {{ "$@"; }}
            systemctl() {{ printf '%s\\n' "$*" >> "{systemctl_log}"; }}
            docker() {{ return 0; }}
            proxy docker off
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert not daemon_conf.exists()

    docker_config = json.loads(docker_config_file.read_text(encoding="utf-8"))
    assert docker_config == {
        "auths": {"registry.example.com": {}},
        "proxies": {"tcp://builder": {"noProxy": "internal.example.com"}},
    }
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "daemon-reload",
        "restart docker",
    ]


def test_proxy_docker_on_requires_jq_and_shows_ubuntu_hint(tmp_path: Path) -> None:
    docker_config_dir = tmp_path / "docker-config"
    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(docker_config_dir)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_prepare_proxy_config() {{ printf '127.0.0.1 1080\\n'; }}
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            _proxy_is_ubuntu_like() {{ return 0; }}
            docker() {{ return 0; }}
            proxy docker on
            """
        ).strip(),
        env=env,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "jq is required for this command." in combined_output
    assert "sudo apt install jq" in combined_output
    assert "https://jqlang.org/download/" not in combined_output
    assert not (docker_config_dir / "config.json").exists()


def test_proxy_docker_on_missing_jq_skips_ubuntu_hint_elsewhere(tmp_path: Path) -> None:
    docker_config_dir = tmp_path / "docker-config"
    env = os.environ.copy()
    env["DOCKER_CONFIG"] = str(docker_config_dir)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_prepare_proxy_config() {{ printf '127.0.0.1 1080\\n'; }}
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            _proxy_is_ubuntu_like() {{ return 1; }}
            docker() {{ return 0; }}
            proxy docker on
            """
        ).strip(),
        env=env,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "https://jqlang.org/download/" in combined_output
    assert "sudo apt install jq" not in combined_output


def test_proxy_check_ip_requires_jq_and_shows_ubuntu_hint() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            _proxy_is_ubuntu_like() {{ return 0; }}
            proxy check ip
            """
        ).strip(),
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "jq is required for this command." in combined_output
    assert "sudo apt install jq" in combined_output
    assert "https://jqlang.org/download/" not in combined_output


def test_proxy_check_ip_cn_uses_cip_cc_without_jq() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            curl() {{ return 0; }}
            _fetch_url() {{
                if [[ "$1" != "http://cip.cc" ]]; then
                    return 1
                fi
                local location_key location_value
                location_key=$(printf '\\345\\234\\260\\345\\235\\200')
                location_value=$(printf '\\344\\270\\255\\345\\233\\275 \\346\\276\\263\\351\\227\\250')
                printf '%s\n' \
                    'IP      : 149.102.98.22' \
                    "${{location_key}}    : ${{location_value}}" >"$2"
            }}
            proxy check ip cn
            """
        ).strip(),
    )

    assert result.returncode == 0
    assert "\u4e2d\u56fd \u6fb3\u95e8, 149.102.98.22" in result.stdout


def test_proxy_check_quota_requires_jq_and_shows_ubuntu_hint() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            _proxy_is_ubuntu_like() {{ return 0; }}
            proxy check quota
            """
        ).strip(),
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "jq is required for this command." in combined_output
    assert "sudo apt install jq" in combined_output
    assert "https://jqlang.org/download/" not in combined_output


def test_proxy_check_quota_shows_vps_and_user_sections(tmp_path: Path) -> None:
    config_home = tmp_path / "xdg-config"
    token_file = config_home / "sing-box" / "portal-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("saved-machine-token", encoding="utf-8")

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_vps_info() {{
                printf '%s\n' '{{"hostname":"vps-host","location":"JP, Tokyo","bandwidth_used_gb":12.34,"bandwidth_total_gb":500,"bandwidth_reset_date":"2026-05-01"}}'
            }}
            _proxy_fetch_portal_usage() {{
                _PROXY_PORTAL_USAGE_STATUS=200
                _PROXY_PORTAL_USAGE_PAYLOAD='{{"cycle_start":"2026-04-01","cycle_end":"2026-04-30","upload_bytes":1536,"download_bytes":4096,"total_bytes":5632,"updated_at":"2026-04-11T12:34:56+00:00"}}'
            }}
            proxy check quota
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert "VPS" in result.stdout
    assert "Hostname: vps-host" in result.stdout
    assert "Location: JP, Tokyo" in result.stdout
    assert "Quota: 12.34 / 500 GB" in result.stdout
    assert "Reset date: 2026-05-01" in result.stdout
    assert "User" in result.stdout
    assert "Cycle: 2026-04-01 ~ 2026-04-30" in result.stdout
    assert "Total: 5.50 KiB" in result.stdout
    assert result.stdout.index("Hostname:") < result.stdout.index("Cycle:")


def test_proxy_check_vps_is_no_longer_a_command() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            proxy check vps
            """
        ).strip(),
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "check vps" in combined_output


def test_proxy_check_quota_reads_saved_token_and_formats_totals(tmp_path: Path) -> None:
    config_home = tmp_path / "xdg-config"
    token_file = config_home / "sing-box" / "portal-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("saved-machine-token", encoding="utf-8")
    fetch_log = tmp_path / "fetch.log"

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_vps_info() {{
                printf '%s\n' '{{"hostname":"vps-host","location":"JP, Tokyo","bandwidth_used_gb":1,"bandwidth_total_gb":500,"bandwidth_reset_date":"2026-05-01"}}'
            }}
            _proxy_fetch_portal_usage() {{
                printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" > "{fetch_log}"
                _PROXY_PORTAL_USAGE_STATUS=200
                _PROXY_PORTAL_USAGE_PAYLOAD='{{"cycle_start":"2026-04-01","cycle_end":"2026-04-30","upload_bytes":1536,"download_bytes":4096,"total_bytes":5632,"updated_at":"2026-04-11T12:34:56+00:00"}}'
            }}
            proxy check quota
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert fetch_log.read_text(encoding="utf-8").strip() == (
        "https://portal.example.com|saved-machine-token||"
    )
    assert "Cycle: 2026-04-01 ~ 2026-04-30" in result.stdout
    assert "Upload: 1.50 KiB" in result.stdout
    assert "Download: 4.00 KiB" in result.stdout
    assert "Total: 5.50 KiB" in result.stdout
    assert "Last updated: 2026-04-11T12:34:56+00:00" in result.stdout
    # A portal predating relay_multiplier sends no multiplier, and a plan share
    # computed without one would understate usage by half.
    assert "Plan usage" not in result.stdout


def test_proxy_check_quota_projects_plan_usage_onto_the_host_counter(
    tmp_path: Path,
) -> None:
    """The measured rows stay measured; the plan share is the doubled figure.

    A relayed byte crosses the VPS interface twice, so a user reading `Total`
    against the VPS quota would think they use half the plan they really do.
    """
    config_home = tmp_path / "xdg-config"
    token_file = config_home / "sing-box" / "portal-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("saved-machine-token", encoding="utf-8")

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            """
            source "{script}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_usage() {{
                _PROXY_PORTAL_USAGE_STATUS=200
                _PROXY_PORTAL_USAGE_PAYLOAD='{payload}'
            }}
            _proxy_check_user_quota
            """
        )
        .strip()
        .format(
            script=SETUP_SCRIPT,
            payload=(
                '{"cycle_start":"2026-04-01","cycle_end":"2026-04-30",'
                '"upload_bytes":1536,"download_bytes":4096,"total_bytes":5632,'
                '"relay_multiplier":2,"plan_total_bytes":22528,'
                '"updated_at":"2026-04-11T12:34:56+00:00"}'
            ),
        ),
        env=env,
    )

    assert result.returncode == 0
    assert "Total: 5.50 KiB" in result.stdout
    assert "Plan usage: 11.00 KiB / 22.00 KiB (50.0%, x2 relay)" in result.stdout


def test_proxy_check_quota_plan_usage_omits_share_without_a_plan_total(
    tmp_path: Path,
) -> None:
    """No plan total means no denominator, so print the billed figure alone.

    Inventing a percentage against an unknown plan would be worse than omitting
    it, and the billed byte count is still useful on its own.
    """
    config_home = tmp_path / "xdg-config"
    token_file = config_home / "sing-box" / "portal-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("saved-machine-token", encoding="utf-8")

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            """
            source "{script}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_usage() {{
                _PROXY_PORTAL_USAGE_STATUS=200
                _PROXY_PORTAL_USAGE_PAYLOAD='{payload}'
            }}
            _proxy_check_user_quota
            """
        )
        .strip()
        .format(
            script=SETUP_SCRIPT,
            payload=(
                '{"cycle_start":"2026-04-01","cycle_end":"2026-04-30",'
                '"upload_bytes":1536,"download_bytes":4096,"total_bytes":5632,'
                '"relay_multiplier":2,"updated_at":"2026-04-11T12:34:56+00:00"}'
            ),
        ),
        env=env,
    )

    assert result.returncode == 0
    assert "Plan usage: 11.00 KiB (x2 relay)" in result.stdout


def test_plan_usage_at_multiplier_one_drops_the_relay_annotation() -> None:
    """An egress-only host bills each relayed byte once.

    There is no doubling to explain there, so the `x1 relay` note would be noise;
    the plan share is still worth printing.
    """
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_format_plan_usage 5632 1 22528
            """
        ).strip()
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "5.50 KiB / 22.00 KiB (25.0%)"


def test_plan_usage_is_omitted_when_it_would_repeat_the_total() -> None:
    """A multiplier of 1 with no plan total leaves nothing to project.

    The row would print the measured total a second time under a different
    label, which reads as two quantities that happen to agree rather than as
    one quantity printed twice.
    """
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_format_plan_usage 5632 1 ''
            """
        ).strip()
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_proxy_check_quota_falls_back_to_env_credentials_when_token_rejected(
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "xdg-config"
    token_file = config_home / "sing-box" / "portal-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("expired-machine-token", encoding="utf-8")
    fetch_log = tmp_path / "fetch.log"

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["SBM_USERNAME"] = "alice"
    env["SBM_PASSWORD"] = "portal-secret"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_vps_info() {{
                printf '%s\n' '{{"hostname":"vps-host","location":"JP, Tokyo","bandwidth_used_gb":1,"bandwidth_total_gb":500,"bandwidth_reset_date":"2026-05-01"}}'
            }}
            _proxy_fetch_portal_usage() {{
                printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" >> "{fetch_log}"
                if [[ -n "$2" ]]; then
                    _PROXY_PORTAL_USAGE_STATUS=401
                    return 1
                fi
                _PROXY_PORTAL_USAGE_STATUS=200
                _PROXY_PORTAL_USAGE_PAYLOAD='{{"cycle_start":"2026-04-01","cycle_end":"2026-04-30","upload_bytes":10,"download_bytes":20,"total_bytes":30}}'
            }}
            proxy check quota
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert fetch_log.read_text(encoding="utf-8").splitlines() == [
        "https://portal.example.com|expired-machine-token||",
        "https://portal.example.com||alice|portal-secret",
    ]
    assert "Total: 30 B" in result.stdout


def test_proxy_check_quota_requires_saved_token_or_env_credentials(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    home_dir.mkdir()
    config_home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env.pop("SBM_TOKEN", None)
    env.pop("SBM_USERNAME", None)
    env.pop("SBM_PASSWORD", None)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_resolve_portal_base_url() {{ printf 'https://portal.example.com\n'; }}
            _proxy_fetch_portal_vps_info() {{
                printf '%s\n' '{{"hostname":"vps-host","location":"JP, Tokyo","bandwidth_used_gb":1,"bandwidth_total_gb":500,"bandwidth_reset_date":"2026-05-01"}}'
            }}
            proxy check quota
            """
        ).strip(),
        env=env,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "SBM_TOKEN" in combined_output
    assert "SBM_USERNAME/SBM_PASSWORD" in combined_output


def test_proxy_check_ip_private_does_not_require_jq() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _has() {{
                if [[ "$1" == "jq" ]]; then
                    return 1
                fi
                command -v "$1" >/dev/null 2>&1
            }}
            hostname() {{
                if [[ "$1" == "-I" ]]; then
                    printf '192.0.2.5 10.0.0.8\\n'
                    return 0
                fi
                command hostname "$@"
            }}
            proxy check ip private
            """
        ).strip(),
    )

    assert result.stdout.splitlines()[-1] == "192.0.2.5"


def test_proxy_protocol_switches_to_installed_user_protocol(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    service_dir = config_home / "systemd" / "user"
    log_file = tmp_path / "systemctl.log"
    home_dir.mkdir()
    service_dir.mkdir(parents=True)
    for protocol in ("trojan", "hysteria2"):
        protocol_dir = config_root / protocol
        protocol_dir.mkdir(parents=True)
        (protocol_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (service_dir / f"sing-box-{protocol}.service").write_text(
            "[Service]\n", encoding="utf-8"
        )

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            systemctl() {{
                printf '%s\n' "$*" >> "{log_file}"
                if [[ "$*" == "--user is-active --quiet sing-box-naive.service" ]]; then
                    return 1
                fi
                return 0
            }}
            proxy protocol hysteria2
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-protocol").read_text(encoding="utf-8") == (
        "hysteria2\n"
    )
    assert log_file.read_text(encoding="utf-8").splitlines() == [
        "--user disable --now sing-box-trojan.service",
        "--user is-active --quiet sing-box-naive.service",
        "--user enable --now sing-box-hysteria2.service",
        "--user is-active --quiet sing-box-hysteria2.service",
    ]


def test_proxy_protocol_without_argument_shows_active_protocol() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            systemctl() {{
                if [[ "$*" == "--user is-active --quiet sing-box-hysteria2.service" ]]; then
                    return 0
                fi
                return 1
            }}
            proxy protocol
            """
        ).strip(),
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Active protocol: hysteria2" in combined_output
    assert (
        "Missing protocol. Supported protocols: trojan hysteria2 naive"
        in combined_output
    )


def test_proxy_protocol_rejects_protocol_not_installed_for_user(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    service_dir = config_home / "systemd" / "user"
    log_file = tmp_path / "systemctl.log"
    home_dir.mkdir()
    (config_root / "trojan").mkdir(parents=True)
    (config_root / "trojan" / "config.json").write_text("{}\n", encoding="utf-8")
    service_dir.mkdir(parents=True)
    (service_dir / "sing-box-trojan.service").write_text(
        "[Service]\n", encoding="utf-8"
    )

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            systemctl() {{
                printf '%s\n' "$*" >> "{log_file}"
                return 0
            }}
            proxy protocol hysteria2
            """
        ).strip(),
        env=env,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Protocol hysteria2 is not installed for this user" in combined_output
    assert not (config_root / "selected-protocol").exists()
    assert not log_file.exists()


def _prepare_route_client(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    binary = home_dir / ".local" / "bin" / "sing-box"
    validation_log = tmp_path / "validation.log"
    home_dir.mkdir()
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env bash\n"
        # `tools fetch` is the route probe: log the whole invocation so a test
        # can assert it never shares the working directory a running service
        # holds the cache file in.
        'if [[ "$1" == tools && "$2" == fetch ]]; then\n'
        '  printf \'%s\\n\' "$*" >> "$PROBE_LOG"\n'
        '  exit "${PROBE_STATUS:-0}"\n'
        "fi\n"
        '[[ "$1" == check && "$2" == -D && "$3" == "$XDG_STATE_HOME/sing-box" && "$4" == -c ]] || exit 1\n'
        'printf \'%s\\n\' "$5" >> "$VALIDATION_LOG"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)

    for protocol in ("trojan", "hysteria2"):
        protocol_dir = config_root / protocol
        protocol_dir.mkdir(parents=True)
        for route in ("china", "gfw", "ai", "global"):
            (protocol_dir / f"config-{route}.json").write_text(
                f"{protocol}-{route}\n", encoding="utf-8"
            )
        (protocol_dir / "config.json").write_text(
            f"{protocol}-china\n", encoding="utf-8"
        )
    (config_root / "selected-route").write_text("china\n", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["VALIDATION_LOG"] = str(validation_log)
    env["PROBE_LOG"] = str(tmp_path / "probe.log")
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg state")
    return env, config_root, binary, validation_log


def test_proxy_route_updates_all_protocols_and_restarts_only_active_service(
    tmp_path: Path,
) -> None:
    env, config_root, binary, validation_log = _prepare_route_client(tmp_path)
    service_log = tmp_path / "services.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            _ROUTE_ACTIVE=true
            _proxy_user_service_is_active() {{
                [[ "$1" == "sing-box-trojan.service" && "$_ROUTE_ACTIVE" == true ]]
            }}
            _proxy_user_service_stop() {{
                printf 'stop %s\n' "$1" >> "{service_log}"
                _ROUTE_ACTIVE=false
            }}
            _proxy_user_service_start() {{
                printf 'start %s\n' "$1" >> "{service_log}"
                _ROUTE_ACTIVE=true
            }}
            _proxy_listener_is_ready() {{ return 0; }}
            proxy route gfw
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-gfw\n"
    assert (config_root / "hysteria2" / "config.json").read_text() == (
        "hysteria2-gfw\n"
    )
    assert (config_root / "selected-route").read_text() == "gfw\n"
    assert service_log.read_text().splitlines() == ["stop trojan", "start trojan"]
    assert validation_log.read_text().splitlines() == [
        str(config_root / "trojan" / "config-gfw.json"),
        str(config_root / "hysteria2" / "config-gfw.json"),
    ]


def test_proxy_route_probe_keeps_its_own_working_directory(tmp_path: Path) -> None:
    """The probe must not open the cache file a running service holds.

    sing-box locks the cache file named in a config, so probing with the state
    directory the service runs in blocks until the probe times out. Bundled
    rule snapshots are read from the working directory, so the throwaway one
    links them back in.
    """
    env, config_root, _, _ = _prepare_route_client(tmp_path)
    state_root = Path(env["XDG_STATE_HOME"]) / "sing-box"
    (state_root / "rules").mkdir(parents=True)
    (state_root / "rules" / "files.txt").write_text("", encoding="utf-8")

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{tmp_path / "home" / ".local" / "bin" / "sing-box"}"; }}
            _proxy_user_service_is_active() {{ return 1; }}
            proxy route gfw
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    probe = (tmp_path / "probe.log").read_text().splitlines()
    assert len(probe) == 1
    arguments = probe[0].split()
    working_directory = arguments[arguments.index("-D") + 1]
    assert working_directory != str(state_root)
    assert arguments[arguments.index("-c") + 1] == str(
        config_root / "trojan" / "config-gfw.json"
    )
    assert arguments[-1] == "http://captive.apple.com"


def _write_probe_binary(binary: Path, failing: str) -> None:
    """A stand-in whose route probe fails for one config and passes elsewhere."""
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == tools && "$2" == fetch ]]; then\n'
        f'  case "$6" in {failing}) exit 1 ;; esac\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)


def test_proxy_route_refuses_a_route_that_cannot_resolve(tmp_path: Path) -> None:
    """A valid config that cannot look anything up must not replace a working one.

    This is the ai route on a host whose resolver sing-box cannot query: the
    config parses, the service starts and listens, and every direct
    destination fails to resolve.
    """
    env, config_root, binary, _ = _prepare_route_client(tmp_path)
    _write_probe_binary(binary, "*config-ai.json")

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            _proxy_resolved_stub_is_active() {{ return 1; }}
            _proxy_user_service_is_active() {{ return 1; }}
            proxy route ai
            """
        ).strip(),
        env=env,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Route ai cannot reach the network on this machine" in combined_output
    assert "systemd-resolved" in combined_output
    assert "proxy route gfw" in combined_output
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-china\n"
    assert (config_root / "hysteria2" / "config.json").read_text() == (
        "hysteria2-china\n"
    )
    assert (config_root / "selected-route").read_text() == "china\n"


def test_proxy_route_switches_when_the_current_route_fails_the_same_probe(
    tmp_path: Path,
) -> None:
    """An offline machine still gets to change routes."""
    env, config_root, binary, _ = _prepare_route_client(tmp_path)
    _write_probe_binary(binary, "*")

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            _proxy_user_service_is_active() {{ return 1; }}
            proxy route ai
            """
        ).strip(),
        env=env,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0
    assert "Could not verify route ai" in combined_output
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-ai\n"
    assert (config_root / "selected-route").read_text() == "ai\n"


def test_proxy_route_validation_failure_does_not_modify_state(tmp_path: Path) -> None:
    env, config_root, binary, _ = _prepare_route_client(tmp_path)
    binary.write_text(
        '#!/usr/bin/env bash\ncase "$5" in *hysteria2*) exit 1;; esac\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            proxy route gfw
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-china\n"
    assert (config_root / "hysteria2" / "config.json").read_text() == (
        "hysteria2-china\n"
    )
    assert (config_root / "selected-route").read_text() == "china\n"


def test_proxy_route_restart_failure_rolls_back_configs_marker_and_service(
    tmp_path: Path,
) -> None:
    env, config_root, binary, _ = _prepare_route_client(tmp_path)
    service_log = tmp_path / "services.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            _ROUTE_ACTIVE=true
            _ROUTE_STARTS=0
            _proxy_user_service_is_active() {{
                [[ "$1" == "sing-box-trojan.service" && "$_ROUTE_ACTIVE" == true ]]
            }}
            _proxy_user_service_stop() {{
                printf 'stop %s\n' "$1" >> "{service_log}"
                _ROUTE_ACTIVE=false
            }}
            _proxy_user_service_start() {{
                _ROUTE_STARTS=$((_ROUTE_STARTS + 1))
                printf 'start %s\n' "$1" >> "{service_log}"
                if [[ "$_ROUTE_STARTS" -eq 1 ]]; then return 1; fi
                _ROUTE_ACTIVE=true
            }}
            proxy route gfw
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-china\n"
    assert (config_root / "hysteria2" / "config.json").read_text() == (
        "hysteria2-china\n"
    )
    assert (config_root / "selected-route").read_text() == "china\n"
    assert service_log.read_text().splitlines() == [
        "stop trojan",
        "start trojan",
        "start trojan",
    ]


def test_proxy_route_listener_failure_rolls_back_configs_marker_and_service(
    tmp_path: Path,
) -> None:
    env, config_root, binary, _ = _prepare_route_client(tmp_path)
    service_log = tmp_path / "services.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 0; }}
            _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
            _ROUTE_ACTIVE=true
            _ROUTE_READY_CHECKS=0
            _proxy_user_service_is_active() {{
                [[ "$1" == "sing-box-trojan.service" && "$_ROUTE_ACTIVE" == true ]]
            }}
            _proxy_user_service_stop() {{
                printf 'stop %s\n' "$1" >> "{service_log}"
                _ROUTE_ACTIVE=false
            }}
            _proxy_user_service_start() {{
                printf 'start %s\n' "$1" >> "{service_log}"
                _ROUTE_ACTIVE=true
            }}
            _proxy_user_service_wait_ready() {{
                _ROUTE_READY_CHECKS=$((_ROUTE_READY_CHECKS + 1))
                [[ "$_ROUTE_READY_CHECKS" -gt 1 ]]
            }}
            proxy route gfw
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert (config_root / "trojan" / "config.json").read_text() == "trojan-china\n"
    assert (config_root / "hysteria2" / "config.json").read_text() == (
        "hysteria2-china\n"
    )
    assert (config_root / "selected-route").read_text() == "china\n"
    assert service_log.read_text().splitlines() == [
        "stop trojan",
        "start trojan",
        "stop trojan",
        "start trojan",
    ]


def test_proxy_route_reports_saved_route_and_tun_override(tmp_path: Path) -> None:
    env, _, _, _ = _prepare_route_client(tmp_path)
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            tun_is_safely_off() {{ return 1; }}
            proxy check route
            """
        ).strip(),
        env=env,
    )

    assert "Selected route: china" in result.stdout
    assert "Active TUN route: fixed/global" in result.stdout


def _macos_client_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Lay out a macOS client install: configs plus LaunchAgents."""
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    agent_dir = home_dir / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True)

    for protocol in ("trojan", "hysteria2"):
        protocol_dir = config_root / protocol
        protocol_dir.mkdir(parents=True)
        (protocol_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (agent_dir / f"io.sing-box.{protocol}.plist").write_text(
            "<plist/>\n", encoding="utf-8"
        )

    return home_dir, config_home, agent_dir


def test_proxy_service_on_bootstraps_launch_agent_on_macos(tmp_path: Path) -> None:
    home_dir, config_home, agent_dir = _macos_client_tree(tmp_path)
    (config_home / "sing-box" / "selected-protocol").write_text(
        "hysteria2\n", encoding="utf-8"
    )
    log_file = tmp_path / "launchctl.log"

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            plat_is_macos() {{ return 0; }}
            launchctl() {{
                printf '%s\n' "$*" >> "{log_file}"
                return 0
            }}
            _proxy_port_is_available() {{ return 0; }}
            proxy service on
            """
        ).strip(),
        env=env,
    )

    uid = os.getuid()
    assert result.returncode == 0
    assert log_file.read_text(encoding="utf-8").splitlines() == [
        f"bootout gui/{uid}/io.sing-box.trojan",
        f"print gui/{uid}/io.sing-box.naive",
        f"print gui/{uid}/io.sing-box.hysteria2",
        f"enable gui/{uid}/io.sing-box.hysteria2",
        # bootstrap refuses a label that is already loaded.
        f"bootout gui/{uid}/io.sing-box.hysteria2",
        f"bootstrap gui/{uid} {agent_dir}/io.sing-box.hysteria2.plist",
    ]


def test_proxy_desktop_on_configures_networksetup_on_macos(tmp_path: Path) -> None:
    home_dir, config_home, _ = _macos_client_tree(tmp_path)
    (config_home / "sing-box" / "selected-protocol").write_text(
        "trojan\n", encoding="utf-8"
    )
    log_file = tmp_path / "networksetup.log"

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            plat_is_macos() {{ return 0; }}
            launchctl() {{ printf 'state = running\n'; return 0; }}
            sudo() {{ "$@"; }}
            route() {{ printf '   route to: default\n  interface: en0\n'; }}
            networksetup() {{
                if [[ "$1" == "-listnetworkserviceorder" ]]; then
                    printf '(1) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n'
                    return 0
                fi
                printf '%s\n' "$*" >> "{log_file}"
                return 0
            }}
            proxy desktop on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    # The bypass list is passed as separate arguments, not one comma-joined string.
    assert log_file.read_text(encoding="utf-8").splitlines() == [
        "-setwebproxy Wi-Fi 127.0.0.1 1080",
        "-setsecurewebproxy Wi-Fi 127.0.0.1 1080",
        "-setsocksfirewallproxy Wi-Fi 127.0.0.1 1080",
        "-setproxybypassdomains Wi-Fi localhost 127.0.0.0/8 ::1 host.docker.internal",
    ]


def test_proxy_desktop_off_disables_networksetup_proxies_on_macos(
    tmp_path: Path,
) -> None:
    home_dir, config_home, _ = _macos_client_tree(tmp_path)
    log_file = tmp_path / "networksetup.log"

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            plat_is_macos() {{ return 0; }}
            sudo() {{ "$@"; }}
            route() {{ printf '  interface: en0\n'; }}
            networksetup() {{
                if [[ "$1" == "-listnetworkserviceorder" ]]; then
                    printf '(1) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n'
                    return 0
                fi
                printf '%s\n' "$*" >> "{log_file}"
                return 0
            }}
            proxy desktop off
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert log_file.read_text(encoding="utf-8").splitlines() == [
        "-setwebproxystate Wi-Fi off",
        "-setsecurewebproxystate Wi-Fi off",
        "-setsocksfirewallproxystate Wi-Fi off",
    ]


def test_proxy_check_system_reports_macos_runtime(tmp_path: Path) -> None:
    home_dir, config_home, _ = _macos_client_tree(tmp_path)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            plat_is_macos() {{ return 0; }}
            proxy check system
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert "Runtime: macos" in result.stdout
    assert "Desktop: aqua" in result.stdout
    assert "Support: full" in result.stdout
    # The report names the tools this platform actually uses.
    assert "launchctl:" in result.stdout
    assert "networksetup:" in result.stdout
    assert "systemctl:" not in result.stdout
    assert "dconf:" not in result.stdout
    assert "Proxy target: 127.0.0.1:1080" in result.stdout


def test_prepare_proxy_config_keeps_warnings_out_of_the_proxy_target() -> None:
    """`warning` prints to stdout, and this function's stdout is its return value."""
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_preferred_protocol() {{ printf 'trojan\n'; return 0; }}
            _proxy_user_service_is_active() {{ return 1; }}
            _proxy_system_service_is_active() {{ return 1; }}
            config=$(_proxy_prepare_proxy_config true 2>/dev/null)
            read -r host port <<<"$config"
            printf 'host=%s port=%s\n' "$host" "$port"
            """
        ).strip(),
    )

    assert result.returncode == 0
    assert "host=127.0.0.1 port=1080" in result.stdout


def test_prepare_proxy_config_still_shows_the_inactive_service_warning() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_preferred_protocol() {{ printf 'trojan\n'; return 0; }}
            _proxy_user_service_is_active() {{ return 1; }}
            _proxy_system_service_is_active() {{ return 1; }}
            _proxy_prepare_proxy_config true >/dev/null
            """
        ).strip(),
    )

    assert result.returncode == 0
    assert "is not active" in result.stderr


def test_proxy_service_on_uses_saved_selected_protocol(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    service_dir = config_home / "systemd" / "user"
    log_file = tmp_path / "systemctl.log"
    home_dir.mkdir()
    service_dir.mkdir(parents=True)
    for protocol in ("trojan", "hysteria2"):
        protocol_dir = config_root / protocol
        protocol_dir.mkdir(parents=True)
        (protocol_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (service_dir / f"sing-box-{protocol}.service").write_text(
            "[Service]\n", encoding="utf-8"
        )
    (config_root / "selected-protocol").write_text("hysteria2\n", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            systemctl() {{
                printf '%s\n' "$*" >> "{log_file}"
                if [[ "$*" == "--user is-active --quiet sing-box-naive.service" ]]; then
                    return 1
                fi
                return 0
            }}
            _proxy_port_is_available() {{ return 0; }}
            proxy service on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert log_file.read_text(encoding="utf-8").splitlines() == [
        "--user disable --now sing-box-trojan.service",
        "--user is-active --quiet sing-box-naive.service",
        "--user enable --now sing-box-hysteria2.service",
    ]


def _mixed_config(port: int) -> str:
    return (
        json.dumps(
            {
                "inbounds": [
                    {
                        "type": "mixed",
                        "listen": "127.0.0.1",
                        "listen_port": port,
                    }
                ]
            },
            indent=2,
        )
        + "\n"
    )


_ROUTE_CONFIG_NAMES = (
    "config.json",
    "config-china.json",
    "config-ai.json",
    "config-global.json",
)


def _prepare_port_client(
    tmp_path: Path, *, port: int = 1080
) -> tuple[dict[str, str], Path]:
    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    config_root = config_home / "sing-box"
    home_dir.mkdir()

    protocol_dir = config_root / "trojan"
    protocol_dir.mkdir(parents=True)
    for name in _ROUTE_CONFIG_NAMES:
        config_file = protocol_dir / name
        config_file.write_text(_mixed_config(port), encoding="utf-8")
        config_file.chmod(0o600)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    return env, config_root


def _port_stubs(*, log: Path, occupied: str = "", active: bool = False) -> str:
    """Stub the service manager and model which TCP ports answer.

    `occupied` is the set of ports something is listening on; `active` says
    whether that something is our own client, which is the distinction
    `proxy on` has to make before deciding a port is taken.
    """
    return textwrap.dedent(
        f"""
        tun_is_safely_off() {{ return 0; }}
        _proxy_runtime_kind() {{ printf 'linux\\n'; }}
        _OCCUPIED="{occupied}"
        _ACTIVE={"true" if active else "false"}
        _proxy_listener_is_ready() {{
            local probe="${{1:-$(_proxy_read_config_port)}}"
            case " $_OCCUPIED " in *" $probe "*) return 0 ;; esac
            return 1
        }}
        _proxy_port_is_available() {{
            _PROXY_PORT_CHECK_ERROR=""
            case " $_OCCUPIED " in *" $1 "*) return 1 ;; esac
            return 0
        }}
        _proxy_has_active_service() {{ [[ "$_ACTIVE" == true ]]; }}
        _proxy_user_service_is_active() {{
            [[ "$_ACTIVE" == true && "$1" == "sing-box-trojan.service" ]]
        }}
        _proxy_user_service_stop() {{
            printf 'stop %s\\n' "$1" >> "{log}"
            _ACTIVE=false
            _OCCUPIED=""
        }}
        _proxy_user_service_start() {{
            printf 'start %s\\n' "$1" >> "{log}"
            _ACTIVE=true
            _OCCUPIED="$(_proxy_read_config_port)"
        }}
        _proxy_enable_services() {{ printf 'enable\\n' >> "{log}"; return 0; }}
        _ensure_proxy_service_active() {{ return 0; }}
        _proxy_apply_shell_proxy() {{ printf 'shell %s\\n' "$2" >> "{log}"; }}
        _proxy_apply_git_proxy() {{ printf 'git %s\\n' "$3" >> "{log}"; }}
        _proxy_apply_desktop_proxy() {{ printf 'desktop %s\\n' "$2" >> "{log}"; }}
        """
    ).strip()


def _configured_ports(config_root: Path) -> set[str]:
    return {
        str(
            json.loads((config_root / "trojan" / name).read_text())["inbounds"][0][
                "listen_port"
            ]
        )
        for name in _ROUTE_CONFIG_NAMES
    }


def test_proxy_on_records_the_selected_port_and_patches_every_route_config(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log)}
            proxy on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1080\n"
    assert _configured_ports(config_root) == {"1080"}
    assert "shell 1080" in log.read_text()


def test_proxy_on_moves_past_a_foreign_listener_and_records_the_new_port(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1080 1081", active=False)}
            proxy on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1082\n"
    assert _configured_ports(config_root) == {"1082"}
    assert "1082" in result.stdout
    assert "shell 1082" in log.read_text()


def test_proxy_on_keeps_the_port_a_running_client_already_serves(
    tmp_path: Path,
) -> None:
    """The second `proxy on` must not mistake our own inbound for a conflict.

    Starting an already-active service is deliberately a no-op, so moving the
    port here would leave every proxy setting pointing at an unbound port.
    """
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1080", active=True)}
            proxy on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1080\n"
    assert _configured_ports(config_root) == {"1080"}
    assert "shell 1080" in log.read_text()
    # Nothing changed, so the running client must not have been bounced.
    assert "stop trojan" not in log.read_text()


def test_proxy_on_restores_the_recorded_port_after_an_upgrade_reset_the_configs(
    tmp_path: Path,
) -> None:
    """An install rewrites the configs from the shipped templates.

    That resets listen_port to the default while the marker still records the
    port the user is actually on, so `proxy on` has to reconcile the configs
    back and restart the daemon that is still holding the old one.
    """
    env, config_root = _prepare_port_client(tmp_path, port=1080)
    (config_root / "selected-port").write_text("1085\n", encoding="utf-8")
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1080", active=True)}
            proxy on
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1085\n"
    assert _configured_ports(config_root) == {"1085"}
    actions = log.read_text().splitlines()
    assert "stop trojan" in actions
    assert "start trojan" in actions
    assert "shell 1085" in actions


def test_proxy_on_port_pins_the_requested_port_and_preserves_config_modes(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log)}
            proxy on --port 1090
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1090\n"
    assert _configured_ports(config_root) == {"1090"}
    # These carry the user's credentials; a rewrite must not widen the mode.
    for name in _ROUTE_CONFIG_NAMES:
        mode = (config_root / "trojan" / name).stat().st_mode & 0o777
        assert mode == 0o600, name
    assert (config_root / "selected-port").stat().st_mode & 0o777 == 0o600
    assert not list((config_root / "trojan").glob("*.port.*"))


def test_proxy_on_port_accepts_the_port_our_own_client_already_serves(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path, port=1085)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1085", active=True)}
            proxy on --port 1085
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1085\n"
    assert "stop trojan" not in log.read_text()


def test_proxy_on_port_rejects_a_port_held_by_another_program(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="3128", active=False)}
            proxy on --port 3128
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "3128" in result.stdout + result.stderr
    assert not (config_root / "selected-port").exists()
    assert _configured_ports(config_root) == {"1080"}


def test_proxy_port_reports_the_saved_selection(tmp_path: Path) -> None:
    env, config_root = _prepare_port_client(tmp_path, port=1080)
    (config_root / "selected-port").write_text("1085\n", encoding="ascii")

    result = _run_bash(
        f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\n_proxy_has_active_service() {{ return 1; }}\nproxy port',
        env=env,
    )

    assert result.returncode == 0
    assert "Selected port" in result.stdout
    assert "1085" in result.stdout


def test_proxy_port_changes_configs_without_starting_an_inactive_service(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY ftp_proxy FTP_PROXY
            unset socks_proxy SOCKS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log)}
            proxy port 1090
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert (config_root / "selected-port").read_text() == "1090\n"
    assert _configured_ports(config_root) == {"1090"}
    actions = log.read_text().splitlines() if log.exists() else []
    assert "enable" not in actions
    assert not any(action.startswith(("stop ", "start ")) for action in actions)


def test_proxy_port_migrates_active_owned_integrations(tmp_path: Path) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            unset HTTP_PROXY HTTPS_PROXY FTP_PROXY SOCKS_PROXY ALL_PROXY NO_PROXY
            export http_proxy=http://127.0.0.1:1080
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1080", active=True)}
            _proxy_tun_classify_desktop_proxy() {{ printf 'ours\n'; }}
            _proxy_tun_classify_docker_proxy() {{ printf 'off\n'; }}
            proxy port 1090
            printf 'shell=%s\n' "$http_proxy"
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert _configured_ports(config_root) == {"1090"}
    assert "shell=http://127.0.0.1:1090" in result.stdout
    actions = log.read_text().splitlines()
    assert "stop trojan" in actions
    assert "start trojan" in actions
    assert "desktop 1090" in actions


def test_proxy_port_rolls_back_configs_marker_and_service_on_restart_failure(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    (config_root / "selected-port").write_text("1080\n", encoding="ascii")
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY ftp_proxy FTP_PROXY
            unset socks_proxy SOCKS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log, occupied="1080", active=True)}
            _proxy_user_service_start() {{
                printf 'start %s %s\n' "$1" "$(_proxy_read_config_port)" >> "{log}"
                if [[ "$(_proxy_read_config_port)" == 1090 ]]; then
                    _ACTIVE=false
                    _OCCUPIED=""
                    return 1
                fi
                _ACTIVE=true
                _OCCUPIED="$(_proxy_read_config_port)"
            }}
            proxy port 1090
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert (config_root / "selected-port").read_text() == "1080\n"
    assert _configured_ports(config_root) == {"1080"}
    actions = log.read_text().splitlines()
    assert "start trojan 1090" in actions
    assert "start trojan 1080" in actions
    assert "Restoring the previous port" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "arguments", ["80", "1023", "+1080", "1e3", "65536", '""', '" 1080"']
)
def test_proxy_port_rejects_non_decimal_privileged_and_out_of_range_values(
    tmp_path: Path, arguments: str
) -> None:
    env, config_root = _prepare_port_client(tmp_path)

    result = _run_bash(
        f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\nproxy port {arguments}',
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "1024" in result.stdout + result.stderr
    assert not (config_root / "selected-port").exists()


@pytest.mark.parametrize("socket_type", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_port_availability_rejects_tcp_and_udp_occupants(socket_type: int) -> None:
    try:
        occupant = socket.socket(socket.AF_INET, socket_type)
    except PermissionError:
        pytest.skip("the test sandbox does not permit creating sockets")

    with occupant:
        occupant.bind(("127.0.0.1", 0))
        if socket_type == socket.SOCK_STREAM:
            occupant.listen(1)
        port = occupant.getsockname()[1]
        result = _run_bash(
            f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\n_proxy_port_is_available {port}',
            check=False,
        )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("--port 70000", "65535"),
        ("--port abc", "65535"),
        ("--port", "Missing value"),
    ],
)
def test_proxy_on_rejects_invalid_port_arguments(
    tmp_path: Path, arguments: str, expected: str
) -> None:
    env, config_root = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log)}
            proxy on {arguments}
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert expected in result.stdout + result.stderr
    assert not (config_root / "selected-port").exists()


def test_port_flag_is_rejected_for_commands_that_do_not_place_the_inbound(
    tmp_path: Path,
) -> None:
    env, _ = _prepare_port_client(tmp_path)
    log = tmp_path / "actions.log"

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            {_port_stubs(log=log)}
            proxy shell on --port 1090
            """
        ).strip(),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "--port" in result.stdout + result.stderr


def test_patching_the_listen_port_is_skipped_when_it_already_matches(
    tmp_path: Path,
) -> None:
    env, config_root = _prepare_port_client(tmp_path, port=1085)
    config_file = config_root / "trojan" / "config.json"
    before = config_file.stat().st_mtime_ns

    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_patch_listen_port 1085
            """
        ).strip(),
        env=env,
    )

    assert result.returncode == 0
    assert config_file.stat().st_mtime_ns == before


SYSTEM_PROXY_LIB = ROOT / "scripts" / "lib" / "system-proxy.sh"


def _classify_gnome(config_root: Path, desktop_port: int) -> str:
    result = _run_bash(
        textwrap.dedent(
            f"""
            SYSTEM_PROXY_CONFIG_ROOT="{config_root}"
            source "{SYSTEM_PROXY_LIB}"
            dconf() {{
                case "$2" in
                /system/proxy/mode) printf "'manual'\\n" ;;
                */host) printf "'127.0.0.1'\\n" ;;
                */port) printf '%s\\n' "{desktop_port}" ;;
                esac
            }}
            gnome_proxy_classify
            """
        ).strip(),
    )
    return result.stdout.strip()


def test_desktop_proxy_on_the_configured_port_is_classified_as_ours(
    tmp_path: Path,
) -> None:
    _, config_root = _prepare_port_client(tmp_path, port=1080)

    assert _classify_gnome(config_root, 1080) == "ours"
    assert _classify_gnome(config_root, 3128) == "other"


def test_desktop_proxy_on_the_recorded_port_stays_ours_after_a_config_reset(
    tmp_path: Path,
) -> None:
    """An upgrade resets the configs but leaves the marker.

    Until the next `proxy on` reconciles them the two disagree, and a setting
    naming either one is still ours to revert -- otherwise the uninstaller
    would walk away from a system proxy aimed at a dead port.
    """
    _, config_root = _prepare_port_client(tmp_path, port=1080)
    (config_root / "selected-port").write_text("1085\n", encoding="utf-8")

    assert _classify_gnome(config_root, 1085) == "ours"
    assert _classify_gnome(config_root, 1080) == "ours"
    assert _classify_gnome(config_root, 3128) == "other"


@pytest.mark.parametrize("marker", ["", "garbage\n", "0\n", "1023\n", "70000\n"])
def test_an_unusable_port_marker_falls_back_to_the_configured_port(
    tmp_path: Path, marker: str
) -> None:
    _, config_root = _prepare_port_client(tmp_path, port=1080)
    if marker:
        (config_root / "selected-port").write_text(marker, encoding="utf-8")

    result = _run_bash(
        textwrap.dedent(
            f"""
            SYSTEM_PROXY_CONFIG_ROOT="{config_root}"
            source "{SYSTEM_PROXY_LIB}"
            printf '[%s]\\n' "$(_system_proxy_read_selected_port)"
            _system_proxy_owned_ports
            """
        ).strip(),
    )

    assert result.stdout.splitlines() == ["[]", "1080"]


def test_the_setup_script_and_the_library_agree_on_the_marker_path(
    tmp_path: Path,
) -> None:
    """client-uninstall.sh sources the library alone and must find the same file."""
    env, config_root = _prepare_port_client(tmp_path)

    from_setup = _run_bash(
        f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\n_proxy_selected_port_file',
        env=env,
    ).stdout.strip()
    from_library = _run_bash(
        textwrap.dedent(
            f"""
            SYSTEM_PROXY_CONFIG_ROOT="{config_root}"
            source "{SYSTEM_PROXY_LIB}"
            _system_proxy_selected_port_file
            """
        ).strip(),
    ).stdout.strip()

    assert from_setup == from_library == str(config_root / "selected-port")


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("failure", ["missing", "exit"])
def test_proxy_uninstall_failure_keeps_current_shell(
    version_env: dict[str, str], shell: str, failure: str
) -> None:
    data = Path(version_env["XDG_DATA_HOME"]) / "sing-box"
    data.mkdir(parents=True)
    if failure == "exit":
        (data / "client-uninstall.sh").write_text("exit 7\n")
    result = subprocess.run(
        [
            _require_shell(shell),
            "-f",
            "-c",
            f'''
        source "{SETUP_SCRIPT}"
        export http_proxy=http://127.0.0.1:1080
        proxy uninstall --yes
        result=$?
        printf 'RESULT=%s HTTP=%s\\n' "$result" "$http_proxy"
        typeset -f proxy >/dev/null || exit 99
        ''',
        ],
        env=version_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = 7 if failure == "exit" else 1
    assert f"RESULT={expected} HTTP=http://127.0.0.1:1080" in result.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("modified", [False, True])
def test_uninstall_docker_daemon_requires_exact_ownership(
    version_env: dict[str, str], tmp_path: Path, shell: str, modified: bool
) -> None:
    daemon = tmp_path / "http-proxy.conf"
    log = tmp_path / "systemctl.log"
    config = tmp_path / "docker-config.json"
    config.write_text(
        '{"proxies":{"default":{"httpProxy":"http://127.0.0.1:1085","noProxy":"localhost,127.0.0.0/8,::1,host.docker.internal"}},"auths":{}}'
    )
    result = subprocess.run(
        [
            _require_shell(shell),
            "-f",
            "-c",
            f'''
        source "{SETUP_SCRIPT}"
        plat_is_linux() {{ return 0; }}
        _proxy_read_config_port() {{ echo 1085; }}
        _proxy_read_selected_port() {{ :; }}
        _proxy_docker_config_file() {{ echo "{config}"; }}
        _proxy_docker_daemon_service_dir() {{ echo "{tmp_path}"; }}
        sudo() {{ "$@"; }}
        systemctl() {{ printf '%s\\n' "$*" >> "{log}"; }}
        git() {{ return 1; }}
        _proxy_apply_docker_daemon_proxy 127.0.0.1 1085 || exit 80
        : > "{log}"
        {f'echo "# user modification" >> "{daemon}"' if modified else ":"}
        _proxy_uninstall_settings
        ''',
        ],
        env=version_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert daemon.exists() == modified
    assert log.read_text().splitlines() == (
        [] if modified else ["daemon-reload", "restart docker"]
    )
    assert json.loads(config.read_text()) == {"auths": {}}


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_uninstall_shell_clears_owned_variables_and_default_bypass(
    version_env: dict[str, str], shell: str
) -> None:
    env = {
        key: value
        for key, value in version_env.items()
        if not key.lower().endswith("_proxy")
    }
    result = subprocess.run(
        [
            _require_shell(shell),
            "-f",
            "-c",
            f'''
        source "{SETUP_SCRIPT}"
        _set_proxy_env_vars 127.0.0.1 1085
        _proxy_uninstall_shell 1080 1085
        env | grep -i '_proxy=' && exit 90
        exit 0
        ''',
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_gfw_variant_refuses_before_any_mutation(tmp_path: Path) -> None:
    env, root, binary, validation_log = _prepare_route_client(tmp_path)
    (root / "hysteria2/config-gfw.json").unlink()
    result = _run_bash(
        f'''source "{SETUP_SCRIPT}" >/dev/null 2>&1
        tun_is_safely_off() {{ return 0; }}
        _proxy_client_binary_path() {{ printf '%s\n' "{binary}"; }}
        _proxy_user_service_stop() {{ echo UNEXPECTED_STOP; }}
        proxy route gfw''',
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "Reinstall" in result.stdout + result.stderr
    assert "UNEXPECTED_STOP" not in result.stdout
    assert not validation_log.exists()
    assert (root / "selected-route").read_text() == "china\n"
    for protocol in ("trojan", "hysteria2"):
        assert (root / protocol / "config.json").read_text() == f"{protocol}-china\n"


def test_wsl2_targets_the_shared_loopback(version_env: dict[str, str]) -> None:
    """The client proxy listens on 127.0.0.1 only, so WSL2 needs mirrored networking."""
    result = _run_bash(
        f'''source "{SETUP_SCRIPT}" >/dev/null 2>&1
        _proxy_runtime_kind() {{ printf 'wsl2\\n'; }}
        _proxy_read_config_port() {{ printf '1080\\n'; }}
        _get_proxy_config 2>/dev/null
        _proxy_describe_proxy_target''',
        env=version_env,
    )

    assert result.stdout.splitlines() == ["127.0.0.1 1080", "127.0.0.1:1080"]
