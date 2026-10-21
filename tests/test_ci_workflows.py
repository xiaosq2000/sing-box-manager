"""Check CI parallelism, cache boundaries and accelerator selection without a VM."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(),
        Loader=yaml.BaseLoader,  # noqa: S506 -- BaseLoader constructs strings only.
    )


def accelerator_module():
    spec = importlib.util.spec_from_file_location(
        "select_accel", ROOT / "scripts/dev/tun-vm/select-accel.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tun_suites_run_independently_without_skips_or_failure_retries() -> None:
    config = workflow("sbc.yml")
    job = config["jobs"]["e2e-tun"]
    assert job["strategy"] == {
        "fail-fast": "false",
        "matrix": {"suite": ["core", "lifecycle"]},
    }
    assert "needs" not in job
    assert "SBC_TUN_E2E_ACCEL" not in job.get("env", {})
    steps = job["steps"]
    selection = next(
        step for step in steps if step.get("name") == "Select the VM accelerator"
    )
    assert "scripts/dev/tun-vm/select-accel.py" in selection["run"]
    assert 'echo "SBC_TUN_E2E_ACCEL=$accel" >> "$GITHUB_ENV"' in selection["run"]
    assert "$GITHUB_STEP_SUMMARY" in selection["run"]
    acceptance = [step for step in steps if "test-tun-vm.sh" in step.get("run", "")]
    assert len(acceptance) == 1
    assert acceptance[0]["run"] == (
        'pixi run bash scripts/dev/test-tun-vm.sh "${{ matrix.suite }}"'
    )
    assert "if" not in acceptance[0]
    assert "continue-on-error" not in acceptance[0]
    assert steps.index(selection) < steps.index(acceptance[0])
    for event in ("push", "pull_request"):
        assert "scripts/dev/tun-vm/**" in config["on"][event]["paths"]


@pytest.mark.parametrize(
    ("name", "job_id"), [("ci.yml", "test"), ("sbc.yml", "e2e-tun")]
)
def test_build_caches_restore_for_prs_but_save_only_after_success_on_main(
    name: str, job_id: str
) -> None:
    steps = workflow(name)["jobs"][job_id]["steps"]
    restore = next(step for step in steps if step.get("id") == "build-cache")
    save = steps[-1]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert "if" not in restore
    assert save["uses"].startswith("actions/cache/save@")
    assert save["if"] == (
        "github.event_name == 'push' && github.ref_name == 'main' "
        "&& steps.build-cache.outputs.cache-hit != 'true'"
    )
    assert save["with"]["key"] == "${{ steps.build-cache.outputs.cache-primary-key }}"
    assert restore["with"]["path"] == save["with"]["path"]
    paths = set(restore["with"]["path"].splitlines())
    expected = {"~/.cache/go-build", "~/go/pkg/mod"}
    if job_id == "test":
        assert {"run": "pixi run go test -count=1 ./..."} in steps
        expected.add(".cache/sing-box-manager/server-binary")
        assert "sing_box_manager/traffic_stats.py" in restore["with"]["key"]
        assert "config/inventory/example.yaml" in restore["with"]["key"]
    assert paths == expected
    assert "hashFiles('pixi.lock')" in restore["with"]["key"]
    assert "hashFiles('pixi.lock')" in restore["with"]["restore-keys"]


def qmp_response(*, enabled: bool = True, identity: str = "kvm-probe") -> str:
    return json.dumps({"return": {"enabled": enabled, "present": True}, "id": identity})


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, qmp_response(), "kvm"),
        (1, qmp_response(), "tcg"),
        (0, qmp_response(enabled=False), "tcg"),
        (0, qmp_response(identity="other"), "tcg"),
        (0, "", "tcg"),
        (0, 'not JSON\n[]\n{"error": {}}', "tcg"),
        (0, '{"QMP": {}}\n' + qmp_response() + '\n{"event": "SHUTDOWN"}', "kvm"),
    ],
)
def test_accelerator_requires_successful_kvm_startup_and_qmp_proof(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
    output: str,
    expected: str,
) -> None:
    module = accelerator_module()

    def run(command, **kwargs):
        assert command == [
            "qemu-system-x86_64",
            "-machine",
            "accel=kvm",
            "-cpu",
            "max",
            "-m",
            "2048",
            "-smp",
            "2",
            "-nodefaults",
            "-display",
            "none",
            "-S",
            "-qmp",
            "stdio",
        ]
        assert kwargs["timeout"] == 15
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert [json.loads(line) for line in kwargs["input"].splitlines()] == [
            {"execute": "qmp_capabilities"},
            {"execute": "query-kvm", "id": "kvm-probe"},
            {"execute": "quit"},
        ]
        return subprocess.CompletedProcess(command, returncode, output, "probe details")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.select_accelerator() == expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Selecting {expected.upper()}" in captured.err


def test_accelerator_probe_timeout_selects_tcg(monkeypatch: pytest.MonkeyPatch) -> None:
    module = accelerator_module()

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.select_accelerator() == "tcg"


def test_missing_qemu_is_a_setup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = accelerator_module()

    def run(*args, **kwargs):
        raise FileNotFoundError("qemu-system-x86_64")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(FileNotFoundError):
        module.select_accelerator()
