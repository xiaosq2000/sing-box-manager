"""The sbc installer script, run with fake uname, curl and sbc."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_client_install import (
    _linux_env,
    _prepare_client_archive,
    _prepare_fake_commands,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sing_box_manager" / "web" / "client" / "install.sh"
PORTAL = "https://vpn.example.com"
LINK = f"{PORTAL}/sub/abcdefghijklmnopqrstuv"
# A machine token as the bash client saves it: username, timestamp, HMAC.
TOKEN = "alice:1700000000:" + "ab" * 32

# Reads the URL from the command line or, with -K -, from its config on
# standard input, the way install.sh passes credentials.
FAKE_CURL = r"""#!/bin/sh
url="" out="" config="" prev=""
for arg in "$@"; do
	case "$prev" in
	-o | --output) out=$arg ;;
	-K) if [ "$arg" = - ]; then config=$(cat); fi ;;
	esac
	case "$arg" in http://* | https://*) url=$arg ;; esac
	prev=$arg
done
if [ -n "$config" ]; then
	url=$(printf '%s\n' "$config" | sed -n 's/^url = "\(.*\)"$/\1/p')
fi
printf 'curl args: %s\ncurl url: %s\n' "$*" "$url" >> "$FAKE_LOG"
case "$url" in
*/api/sub)
	case "$config" in
	*"header = \"Authorization: Bearer $FAKE_TOKEN\""*) ;;
	*) exit 22 ;;
	esac
	if [ -n "${FAKE_TRADE_FAILS:-}" ]; then exit 22; fi
	printf '{"url":"%s","username":"alice"}' "$FAKE_LINK"
	;;
