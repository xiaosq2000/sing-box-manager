"""Root supervisor logic with synthetic files and mocked service/database calls."""

import hashlib
import json
import os
import sys
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sing_box_manager.account_copy.schema import copy_context_identity
from sing_box_manager.control_plane.import_schema import canonical
from sing_box_manager.managed_access import node, storage
from tests.test_control_plane_account_copy import source_files


@pytest.fixture
def host(tmp_path, monkeypatch):
    runtime, auth, proxy = source_files(tmp_path)
    state = tmp_path / "state"
    state.mkdir(mode=0o750)
    binary = tmp_path / "installed-sing-box"
    binary.write_bytes(b"synthetic binary")
    binary.chmod(0o755)
    unit = tmp_path / "unit"
    unit.write_text("synthetic unit")
    snapshot = {
        "identity": str(uuid4()),
        "revision": 1,
        "active": False,
        "context": copy_context_identity(runtime),
        "access": {
            kind: [
                row.model_dump(mode="json")
                for row in getattr(
                    runtime, "web_portal" if kind == "portal" else kind
                ).users
            ]
            for kind in ("portal", "trojan", "hysteria2", "naive")
        },
    }
    calls = []
    state_data = {
        "restart_failure": False,
        "pending": False,
        "failed": False,
        "invocation": "before",
        "legacy_code": 3,
    }

    def rpc(runtime, action, **payload):
        calls.append(action)
        if action == "snapshot":
            return deepcopy(snapshot)
        if action == "begin":
            state_data["pending"] = True
            return {"started": True}
        if action == "result":
            state_data["pending"] = state_data["failed"] = payload["failed"]
            return {
                "revision": snapshot["revision"],
                "applied_revision": payload["revision"],
                "pending": payload["failed"],
            }
        raise AssertionError(action)

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        code = 0
        if argv[:2] == ["systemctl", "restart"]:
            code = int(state_data["restart_failure"])
            state_data["invocation"] = "after"
        elif argv[:2] == ["systemctl", "is-active"]:
            code = state_data["legacy_code"]
        return SimpleNamespace(
            returncode=code, stdout=b"", stderr=b"synthetic-private-error"
        )

    monkeypatch.setattr(node, "_locked", lambda: nullcontext())
    monkeypatch.setattr(
        node, "_root_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(node, "_unit_guard", lambda: calls.append("unit-check"))
    monkeypatch.setattr(node, "_check", lambda *args: calls.append("config-check"))
    monkeypatch.setattr(
        node, "_running", lambda: {"InvocationID": state_data["invocation"]}
    )
    monkeypatch.setattr(node, "_rpc", rpc)
    monkeypatch.setattr(node, "_run", run)
    monkeypatch.setattr(
        node, "_stored_source", lambda runtime: {"synthetic": "unchanged"}
    )
    monkeypatch.setattr(node, "BINARY", binary)
    monkeypatch.setattr(node, "CONFIG", proxy)
    monkeypatch.setattr(node, "UNIT", unit)
    monkeypatch.setattr(storage, "STATE_ROOT", state)
    monkeypatch.setattr(storage, "FENCE", state / "owner.json")
    storage.FENCE.write_bytes(
        canonical({"version": 1, "identity": snapshot["identity"]}) + b"\n"
    )
    storage.FENCE.chmod(0o600)
    return SimpleNamespace(
        runtime=runtime,
        snapshot=snapshot,
        calls=calls,
        state=state_data,
        root=tmp_path,
        run=tmp_path / "plan",
        binary=binary,
        proxy=proxy,
    )


def test_plan_and_exact_apply_order_with_retained_recovery(host):
    original = host.proxy.read_bytes()
    assert node.plan(host.runtime, host.run)["revision"] == 1
    assert "begin" not in host.calls
    result = node.apply(host.runtime, host.run, acknowledge_interrupt=True)
    assert result["applied"] and not result["pending"]
    assert (
        host.calls.index("config-check")
        < host.calls.index("begin")
        < host.calls.index(("systemctl", "restart", node.SERVICE))
    )
    assert (host.run / "config.before.json").read_bytes() == original
    assert (host.run / "sing-box.before").read_bytes() == b"synthetic binary"
    for name in (
        "plan.json",
        "config.json",
        "started.json",
        "result.json",
        "config.before.json",
        "sing-box.before",
    ):
        assert (host.run / name).stat().st_mode & 0o777 == 0o600
    assert b"password_hash" not in (host.run / "plan.json").read_bytes()
    with pytest.raises(node.NodeRejected, match="outcome_requires_inspection"):
        node.apply(host.runtime, host.run, acknowledge_interrupt=True)


@pytest.mark.parametrize(
    "change",
    [
        "config",
        "binary",
        "runtime",
        "unit",
        "candidate",
        "revision",
        "boolean_revision",
        "context",
        "membership",
    ],
)
def test_plan_changes_stop_before_application(host, change):
    node.plan(host.runtime, host.run)
    files = {
        "config": host.proxy,
        "binary": host.binary,
        "runtime": host.runtime.require_config_path(),
        "unit": node.UNIT,
        "candidate": host.run / "config.json",
    }
    if change in files:
        with files[change].open("ab") as handle:
            handle.write(b"\n")
    elif change == "revision":
        host.snapshot["revision"] += 1
    elif change == "boolean_revision":
        selected = json.loads((host.run / "plan.json").read_bytes())
        selected["revision"] = True
        (host.run / "plan.json").write_bytes(canonical(selected) + b"\n")
    elif change == "context":
        host.snapshot["context"] = "0" * 64
    else:
        host.snapshot["access"]["trojan"][0]["password"] = "synthetic-changed-value"
    with pytest.raises(node.NodeRejected):
        node.apply(host.runtime, host.run, acknowledge_interrupt=True)
    assert "begin" not in host.calls
    assert not (host.run / "started.json").exists()


def test_failed_restart_never_restores_old_credentials(host):
    node.plan(host.runtime, host.run)
    host.state["restart_failure"] = True
    with pytest.raises(node.NodeRejected, match="failed_or_uncertain"):
        node.apply(host.runtime, host.run, acknowledge_interrupt=True)
    assert host.state["failed"] and host.state["pending"]
    assert host.proxy.read_bytes() == (host.run / "config.json").read_bytes()
    assert (host.run / "started.json").exists()
    assert not (host.run / "result.json").exists()
    with pytest.raises(node.NodeRejected, match="outcome_requires_inspection"):
        node.apply(host.runtime, host.run, acknowledge_interrupt=True)


def test_interruption_requires_explicit_acknowledgement(host):
    with pytest.raises(node.NodeRejected, match="acknowledgement_required"):
        node.apply(host.runtime, host.run, acknowledge_interrupt=False)
    with pytest.raises(node.NodeRejected, match="acknowledgement_required"):
        node.fence(host.runtime, acknowledge_interrupt=False)
    assert host.calls == []


def test_partial_backups_are_not_overwritten(host):
    node.plan(host.runtime, host.run)
    (host.run / "config.before.json").write_bytes(b"synthetic retained partial")
    with pytest.raises(FileExistsError):
        node.apply(host.runtime, host.run, acknowledge_interrupt=True)
    assert (
        host.run / "config.before.json"
    ).read_bytes() == b"synthetic retained partial"
    assert "begin" not in host.calls


@pytest.mark.parametrize("legacy_code", [0, 1, 3, 4])
def test_fence_requires_verified_stopped_legacy_service(host, monkeypatch, legacy_code):
    storage.FENCE.rename(host.root / "retained-fence.json")
    monkeypatch.setattr(node.os, "fchown", lambda *args: None)
    monkeypatch.setattr(node.os, "chown", lambda *args: None)
    monkeypatch.setattr(
        node.pwd, "getpwnam", lambda name: SimpleNamespace(pw_gid=os.getgid())
    )
    host.state["legacy_code"] = legacy_code
    if legacy_code == 3:
        assert node.fence(host.runtime, acknowledge_interrupt=True)["fenced"]
        storage.require_fence(UUID(host.snapshot["identity"]))
    else:
        with pytest.raises(node.NodeRejected, match="stop_not_verified"):
            node.fence(host.runtime, acknowledge_interrupt=True)
        assert storage.legacy_blocked()
        with pytest.raises(ValueError, match="host_ownership_fence_required"):
            storage.require_fence(UUID(host.snapshot["identity"]))


def test_process_capture_is_private_and_bounded(monkeypatch):
    result = node._run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys;sys.stderr.write('synthetic private error');sys.stdout.write(sys.stdin.read())",
        ],
        payload=b"synthetic message",
    )
    assert (
        result.returncode == 0
        and result.stdout == b"synthetic message"
        and result.stderr == b""
    )
    monkeypatch.setattr(node, "OUTPUT_LIMIT", 256)
    with pytest.raises(node.NodeRejected, match="output_limit"):
        node._run([sys.executable, "-I", "-c", "print('x'*1024)"])
    with pytest.raises(node.NodeRejected, match="private_request_too_large"):
        node._run(["must-not-run"], payload=b"x" * 4097)


def test_process_timeout_stops_descendants_even_if_parent_exited(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys;from pathlib import Path;p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']);Path("
        + repr(str(pid_file))
        + ").write_text(str(p.pid))"
    )
    with pytest.raises(node.NodeRejected, match="node_command_timeout"):
        node._run([sys.executable, "-I", "-c", script], timeout=0.5)
    assert pid_file.exists()
    state = Path("/proc") / pid_file.read_text() / "stat"
    for _ in range(100):
        try:
            if state.read_text().split()[2] == "Z":
                break
        except FileNotFoundError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("The synthetic child did not stop")


def test_supervisor_requires_root_before_file_or_process_operations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(node.os, "geteuid", lambda: 1000)
    with pytest.raises(node.NodeRejected, match="root_required"):
        node.plan(None, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()
