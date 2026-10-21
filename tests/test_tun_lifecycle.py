"""Check lifecycle guards and proof logic without services, sudo or a VM."""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "scripts/dev/tun-vm"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("action", ["bootstrap", "network", "run"])
@pytest.mark.parametrize("optimize", [False, True])
def test_lifecycle_entry_refuses_operator_host(action: str, optimize: bool) -> None:
    command = [
        sys.executable,
        *(["-O"] if optimize else []),
        str(DIRECTORY / "lifecycle.py"),
        action,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert (
        "outside the disposable TUN guest" in result.stderr
        or "optimization disables acceptance assertions" in result.stderr
    )


@pytest.mark.parametrize("identity", ["", "a" * 31, "A" * 32, "../" + "a" * 32])
def test_guard_requires_exact_run_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    module = load("lifecycle")
    (tmp_path / "run-id").write_text(identity)
    monkeypatch.setattr(module, "ASSETS", tmp_path)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "tun-fixture")
    observed = []
    monkeypatch.setattr(module, "run", lambda *args: observed.append(args))
    with pytest.raises(RuntimeError, match="Invalid disposable guest identity"):
        module.guard()
    assert observed == [("systemd-detect-virt", "--vm", "--quiet")]


@pytest.mark.parametrize(
    ("text", "boot", "boots", "expected"),
    [
        ("TUN_E2E_RESULT=abc:PASS", 1, 1, "PASS"),
        ("[ 12] python[3]: TUN_E2E_RESULT=abc:PASS\n", 4, 4, "PASS"),
        ("TUN_E2E_RESULT=abc:PASS", 1, 4, "FAIL"),
        ("TUN_E2E_RESULT=abc:PASS\nTUN_E2E_RESULT=abc:FAIL", 4, 4, "FAIL"),
        ("TUN_E2E_RESULT=other:PASS", 4, 4, "FAIL"),
        ("TUN_E2E_RESULT=abc:PASS extra", 4, 4, "FAIL"),
        ("TUN_LIFECYCLE_REBOOT=abc:committed", 1, 4, "REBOOT"),
        ("TUN_LIFECYCLE_REBOOT=abc:pending", 2, 4, "REBOOT"),
        ("TUN_LIFECYCLE_REBOOT=abc:dirty", 3, 4, "REBOOT"),
        ("TUN_LIFECYCLE_REBOOT=abc:dirty", 1, 4, "FAIL"),
        ("TUN_LIFECYCLE_REBOOT=other:pending", 2, 4, "FAIL"),
        ("TUN_LIFECYCLE_REBOOT=abc:pending\nTUN_E2E_RESULT=abc:FAIL", 2, 4, "FAIL"),
        ("TUN_LIFECYCLE_REBOOT=abc:dirty", 4, 4, "FAIL"),
        ("", 1, 4, "FAIL"),
    ],
)
def test_boot_marker_requires_current_identity_and_phase(
    text: str, boot: int, boots: int, expected: str
) -> None:
    assert load("lifecycle-host").boot_result(text, "abc", boot, boots) == expected


def connection() -> dict:
    return {
        "id": "fresh",
        "metadata": {
            "sourceIP": "10.77.0.1",
            "sourcePort": "12345",
            "destinationIP": "198.18.0.20",
            "destinationPort": "443",
            "host": "gfw.fixture.test",
            "network": "tcp",
            "type": "tun/tun-in",
        },
        "chains": ["naive", "proxy"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sourceIP", "10.77.0.2"),
        ("sourcePort", "12346"),
        ("destinationIP", "198.18.0.10"),
        ("destinationPort", "8443"),
        ("host", "other.fixture.test"),
        ("network", "udp"),
        ("type", "mixed/tun-in"),
    ],
)
def test_socket_proof_rejects_wrong_metadata(field: str, value: str) -> None:
    match = load("lifecycle").matches_socket
    item = connection()
    arguments = (
        set(),
        (("10.77.0.1", 12345), ("198.18.0.20", 443)),
        "gfw.fixture.test",
        "proxy-alt",
    )
    assert match(item, *arguments)
    item["metadata"][field] = value
    assert not match(item, *arguments)


@pytest.mark.parametrize(
    "chains", [["direct"], ["trojan", "proxy"], ["naive", "proxy", "direct"], []]
)
def test_socket_proof_rejects_wrong_chain_and_replayed_id(chains: list[str]) -> None:
    match = load("lifecycle").matches_socket
    item = connection()
    arguments = (
        (("10.77.0.1", 12345), ("198.18.0.20", 443)),
        "gfw.fixture.test",
        "proxy-alt",
    )
    assert not match(item, {"fresh"}, *arguments)
    item["chains"] = chains
    assert not match(item, set(), *arguments)


def test_archive_requires_pinned_digest_and_real_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("lifecycle-host")
    path = tmp_path / "engine.tar.gz"
    files = {"sing-box": b"fixture executable", "libcronet.so": b"fixture library"}
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo("engine/" + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="digest mismatch"):
        module.checked_archive(path, "1.14.2")
    monkeypatch.setitem(
        module.ENGINES, "1.14.2", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    receipt = module.checked_archive(path, "1.14.2")
    assert receipt["files"] == {
        k: hashlib.sha256(v).hexdigest() for k, v in files.items()
    }


def test_archive_refuses_symlink_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load("lifecycle-host")
    path = tmp_path / "engine.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("engine/sing-box")
        member.type = tarfile.SYMTYPE
        member.linkname = "/bin/true"
        archive.addfile(member)
    monkeypatch.setitem(
        module.ENGINES, "1.14.2", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError, match="Invalid engine archive member"):
        module.checked_archive(path, "1.14.2")


@pytest.mark.parametrize("mismatch", ["run", "boot", "transaction", "files"])
def test_resume_rejects_changed_identity_before_mutation(
    monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    module = load("lifecycle")
    state = {
        "run_id": "run",
        "boot_id": "before",
        "transaction": "known",
        "files": {},
        "phase": "committed",
    }
    monkeypatch.setattr(
        module, "guard", lambda: "other" if mismatch == "run" else "run"
    )
    monkeypatch.setattr(
        module, "boot_id", lambda: "before" if mismatch == "boot" else "after"
    )
    monkeypatch.setattr(
        module,
        "read",
        lambda _: {"id": "other" if mismatch == "transaction" else "known"},
    )
    monkeypatch.setattr(
        module,
        "fingerprint",
        lambda: {"changed": "digest"} if mismatch == "files" else {},
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Identity failure must not start or repair capture")

    for name in ["sbc", "root", "run", "prepare_pending", "repair_after_boot"]:
        monkeypatch.setattr(module, name, forbidden)
    with pytest.raises(AssertionError):
        module.resumed(state)


def test_checkpoint_round_trip(tmp_path: Path) -> None:
    save = load("lifecycle").save
    path = tmp_path / "state.json"
    save(path, {"phase": "pending", "transaction": "known"})
    assert json.loads(path.read_text()) == {"phase": "pending", "transaction": "known"}
    assert not path.with_suffix(".next").exists()