*/install/linux.sh | */install/macos.sh) cp "$FAKE_INSTALL_SH" "$out" ;;
*/files/sbc/*) cp "$FAKE_SBC" "$out" ;;
*) exit 22 ;;
esac
"""

# Logs every call. Its install takes a spare port, as the real one does while
# the bash client holds the old port.
FAKE_SBC = r"""#!/bin/sh
printf 'sbc %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
install)
	IFS= read -r link || true
	printf 'sbc stdin: %s\n' "$link" >> "$FAKE_LOG"
	if [ -n "${FAKE_SBC_FAILS:-}" ]; then exit 1; fi
	echo 41137 > "$FAKE_STATE/port"
	;;
port)
	if [ $# -eq 1 ]; then cat "$FAKE_STATE/port"; exit 0; fi
	tries=$(cat "$FAKE_STATE/tries" 2>/dev/null || echo 0)
	echo $((tries + 1)) > "$FAKE_STATE/tries"
	if [ "$tries" -lt "${FAKE_PORT_BUSY:-0}" ]; then exit 1; fi
	echo "$2" > "$FAKE_STATE/port"
	;;
status) echo "proxy:    127.0.0.1:$(cat "$FAKE_STATE/port")" ;;
esac
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fakes(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    """Put fake curl and sleep on bin_dir and return the variables they read."""
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / "curl", FAKE_CURL)
    # Returns at once. FAKE_RELEASE names a directory to make writable, as
    # NFS releases a file a moment after the process holding it exits.
    _executable(
        bin_dir / "sleep",
        '#!/bin/sh\nif [ -n "${FAKE_RELEASE:-}" ]; then chmod u+w "$FAKE_RELEASE"; fi\n',
    )
    _executable(tmp_path / "fake-sbc", FAKE_SBC)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_STATE": str(state),
        "FAKE_SBC": str(tmp_path / "fake-sbc"),
        "FAKE_INSTALL_SH": str(SCRIPT),
        "FAKE_LINK": LINK,
        "FAKE_TOKEN": TOKEN,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


# Answers like GNOME's dconf, with a manual proxy when DCONF_PROXY_HOST is set.
FAKE_DCONF = r"""#!/bin/sh
[ "$1" = read ] || exit 0
case "$2" in
/system/proxy/mode) if [ -n "${DCONF_PROXY_HOST:-}" ]; then echo "'manual'"; else echo "'none'"; fi ;;
*/host) if [ -n "${DCONF_PROXY_HOST:-}" ]; then echo "'$DCONF_PROXY_HOST'"; fi ;;
*/port) if [ -n "${DCONF_PROXY_HOST:-}" ]; then echo "$DCONF_PROXY_PORT"; fi ;;
esac
"""


def _run(
    tmp_path: Path,
    *args: str,
    answer: str = LINK,
    system: str = "Linux",
    machine: str = "x86_64",
    **extra_env: str,
):
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = _fakes(tmp_path, bin_dir)
    _executable(
        bin_dir / "uname",
        f'#!/bin/sh\ncase "$1" in -s) echo {system} ;; -m) echo {machine} ;; esac\n',
    )
    _executable(bin_dir / "stty", "#!/bin/sh\nexit 0\n")
    _executable(bin_dir / "dconf", FAKE_DCONF)
    tty = tmp_path / "tty"
    tty.write_text(answer + "\n")
    result = subprocess.run(
        ["sh", str(SCRIPT), *args],
        env={
            **env,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "SBC_INSTALL_TTY": str(tty),
            "SBM_TUN_ROOT": os.environ["SBM_TUN_ROOT"],
            **extra_env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    log = tmp_path / "log"
    return result, (log.read_text() if log.exists() else "")


def _assert_credentials_stay_off_command_lines(log: str) -> None:
    for line in log.splitlines():
        if line.startswith(("curl args:", "sbc ")) and not line.startswith(
            "sbc stdin:"
        ):
            assert "abcdefghijklmnopqrstuv" not in line
            assert TOKEN not in line


def _bash_client(home: Path, *, preferences: str = "rc_enabled=false\nshell=zsh\n"):
    """Lay out the files of a bash install that listens on 1085."""
    config = home / ".config" / "sing-box"
    data = home / ".local" / "share" / "sing-box"
    (config / "trojan").mkdir(parents=True)
    (data / "lib").mkdir(parents=True)
    for library in ("platform.sh", "system-proxy.sh"):
        shutil.copy(ROOT / "scripts" / "lib" / library, data / "lib" / library)
    (config / "trojan" / "config.json").write_text(
        '{"inbounds": [{"type": "mixed", "listen_port": 1080}]}\n'
    )
    (config / "selected-protocol").write_text("hysteria2\n")
    (config / "selected-route").write_text("gfw\n")
    (config / "selected-port").write_text("1085\n")
    (config / "portal-token").write_text(TOKEN)
    (data / "setup.sh").write_text("# the bash client\n")
    (data / "portal-base-url").write_text(PORTAL + "\n")
    (data / "install-preferences").write_text(preferences)
    _executable(
        data / "client-uninstall.sh",
        "#!/bin/sh\n"
        'printf "uninstaller %s clean=%s\\n" "$*" "${SBM_UNINSTALL_CLEAN_PROXY-}"'
        ' >> "$FAKE_LOG"\n'
        'rm -rf "$HOME/.config/sing-box" "$HOME/.local/share/sing-box"\n',
    )
    return config, data


def test_the_link_reaches_curl_and_sbc_only_on_standard_input(tmp_path: Path) -> None:
    result, log = _run(tmp_path, "--shell", "zsh", "--no-rc", answer=f"  {LINK}/ ")

    assert result.returncode == 0, result.stderr
    assert f"curl url: {LINK}/files/sbc/linux-amd64/sbc" in log
    assert "sbc install --shell zsh --no-rc\n" in log
    assert f"sbc stdin: {LINK}" in log
    _assert_credentials_stay_off_command_lines(log)


@pytest.mark.parametrize(
    ("system", "machine", "platform"),
    [("Linux", "aarch64", "linux-arm64"), ("Darwin", "arm64", "darwin-arm64")],
)
def test_each_platform_gets_its_build(
    tmp_path: Path, system: str, machine: str, platform: str
) -> None:
    _, log = _run(tmp_path, system=system, machine=machine)
    assert f"/files/sbc/{platform}/sbc" in log


@pytest.mark.parametrize(
    "answer", ["", "https://vpn.example.com/files/x", "not a link"]
)
def test_anything_but_a_link_stops_before_downloading(
    tmp_path: Path, answer: str
) -> None:
    result, log = _run(tmp_path, answer=answer)
    assert result.returncode == 1
    assert "not a subscription link" in result.stderr
    assert log == ""


def test_the_protocol_flag_picks_the_protocol(tmp_path: Path) -> None:
    _, log = _run(tmp_path, "-p", "naive")
    assert "sbc install --protocol naive\n" in log


def test_unknown_options_show_usage(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "--fish")
    assert result.returncode == 2
    assert "Usage: install.sh" in result.stderr


def test_a_bash_install_moves_to_sbc_with_its_port_route_and_protocol(
    tmp_path: Path,
) -> None:
    config, data = _bash_client(tmp_path / "home")
    (tmp_path / "gitconfig").write_text(
        "[http]\n\tproxy = http://127.0.0.1:1085\n\tproxy = http://corporate:8080\n"
        "[https]\n\tproxy = http://127.0.0.1:1080\n"
    )

    result, log = _run(tmp_path, answer="never asked")

    assert result.returncode == 0, result.stderr
    assert "Subscription link:" not in result.stderr
    assert f"curl url: {PORTAL}/api/sub" in log
    assert "sbc install --shell zsh --no-rc --route gfw --protocol hysteria2\n" in log
    assert f"sbc stdin: {LINK}" in log
    # The Docker settings stay, since sbc serves the same port.
    assert "uninstaller --yes --no-rc clean=\n" in log
    assert "sbc port 1085\n" in log
    assert log.index("sbc install") < log.index("uninstaller") < log.index("port 1085")
    assert not config.exists()
    assert not data.exists()
    gitconfig = (tmp_path / "gitconfig").read_text()
    assert "127.0.0.1" not in gitconfig
    assert "corporate:8080" in gitconfig
    assert "sbc replaced the bash client" in result.stderr
    assert "proxy:    127.0.0.1:1085" in result.stdout
    _assert_credentials_stay_off_command_lines(log)


OLD_RC_BLOCK = (
    "\n# Network proxy management configuration (sing-box)\n"
    '[ -f "${XDG_DATA_HOME:-$HOME/.local/share}/sing-box/setup.sh" ]'
    ' && source "${XDG_DATA_HOME:-$HOME/.local/share}/sing-box/setup.sh"\n'
)


def test_the_rc_lines_the_bash_client_added_are_removed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _bash_client(home, preferences="rc_enabled=true\nshell=bash\n")
    (home / ".bashrc").write_text("alias ll='ls -l'\n" + OLD_RC_BLOCK)

    result, log = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "sbc install --shell bash --route gfw --protocol hysteria2\n" in log
    # Its uninstaller edits rc files with GNU sed, so install.sh does that part.
    assert "uninstaller --yes --no-rc clean=\n" in log
    assert (home / ".bashrc").read_text() == "alias ll='ls -l'\n\n"
    assert f"Removed the bash client's lines from {home}/.bashrc." in result.stderr


def test_rc_files_the_user_manages_are_left_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _bash_client(home)
    (home / ".zshrc").write_text(OLD_RC_BLOCK)

    result, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (home / ".zshrc").read_text() == OLD_RC_BLOCK
    assert ".zshrc still runs the bash client on lines 3." in result.stderr


@pytest.mark.parametrize(
    ("host", "port", "turned_on"),
    [("127.0.0.1", "1085", True), ("127.0.0.1", "7890", False), ("", "", False)],
)
def test_the_desktop_proxy_comes_back_only_when_it_was_the_bash_clients(
    tmp_path: Path, host: str, port: str, turned_on: bool
) -> None:
    _bash_client(tmp_path / "home")

    result, log = _run(tmp_path, DCONF_PROXY_HOST=host, DCONF_PROXY_PORT=port)

    assert result.returncode == 0, result.stderr
    assert ("sbc desktop on\n" in log) is turned_on
    if turned_on:
        assert log.index("sbc port 1085") < log.index("sbc desktop on")


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("etc/systemd/system/sing-box-manager-tun.service", ""),
        ("var/lib/sing-box-manager-tun.transaction", ""),
        ("var/lib/sing-box-manager-tun.status", "unclean\n"),
    ],
)
def test_a_machine_with_tun_set_up_keeps_the_bash_client(
    tmp_path: Path, path: str, content: str
) -> None:
    _, data = _bash_client(tmp_path / "home")
    marker = Path(os.environ["SBM_TUN_ROOT"]) / path
    marker.parent.mkdir(parents=True)
    marker.write_text(content)

    result, log = _run(tmp_path)

    assert result.returncode == 1
    assert "Keep using the bash client until sbc supports TUN" in result.stderr
    assert log == ""
    assert (data / "setup.sh").exists()


def test_a_tunnel_that_was_turned_off_safely_does_not_block_the_move(
    tmp_path: Path,
) -> None:
    _bash_client(tmp_path / "home")
    status = Path(os.environ["SBM_TUN_ROOT"]) / "var/lib/sing-box-manager-tun.status"
    status.parent.mkdir(parents=True)
    status.write_text("safe-off\n")

    result, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr


def test_a_bash_client_without_its_uninstaller_is_left_alone(tmp_path: Path) -> None:
    _, data = _bash_client(tmp_path / "home")
    (data / "client-uninstall.sh").unlink()

    result, log = _run(tmp_path)

    assert result.returncode == 1
    assert "its uninstaller is missing" in result.stderr
    assert log == ""


def test_when_the_saved_token_fails_the_link_is_asked_for(tmp_path: Path) -> None:
    _bash_client(tmp_path / "home")

    result, log = _run(tmp_path, FAKE_TRADE_FAILS="1")

    assert result.returncode == 0, result.stderr
    assert "Paste the link from the portal's files page" in result.stderr
    assert f"sbc stdin: {LINK}" in log


def test_when_sbc_does_not_work_the_bash_client_stays(tmp_path: Path) -> None:
    config, data = _bash_client(tmp_path / "home")

    result, log = _run(tmp_path, FAKE_SBC_FAILS="1")

    assert result.returncode == 1
    assert "The bash client keeps running as before" in result.stderr
    assert "sbc uninstall --yes\n" in log
    assert "uninstaller" not in log
    assert (config / "portal-token").exists()
    assert (data / "setup.sh").exists()


def test_an_uninstaller_that_stops_after_the_services_is_finished(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config, data = _bash_client(home)
    # It removed the services and the config, then could not remove the data
    # directory, where a file was still held open.
    _executable(
        data / "client-uninstall.sh",
        '#!/bin/sh\nrm -rf "$HOME/.config/sing-box"\nexit 1\n',
    )
    held = data / "held"
    held.mkdir()
    (held / ".nfs0001").write_text("")
    held.chmod(0o500)
    state = home / ".local" / "state" / "sing-box"
    state.mkdir(parents=True)
    (tmp_path / "gitconfig").write_text("[https]\n\tproxy = http://127.0.0.1:1085\n")

    result, log = _run(tmp_path, FAKE_RELEASE=str(held))

    assert result.returncode == 0, result.stderr
    assert "stopped partway, so install.sh removed the rest" in result.stderr
    assert not config.exists()
    assert not data.exists()
    assert not state.exists()
    assert "sbc port 1085\n" in log
    assert "127.0.0.1" not in (tmp_path / "gitconfig").read_text()


def test_an_uninstaller_that_stops_before_the_services_leaves_the_rest(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _, data = _bash_client(home)
    _executable(data / "client-uninstall.sh", "#!/bin/sh\nexit 1\n")
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "sing-box-trojan.service").write_text("[Service]\n")

    result, log = _run(tmp_path)

    assert result.returncode == 1
    assert "the bash client's uninstaller stopped. sbc is running" in result.stderr
    assert (data / "setup.sh").exists()
    assert "sbc port" not in log


def test_sbc_waits_for_the_old_port_to_free_up(tmp_path: Path) -> None:
    _bash_client(tmp_path / "home")

    result, log = _run(tmp_path, FAKE_PORT_BUSY="3")

    assert result.returncode == 0, result.stderr
    assert log.count("sbc port 1085\n") == 4
    assert "still taken" not in result.stderr


def test_sbc_says_which_port_it_kept_when_the_old_one_stays_busy(
    tmp_path: Path,
) -> None:
    _bash_client(tmp_path / "home")

    result, _ = _run(tmp_path, FAKE_PORT_BUSY="99")

    assert result.returncode == 0, result.stderr
    assert (
        "sbc stays on 127.0.0.1:41137 because port 1085 is still taken."
        in result.stderr
    )


def test_rc_lines_that_still_run_the_bash_client_are_named(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _bash_client(home)
    (home / ".zshrc").write_text(
        "# `proxy shell on` takes 130 ms\n"
        '[ -f "$HOME/.local/share/sing-box/setup.sh" ]'
        ' && source "$HOME/.local/share/sing-box/setup.sh"\n'
        "alias ll='ls -l'\n"
        "if has proxy; then proxy shell on; fi\n"
    )

    result, _ = _run(tmp_path)

    assert (
        f"{home}/.zshrc still runs the bash client on lines 2 4. Remove those lines."
        in result.stderr
    )


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_proxy_upgrade_moves_a_real_bash_install_to_sbc(
    tmp_path: Path, shell: str
) -> None:
    """The bash client's own `proxy upgrade` runs install.sh and ends cleanly."""
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is not available")
    install_script = _prepare_client_archive(tmp_path)
    fakebin = _prepare_fake_commands(tmp_path)
    env = {**_linux_env(tmp_path, fakebin), **_fakes(tmp_path, fakebin)}
    env["SBM_SKIP_UPDATE_CHECK"] = "1"
    # The bash client set the GNOME proxy, which its uninstaller turns off.
    env["DCONF_PROXY_HOST"], env["DCONF_PROXY_PORT"] = "127.0.0.1", "1085"
    for name in list(env):
        if name.lower().endswith("_proxy"):
            del env[name]
    installed = subprocess.run(
        ["bash", str(install_script), "--shell", shell],
        env=env,
        input="n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    config = Path(env["XDG_CONFIG_HOME"]) / "sing-box"
    data = Path(env["XDG_DATA_HOME"]) / "sing-box"
    state = Path(env["XDG_STATE_HOME"]) / "sing-box"
    (data / "portal-base-url").write_text(PORTAL + "\n")
    (config / "portal-token").write_text(TOKEN)
    (config / "selected-route").write_text("gfw\n")
    (config / "selected-port").write_text("1085\n")
    rc = Path(env["HOME"]) / (".zshrc" if shell == "zsh" else ".bashrc")
    assert "sing-box/setup.sh" in rc.read_text()

    result = subprocess.run(
        [
            shell_path,
            "-f",
            "-c",
            f'source "{data}/setup.sh"; proxy upgrade; printf "RESULT=%s\\n" "$?"',
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert "RESULT=0" in result.stdout, output
    log = (tmp_path / "log").read_text()
    assert f"sbc install --shell {shell} --route gfw --protocol trojan\n" in log
    assert "sbc port 1085\n" in log
    assert "/system/proxy/mode 'none'" in Path(env["DCONF_LOG"]).read_text()
    assert log.index("sbc port 1085") < log.index("sbc desktop on\n")
    for path in (config, data, state, Path(env["HOME"]) / ".local/bin/sing-box"):
        assert not path.exists(), path
    assert "sing-box/setup.sh" not in rc.read_text()
    assert "sbc replaced the bash client" in output
    _assert_credentials_stay_off_command_lines(log)


def _messages_and_translations() -> tuple[set[str], dict[str, str]]:
    text = SCRIPT.read_text(encoding="utf-8")
    messages = set()
    for match in re.finditer(r"\b(?:say|fail|t) (?:'([^']*)'|\"([^\"]*)\")", text):
        message = match.group(1) if match.group(1) is not None else match.group(2)
        if "$" not in message:
            messages.add(message)
    translations = dict(
        re.findall(r'^\t\t"(.+?)"\) printf \'%s\' "(.+?)" && return ;;$', text, re.M)
    )
    return messages, translations


def test_every_message_has_a_translation_with_the_same_values() -> None:
    messages, translations = _messages_and_translations()

    assert len(messages) > 15
    assert messages - translations.keys() == set()
    assert translations.keys() - messages == set()
    for message, translated in translations.items():
        assert message.count("%s") == translated.count("%s"), message


@pytest.mark.parametrize(
    ("locale", "chinese"),
    [
        ({"LANG": "zh_CN.UTF-8"}, True),
        ({"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"}, False),
        ({"SBC_LANG": "zh", "LANG": "C"}, True),
    ],
)
def test_messages_follow_the_locale(
    tmp_path: Path, locale: dict[str, str], chinese: bool
) -> None:
    _bash_client(tmp_path / "home")

    result, _ = _run(tmp_path, FAKE_PORT_BUSY="99", **locale)

    assert result.returncode == 0, result.stderr
    assert ("sbc 已取代 bash 客户端" in result.stderr) is chinese
    assert ("sbc replaced the bash client" in result.stderr) is not chinese
    if chinese:
        assert "sbc 仍在 127.0.0.1:41137，因为端口 1085 仍被占用" in result.stderr


def test_the_portal_serves_the_script_at_every_installer_path(
    client: TestClient,
) -> None:
    for path in ("/install.sh", "/install/linux.sh", "/install/macos.sh"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == SCRIPT.read_text(encoding="utf-8")
