"""Tests for client-install.sh behavior."""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def _require_command(name: str) -> str:
    command_path = shutil.which(name)
    if command_path is None:
        pytest.skip(f"{name} is not available")

    return command_path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_client_archive(tmp_path: Path) -> Path:
    archive_root = tmp_path / "sing-box"
    lib_dir = archive_root / "lib"
    lib_dir.mkdir(parents=True)

    rules_dir = archive_root / "rules"
    rules_dir.mkdir()
    rule_name = "a" * 64 + ".srs"
    (rules_dir / rule_name).write_bytes(b"fixture rule")
    (rules_dir / "files.txt").write_text(rule_name + "\n", encoding="ascii")

    shutil.copy2(SCRIPTS_DIR / "client-install.sh", archive_root / "client-install.sh")
    shutil.copy2(
        SCRIPTS_DIR / "client-uninstall.sh", archive_root / "client-uninstall.sh"
    )
    shutil.copy2(SCRIPTS_DIR / "setup.sh", archive_root / "setup.sh")
    shutil.copy2(SCRIPTS_DIR / "lib" / "ui.sh", lib_dir / "ui.sh")
    shutil.copy2(SCRIPTS_DIR / "lib" / "platform.sh", lib_dir / "platform.sh")
    shutil.copy2(SCRIPTS_DIR / "lib" / "system-proxy.sh", lib_dir / "system-proxy.sh")
    shutil.copy2(SCRIPTS_DIR / "lib" / "tun.sh", lib_dir / "tun.sh")

    (archive_root / "libcronet.so").write_bytes(b"cronet")
    (archive_root / "trojan-client.json").write_text("{}\n", encoding="utf-8")
    (archive_root / "trojan-gfw-client.json").write_text(
        '{"route":"gfw"}\n', encoding="utf-8"
    )
    (archive_root / "trojan-ai-client.json").write_text(
        '{"route":"ai"}\n', encoding="utf-8"
    )
    (archive_root / "trojan-global-client.json").write_text(
        '{"route":"global"}\n', encoding="utf-8"
    )
    (archive_root / "trojan-linux-tun-client.json").write_text(
        '{"tun": true}\n', encoding="utf-8"
    )
    _write_executable(
        archive_root / "sing-box",
        """\
        #!/usr/bin/env bash
        exit 0
        """,
    )

    return archive_root / "client-install.sh"


def _add_protocol_route_configs(archive_root: Path, protocol: str) -> None:
    (archive_root / f"{protocol}-client.json").write_text("{}\n", encoding="utf-8")
    (archive_root / f"{protocol}-gfw-client.json").write_text(
        '{"route":"gfw"}\n', encoding="utf-8"
    )
    (archive_root / f"{protocol}-ai-client.json").write_text(
        '{"route":"ai"}\n', encoding="utf-8"
    )
    (archive_root / f"{protocol}-global-client.json").write_text(
        '{"route":"global"}\n', encoding="utf-8"
    )


