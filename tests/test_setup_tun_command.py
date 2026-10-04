"""Tests for the privileged `proxy tun` toggle in setup.sh.

The transaction is driven entirely through PATH-shimmed `sudo`, `systemctl`,
`systemd-run`, `install`, `ip`, and account tools, and every managed path is
redirected under a temporary root via SBM_TUN_ROOT. Nothing here touches the
developer's real routing table, systemd, or /etc.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"
TUN_LIBRARY = ROOT / "scripts" / "lib" / "tun.sh"


def _require_shell(name: str) -> str:
    shell_path = shutil.which(name)
    if shell_path is None:
        pytest.skip(f"{name} is not available")

    return shell_path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_bin(tmp_path: Path) -> Path:
    """Shims for every privileged tool the transaction reaches for.

    `sudo` runs the command directly, so the whole flow executes as the test
    user against the redirected root. The systemctl and ip shims share a small
    state directory so starting the unit really does make the interface and its
    routes appear, and stopping it really does take them away -- otherwise a
    verification or cleanup check would be asserting against a constant.

    Every shim logs its arguments. The ordering in that log is where most of
    this feature's correctness lives.
    """
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    state = tmp_path / "fakestate"
    state.mkdir()

    real_install = shutil.which("install")
    if real_install is None:
        pytest.skip("install is not available")

    _write_executable(
        fake_bin / "sudo",
        f"""\
        #!/usr/bin/env bash
        if [[ "${{1:-}}" == "-n" ]]; then
            shift
            if [[ "${{FAKE_SUDO_NONINTERACTIVE_STATUS:-0}}" != "0" ]]; then
                exit "${{FAKE_SUDO_NONINTERACTIVE_STATUS}}"
            fi
            if [[ "${{1:-}}" == "true" ]]; then
                exit 0
            fi
        fi
        if [[ "${{1:-}}" == "-v" ]]; then
            exit 0
        fi
        printf 'sudo %s\\n' "$*" >> "{log}"
        exec "$@"
        """,
    )

    # `install -o root -g root` cannot work unprivileged, so the shim keeps the
    # mode and drops the ownership flags. Ownership is asserted from the logged
    # arguments instead.
    _write_executable(
        fake_bin / "install",
        f"""\
        #!/usr/bin/env bash
        printf 'install %s\\n' "$*" >> "{log}"
        args=()
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -o|-g)
                    # Real `install` fails on a user or group that does not
                    # exist yet, so the shim has to as well. Dropping these
                    # silently is what let an ordering bug reach a real run.
                    if [[ "${{2:-}}" == "sing-box-tun" \
                        && ! -f "{state}/account" \
                        && "${{FAKE_ACCOUNT_EXISTS:-0}}" != "1" ]]; then
                        printf "install: invalid group 'sing-box-tun'\\n" >&2
                        exit 1
                    fi
                    shift 2
                    ;;
                *) args+=("$1"); shift ;;
            esac
        done
        exec "{real_install}" "${{args[@]}}"
        """,
    )

    _write_executable(
        fake_bin / "chown",
        f"""\
        #!/usr/bin/env bash
        printf 'chown %s\\n' "$*" >> "{log}"
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "systemctl",
        f"""\
        #!/usr/bin/env bash
        printf 'systemctl %s\\n' "$*" >> "{log}"
        unit_state="{state}/active-state"
        interface="{state}/interface"

        if [[ "${{1:-}}" == "--user" ]]; then
            shift
            case "${{1:-}}" in
                is-active) exit "${{FAKE_USER_IS_ACTIVE_STATUS:-1}}" ;;
                is-enabled) exit "${{FAKE_USER_IS_ENABLED_STATUS:-1}}" ;;
            esac
            exit "${{FAKE_USER_SYSTEMCTL_STATUS:-0}}"
        fi

        case "$*" in
            "start sing-box-manager-tun.service")
                printf 'active\\n' > "$unit_state"
                if [[ "${{FAKE_TUN_NO_INTERFACE:-0}}" != "1" ]]; then
                    : > "$interface"
                fi
                exit 0
                ;;
            "stop sing-box-manager-tun.service")
                printf 'inactive\\n' > "$unit_state"
                if [[ "${{FAKE_TUN_STICKY_INTERFACE:-0}}" != "1" ]]; then
                    rm -f "$interface"
                fi
                exit 0
                ;;
        esac

        if [[ "${{1:-}}" == "show" ]]; then
            for arg in "$@"; do
                case "$arg" in
                    --property=ActiveState)
                        if [[ -n "${{FAKE_ACTIVE_STATE:-}}" ]]; then
                            printf '%s\\n' "$FAKE_ACTIVE_STATE"
                        elif [[ -f "$unit_state" ]]; then
                            cat "$unit_state"
                        else
                            printf 'inactive\\n'
                        fi
                        ;;
                    --property=SubState) printf '%s\\n' "${{FAKE_SUB_STATE:-running}}" ;;
                    --property=MainPID) printf '%s\\n' "${{FAKE_MAIN_PID:-4242}}" ;;
                    --property=NRestarts) printf '%s\\n' "${{FAKE_RESTARTS:-0}}" ;;
                    --property=LoadState) printf '%s\\n' "${{FAKE_LOAD_STATE:-not-found}}" ;;
                esac
            done
            exit 0
        fi
        if [[ "${{1:-}}" == "is-enabled" ]]; then
            printf '%s\\n' "${{FAKE_ENABLED_STATE:-disabled}}"
            exit 0
        fi
        if [[ "${{1:-}}" == "is-active" ]]; then
            exit "${{FAKE_IS_ACTIVE_STATUS:-1}}"
        fi
        exit "${{FAKE_SYSTEMCTL_STATUS:-0}}"
        """,
    )

    _write_executable(
        fake_bin / "systemd-run",
        f"""\
        #!/usr/bin/env bash
        printf 'systemd-run %s\\n' "$*" >> "{log}"
        exit "${{FAKE_SYSTEMD_RUN_STATUS:-0}}"
        """,
    )

    _write_executable(
        fake_bin / "ip",
        f"""\
        #!/usr/bin/env bash
        printf 'ip %s\\n' "$*" >> "{log}"
        interface="{state}/interface"
        case "$*" in
            *"link show dev"*)
                [[ -f "$interface" ]] || exit 1
                printf '9: sbm-tun0: <POINTOPOINT,UP> mtu 9000\\n'
                ;;
            *"route show table all"*)
                if [[ -f "$interface" ]]; then
                    printf 'default dev sbm-tun0 scope link\\n'
                fi
                ;;
        esac
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "getent",
        f"""\
        #!/usr/bin/env bash
        case "${{1:-}}" in
            passwd)
                if [[ "${{2:-}}" == "sing-box-tun" ]] \
                    && [[ -f "{state}/account" || "${{FAKE_ACCOUNT_EXISTS:-0}}" == "1" ]]; then
                    printf 'sing-box-tun:x:%s:%s::/nonexistent:%s\\n' \
                        "${{FAKE_ACCOUNT_UID:-142}}" "${{FAKE_ACCOUNT_UID:-142}}" \
                        "${{FAKE_ACCOUNT_SHELL:-/usr/sbin/nologin}}"
                    exit 0
                fi
                exit 2
                ;;
            ahosts)
                exit "${{FAKE_DNS_STATUS:-0}}"
                ;;
        esac
        exit 2
        """,
    )

    _write_executable(
        fake_bin / "useradd",
        f"""\
        #!/usr/bin/env bash
        printf 'useradd %s\\n' "$*" >> "{log}"
        if [[ "${{FAKE_USERADD_STATUS:-0}}" != "0" ]]; then
            exit "${{FAKE_USERADD_STATUS}}"
        fi
        : > "{state}/account"
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "userdel",
        f"""\
        #!/usr/bin/env bash
        printf 'userdel %s\\n' "$*" >> "{log}"
        rm -f "{state}/account"
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "passwd",
        """\
        #!/usr/bin/env bash
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "curl",
        """\
        #!/usr/bin/env bash
        if [[ -f "${FAKE_TX_COUNTER:-}" ]]; then
            printf '%s' "$(($(cat "$FAKE_TX_COUNTER") + 12))" > "$FAKE_TX_COUNTER"
        fi
        printf '%s' "${FAKE_EGRESS_CODE:-204}"
        exit 0
        """,
    )

    return fake_bin


