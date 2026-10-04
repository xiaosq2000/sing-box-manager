"""Run the node installer against a scratch filesystem and fake systemd only."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGACY = [
    f"sing-box-{protocol}.service" for protocol in ("trojan", "hysteria2", "naive")
]


@pytest.fixture
def server_fixture(tmp_path: Path):
    root = tmp_path / "sysroot"
    package = tmp_path / "package"
    package.mkdir()
    (package / "lib").mkdir()
    for name in ("server-install.sh", "server-uninstall.sh", "sing-box.service"):
        shutil.copy2(ROOT / "scripts" / name, package / name)
    shutil.copy2(ROOT / "scripts/lib/ui.sh", package / "lib/ui.sh")
    # The actual script's filesystem paths and systemctl calls remain intact.
    # Only its root-user guard is bypassed in the scratch copy for rootless CI.
    script = package / "server-install.sh"
    script.write_text(
        script.read_text().replace("if [[ $EUID -ne 0 ]]; then", "if false; then")
    )
    (package / "config.json").write_text(
        json.dumps(
            {
                "inbounds": [
                    {"type": protocol, "tag": protocol}
                    for protocol in ("trojan", "hysteria2", "naive")
                ]
            }
        )
    )
    binary = package / "sing-box"
    binary.write_text('#!/usr/bin/env bash\n[[ "${CHECK_FAIL:-0}" != 1 ]]\n')
    binary.chmod(0o755)
    units = root / "etc/systemd/system"
    units.mkdir(parents=True)
    for unit in LEGACY:
        (units / unit).write_text(unit)
    (units / "sing-box-manager-web.service").write_text("portal must survive")
    old_binary = root / "usr/local/bin/sing-box"
    old_binary.parent.mkdir(parents=True)
    old_binary.write_text("old-binary")
    old_config = root / "usr/local/etc/sing-box/config.json"
    old_config.parent.mkdir(parents=True)
    old_config.write_text("old-config")
    for protocol in ("trojan", "hysteria2", "naive"):
        (old_config.parent / protocol).mkdir()
        (old_config.parent / protocol / "config.json").write_text("old-protocol-config")
    state = tmp_path / "systemd.json"
    state.write_text(json.dumps({"active": LEGACY, "enabled": LEGACY, "calls": []}))
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "systemctl"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
path = Path(os.environ['SYSTEMD_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
state['calls'].append(args)
status = 0
verb, unit = args[0], args[-1]
if verb in ('is-active', 'is-enabled'):
    status = 0 if unit in state['active' if verb == 'is-active' else 'enabled'] else 1
elif verb == 'stop':
    state['active'] = [name for name in state['active'] if name != unit]
elif verb == 'disable':
    state['enabled'] = [name for name in state['enabled'] if name != unit]
elif verb == 'enable':
    state['enabled'] = sorted(set(state['enabled'] + [unit]))
elif verb == 'restart':
    if (unit == 'sing-box.service' and os.environ.get('START_FAIL') == '1') or (unit != 'sing-box.service' and os.environ.get('ROLLBACK_FAIL') == '1'):
        status = 1
    else:
        state['active'] = sorted(set(state['active'] + [unit]))
path.write_text(json.dumps(state))
sys.exit(status)
""")
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "SBM_SERVER_ROOT": str(root),
        "SYSTEMD_STATE": str(state),
    }
    return root, package, state, env