def _prepare_fake_commands(tmp_path: Path) -> Path:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    _write_executable(
        fakebin / "systemctl",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "--user" ]]; then
            shift
        fi

        case "${1:-}" in
            is-active)
                if [[ "${2:-}" != "--quiet" ]]; then
                    printf 'active\n'
                fi
                exit 0
                ;;
            daemon-reload|enable|disable|stop|restart)
                exit 0
                ;;
            *)
                exit 0
                ;;
        esac
        """,
    )
    _write_executable(
        fakebin / "loginctl",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "show-user" ]]; then
            printf 'no\n'
        fi
        exit 0
        """,
    )
    _write_executable(
        fakebin / "sudo",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-n" && "${2:-}" == "true" ]]; then
            exit 0
        fi
        exit 0
        """,
    )
    # Shimmed so a test can never read or write the developer's real desktop
    # proxy settings. Reports whatever DCONF_PROXY_HOST/PORT say is configured.
    _write_executable(
        fakebin / "dconf",
        """\
        #!/usr/bin/env bash
        case "${1:-}" in
            read)
                case "${2:-}" in
                    /system/proxy/mode)
                        if [[ -n "${DCONF_PROXY_HOST:-}" ]]; then
                            printf "'manual'\\n"
                        else
                            printf "'none'\\n"
                        fi
                        ;;
                    */host)
                        if [[ -n "${DCONF_PROXY_HOST:-}" ]]; then
                            printf "'%s'\\n" "${DCONF_PROXY_HOST}"
                        fi
                        ;;
                    */port)
                        if [[ -n "${DCONF_PROXY_HOST:-}" ]]; then
                            printf '%s\\n' "${DCONF_PROXY_PORT}"
                        fi
                        ;;
                esac
                ;;
            write)
                printf '%s %s\\n' "${2:-}" "${3:-}" >> "${DCONF_LOG:-/dev/null}"
                ;;
        esac
        exit 0
        """,
    )

    return fakebin


def _linux_env(tmp_path: Path, fakebin: Path) -> dict:
    home_dir = tmp_path / "home"
    for directory in ("home", "xdg-config", "xdg-data", "xdg-state"):
        (tmp_path / directory).mkdir(exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env["DCONF_LOG"] = str(tmp_path / "dconf.log")
    env.pop("DCONF_PROXY_HOST", None)
    env.pop("DCONF_PROXY_PORT", None)
    env.pop("SUDO_USER", None)

    return env


def _install_then_uninstall_on_linux(
    tmp_path: Path, *, gnome_proxy: tuple[str, str] | None
) -> subprocess.CompletedProcess[str]:
    """Install, point the GNOME proxy somewhere, then uninstall."""
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_fake_commands(tmp_path)
    env = _linux_env(tmp_path, fakebin)

    install = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    if gnome_proxy is not None:
        env["DCONF_PROXY_HOST"], env["DCONF_PROXY_PORT"] = gnome_proxy

    return subprocess.run(
        [
            bash_path,
            str(install_script.parent / "client-uninstall.sh"),
            "--yes",
            "--no-rc",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_client_uninstall_reverts_our_gnome_proxy(tmp_path: Path) -> None:
    result = _install_then_uninstall_on_linux(
        tmp_path, gnome_proxy=("127.0.0.1", "1080")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "System proxy: GNOME desktop" in result.stdout
    # Same as `proxy desktop off`: only the mode changes, the hosts stay.
    assert (tmp_path / "dconf.log").read_text(encoding="utf-8").splitlines() == [
        "/system/proxy/mode 'none'"
    ]


def test_client_uninstall_leaves_a_foreign_gnome_proxy_alone(tmp_path: Path) -> None:
    result = _install_then_uninstall_on_linux(
        tmp_path, gnome_proxy=("10.0.0.1", "3128")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "points somewhere else and was left unchanged" in result.stdout
    assert not (tmp_path / "dconf.log").exists()


def test_client_uninstall_ignores_a_disabled_gnome_proxy(tmp_path: Path) -> None:
    result = _install_then_uninstall_on_linux(tmp_path, gnome_proxy=None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "System proxy" not in result.stdout
    assert "points somewhere else" not in result.stdout
    assert not (tmp_path / "dconf.log").exists()


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_bundled_rules_install_into_private_state_and_reinstall_clears_cache(
    tmp_path: Path,
    platform: str,
) -> None:
    bash = _require_command("bash")
    script = _prepare_client_archive(tmp_path)
    fakebin = (
        _prepare_fake_commands if platform == "linux" else _prepare_macos_fake_commands
    )(tmp_path)
    env = (_linux_env if platform == "linux" else _macos_env)(tmp_path, fakebin)
    env["XDG_STATE_HOME"] = str(tmp_path / "state with spaces")
    state = Path(env["XDG_STATE_HOME"]) / "sing-box"
    for _ in range(2):
        result = subprocess.run(
            [bash, str(script), "--no-rc"], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert state.stat().st_mode & 0o777 == 0o700
        for name in ("a" * 64 + ".srs", "files.txt"):
            assert (state / "rules" / name).read_bytes() == (
                script.parent / "rules" / name
            ).read_bytes()
        assert not (state / "cache-trojan-china.db").exists()
        (state / "cache-trojan-china.db").write_bytes(b"previous cache")
    if platform == "linux":
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd/user/sing-box-trojan.service"
        ).read_text()
        assert "UMask=0077" in unit
        assert f'-D "{state}"' in unit


@pytest.mark.parametrize(
    "failure", ["manifest", "missing_file", "empty_file", "symlink", "traversal"]
)
def test_invalid_rule_bundle_is_rejected_before_uninstall(
    tmp_path: Path,
    failure: str,
) -> None:
    bash = _require_command("bash")
    script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_fake_commands(tmp_path)
    env = _linux_env(tmp_path, fakebin)
    previous = Path(env["XDG_STATE_HOME"]) / "sing-box" / "previous.db"
    previous.parent.mkdir()
    previous.write_bytes(b"previous installation")
    rule = script.parent / "rules" / ("a" * 64 + ".srs")
    manifest = rule.parent / "files.txt"
    if failure == "manifest":
        manifest.unlink()
    elif failure == "missing_file":
        rule.unlink()
    elif failure == "empty_file":
        rule.write_bytes(b"")
    elif failure == "symlink":
        rule.unlink()
        rule.symlink_to(previous)
    else:
        manifest.write_text("../../previous.db\n")
    result = subprocess.run(
        [bash, str(script), "--no-rc"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "Bundled rule files are missing or invalid" in result.stdout + result.stderr
    assert previous.read_bytes() == b"previous installation"


def test_reinstall_preserves_selected_port_but_standalone_uninstall_removes_it(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_fake_commands(tmp_path)
    env = _linux_env(tmp_path, fakebin)
    marker = Path(env["XDG_CONFIG_HOME"]) / "sing-box" / "selected-port"

    for _ in range(2):
        result = subprocess.run(
            [bash_path, str(install_script), "--no-rc"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if not marker.exists():
            marker.write_text("65535\n", encoding="ascii")

    assert marker.read_text(encoding="ascii") == "65535\n"

    result = subprocess.run(
        [
            bash_path,
            str(install_script.parent / "client-uninstall.sh"),
            "--yes",
            "--no-rc",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


def test_bash_port_helpers_reject_foreign_listeners_wrap_and_repair_all_configs(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    config_root = tmp_path / "config"
    protocol_root = config_root / "trojan"
    protocol_root.mkdir(parents=True)
    for name, port in (
        ("config.json", 1080),
        ("config-china.json", 1080),
        ("config-gfw.json", 3128),
        ("config-ai.json", 3128),
        ("config-global.json", 1080),
    ):
        (protocol_root / name).write_text(
            '{"inbounds":[{"type":"mixed","listen_port":' + str(port) + "}]}\n",
            encoding="utf-8",
        )

    script = textwrap.dedent(
        f"""\
        source {SCRIPTS_DIR / "setup.sh"}
        SING_BOX_CONFIG_ROOT={config_root}
        SUPPORTED_PROTOCOLS=(trojan)
        SUPPORTED_ROUTES=(china gfw ai global)
        _proxy_read_config_port() {{ printf '1080\\n'; }}
        _proxy_has_active_service() {{ return 0; }}
        _proxy_listener_is_ready() {{ return 0; }}
        _proxy_client_is_serving 1080 || exit 10
        if _proxy_client_is_serving 3128; then exit 11; fi
        _proxy_port_is_available() {{ [[ "$1" == "1080" ]]; }}
        [[ "$(_proxy_find_free_port 65535)" == "1080" ]] || exit 12
        _proxy_patch_listen_port 1080 || exit 13
        """
    )
    result = subprocess.run(
        [bash_path, "--noprofile", "--norc", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert '"listen_port": 1080' in (protocol_root / "config-ai.json").read_text(
        encoding="utf-8"
    )
    assert '"listen_port": 1080' in (protocol_root / "config-gfw.json").read_text(
        encoding="utf-8"
    )


def _prepare_macos_fake_commands(tmp_path: Path) -> Path:
    """Fake enough of macOS to drive the launchd install path on Linux."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()

    _write_executable(
        fakebin / "uname",
        """\
        #!/usr/bin/env bash
        case "${1:-}" in
            -m) printf 'arm64\\n' ;;
            *) printf 'Darwin\\n' ;;
        esac
        exit 0
        """,
    )
    _write_executable(
        fakebin / "launchctl",
        """\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "${LAUNCHCTL_LOG:-/dev/null}"
        if [[ "${1:-}" == "print" ]]; then
            printf 'state = running\\n'
        fi
        exit 0
        """,
    )
    _write_executable(
        fakebin / "xattr",
        """\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "${XATTR_LOG:-/dev/null}"
        exit 0
        """,
    )
    _write_executable(
        fakebin / "route",
        """\
        #!/usr/bin/env bash
        printf '   route to: default\\n  interface: en0\\n'
        exit 0
        """,
    )
    # Passthrough, so networksetup calls made through sudo still hit the shim.
    _write_executable(
        fakebin / "sudo",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-n" ]]; then
            shift
        fi
        exec "$@"
        """,
    )
    # Reports whatever NS_SERVER/NS_PORT say is configured, and records the
    # state changes it is asked to make.
    _write_executable(
        fakebin / "networksetup",
        """\
        #!/usr/bin/env bash
        case "${1:-}" in
            -listnetworkserviceorder)
                printf '(1) Wi-Fi\\n(Hardware Port: Wi-Fi, Device: en0)\\n'
                ;;
            -getwebproxy|-getsecurewebproxy|-getsocksfirewallproxy)
                if [[ -n "${NS_SERVER:-}" ]]; then
                    printf 'Enabled: Yes\\nServer: %s\\nPort: %s\\n' \\
                        "${NS_SERVER}" "${NS_PORT}"
                else
                    printf 'Enabled: No\\nServer:\\nPort: 0\\n'
                fi
                ;;
            *)
                printf '%s\\n' "$*" >> "${NS_LOG:-/dev/null}"
                ;;
        esac
        exit 0
        """,
    )

    return fakebin


def _macos_env(tmp_path: Path, fakebin: Path, *, shell: str = "/bin/zsh") -> dict:
    home_dir = tmp_path / "home"
    for directory in ("home", "xdg-config", "xdg-data", "xdg-state"):
        (tmp_path / directory).mkdir(exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SHELL"] = shell
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env["LAUNCHCTL_LOG"] = str(tmp_path / "launchctl.log")
    env["XATTR_LOG"] = str(tmp_path / "xattr.log")
    env["NS_LOG"] = str(tmp_path / "networksetup.log")
    env.pop("NS_SERVER", None)
    env.pop("NS_PORT", None)
    env.pop("SUDO_USER", None)

    return env


def _install_then_uninstall_on_macos(
    tmp_path: Path, *, system_proxy: tuple[str, str] | None
) -> subprocess.CompletedProcess[str]:
    """Install, point the system proxy somewhere, then uninstall."""
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_macos_fake_commands(tmp_path)
    env = _macos_env(tmp_path, fakebin)

    install = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # Only now does a system proxy exist, so the install run above cannot have
    # recorded any networksetup state changes.
    if system_proxy is not None:
        env["NS_SERVER"], env["NS_PORT"] = system_proxy

    return subprocess.run(
        [
            bash_path,
            str(install_script.parent / "client-uninstall.sh"),
            "--yes",
            "--no-rc",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_client_uninstall_reverts_our_system_proxy_on_macos(tmp_path: Path) -> None:
    result = _install_then_uninstall_on_macos(
        tmp_path, system_proxy=("127.0.0.1", "1080")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "System proxy: Wi-Fi" in result.stdout
    assert (tmp_path / "networksetup.log").read_text(encoding="utf-8").splitlines() == [
        "-setwebproxystate Wi-Fi off",
        "-setsecurewebproxystate Wi-Fi off",
        "-setsocksfirewallproxystate Wi-Fi off",
    ]


def test_client_uninstall_leaves_a_foreign_system_proxy_alone_on_macos(
    tmp_path: Path,
) -> None:
    """Clearing a proxy we did not set would be worse than leaving ours behind."""
    result = _install_then_uninstall_on_macos(
        tmp_path, system_proxy=("10.0.0.1", "3128")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "System proxy" not in result.stdout.replace(
        "The macOS system proxy points somewhere else", ""
    )
    assert "points somewhere else and was left unchanged" in result.stdout
    assert not (tmp_path / "networksetup.log").exists()


def test_client_uninstall_ignores_a_disabled_system_proxy_on_macos(
    tmp_path: Path,
) -> None:
    result = _install_then_uninstall_on_macos(tmp_path, system_proxy=None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "System proxy" not in result.stdout
    assert "points somewhere else" not in result.stdout
    assert not (tmp_path / "networksetup.log").exists()


def test_client_install_writes_launch_agents_on_macos(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    archive_root = install_script.parent
    _add_protocol_route_configs(archive_root, "hysteria2")
    _add_protocol_route_configs(archive_root, "naive")

    fakebin = _prepare_macos_fake_commands(tmp_path)
    env = _macos_env(tmp_path, fakebin)
    home_dir = Path(env["HOME"])
    config_home = Path(env["XDG_CONFIG_HOME"])

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc", "--protocol", "hysteria2"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    agent_dir = home_dir / "Library" / "LaunchAgents"
    for protocol in ("trojan", "hysteria2", "naive"):
        assert (agent_dir / f"io.sing-box.{protocol}.plist").is_file()

    # systemd units must not be written on macOS.
    assert not (config_home / "systemd").exists()
    assert (home_dir / "Library" / "Logs" / "sing-box").is_dir()

    uid = os.getuid()
    plist = agent_dir / "io.sing-box.hysteria2.plist"
    assert launchctl_lines(tmp_path) == [
        f"bootout gui/{uid}/io.sing-box.trojan",
        f"bootout gui/{uid}/io.sing-box.naive",
        f"bootout gui/{uid}/io.sing-box.hysteria2",
        f"enable gui/{uid}/io.sing-box.hysteria2",
        f"bootstrap gui/{uid} {plist}",
        f"print gui/{uid}/io.sing-box.hysteria2",
    ]

    # The quarantine flag is cleared from the installed binary.
    assert f"-d com.apple.quarantine {home_dir}/.local/bin/sing-box" in (
        tmp_path / "xattr.log"
    ).read_text(encoding="utf-8")


def test_macos_launch_agent_contents(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_macos_fake_commands(tmp_path)
    env = _macos_env(tmp_path, fakebin)
    home_dir = Path(env["HOME"])
    config_home = Path(env["XDG_CONFIG_HOME"])
    state_home = Path(env["XDG_STATE_HOME"])

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    # Parse it as a real plist: launchd silently ignores a malformed one.
    with (home_dir / "Library" / "LaunchAgents" / "io.sing-box.trojan.plist").open(
        "rb"
    ) as plist_file:
        plist = plistlib.load(plist_file)

    log_path = f"{home_dir}/Library/Logs/sing-box/trojan.log"
    assert plist == {
        "Label": "io.sing-box.trojan",
        "ProgramArguments": [
            f"{home_dir}/.local/bin/sing-box",
            "run",
            "-D",
            f"{state_home}/sing-box",
            # `-c <file>`, not `-C <directory>`: the protocol directory can also
            # hold the self-managed TUN profile, and loading both would merge
            # two inbounds into one instance.
            "-c",
            f"{config_home}/sing-box/trojan/config.json",
        ],
        "RunAtLoad": True,
        "Umask": 63,
        # The launchd equivalent of systemd's Restart=on-failure.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def test_client_install_targets_bash_profile_on_macos(tmp_path: Path) -> None:
    """macOS bash login shells read .bash_profile, not .bashrc."""
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_macos_fake_commands(tmp_path)
    env = _macos_env(tmp_path, fakebin, shell="/bin/bash")
    home_dir = Path(env["HOME"])

    result = subprocess.run(
        [bash_path, str(install_script), "--shell", "bash"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home_dir / ".bash_profile").is_file()
    assert "sing-box/setup.sh" in (home_dir / ".bash_profile").read_text(
        encoding="utf-8"
    )
    assert not (home_dir / ".bashrc").exists()


def test_client_uninstall_removes_launch_agents_on_macos(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    uninstall_script = install_script.parent / "client-uninstall.sh"
    fakebin = _prepare_macos_fake_commands(tmp_path)
    env = _macos_env(tmp_path, fakebin)
    home_dir = Path(env["HOME"])

    install = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    agent = home_dir / "Library" / "LaunchAgents" / "io.sing-box.trojan.plist"
    log_dir = home_dir / "Library" / "Logs" / "sing-box"
    assert agent.is_file()
    assert log_dir.is_dir()

    (tmp_path / "launchctl.log").unlink()

    uninstall = subprocess.run(
        [bash_path, str(uninstall_script), "--yes", "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    assert not agent.exists()
    assert not log_dir.exists()
    assert f"bootout gui/{os.getuid()}/io.sing-box.trojan" in launchctl_lines(tmp_path)


def launchctl_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "launchctl.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def test_client_install_no_rc_preserves_existing_shell_integration(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    packaged_version = (
        "schema=1\n"
        f"client_build_id={'a' * 64}\n"
        "commit_sha=9237d372\n"
        "portal_base_url=https://vpn.example.com\n"
    )
    (install_script.parent / "client-version").write_text(
        packaged_version, encoding="utf-8"
    )
    fakebin = _prepare_fake_commands(tmp_path)

    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    home_dir.mkdir()
    config_home.mkdir()
    data_home.mkdir()
    state_home.mkdir()

    rc_file = home_dir / ".bashrc"
    original_rc = textwrap.dedent(
        """\
        export PATH="$HOME/bin:$PATH"

        # Network proxy management configuration (sing-box)
        [ -f "${XDG_DATA_HOME:-$HOME/.local/share}/sing-box/setup.sh" ] && source "${XDG_DATA_HOME:-$HOME/.local/share}/sing-box/setup.sh"
        """
    )
    rc_file.write_text(original_rc, encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env["XDG_STATE_HOME"] = str(state_home)
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env.pop("SUDO_USER", None)

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert rc_file.read_text(encoding="utf-8") == original_rc
    assert (home_dir / ".local" / "bin" / "libcronet.so").read_bytes() == b"cronet"
    assert (data_home / "sing-box" / "setup.sh").is_file()
    assert (data_home / "sing-box" / "client-version").read_text(
        encoding="utf-8"
    ) == packaged_version
    assert (data_home / "sing-box" / "portal-base-url").read_text(
        encoding="utf-8"
    ).strip() == "https://vpn.example.com"
    preferences = data_home / "sing-box" / "install-preferences"
    assert "rc_enabled=false" in preferences.read_text(encoding="utf-8")


def test_client_install_installs_all_packaged_protocols_and_starts_selected(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    archive_root = install_script.parent
    _add_protocol_route_configs(archive_root, "hysteria2")
    _add_protocol_route_configs(archive_root, "naive")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fakebin / "systemctl",
        f"""\
        #!/usr/bin/env bash
        if [[ "${{1:-}}" == "--user" ]]; then
            shift
        fi
        printf '%s\n' "$*" >> "{systemctl_log}"
        exit 0
        """,
    )

    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    home_dir.mkdir()
    config_home.mkdir()
    data_home.mkdir()
    state_home.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env["XDG_STATE_HOME"] = str(state_home)
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env.pop("SUDO_USER", None)

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc", "--protocol", "hysteria2"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (config_home / "sing-box" / "trojan" / "config.json").is_file()
    assert (config_home / "sing-box" / "hysteria2" / "config.json").is_file()
    assert (config_home / "sing-box" / "naive" / "config.json").is_file()
    assert (config_home / "sing-box" / "selected-protocol").read_text(
        encoding="utf-8"
    ) == "hysteria2\n"
    assert (home_dir / ".local" / "bin" / "libcronet.so").read_bytes() == b"cronet"
    assert (config_home / "systemd" / "user" / "sing-box-trojan.service").is_file()
    assert (config_home / "systemd" / "user" / "sing-box-hysteria2.service").is_file()
    assert (config_home / "systemd" / "user" / "sing-box-naive.service").is_file()
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "daemon-reload",
        "disable --now sing-box-trojan.service",
        "disable --now sing-box-naive.service",
        "enable --now sing-box-hysteria2.service",
        "is-active --quiet sing-box-hysteria2.service",
        # The Linux ai route resolves direct traffic through systemd-resolved.
        "is-active systemd-resolved",
    ]
    # This fake reports no state for it, so the install ends by saying the ai
    # route cannot resolve while every other route still can.
    assert "systemd-resolved is not running" in result.stdout
    assert "Every other route is unaffected." in result.stdout


def test_client_install_uses_packaged_default_protocol_file(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    archive_root = install_script.parent
    _add_protocol_route_configs(archive_root, "naive")
    (archive_root / "default-protocol").write_text("naive\n", encoding="utf-8")
    fakebin = _prepare_fake_commands(tmp_path)

    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    home_dir.mkdir()
    config_home.mkdir()
    data_home.mkdir()
    state_home.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env["XDG_STATE_HOME"] = str(state_home)
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env.pop("SUDO_USER", None)

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (config_home / "sing-box" / "selected-protocol").read_text(
        encoding="utf-8"
    ) == "naive\n"


def test_client_uninstall_removes_installed_cronet_library(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    uninstall_script = install_script.parent / "client-uninstall.sh"

    home_dir = tmp_path / "home"
    bin_dir = home_dir / ".local" / "bin"
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    bin_dir.mkdir(parents=True)
    config_home.mkdir()
    data_home.mkdir()
    state_home.mkdir()
    (bin_dir / "libcronet.so").write_bytes(b"stale-cronet")

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env["XDG_STATE_HOME"] = str(state_home)
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env.pop("SUDO_USER", None)

    result = subprocess.run(
        [bash_path, str(uninstall_script), "--yes", "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (bin_dir / "libcronet.so").exists()


# --- machine-wide TUN ---------------------------------------------------------


def _tun_install_env(tmp_path: Path, sysroot: Path) -> tuple[dict[str, str], Path]:
    """Installer environment with every managed TUN path under a scratch root."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    _write_executable(
        fakebin / "systemctl",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "--user" ]]; then
            shift
        fi
        if [[ "${1:-}" == "show" ]]; then
            for arg in "$@"; do
                case "$arg" in
                    --property=ActiveState) printf 'inactive\\n' ;;
                    --property=SubState) printf 'dead\\n' ;;
                esac
            done
            exit 0
        fi
        if [[ "${1:-}" == "is-enabled" ]]; then
            printf '%s\\n' "${FAKE_ENABLED_STATE:-disabled}"
            exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fakebin / "sudo",
        """\
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-n" ]]; then
            shift
        fi
        exec "$@"
        """,
    )
    _write_executable(
        fakebin / "ip",
        """\
        #!/usr/bin/env bash
        exit 1
        """,
    )

    home_dir = tmp_path / "home"
    config_home = tmp_path / "xdg-config"
    for directory in (
        home_dir,
        config_home,
        tmp_path / "xdg-data",
        tmp_path / "xdg-state",
    ):
        directory.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    env["PATH"] = f"{fakebin}:{env.get('PATH', os.defpath)}"
    env["SBM_TUN_ROOT"] = str(sysroot)
    env["SHELL"] = "/bin/bash"
    env["LANG"] = "C.UTF-8"
    env["USER"] = env.get("USER", "tester")
    env.pop("SUDO_USER", None)

    return env, config_home


