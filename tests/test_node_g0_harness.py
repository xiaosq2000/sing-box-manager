"""Check G0 safety and fixture construction without starting a VM or service."""

import errno
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.node_g0 import run as harness


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


@pytest.mark.parametrize(
    ("output", "category"),
    [
        (b"KVM: entry failed, hardware error 0x80000021", "kvm-run-error"),
        (b"failed to initialize KVM: Permission denied", "kvm-init-error"),
        (b"Could not set up host forwarding rule", "host-forward-error"),
        (b"Could not set up guest forwarding rule", "guest-forward-error"),
        (b'Failed to get "write" lock', "drive-lock-error"),
        (b"Could not open private-disk", "drive-open-error"),
        (b"cannot set up guest memory", "memory-error"),
        (b"could not load PC BIOS", "firmware-error"),
        (b"invalid option", "option-error"),
        (b"terminating on signal 15", "signal"),
        (b"unknown private error", "unclassified"),
        (b"", "empty"),
    ],
)
def test_qemu_diagnostic_uses_only_fixed_categories(tmp_path, capsys, output, category):
    private = b"PRIVATE-fixture-credential-and-path"
    (tmp_path / "qemu.log").write_bytes(output + (b" " + private if output else b""))
    (tmp_path / "serial.log").write_bytes(b"Linux version\nKernel panic\n" + private)
    result = harness.qemu_diagnostic(tmp_path)
    assert result["log_category"] == category
    assert result["kvm_markers"]["initialize"] == (
        b"failed to initialize kvm" in output.lower()
    )
    assert result["errno_markers"]["permission-denied"] == (
        b"permission denied" in output.lower()
    )
    assert result["boot_markers"]["kernel-start"]
    assert result["boot_markers"]["kernel-panic"]
    assert not result["boot_markers"]["cloud-init"]
    diagnostics = harness.Diagnostics()
    process = subprocess.CompletedProcess([], 1)
    process.poll = lambda: 1
    diagnostics.failure(RuntimeError(private), {"qemu": process}, tmp_path)
    assert private.decode() not in capsys.readouterr().out
    assert private.decode() not in json.dumps(result)


def test_absent_qemu_logs_do_not_hide_original_failure(tmp_path):
    assert harness.qemu_diagnostic(tmp_path)["log_category"] == "empty"


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_inaccessible_kvm_selects_tcg_before_a_vm_starts(
    monkeypatch, tmp_path, capsys, failure
):
    def denied(path, flags):
        assert path == "/dev/kvm"
        assert flags == harness.os.O_RDWR | harness.os.O_CLOEXEC
        raise failure("PRIVATE-device-error")

    monkeypatch.setattr(harness.os, "open", denied)
    monkeypatch.setattr(
        harness, "run", lambda *a, **kw: pytest.fail("started a VM probe")
    )
    log = tmp_path / "probe.log"
    assert (
        harness.select_acceleration(log, ["-device", "fixture"], {"FIXTURE": "1"})
        == "tcg"
    )
    assert "PRIVATE-device-error" not in capsys.readouterr().out
    assert "Selecting TCG" in log.read_text()


@pytest.mark.parametrize("result", [b"kvm\n", b"tcg\n"])
def test_accessible_kvm_still_requires_startup_probe(monkeypatch, tmp_path, result):
    closed = []
    monkeypatch.setattr(harness.os, "open", lambda path, flags: 99)
    monkeypatch.setattr(harness.os, "close", closed.append)
    log = tmp_path / "probe.log"
    options = [
        "-drive",
        "file=PRIVATE-fixture-disk",
        "-netdev",
        "PRIVATE-fixture-network",
    ]
    process_env = {"FIXTURE": "1"}

    def probe(argv, **kwargs):
        assert closed == [99]
        assert argv == [
            harness.sys.executable,
            str(harness.ROOT / "scripts/dev/tun-vm/select-accel.py"),
            *options,
        ]
        assert kwargs == {"log": log, "env": process_env}
        return result

    monkeypatch.setattr(harness, "run", probe)
    assert (
        harness.select_acceleration(log, options, process_env)
        == result.decode().strip()
    )


def test_kvm_snapshots_distinguish_access_change_without_raw_acl(
    monkeypatch, tmp_path, capsys
):
    original_stat, original_open, original_close = (
        harness.os.stat,
        harness.os.open,
        harness.os.close,
    )
    changed = False
    closed = []
    private = b"PRIVATE-fixture-acl-and-error"

    def stat(path, *args, **kwargs):
        if path == "/dev/kvm":
            return SimpleNamespace(st_mode=0o20660, st_uid=0, st_gid=108, st_ino=7)
        return original_stat(path, *args, **kwargs)

    def acl(path, name):
        assert (path, name) == ("/dev/kvm", "system.posix_acl_access")
        if changed:
            raise OSError(errno.ENODATA, private.decode())
        return private

    def open_device(path, *args, **kwargs):
        if path != "/dev/kvm":
            return original_open(path, *args, **kwargs)
        assert args == (harness.os.O_RDWR | harness.os.O_CLOEXEC,)
        if changed:
            raise PermissionError(errno.EACCES, private.decode())
        return 91

    def close(descriptor):
        if descriptor == 91:
            closed.append(descriptor)
        else:
            original_close(descriptor)

    monkeypatch.setattr(harness.os, "stat", stat)
    monkeypatch.setattr(harness.os, "getxattr", acl)
    monkeypatch.setattr(harness.os, "open", open_device)
    monkeypatch.setattr(harness.os, "close", close)
    before = harness.kvm_snapshot(tmp_path, "before-probe")
    changed = True
    after = harness.kvm_snapshot(tmp_path, "after-probe")
    assert before["device"] == after["device"]
    assert before["rdwr_open"] is True
    assert before["acl_sha256"] == harness.hashlib.sha256(private).hexdigest()
    assert after["rdwr_open"] is False
    assert after["open_errno"] == errno.EACCES
    assert after["acl_errno"] == errno.ENODATA
    assert closed == [91]
    assert private.decode() not in capsys.readouterr().out
    for point, expected in (("before-probe", before), ("after-probe", after)):
        path = tmp_path / f"kvm-{point}.json"
        assert json.loads(path.read_text()) == expected
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        harness.kvm_snapshot(tmp_path, "PRIVATE-invalid-point")


def test_qemu_exit_observation_does_not_change_wait_or_retry(monkeypatch, tmp_path):
    observations = []
    poll_results = iter([None, 1, 1])
    process = SimpleNamespace(poll=lambda: next(poll_results))
    diagnostics = harness.Diagnostics()

    def guest(*args, **kwargs):
        raise harness.CommandFailure("ssh-refused", 255)

    def wait(predicate, seconds):
        assert seconds == 180
        assert [predicate() for _ in range(3)] == [False, False, False]
        raise harness.StateTimeout("fixture timeout")

    monkeypatch.setattr(harness, "wait", wait)
    monkeypatch.setattr(
        harness, "kvm_snapshot", lambda *args: observations.append(args)
    )
    with pytest.raises(harness.StateTimeout):
        harness.wait_for_guest(
            guest, diagnostics, tmp_path / "guest.log", qemu_process=process
        )
    assert diagnostics.guest_attempts == 3
    assert observations == [(tmp_path, "after-qemu-exit", 1)]


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
    assert "continue-on-error" not in native
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    assert all("self-hosted" not in json.dumps(step) for step in steps)
    assert "node-agent/**" in config["on"]["pull_request"]["paths"]