def _client_tree(tmp_path: Path, *, protocol: str = "trojan") -> dict[str, Path]:
    """A user-scope install: binary, mixed config, and the TUN profile."""
    home = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    bin_dir = home / ".local" / "bin"
    protocol_dir = config_home / "sing-box" / protocol
    service_dir = config_home / "systemd" / "user"

    bin_dir.mkdir(parents=True)
    protocol_dir.mkdir(parents=True)
    service_dir.mkdir(parents=True)

    _write_executable(
        bin_dir / "sing-box",
        """\
        #!/usr/bin/env bash
        exit "${FAKE_SING_BOX_CHECK_STATUS:-0}"
        """,
    )
    (protocol_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (protocol_dir / "config-tun.json").write_text(
        '{"inbounds":[{"type":"tun"}]}\n', encoding="utf-8"
    )
    (config_home / "sing-box" / "selected-protocol").write_text(
        f"{protocol}\n", encoding="utf-8"
    )
    (service_dir / f"sing-box-{protocol}.service").write_text(
        "[Service]\n", encoding="utf-8"
    )

    return {"home": home, "config_home": config_home, "protocol_dir": protocol_dir}


def _tun_env(tmp_path: Path, tree: dict[str, Path], fake_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tree["home"])
    env["XDG_CONFIG_HOME"] = str(tree["config_home"])
    env["SBM_TUN_ROOT"] = str(tmp_path / "sysroot")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_MAIN_PID"] = "4242"
    env["FAKE_RESTARTS"] = "0"
    # Stands in for /sys/class/net/sbm-tun0/statistics/tx_packets, which no test
    # can create. The curl shim bumps it, so verification only passes when the
    # egress probe actually ran.
    counter = tmp_path / "tx_packets"
    counter.write_text("100", encoding="utf-8")
    env["FAKE_TX_COUNTER"] = str(counter)
    # A developer running the suite over SSH must not change these results.
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        env.pop(name, None)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(name, None)
    return env


def _run_tun(
    script: str,
    *,
    env: dict[str, str],
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_require_shell("bash"), "--noprofile", "--norc", "-ic", script],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_tun_zsh(
    script: str,
    *,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """setup.sh is sourced into interactive zsh too, so it has to run there."""
    return subprocess.run(
        [_require_shell("zsh"), "-fic", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _preamble(extra: str = "") -> str:
    """Source setup.sh and stub the facts a unit test cannot supply itself."""
    base = textwrap.dedent(
        f"""
        source "{SETUP_SCRIPT}" >/dev/null 2>&1
        # Properties of the developer's machine, not of this feature.
        _proxy_runtime_kind() {{ printf 'linux\\n'; }}
        tun_systemd_available() {{ return 0; }}
        _proxy_tun_dev_tun_present() {{ return 0; }}
        _proxy_tun_interface_tx_packets() {{
            if [[ ! -f "$FAKE_TX_COUNTER" ]]; then
                return 1
            fi
            cat "$FAKE_TX_COUNTER"
        }}
        sleep() {{ return 0; }}
        """
    ).strip()

    if extra:
        base = f"{base}\n{textwrap.dedent(extra).strip()}"

    return base


# --- state model -------------------------------------------------------------


def test_safely_off_requires_the_root_attestation_not_just_an_inactive_unit(
    tmp_path: Path,
) -> None:
    """`is-active` alone races startup and cannot see a half-finished change."""
    sysroot = tmp_path / "sysroot"
    unit_dir = sysroot / "etc" / "systemd" / "system"
    var_lib = sysroot / "var" / "lib"
    unit_dir.mkdir(parents=True)
    var_lib.mkdir(parents=True)
    (unit_dir / "sing-box-manager-tun.service").write_text("[Unit]\n", encoding="utf-8")

    fake_bin = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["SBM_TUN_ROOT"] = str(sysroot)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_ACTIVE_STATE"] = "inactive"
    env["FAKE_SUB_STATE"] = "dead"
    env["FAKE_ENABLED_STATE"] = "disabled"

    def probe() -> str:
        return _run_tun(
            f'source "{TUN_LIBRARY}"; tun_is_safely_off && echo yes || echo no',
            env=env,
        ).stdout.strip()

    # Installed and inactive, but nothing has attested that the interface and
    # routes were cleaned up.
    assert probe() == "no"

    status = var_lib / "sing-box-manager-tun.status"
    status.write_text("dirty\n", encoding="utf-8")
    assert probe() == "no"

    status.write_text("safe-off\n", encoding="utf-8")
    assert probe() == "yes"

    # A transaction marker outranks every other signal.
    (var_lib / "sing-box-manager-tun.transaction").write_text("in-progress\n")
    assert probe() == "no"


def test_safely_off_refuses_an_enabled_or_masked_unit(tmp_path: Path) -> None:
    sysroot = tmp_path / "sysroot"
    unit_dir = sysroot / "etc" / "systemd" / "system"
    var_lib = sysroot / "var" / "lib"
    unit_dir.mkdir(parents=True)
    var_lib.mkdir(parents=True)
    (unit_dir / "sing-box-manager-tun.service").write_text("[Unit]\n", encoding="utf-8")
    (var_lib / "sing-box-manager-tun.status").write_text("safe-off\n", encoding="utf-8")

    fake_bin = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["SBM_TUN_ROOT"] = str(sysroot)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_ACTIVE_STATE"] = "inactive"
    env["FAKE_SUB_STATE"] = "dead"

    for enabled_state in ("enabled", "masked", "static"):
        env["FAKE_ENABLED_STATE"] = enabled_state
        result = _run_tun(
            f'source "{TUN_LIBRARY}"; tun_is_safely_off && echo yes || echo no',
            env=env,
        )
        assert result.stdout.strip() == "no", enabled_state


def test_a_never_installed_tunnel_is_safely_off(tmp_path: Path) -> None:
    fake_bin = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["SBM_TUN_ROOT"] = str(tmp_path / "empty")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_tun(
        f'source "{TUN_LIBRARY}"; tun_is_safely_off && echo yes || echo no',
        env=env,
    )
    assert result.stdout.strip() == "yes"


# --- guards ------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "proxy on --force",
        "proxy service on",
        "proxy shell on --force",
        "proxy env on --force",
        "proxy git on --force",
        "proxy desktop on --force",
        "proxy docker on --force",
    ],
)
def test_mixed_commands_refuse_under_an_active_tunnel_even_with_force(
    tmp_path: Path, command: str
) -> None:
    sysroot = tmp_path / "sysroot"
    (sysroot / "etc" / "systemd" / "system").mkdir(parents=True)
    (sysroot / "var" / "lib").mkdir(parents=True)
    (sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service").write_text(
        "[Unit]\n", encoding="utf-8"
    )
    (sysroot / "var" / "lib" / "sing-box-manager-tun.status").write_text(
        "active\n", encoding="utf-8"
    )

    fake_bin = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["SBM_TUN_ROOT"] = str(sysroot)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_tun(f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\n{command}', env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, combined
    assert "machine-wide TUN mode is not safely off" in combined
    assert "proxy tun off" in combined


def test_off_commands_clear_their_surface_and_report_the_tunnel(
    tmp_path: Path,
) -> None:
    sysroot = tmp_path / "sysroot"
    (sysroot / "etc" / "systemd" / "system").mkdir(parents=True)
    (sysroot / "var" / "lib").mkdir(parents=True)
    (sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service").write_text(
        "[Unit]\n", encoding="utf-8"
    )
    (sysroot / "var" / "lib" / "sing-box-manager-tun.status").write_text(
        "active\n", encoding="utf-8"
    )

    fake_bin = _fake_bin(tmp_path)
    env = os.environ.copy()
    env["SBM_TUN_ROOT"] = str(sysroot)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["http_proxy"] = "http://127.0.0.1:1080"

    result = _run_tun(
        f"""
        source "{SETUP_SCRIPT}" >/dev/null 2>&1
        proxy shell off
        printf 'http_proxy=[%s]\\n' "${{http_proxy:-}}"
        """,
        env=env,
    )

    assert result.returncode == 0
    assert "http_proxy=[]" in result.stdout
    assert "TUN mode is still active" in result.stdout


def test_the_mixed_user_unit_refuses_to_start_during_a_transaction(
    tmp_path: Path,
) -> None:
    """A delayed `systemctl --user start` must not race a TUN transaction."""
    marker = tmp_path / "sysroot" / "var" / "lib" / "sing-box-manager-tun.transaction"
    marker.parent.mkdir(parents=True)
    guard = f"/bin/sh -c '! test -e {marker}'"

    assert subprocess.run(guard, shell=True, check=False).returncode == 0
    marker.write_text("in-progress\n", encoding="utf-8")
    assert subprocess.run(guard, shell=True, check=False).returncode != 0


# --- preflight ---------------------------------------------------------------


def test_tun_on_refuses_a_remote_shell(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["SSH_CONNECTION"] = "10.0.0.2 51234 10.0.0.1 22"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "remote shell" in combined
    assert not (tmp_path / "sysroot" / "etc").exists()


def test_tun_on_refuses_a_non_linux_runtime(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(
        _preamble("_proxy_runtime_kind() { printf 'wsl2\\n'; }") + "\nproxy tun on",
        env=env,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "native Linux only" in combined


def test_tun_on_refuses_when_the_protocol_has_no_tun_profile(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    (tree["protocol_dir"] / "config-tun.json").unlink()
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "no installed TUN profile" in combined


def test_tun_on_refuses_a_symlinked_source_config(tmp_path: Path) -> None:
    """A symlink is a path the privileged half would otherwise follow."""
    tree = _client_tree(tmp_path)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}\n", encoding="utf-8")
    tun_config = tree["protocol_dir"] / "config-tun.json"
    tun_config.unlink()
    tun_config.symlink_to(elsewhere)

    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "not a regular file" in combined


def test_tun_on_refuses_when_another_uid_owns_the_manifest(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    manifest = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"owner_uid={os.getuid() + 1}\nprotocol=trojan\n", encoding="utf-8"
    )

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "owned by another local user" in combined
    assert "automatic takeover is not supported" in combined


# --- provisioning ------------------------------------------------------------


def test_tun_on_provisions_exact_paths_modes_account_unit_and_manifest(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined

    sysroot = tmp_path / "sysroot"
    binary = sysroot / "usr" / "local" / "libexec" / "sing-box-manager-tun" / "sing-box"
    config = sysroot / "etc" / "sing-box-manager" / "tun" / "config.json"
    manifest = sysroot / "etc" / "sing-box-manager" / "tun" / "manifest"
    unit = sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"

    assert binary.is_file()
    assert config.read_text(encoding="utf-8") == '{"inbounds":[{"type":"tun"}]}\n'
    assert unit.is_file()
    assert manifest.is_file()

    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644

    manifest_text = manifest.read_text(encoding="utf-8")
    assert f"owner_uid={os.getuid()}" in manifest_text
    assert "protocol=trojan" in manifest_text
    assert "runtime_account_created=true" in manifest_text
    # Provenance only: no credentials and no rendered config values.
    assert "password" not in manifest_text

    unit_text = unit.read_text(encoding="utf-8")
    assert "User=sing-box-tun" in unit_text
    assert "AmbientCapabilities=CAP_NET_ADMIN" in unit_text
    assert "CAP_NET_BIND_SERVICE" not in unit_text
    assert "DeviceAllow=/dev/net/tun rw" in unit_text
    assert f"ExecStart={binary} run -D " in unit_text

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    # The account is created before the capability probe, and the probe runs
    # under the same sandbox the unit will use.
    assert log.index("useradd ") < log.index("systemd-run --wait")
    assert "--property=CapabilityBoundingSet=CAP_NET_ADMIN" in log
    assert "--property=DeviceAllow=/dev/net/tun rw" in log
    # Ownership is set at creation time, never widened and then narrowed.
    assert "install -o root -g root -m 0755" in log
    assert "install -o root -g sing-box-tun -m 0640" in log

    # Not enabled without --persist.
    assert "systemctl enable sing-box-manager-tun.service" not in log
    status = (sysroot / "var" / "lib" / "sing-box-manager-tun.status").read_text()
    assert status.strip() == "active"
    assert not (sysroot / "var" / "lib" / "sing-box-manager-tun.transaction").exists()


def test_tun_on_persist_enables_only_after_verification(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(f"{_preamble()}\nproxy tun on --persist", env=env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    enable_index = log.index("systemctl enable sing-box-manager-tun.service")
    start_index = log.index("systemctl start sing-box-manager-tun.service")
    assert start_index < enable_index


def test_tun_on_stops_mixed_services_and_arms_the_safeguard_before_publishing(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    safeguard_index = log.index("--unit=sing-box-manager-tun-safeguard")
    quiesce_index = log.index("--user disable --now sing-box-trojan.service")
    reload_index = log.index("systemctl daemon-reload")
    start_index = log.index("systemctl start sing-box-manager-tun.service")

    # Armed before anything is stopped, so a lost shell still recovers.
    assert safeguard_index < quiesce_index
    # Mixed mode is down before the tunnel comes up; the two never overlap.
    assert quiesce_index < reload_index < start_index
    # The safeguard is cancelled once the generation is verified.
    assert "systemctl stop sing-box-manager-tun-safeguard.timer" in log


def test_tun_on_rolls_back_when_the_staged_config_fails_validation(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["FAKE_SING_BOX_CHECK_STATUS"] = "1"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "failed sing-box validation" in combined

    sysroot = tmp_path / "sysroot"
    # Nothing was published, and the account this run created is gone again.
    assert not (sysroot / "etc" / "sing-box-manager" / "tun" / "config.json").exists()
    assert not (
        sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"
    ).exists()
    assert not (sysroot / "var" / "lib" / "sing-box-manager-tun.transaction").exists()
    assert (
        sysroot / "var" / "lib" / "sing-box-manager-tun.status"
    ).read_text().strip() == "safe-off"

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert "userdel sing-box-tun" in log


def test_tun_on_rolls_back_when_the_interface_never_appears(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["FAKE_TUN_NO_INTERFACE"] = "1"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "did not appear" in combined
    assert "Rolling back" in combined

    sysroot = tmp_path / "sysroot"
    assert not (sysroot / "etc" / "sing-box-manager" / "tun" / "manifest").exists()
    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    # Stopped and disabled before any file is put back.
    assert log.index("systemctl disable sing-box-manager-tun.service") < log.rindex(
        "systemctl daemon-reload"
    )


def test_tun_on_rolls_back_when_the_capability_probe_fails(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["FAKE_SYSTEMD_RUN_STATUS"] = "1"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "refused to create a TUN device" in combined

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert "userdel sing-box-tun" in log


def test_tun_on_refuses_an_unexpected_existing_account(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["FAKE_ACCOUNT_EXISTS"] = "1"
    # A regular login uid, not the locked system account we would have created.
    env["FAKE_ACCOUNT_UID"] = "1500"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "unexpected account" in combined


def test_a_second_operation_refuses_while_the_lock_is_held(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    control = tmp_path / "sysroot" / "var" / "lib" / "sing-box-manager-tun-control"
    (control / "lock").mkdir(parents=True)
    # A live holder: this very test process.
    (control / "lock" / "owner").write_text(
        f"pid={os.getpid()}\nboot={Path('/proc/sys/kernel/random/boot_id').read_text().strip()}\n",
        encoding="utf-8",
    )

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "holds the transaction lock" in combined


def test_a_lock_from_a_previous_boot_is_reclaimed(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    control = tmp_path / "sysroot" / "var" / "lib" / "sing-box-manager-tun-control"
    (control / "lock").mkdir(parents=True)
    (control / "lock" / "owner").write_text(
        f"pid={os.getpid()}\nboot=00000000-0000-0000-0000-000000000000\n",
        encoding="utf-8",
    )

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


# --- off ---------------------------------------------------------------------


def test_tun_off_stops_the_service_and_leaves_the_managed_files(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    result = _run_tun(f"{_preamble()}\nproxy tun off", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "Mixed mode was not restored" in combined
    assert "proxy on" in combined

    sysroot = tmp_path / "sysroot"
    assert (sysroot / "etc" / "sing-box-manager" / "tun" / "config.json").exists()
    assert (sysroot / "etc" / "sing-box-manager" / "tun" / "manifest").exists()
    assert (
        sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"
    ).exists()
    assert (
        sysroot / "var" / "lib" / "sing-box-manager-tun.status"
    ).read_text().strip() == "safe-off"


def test_tun_off_is_an_error_when_routing_artifacts_survive(tmp_path: Path) -> None:
    """A failure to clean up must not be downgraded to a warning."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    off_env = dict(env)
    # Stopping the unit leaves the interface and its routes behind.
    off_env["FAKE_TUN_STICKY_INTERFACE"] = "1"
    result = _run_tun(f"{_preamble()}\nproxy tun off", env=off_env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "still present" in combined
    status = (
        tmp_path / "sysroot" / "var" / "lib" / "sing-box-manager-tun.status"
    ).read_text()
    # Conservative: never claims safe-off after a failed cleanup.
    assert status.strip() == "dirty"


def test_tun_off_refuses_when_another_uid_owns_the_manifest(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    manifest = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "manifest"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f"owner_uid={os.getuid()}", f"owner_uid={os.getuid() + 1}"
        ),
        encoding="utf-8",
    )

    result = _run_tun(f"{_preamble()}\nproxy tun off", env=env)
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "owned by another local user" in combined


# --- re-sync and protocol switching ------------------------------------------


def test_re_sync_restarts_an_active_service_and_preserves_persistence(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on --persist", env=env).returncode == 0

    # New credentials in the source profile must actually take effect.
    (tree["protocol_dir"] / "config-tun.json").write_text(
        '{"inbounds":[{"type":"tun"}],"rotated":true}\n', encoding="utf-8"
    )
    (tmp_path / "commands.log").unlink()

    resync_env = dict(env)
    resync_env["FAKE_ENABLED_STATE"] = "enabled"
    result = _run_tun(f"{_preamble()}\nproxy tun on", env=resync_env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    config = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "config.json"
    assert "rotated" in config.read_text(encoding="utf-8")

    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    # Re-sync without --persist keeps the installation persistent.
    assert "systemctl enable sing-box-manager-tun.service" in log


def test_protocol_switch_under_tun_runs_the_staged_transaction(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    hysteria2_dir = tree["config_home"] / "sing-box" / "hysteria2"
    hysteria2_dir.mkdir()
    (hysteria2_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (hysteria2_dir / "config-tun.json").write_text(
        '{"inbounds":[{"type":"tun"}],"protocol":"hysteria2"}\n', encoding="utf-8"
    )

    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    switch_env = dict(env)
    result = _run_tun(
        _preamble("tun_is_safely_off() { return 1; }") + "\nproxy protocol hysteria2",
        env=switch_env,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "switched to hysteria2" in combined

    config = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "config.json"
    assert '"protocol":"hysteria2"' in config.read_text(encoding="utf-8")
    # `selected-protocol` is saved only after the new generation verifies.
    selected = tree["config_home"] / "sing-box" / "selected-protocol"
    assert selected.read_text(encoding="utf-8").strip() == "hysteria2"


def test_protocol_switch_restores_the_previous_generation_on_failure(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    hysteria2_dir = tree["config_home"] / "sing-box" / "hysteria2"
    hysteria2_dir.mkdir()
    (hysteria2_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (hysteria2_dir / "config-tun.json").write_text(
        '{"inbounds":[{"type":"tun"}],"protocol":"hysteria2"}\n', encoding="utf-8"
    )

    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    config = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "config.json"
    before = config.read_text(encoding="utf-8")

    switch_env = dict(env)
    switch_env["FAKE_TUN_NO_INTERFACE"] = "1"
    result = _run_tun(
        _preamble("tun_is_safely_off() { return 1; }") + "\nproxy protocol hysteria2",
        env=switch_env,
    )

    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == before
    selected = tree["config_home"] / "sing-box" / "selected-protocol"
    assert selected.read_text(encoding="utf-8").strip() == "trojan"


# --- diagnostics -------------------------------------------------------------


def test_check_tun_reports_state_without_prompting_for_sudo(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    # Non-interactive sudo unavailable: root-only fields must be skipped, not
    # turned into a password prompt.
    report_env = dict(env)
    report_env["FAKE_SUDO_NONINTERACTIVE_STATUS"] = "1"
    result = _run_tun(f"{_preamble()}\nproxy check tun", env=report_env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "sudo would prompt" in combined
    assert "Owner uid" not in combined
    assert "sbm-tun0" in combined
    assert "cannot be enumerated" in combined

    # With sudo already unlocked the provenance shows up.
    result = _run_tun(f"{_preamble()}\nproxy check tun", env=env)
    combined = f"{result.stdout}\n{result.stderr}"
    assert f"Owner uid: {os.getuid()}" in combined
    # The manifest is authoritative for what is running; `Selected protocol` is
    # only what the next activation would use.
    assert "Active protocol: trojan" in combined
    assert "Managed files: match the manifest" in combined


def test_check_tun_reports_tampered_managed_files(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    config = tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "config.json"
    config.write_text('{"inbounds":[{"type":"tun"}],"tampered":true}\n', encoding="utf-8")

    result = _run_tun(f"{_preamble()}\nproxy check tun", env=env)
    assert "Managed files: do not match the manifest" in result.stdout


def test_check_service_reports_the_tun_singleton_separately(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    result = _run_tun(f"{_preamble()}\nproxy check service", env=env)
    assert "TUN service: sing-box-manager-tun.service" in result.stdout


# --- localization ------------------------------------------------------------


@pytest.mark.parametrize(
    ("english", "chinese"),
    [
        ("Machine-wide TUN mode is off.", "全局 TUN 模式已关闭。"),
        (
            "Refusing {}: machine-wide TUN mode is not safely off.",
            "拒绝执行 {}：全局 TUN 模式尚未安全关闭。",
        ),
        (
            "Machine-wide TUN mode is owned by another local user (uid {}).",
            "全局 TUN 模式属于另一位本地用户（uid {}）。",
        ),
        ("Rolling back the TUN transaction.", "正在回滚 TUN 事务。"),
        ("Enable machine-wide TUN mode", "启用全局 TUN 模式"),
        ("Safely off", "安全关闭"),
    ],
)
def test_tun_strings_are_translated_for_zh_cn(english: str, chinese: str) -> None:
    env = os.environ.copy()
    env["FORCE_LANG"] = "zh_CN"

    result = _run_tun(
        f'source "{SETUP_SCRIPT}" >/dev/null 2>&1\n_t {english!r}',
        env=env,
        check=True,
    )

    assert result.stdout.strip() == chinese


def test_tun_on_refuses_when_a_mixed_service_will_not_stop(tmp_path: Path) -> None:
    """The TUN's route capture would otherwise capture the mixed outbound."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    # The user unit reports itself active even after `disable --now`.
    env["FAKE_USER_IS_ACTIVE_STATUS"] = "0"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Failed to stop mixed proxy service sing-box-trojan.service" in combined
    # Nothing was published while both would have been running.
    assert not (
        tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "manifest"
    ).exists()


def test_tun_on_refuses_a_foreign_system_scope_sing_box_service(
    tmp_path: Path,
) -> None:
    """A system-scope sing-box unit is not this client's to stop."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["FAKE_IS_ACTIVE_STATUS"] = "0"

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "outside this client is active" in combined


def test_tun_on_clears_only_proxy_settings_this_client_owns(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["http_proxy"] = "http://127.0.0.1:1080"
    env["HTTPS_PROXY"] = "http://127.0.0.1:1080"

    result = _run_tun(
        f"""{_preamble()}
        proxy tun on
        printf 'http_proxy=[%s]\\n' "${{http_proxy:-}}"
        printf 'HTTPS_PROXY=[%s]\\n' "${{HTTPS_PROXY:-}}"
        """,
        env=env,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "http_proxy=[]" in result.stdout
    assert "HTTPS_PROXY=[]" in result.stdout
    # Process-local state elsewhere cannot be reached, and the command says so.
    assert "may still point at the stopped mixed inbound" in result.stdout


def test_tun_on_leaves_a_foreign_proxy_alone_and_reports_it(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    env["http_proxy"] = "http://corporate.example.com:3128"

    result = _run_tun(
        f"""{_preamble()}
        proxy tun on
        printf 'http_proxy=[%s]\\n' "${{http_proxy:-}}"
        """,
        env=env,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "does not own was left unchanged" in result.stdout
    assert "http_proxy=[http://corporate.example.com:3128]" in result.stdout


def test_an_interrupted_transaction_is_recovered_before_new_work(
    tmp_path: Path,
) -> None:
    """A journal that outlived its process means an unverified generation."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    # Simulate a process lost mid-transaction: the journal and the public
    # marker survive, and the managed files are the half-published generation.
    sysroot = tmp_path / "sysroot"
    control = sysroot / "var" / "lib" / "sing-box-manager-tun-control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "journal").write_text(
        f"phase=published\nowner_uid={os.getuid()}\nprotocol=trojan\n"
        "previous_generation=none\nprevious_tun_active=false\n"
        "previous_tun_enabled=false\nmixed_services=\naccount_created=false\n",
        encoding="utf-8",
    )
    (sysroot / "var" / "lib" / "sing-box-manager-tun.transaction").write_text(
        "in-progress\n", encoding="utf-8"
    )
    (tmp_path / "commands.log").unlink()

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "Recovering an interrupted TUN transaction" in combined
    assert "Interrupted at phase published" in combined
    # Recovery ran before the new generation was published.
    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert log.index("systemctl disable sing-box-manager-tun.service") < log.index(
        "systemctl start sing-box-manager-tun.service"
    )
    assert not (control / "journal").exists()
    assert not (sysroot / "var" / "lib" / "sing-box-manager-tun.transaction").exists()


def test_an_interrupted_transaction_owned_by_another_uid_is_refused(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    control = tmp_path / "sysroot" / "var" / "lib" / "sing-box-manager-tun-control"
    control.mkdir(parents=True)
    (control / "journal").write_text(
        f"phase=quiesced\nowner_uid={os.getuid() + 1}\n", encoding="utf-8"
    )

    result = _run_tun(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "owned by another local user" in combined
    assert (control / "journal").exists()


# --- zsh ---------------------------------------------------------------------


def test_the_whole_transaction_runs_under_zsh(tmp_path: Path) -> None:
    """zsh makes `status` read-only and does not word-split `$var`.

    Both differences broke a real `proxy tun on`, and neither is visible from
    bash, so the flow runs end to end under zsh with more than one mixed
    service in the snapshot -- a loop that fails to split iterates once.
    """
    tree = _client_tree(tmp_path)
    service_dir = tree["config_home"] / "systemd" / "user"
    hysteria2_dir = tree["config_home"] / "sing-box" / "hysteria2"
    hysteria2_dir.mkdir()
    (hysteria2_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (service_dir / "sing-box-hysteria2.service").write_text(
        "[Service]\n", encoding="utf-8"
    )

    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun_zsh(f"{_preamble()}\nproxy tun on", env=env)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "read-only variable" not in combined
    assert (
        tmp_path / "sysroot" / "etc" / "sing-box-manager" / "tun" / "manifest"
    ).is_file()

    # Every snapshot entry was acted on, not just the first.
    log = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert "--user disable --now sing-box-trojan.service" in log
    assert "--user disable --now sing-box-hysteria2.service" in log


def test_tun_off_and_check_run_under_zsh(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun_zsh(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    result = _run_tun_zsh(f"{_preamble()}\nproxy tun off", env=env)
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "read-only variable" not in combined

    result = _run_tun_zsh(f"{_preamble()}\nproxy check tun", env=env)
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "read-only variable" not in combined
    assert "sbm-tun0" in combined


def test_sourcing_setup_does_not_disturb_the_zsh_path(tmp_path: Path) -> None:
    """`local path=...` in zsh rewrites PATH for the length of the call."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)

    result = _run_tun_zsh(
        f"""
        source "{SETUP_SCRIPT}" >/dev/null 2>&1
        _proxy_tun_is_plain_file "{tree["protocol_dir"]}/config-tun.json" || exit 1
        display_path "{tree["protocol_dir"]}" >/dev/null
        tun_sha256_of "{tree["protocol_dir"]}/config-tun.json" >/dev/null || exit 1
        command -v printf >/dev/null || exit 1
        print -r -- "PATH intact"
        """,
        env=env,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "PATH intact" in result.stdout


def test_admin_commands_resolve_when_sbin_is_missing_from_path(
    tmp_path: Path,
) -> None:
    """Plenty of interactive PATHs omit sbin for a non-root user."""
    sbin = tmp_path / "usr" / "sbin"
    sbin.mkdir(parents=True)
    _write_executable(sbin / "ip", "#!/usr/bin/env bash\nexit 0\n")

    env = os.environ.copy()
    env["PATH"] = "/nonexistent"
    env["SBM_TUN_SBIN_DIRS"] = str(sbin)

    result = _run_tun(
        f'source "{TUN_LIBRARY}"; tun_resolve_admin_command ip || echo "unresolved"',
        env=env,
    )

    assert result.stdout.strip() == str(sbin / "ip")


def test_cleanup_verification_fails_closed_when_ip_is_unavailable(
    tmp_path: Path,
) -> None:
    """"No artifacts" must not be able to mean "ip could not be found"."""
    env = os.environ.copy()
    env["PATH"] = "/nonexistent"
    env["SBM_TUN_SBIN_DIRS"] = str(tmp_path / "empty-sbin")
    env["SBM_TUN_ROOT"] = str(tmp_path / "sysroot")

    result = _run_tun(
        f"""
        source "{TUN_LIBRARY}"
        if tun_ip_command >/dev/null 2>&1; then
            echo "resolved"
        else
            echo "unresolved"
        fi
        """,
        env=env,
    )

    assert result.stdout.strip() == "unresolved"


def test_check_tun_says_what_boot_persistence_and_auto_redirect_mean(
    tmp_path: Path,
) -> None:
    """Raw `systemctl is-enabled` output next to an active service misreads."""
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    result = _run_tun(f"{_preamble()}\nproxy check tun", env=env)
    combined = f"{result.stdout}\n{result.stderr}"

    # "Enabled: disabled" beside "Status: active" reads as a contradiction.
    assert "Enabled: disabled" not in combined
    assert "Start at boot: no" in combined
    assert "will not start after a reboot" in combined
    assert "proxy tun on --persist" in combined

    # auto_redirect is a property of the profile, not a status of the tunnel,
    # and it is read from that profile rather than assumed.
    assert "Profile auto_redirect: off" in combined
    # nftables only matters when auto_redirect is on, so it is not reported.
    assert "nftables" not in combined


def test_check_tun_reports_boot_persistence_after_persist(tmp_path: Path) -> None:
    tree = _client_tree(tmp_path)
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on --persist", env=env).returncode == 0

    report_env = dict(env)
    report_env["FAKE_ENABLED_STATE"] = "enabled"
    result = _run_tun(f"{_preamble()}\nproxy check tun", env=report_env)
    combined = f"{result.stdout}\n{result.stderr}"

    assert "Start at boot: yes" in combined
    assert "will not start after a reboot" not in combined


def test_check_tun_reports_nftables_only_when_auto_redirect_is_on(
    tmp_path: Path,
) -> None:
    tree = _client_tree(tmp_path)
    (tree["protocol_dir"] / "config-tun.json").write_text(
        '{"inbounds":[{"type":"tun","auto_redirect":true}]}\n', encoding="utf-8"
    )
    fake_bin = _fake_bin(tmp_path)
    env = _tun_env(tmp_path, tree, fake_bin)
    assert _run_tun(f"{_preamble()}\nproxy tun on", env=env).returncode == 0

    result = _run_tun(f"{_preamble()}\nproxy check tun", env=env)
    combined = f"{result.stdout}\n{result.stderr}"

    assert "Profile auto_redirect: on" in combined
    # It becomes a hard requirement only in this case, so now it is reported.
    assert "nftables:" in combined