def run_installer(fixture, *args, **overrides):
    root, package, state, env = fixture
    return subprocess.run(
        ["bash", str(package / "server-install.sh"), *args],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_migrates_all_protocols_once_and_keeps_the_portal(
    server_fixture,
) -> None:
    root, package, state, env = server_fixture
    result = run_installer(server_fixture)
    assert result.returncode == 0, result.stderr
    installed = json.loads(state.read_text())
    assert installed["active"] == ["sing-box.service"]
    assert installed["enabled"] == ["sing-box.service"]
    units = root / "etc/systemd/system"
    assert (units / "sing-box.service").is_file()
    assert (units / "sing-box-manager-web.service").read_text() == "portal must survive"
    assert not any((units / name).exists() for name in LEGACY)
    config = root / "usr/local/etc/sing-box/config.json"
    assert len(json.loads(config.read_text())["inbounds"]) == 3
    assert config.stat().st_mode & 0o777 == 0o600
    assert not (config.parent / "trojan").exists()
    assert (root / "etc/sysctl.d/99-sing-box-hysteria2.conf").exists()
    # Reinstall must restart the single unit, not leave it running old code.
    assert run_installer(server_fixture).returncode == 0
    assert json.loads(state.read_text())["active"] == ["sing-box.service"]


@pytest.mark.parametrize("failure", ["CHECK_FAIL", "START_FAIL"])
def test_installer_failure_preserves_previous_files_and_legacy_state(
    server_fixture, failure: str
) -> None:
    root, package, state, env = server_fixture
    result = run_installer(server_fixture, **{failure: "1"})
    assert result.returncode != 0
    installed = json.loads(state.read_text())
    assert set(installed["active"]) == set(LEGACY)
    assert set(installed["enabled"]) == set(LEGACY)
    assert (root / "usr/local/bin/sing-box").read_text() == "old-binary"
    assert (root / "usr/local/etc/sing-box/config.json").read_text() == "old-config"
    assert all((root / "etc/systemd/system" / name).is_file() for name in LEGACY)
    assert not (root / "etc/systemd/system/sing-box.service").exists()
    if failure == "CHECK_FAIL":
        assert not any(
            call[0] in {"stop", "disable", "restart"} for call in installed["calls"]
        )


def test_failed_rollback_retains_private_recovery_files(server_fixture) -> None:
    root, package, state, env = server_fixture
    result = run_installer(
        server_fixture, START_FAIL="1", ROLLBACK_FAIL="1", LC_ALL="C"
    )
    assert result.returncode != 0
    recovery = list((root / "usr/local/etc/sing-box").glob(".install.*"))
    assert len(recovery) == 1
    assert recovery[0].stat().st_mode & 0o777 == 0o700
    assert (recovery[0] / "binary.old").read_text() == "old-binary"
    assert (recovery[0] / "config.old").read_text() == "old-config"
    assert "recovery files retained" in result.stdout + result.stderr


@pytest.mark.parametrize("args", [[], ["-p", "trojan"]])
def test_installer_accepts_packages_without_an_unused_naive_inbound(
    server_fixture, args
) -> None:
    root, package, state, env = server_fixture
    source = package / "config.json"
    config = json.loads(source.read_text())
    config["inbounds"] = [
        entry for entry in config["inbounds"] if entry["type"] != "naive"
    ]
    source.write_text(json.dumps(config))
    result = run_installer(server_fixture, *args)
    assert result.returncode == 0, result.stderr
    installed = json.loads((root / "usr/local/etc/sing-box/config.json").read_text())
    assert [entry["type"] for entry in installed["inbounds"]] == (
        ["trojan"] if args else ["trojan", "hysteria2"]
    )
    assert json.loads(state.read_text())["active"] == ["sing-box.service"]


@pytest.mark.parametrize("args", [["-p", "naive"], ["-p", "trojan", "-p", "naive"]])
def test_explicit_missing_naive_selection_fails_before_changing_services(
    server_fixture, args
) -> None:
    root, package, state, env = server_fixture
    source = package / "config.json"
    config = json.loads(source.read_text())
    config["inbounds"] = [
        entry for entry in config["inbounds"] if entry["type"] != "naive"
    ]
    source.write_text(json.dumps(config))
    result = run_installer(server_fixture, *args)
    assert result.returncode != 0
    assert "Selected protocol missing from server config: naive" in result.stderr
    assert json.loads(state.read_text())["calls"] == []
    assert (root / "usr/local/bin/sing-box").read_text() == "old-binary"
    assert (root / "usr/local/etc/sing-box/config.json").read_text() == "old-config"
    assert all((root / "etc/systemd/system" / unit).is_file() for unit in LEGACY)


def test_installer_selects_exactly_the_requested_protocol_set(server_fixture) -> None:
    root, package, state, env = server_fixture
    result = run_installer(server_fixture, "-p", "naive", "-p", "trojan")
    assert result.returncode == 0, result.stderr
    config = json.loads((root / "usr/local/etc/sing-box/config.json").read_text())
    assert [entry["type"] for entry in config["inbounds"]] == ["trojan", "naive"]
    assert not (root / "etc/sysctl.d/99-sing-box-hysteria2.conf").exists()