def _provisioned_tun(sysroot: Path, *, status: str, owner_uid: int) -> None:
    """A managed TUN installation on disk, in a given attested state."""
    unit_dir = sysroot / "etc" / "systemd" / "system"
    tun_dir = sysroot / "etc" / "sing-box-manager" / "tun"
    libexec = sysroot / "usr" / "local" / "libexec" / "sing-box-manager-tun"
    var_lib = sysroot / "var" / "lib"
    for directory in (unit_dir, tun_dir, libexec, var_lib):
        directory.mkdir(parents=True, exist_ok=True)

    (unit_dir / "sing-box-manager-tun.service").write_text("[Unit]\n", encoding="utf-8")
    (tun_dir / "config.json").write_text("{}\n", encoding="utf-8")
    (tun_dir / "manifest").write_text(
        f"owner_uid={owner_uid}\nprotocol=trojan\nruntime_account_created=false\n",
        encoding="utf-8",
    )
    (libexec / "sing-box").write_bytes(b"binary")
    (var_lib / "sing-box-manager-tun.status").write_text(
        f"{status}\n", encoding="utf-8"
    )


def test_client_install_writes_all_routes_and_tun_with_owner_only_modes(
    tmp_path: Path,
) -> None:
    """All credential-bearing configs land owner-only and China starts active."""
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    sysroot = tmp_path / "sysroot"
    env, config_home = _tun_install_env(tmp_path, sysroot)
    # A hostile umask must not widen a file holding the user's credentials.
    env["SBM_TEST_UMASK"] = "000"

    result = subprocess.run(
        [bash_path, "-c", f"umask 000; exec {bash_path} {install_script} --no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    protocol_dir = config_home / "sing-box" / "trojan"
    mixed = protocol_dir / "config.json"
    tun = protocol_dir / "config-tun.json"
    china = protocol_dir / "config-china.json"
    ai = protocol_dir / "config-ai.json"
    global_config = protocol_dir / "config-global.json"
    selected_route = config_home / "sing-box" / "selected-route"

    assert mixed.read_text(encoding="utf-8") == "{}\n"
    assert tun.read_text(encoding="utf-8") == '{"tun": true}\n'
    assert china.read_text(encoding="utf-8") == "{}\n"
    assert ai.read_text(encoding="utf-8") == '{"route":"ai"}\n'
    assert global_config.read_text(encoding="utf-8") == '{"route":"global"}\n'
    assert selected_route.read_text(encoding="utf-8") == "china\n"
    assert stat.S_IMODE(protocol_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(mixed.stat().st_mode) == 0o600
    assert stat.S_IMODE(tun.stat().st_mode) == 0o600
    for path in (china, ai, global_config, selected_route):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_client_install_unit_loads_one_exact_config_and_guards_transactions(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    sysroot = tmp_path / "sysroot"
    env, config_home = _tun_install_env(tmp_path, sysroot)

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    unit = (
        Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "sing-box-trojan.service"
    ).read_text(encoding="utf-8")

    # `-C <directory>` would load config.json and config-tun.json together.
    assert f'-c "{config_home}/sing-box/trojan/config.json"' in unit
    assert "-C " not in unit
    # The same public marker the shell guard checks, so a manual
    # `systemctl --user start` cannot race a TUN transaction.
    assert f"! test -e {sysroot}/var/lib/sing-box-manager-tun.transaction" in unit


def test_client_install_refuses_while_the_tunnel_is_not_safely_off(
    tmp_path: Path,
) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    sysroot = tmp_path / "sysroot"
    env, config_home = _tun_install_env(tmp_path, sysroot)
    _provisioned_tun(sysroot, status="active", owner_uid=os.getuid())

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not safely off" in combined
    assert "proxy tun off" in combined
    # Refused before anything was replaced.
    assert not (config_home / "sing-box" / "trojan").exists()


def test_installer_cleanup_preserves_an_inactive_tun_installation(
    tmp_path: Path,
) -> None:
    """The reinstall path must not delete privileged files it did not stop."""
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    sysroot = tmp_path / "sysroot"
    env, _ = _tun_install_env(tmp_path, sysroot)
    _provisioned_tun(sysroot, status="safe-off", owner_uid=os.getuid())

    result = subprocess.run(
        [bash_path, str(install_script), "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (sysroot / "etc" / "sing-box-manager" / "tun" / "config.json").exists()
    assert (sysroot / "etc" / "sing-box-manager" / "tun" / "manifest").exists()
    assert (
        sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"
    ).exists()
    assert "TUN mode preserved" in result.stdout


def test_manual_uninstall_removes_only_the_recorded_tun_paths(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    uninstall_script = install_script.parent / "client-uninstall.sh"
    sysroot = tmp_path / "sysroot"
    env, _ = _tun_install_env(tmp_path, sysroot)
    _provisioned_tun(sysroot, status="safe-off", owner_uid=os.getuid())

    # Neighbours in the same directories that this client does not own.
    unit_dir = sysroot / "etc" / "systemd" / "system"
    (unit_dir / "sing-box-server.service").write_text("[Unit]\n", encoding="utf-8")
    other_managed = sysroot / "etc" / "sing-box-manager" / "other.conf"
    other_managed.write_text("keep me\n", encoding="utf-8")

    result = subprocess.run(
        [bash_path, str(uninstall_script), "--yes", "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (unit_dir / "sing-box-manager-tun.service").exists()
    assert not (sysroot / "etc" / "sing-box-manager" / "tun" / "manifest").exists()
    assert not (sysroot / "var" / "lib" / "sing-box-manager-tun.status").exists()
    # No glob over /etc/systemd/system, and no sweep of /etc/sing-box-manager.
    assert (unit_dir / "sing-box-server.service").exists()
    assert other_managed.exists()


def test_manual_uninstall_refuses_a_tun_owned_by_another_uid(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    uninstall_script = install_script.parent / "client-uninstall.sh"
    sysroot = tmp_path / "sysroot"
    env, _ = _tun_install_env(tmp_path, sysroot)
    _provisioned_tun(sysroot, status="safe-off", owner_uid=os.getuid() + 1)

    result = subprocess.run(
        [bash_path, str(uninstall_script), "--yes", "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "owned by another local user" in combined
    assert "Uninstallation incomplete" in combined
    # Nothing was deleted while a tunnel somebody else owns may still run.
    assert (sysroot / "etc" / "sing-box-manager" / "tun" / "manifest").exists()
    assert (
        sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"
    ).exists()


def test_manual_uninstall_aborts_when_routing_artifacts_survive(tmp_path: Path) -> None:
    bash_path = _require_command("bash")
    install_script = _prepare_client_archive(tmp_path)
    uninstall_script = install_script.parent / "client-uninstall.sh"
    sysroot = tmp_path / "sysroot"
    env, _ = _tun_install_env(tmp_path, sysroot)
    _provisioned_tun(sysroot, status="safe-off", owner_uid=os.getuid())

    # The interface is still up after the stop.
    _write_executable(
        tmp_path / "fakebin" / "ip",
        """\
        #!/usr/bin/env bash
        case "$*" in
            *"link show dev"*) printf '9: sbm-tun0: <UP>\\n'; exit 0 ;;
        esac
        exit 0
        """,
    )

    result = subprocess.run(
        [bash_path, str(uninstall_script), "--yes", "--no-rc"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "still present" in combined
    assert (
        sysroot / "etc" / "systemd" / "system" / "sing-box-manager-tun.service"
    ).exists()
    assert (sysroot / "usr" / "local" / "libexec").exists()


def test_uninstall_no_longer_claims_a_server_or_third_party_installation(
    tmp_path: Path,
) -> None:
    """File existence alone is not proof this client owns /usr/local/bin."""
    uninstall_script = ROOT / "scripts" / "client-uninstall.sh"
    text = uninstall_script.read_text(encoding="utf-8")

    assert "/etc/systemd/system/sing-box-*.service" not in text
    assert "for legacy_protocol in trojan hysteria2 naive; do" in text
    assert "if [[ ${#legacy_services[@]} -gt 0 ]]; then" in text


@pytest.mark.parametrize("platform", ["linux", "macos"])
@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize(
    "action", ["--yes", "confirm", "--help", "cancel", "--invalid", "broken-config"]
)
def test_proxy_uninstall_from_installed_files(
    tmp_path: Path, platform: str, shell: str, action: str
) -> None:
    shell_path = _require_command(shell)
    _require_command("jq")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = (
        _prepare_fake_commands(tmp_path)
        if platform == "linux"
        else _prepare_macos_fake_commands(tmp_path)
    )
    env = (
        _linux_env(tmp_path, fakebin)
        if platform == "linux"
        else _macos_env(tmp_path, fakebin)
    )
    env["SBM_SKIP_UPDATE_CHECK"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["DOCKER_CONFIG"] = str(tmp_path / "docker")
    for name in list(env):
        if name.lower().endswith("_proxy"):
            del env[name]
    result = subprocess.run(
        [_require_command("bash"), str(install_script), "--shell", shell],
        env=env,
        input="n\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = Path(env["XDG_DATA_HOME"]) / "sing-box"
    config = Path(env["XDG_CONFIG_HOME"]) / "sing-box"
    (config / "selected-port").write_text("1085\n")
    Path(env["GIT_CONFIG_GLOBAL"]).write_text(
        "[http]\n proxy = http://127.0.0.1:1085\n proxy = http://corporate:8080\n"
        "[https]\n proxy = http://127.0.0.1:1085\n"
    )
    docker = Path(env["DOCKER_CONFIG"])
    docker.mkdir()
    (docker / "config.json").write_text(
        '{"auths":{"registry.example":{}},"proxies":{"default":'
        '{"httpProxy":"http://127.0.0.1:1085","httpsProxy":"http://corporate:8080"}}}'
    )
    shutil.rmtree(install_script.parent)
    if action == "broken-config":
        (docker / "config.json").write_text("invalid JSON\n")
    command = (
        ""
        if action in ("confirm", "cancel")
        else "--yes"
        if action == "broken-config"
        else action
    )
    result = subprocess.run(
        [
            shell_path,
            "-f",
            "-c",
            textwrap.dedent(f'''\
            source "{data}/setup.sh"
            export http_proxy=http://127.0.0.1:1085
            export https_proxy=http://corporate:8080
            proxy uninstall {command}
            result=$?
            printf 'RESULT=%s HTTP=%s HTTPS=%s\\n' "$result" "${{http_proxy:-}}" "$https_proxy"
            if typeset -f proxy >/dev/null; then echo PROXY_PRESENT; fi
        '''),
        ],
        env=env,
        input="y\n" if action == "confirm" else "n\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if action in ("--yes", "confirm"):
        assert "RESULT=0 HTTP= HTTPS=http://corporate:8080" in result.stdout
        assert "PROXY_PRESENT" not in result.stdout
        assert not data.exists()
        assert not config.exists()
        assert not (Path(env["XDG_STATE_HOME"]) / "sing-box").exists()
        assert not (Path(env["HOME"]) / ".local/bin/sing-box").exists()
        assert "127.0.0.1" not in Path(env["GIT_CONFIG_GLOBAL"]).read_text()
        docker_text = (docker / "config.json").read_text()
        assert "127.0.0.1" not in docker_text
        assert "corporate:8080" in docker_text
        assert "registry.example" in docker_text
        for rc_name in (".bashrc", ".bash_profile", ".zshrc"):
            rc = Path(env["HOME"]) / rc_name
            if rc.exists():
                assert "sing-box/setup.sh" not in rc.read_text()
        if platform == "linux":
            assert not (
                Path(env["XDG_CONFIG_HOME"]) / "systemd/user/sing-box-trojan.service"
            ).exists()
        else:
            assert not (
                Path(env["HOME"]) / "Library/LaunchAgents/io.sing-box.trojan.plist"
            ).exists()
    else:
        expected = 1 if action in ("--invalid", "broken-config") else 0
        assert f"RESULT={expected} HTTP=http://127.0.0.1:1085" in result.stdout
        assert "PROXY_PRESENT" in result.stdout
        assert (data / "client-uninstall.sh").is_file()
        if action == "broken-config":
            assert "Uninstallation incomplete" in result.stdout
            assert (docker / "config.json").read_text() == "invalid JSON\n"
        else:
            assert "127.0.0.1" in Path(env["GIT_CONFIG_GLOBAL"]).read_text()
            assert "127.0.0.1" in (docker / "config.json").read_text()
