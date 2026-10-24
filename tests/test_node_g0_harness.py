"""Check G0 safety and fixture construction without starting a VM or service."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.node_g0 import run as harness
from tests.node_g0 import reliability


def test_guard_refuses_execution_without_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("SBM_NODE_G0", raising=False)
    monkeypatch.setattr(
        harness.sys, "argv", ["g0", "--engine", str(tmp_path / "unused")]
    )
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("guard started a process"),
    )
    with pytest.raises(SystemExit) as error:
        harness.main()
    assert error.value.code == 2


def test_checksum_failure_precedes_executable_or_vm_start(monkeypatch, tmp_path):
    monkeypatch.setenv("SBM_NODE_G0", "1")
    monkeypatch.setattr(harness.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(harness.shutil, "which", lambda name: "/fixture/" + name)
    monkeypatch.setattr(
        harness.sys,
        "argv",
        [
            "g0",
            "--engine",
            str(tmp_path / "engine"),
            "--image",
            str(tmp_path / "image.img"),
        ],
    )
    (tmp_path / "engine").write_bytes(b"not an executable")
    (tmp_path / "engine.sha256").write_text("0" * 64)
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("executed unverified input"),
    )
    with pytest.raises(SystemExit) as error:
        harness.main()
    assert error.value.code == 2


def test_fixture_bundle_keeps_security_rules_and_exact_accounting_label():
    bundle = harness.engine_bundle("synthetic-g0-secret")
    config = bundle["config"]
    assert len(config["inbounds"]) == 1
    assert config["inbounds"][0]["type"] == "trojan"
    assert config["inbounds"][0]["users"][0]["name"] == bundle["roster"][0]["wire_name"]
    rules = config["route"]["rules"]
    assert rules[0] == {
        "ip_cidr": ["127.0.0.1/32"],
        "port": 18080,
        "action": "route",
        "outbound": "direct",
    }
    assert {"ip_is_private": True, "action": "reject"} in rules
    assert config["experimental"]["v2ray_api"]["listen"] == "127.0.0.1:19080"
    assert "SBM_CONFIG" not in harness.ENV
    assert "HTTPS_PROXY" not in harness.ENV


def test_evidence_fingerprint_covers_untracked_source(monkeypatch, tmp_path):
    for file in (
        "pixi.toml",
        "pixi.lock",
        "tests/test_node_g0.py",
        "node-agent/internal/fixture.go",
    ):
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    before = harness.source_fingerprint()
    (tmp_path / "node-agent/internal/fixture.go").write_text("changed")
    assert harness.source_fingerprint() != before


def test_counter_protobuf_matches_its_pinned_generator(tmp_path):
    root = Path(__file__).parents[1]
    plugin = tmp_path / "protoc-gen-go"
    subprocess.run(
        [
            "go",
            "-C",
            str(root / "node-agent"),
            "build",
            "-o",
            str(plugin),
            "google.golang.org/protobuf/cmd/protoc-gen-go",
        ],
        check=True,
        capture_output=True,
        env=harness.ENV,
        timeout=120,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "--proto_path=" + str(root / "node-agent"),
            "--plugin=protoc-gen-go=" + str(plugin),
            "--go_out=" + str(tmp_path),
            "--go_opt=paths=source_relative",
            str(root / "node-agent/internal/counter/stats.proto"),
        ],
        check=True,
        capture_output=True,
        env=harness.ENV,
        timeout=60,
    )
    assert (tmp_path / "internal/counter/stats.pb.go").read_bytes() == (
        root / "node-agent/internal/counter/stats.pb.go"
    ).read_bytes()


@pytest.mark.parametrize(
    ("returncode", "stderr", "category"),
    [
        (255, b"Host key verification failed.", "ssh-host-key"),
        (255, b"REMOTE HOST IDENTIFICATION HAS CHANGED", "ssh-host-key"),
        (255, b"Permission denied (publickey).", "ssh-authentication"),
        (255, b"Connection refused", "ssh-refused"),
        (255, b"Connection timed out", "ssh-timeout"),
        (255, b"Connection closed by remote host", "ssh-transport"),
        (1, b"", "guest-not-ready"),
    ],
)
def test_ssh_diagnostics_use_fixed_categories(returncode, stderr, category):
    assert harness.ssh_category(returncode, stderr) == category


@pytest.mark.parametrize("timeout", [False, True])
def test_command_failure_keeps_output_private(monkeypatch, tmp_path, capsys, timeout):
    private = b"PRIVATE-fixture-credential-and-path"
    argv = ["ssh", private.decode()]

    def execute(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(argv, 15, output=private, stderr=private)
        return subprocess.CompletedProcess(argv, 255, private, private)

    monkeypatch.setattr(harness.subprocess, "run", execute)
    log = tmp_path / "command.log"
    with pytest.raises(harness.CommandFailure) as caught:
        harness.run(argv, log=log)
    diagnostics = harness.Diagnostics()
    diagnostics.phase("guest-ready")
    diagnostics.failure(caught.value, {})
    public = capsys.readouterr().out + str(caught.value)
    assert private.decode() not in public
    assert log.read_bytes() == private * 2
    assert log.stat().st_mode & 0o777 == 0o600
    expected = "command-timeout" if timeout else "ssh-transport"
    assert harness.safe_error(caught.value)["category"] == expected


def test_unknown_errors_and_fields_cannot_enter_public_diagnostics(capsys):
    private = "PRIVATE-fixture-credential-and-path"
    diagnostics = harness.Diagnostics()
    with pytest.raises(ValueError):
        diagnostics.phase(private)
    diagnostics.current = private
    diagnostics.acceleration = private
    diagnostics.guest_last = harness.CommandFailure(private, private)
    diagnostics.failure(RuntimeError(private), {private: object()})
    public = capsys.readouterr().out
    assert private not in public
    assert '"category": "fixture-error"' in public
    assert '"processes": {}' in public


@pytest.mark.parametrize("ready", [False, True])
def test_guest_readiness_retains_last_safe_outcome(
    monkeypatch, tmp_path, capsys, ready
):
    diagnostics = harness.Diagnostics()
    diagnostics.phase("guest-ready")
    diagnostics.acceleration = "tcg"
    calls = []

    def guest(command, *, log):
        calls.append(log)
        if not ready:
            raise harness.CommandFailure("ssh-authentication", 255)
        return b""

    def wait(predicate, seconds):
        assert seconds == 180
        if not predicate():
            raise harness.StateTimeout("PRIVATE-fixture-state")

    class Exited:
        def poll(self):
            return 1

    monkeypatch.setattr(harness, "wait", wait)
    log = tmp_path / "guest-ready-last.log"
    if ready:
        harness.wait_for_guest(guest, diagnostics, log)
        assert diagnostics.guest_last is None
    else:
        with pytest.raises(harness.StateTimeout) as caught:
            harness.wait_for_guest(guest, diagnostics, log)
        diagnostics.failure(caught.value, {"qemu": Exited()})
        public = capsys.readouterr().out
        assert "PRIVATE-fixture-state" not in public
        assert '"category": "state-timeout"' in public
        assert (
            '"guest_last": {"category": "ssh-authentication", "returncode": 255}'
            in public
        )
        assert '"qemu": {"returncode": 1}' in public
    assert diagnostics.guest_attempts == 1
    assert calls == [log]


def test_qemu_exit_aborts_guest_wait_without_another_ssh_attempt(tmp_path):
    class Exited:
        def poll(self):
            return 1

    diagnostics = harness.Diagnostics()
    with pytest.raises(harness.CommandFailure) as error:
        harness.wait_for_guest(
            lambda *a, **kw: pytest.fail("SSH attempted after QEMU exit"),
            diagnostics,
            tmp_path / "guest.log",
            qemu_process=Exited(),
        )
    assert harness.safe_error(error.value) == {
        "category": "command-exit",
        "returncode": 1,
    }
    assert diagnostics.guest_attempts == 0


def test_state_timeout_is_not_success():
    with pytest.raises(harness.StateTimeout):
        harness.wait(lambda: False, seconds=0)


def test_ci_uses_kvm_ownership_instead_of_a_transient_named_user_acl():
    root = Path(__file__).parents[1]
    workflows = {
        name: yaml.load(
            (root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
        )
        for name in ("node-agent.yml", "sbc.yml")
    }
    job = workflows["node-agent.yml"]["jobs"]["node-g0"]
    setup = next(step for step in job["steps"] if "/dev/kvm" in step.get("run", ""))
    native = next(
        step
        for step in job["steps"]
        if "scripts/dev/node-g0/run.py" in step.get("run", "")
    )
    expected = 'sudo chown "$(id -u)" /dev/kvm'
    assert expected in setup["run"]
    assert any(
        expected in step.get("run", "")
        for step in workflows["sbc.yml"]["jobs"]["e2e-tun"]["steps"]
    )
    assert "setfacl" not in setup["run"]
    assert "chmod" not in setup["run"]
    assert job["steps"].index(setup) < job["steps"].index(native)
    for item in (job, setup, native):
        assert "if" not in item
        assert "continue-on-error" not in item


def test_ci_uses_disposable_runner_and_never_uploads_private_runs():
    root = Path(__file__).parents[1]
    config = yaml.load(
        (root / ".github/workflows/node-agent.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert config["permissions"] == {"contents": "read"}
    job = config["jobs"]["node-g0"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    native = next(
        step for step in steps if "scripts/dev/node-g0/run.py" in step.get("run", "")
    )
    assert native["env"]["SBM_NODE_G0"] == "1"
    assert "--startup-checks" in native["run"]
    assert "continue-on-error" not in native
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    assert all("self-hosted" not in json.dumps(step) for step in steps)
    assert "node-agent/**" in config["on"]["pull_request"]["paths"]


@pytest.mark.parametrize("ready_after", [2, None])
def test_restart_listener_wait_accepts_delay_but_bounds_unavailability(
    monkeypatch, ready_after
):
    elapsed = 0
    attempts = 0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    def guest(command):
        nonlocal attempts
        attempts += 1
        assert "socket.create_connection" in command
        assert "timeout=0.25" in command
        return (
            b"ready\n"
            if ready_after is not None and attempts > ready_after
            else b"pending\n"
        )

    monkeypatch.setattr(harness.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(harness.time, "sleep", sleep)
    if ready_after is None:
        with pytest.raises(harness.StateTimeout):
            reliability.wait_proxy(guest, harness.wait)
        assert elapsed == 5
    else:
        reliability.wait_proxy(guest, harness.wait)
        assert attempts == 3 and elapsed == 0.5


def test_restart_listener_poll_budget_allows_a_completed_late_probe(monkeypatch):
    elapsed = 0

    def guest(command):
        nonlocal elapsed
        elapsed += 6
        return b"ready\n"

    monkeypatch.setattr(harness.time, "monotonic", lambda: elapsed)
    reliability.wait_proxy(guest, harness.wait)
    assert elapsed == 6


def test_restart_listener_wait_does_not_hide_ssh_failure():
    def guest(command):
        raise harness.CommandFailure("ssh-transport", 255)

    with pytest.raises(harness.CommandFailure):
        reliability.wait_proxy(guest, harness.wait)


def test_proxy_starts_through_the_locked_exec_wrapper():
    root = Path(__file__).parents[1]
    unit = (root / "scripts/dev/node-g0/templates/sbm-g0-proxy.service.j2").read_text()
    assert "ExecStart=/opt/sbm-node-g0/sbm-node-g0 exec " in unit
    assert "--engine /opt/sbm-node-g0/sing-box" in unit
    assert "current/config.json" not in unit
    assert "KillMode=control-group" in unit


@pytest.mark.parametrize(
    ("code", "output", "accepted"),
    [
        (1, b"G0 failed: agent writer already active\n", True),
        (255, b"G0 failed: agent writer already active\n", False),
        (1, b"PRIVATE-credential SSH error\n", False),
        (1, b"PRIVATE-credential\nG0 failed: agent writer already active\n", False),
        (None, b"G0 failed: agent writer already active\n", False),
        (0, b"", False),
    ],
)
def test_startup_rejection_requires_the_exact_agent_failure(
    code, output, accepted, tmp_path
):
    def guest(command, *, log):
        assert command == reliability.ONCE
        log.write_bytes(output)
        if code != 0:
            raise harness.CommandFailure("command-exit", code)
        return output

    log = tmp_path / "rejection.log"
    if accepted:
        reliability.reject_once(guest, log, "agent writer already active")
    else:
        with pytest.raises(RuntimeError, match="startup negative control") as error:
            reliability.reject_once(guest, log, "agent writer already active")
        assert "PRIVATE" not in str(error.value)
