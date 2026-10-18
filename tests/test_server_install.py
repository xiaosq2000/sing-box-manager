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
    state.setdefault('configs', []).append((Path(os.environ['SBM_SERVER_ROOT']) / 'usr/local/etc/sing-box/config.json').read_text())
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


@pytest.mark.parametrize("incoming,live", [(True, False), (True, True), (None, True)])
def test_deploy_preserves_database_config_even_with_an_old_release(
    server_fixture, incoming, live, tmp_path
):
    from sing_box_manager.deploy import node_install_command

    root, package, state, env = server_fixture
    deploy_root = tmp_path / "deployed root"
    interpreter = deploy_root / ".pixi/envs/default/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    scripts = deploy_root / "scripts"
    shutil.copytree(package, scripts)
    # An old release's installer does not support config preservation.
    (package / "server-install.sh").write_text("#!/bin/sh\nexit 99\n")
    runtime = deploy_root / "config/inventory/runtime.yaml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        f"sing_box_version: 1.14.2\ncontrol_plane:\n  enabled: true\n  authoritative: {str(live).lower()}\n"
    )
    config = root / "usr/local/etc/sing-box/config.json"
    expected = {
        "inbounds": [
            {
                "type": "trojan",
                "users": [{"name": "db_only", "password": "fixture-db-secret"}],
            }
        ]
    }
    config.write_text(json.dumps(expected))
    command = node_install_command(
        str(deploy_root), str(package), [], authoritative=incoming
    )
    for _ in range(2):
        result = subprocess.run(
            ["bash", "-c", command],
            env={**env, "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(config.read_text()) == expected
    restarted_configs = json.loads(state.read_text())["configs"]
    assert all(json.loads(value) == expected for value in restarted_configs)


@pytest.mark.parametrize("incoming,live", [(False, True), (False, False), (None, False)])
def test_inventory_mode_installs_release_roster(server_fixture, incoming, live, tmp_path):
    from sing_box_manager.deploy import node_install_command

    root, package, state, env = server_fixture
    deployed = tmp_path / "deployed"
    interpreter = deployed / ".pixi/envs/default/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    runtime = deployed / "config/inventory/runtime.yaml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        f"sing_box_version: 1.14.2\ncontrol_plane:\n  enabled: true\n  authoritative: {str(live).lower()}\n"
    )
    command = node_install_command(str(deployed), str(package), [], authoritative=incoming)
    result = subprocess.run(
        ["bash", "-c", command], env={**env, "PYTHONPATH": str(ROOT)}, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((root / "usr/local/etc/sing-box/config.json").read_text()) == json.loads((package / "config.json").read_text())


def test_skip_code_refuses_unreadable_deployed_mode(server_fixture, tmp_path):
    from sing_box_manager.deploy import node_install_command

    root, package, state, env = server_fixture
    deployed = tmp_path / "deployed"
    interpreter = deployed / ".pixi/envs/default/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    command = node_install_command(str(deployed), str(package), [], authoritative=None)
    result = subprocess.run(
        ["bash", "-c", command], env={**env, "PYTHONPATH": str(ROOT)}, text=True, capture_output=True
    )
    assert result.returncode != 0
    assert "Cannot read deployed settings" in result.stderr
    assert json.loads(state.read_text())["calls"] == []
    assert (root / "usr/local/etc/sing-box/config.json").read_text() == "old-config"


@pytest.mark.parametrize("args", [[], ["-p", "trojan"]])
def test_preservation_never_falls_back_to_packaged_users(server_fixture, args):
    root, package, state, env = server_fixture
    config = root / "usr/local/etc/sing-box/config.json"
    config.unlink()
    result = run_installer(server_fixture, "--preserve-config", *args)
    assert result.returncode != 0
    assert not config.exists()
    assert json.loads(state.read_text())["calls"] == []


def test_preservation_check_failure_leaves_live_config_and_service_unchanged(
    server_fixture,
):
    root, package, state, env = server_fixture
    config = root / "usr/local/etc/sing-box/config.json"
    expected = {"inbounds": [{"type": "trojan", "users": []}]}
    config.write_text(json.dumps(expected))
    result = run_installer(server_fixture, "--preserve-config", CHECK_FAIL="1")
    assert result.returncode != 0
    assert json.loads(config.read_text()) == expected
    assert json.loads(state.read_text())["calls"] == []


def test_installer_waits_for_node_sync_lock(server_fixture):
    import fcntl
    import time

    root, package, state, env = server_fixture
    config = root / "usr/local/etc/sing-box/config.json"
    tools = Path(env["PATH"].split(":")[0])
    ready = tools / "waiting"
    real_flock = shutil.which("flock")
    (tools / "flock").write_text(
        f'#!/bin/sh\ntouch "{ready}"\nexec "{real_flock}" "$@"\n'
    )
    (tools / "flock").chmod(0o755)
    with (config.parent / ".sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(package / "server-install.sh"), "--preserve-config"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert ready.exists()
            assert json.loads(state.read_text())["calls"] == []
            updated = {"inbounds": [{"type": "trojan", "users": []}]}
            config.write_text(json.dumps(updated))
            fcntl.flock(lock, fcntl.LOCK_UN)
            _, errors = process.communicate(timeout=15)
            assert process.returncode == 0, errors
            assert json.loads(config.read_text()) == updated
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_installer_selects_exactly_the_requested_protocol_set(server_fixture) -> None:
    root, package, state, env = server_fixture
    result = run_installer(server_fixture, "-p", "naive", "-p", "trojan")
    assert result.returncode == 0, result.stderr
    config = json.loads((root / "usr/local/etc/sing-box/config.json").read_text())
    assert [entry["type"] for entry in config["inbounds"]] == ["trojan", "naive"]
    assert not (root / "etc/sysctl.d/99-sing-box-hysteria2.conf").exists()
