"""Check CI parallelism, cache boundaries and accelerator selection without a VM."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

NODE_AGENT_ORACLE_STEP = {
    "name": "Check shared Python oracles",
    "run": (
        "pixi run -e node-g0 test tests/test_node_g2_counter_parity.py "
        "tests/test_node_accounting_calendar.py "
        "tests/test_node_traffic_contract.py"
    ),
}
NODE_AGENT_GO_STEP = {
    "name": "Check the separate cgo-free agent module",
    "run": "go -C node-agent test -count=1 ./...\ngo -C node-agent vet ./...\n",
    "shell": "pixi run -e node-g0 bash -e {0}",
}
NODE_AGENT_COUNTER_EXPORT_STEP = {
    "name": "Check real-PostgreSQL counter export composition",
    "run": "pixi run -e node-g0 test tests/test_node_counter_export.py",
    "env": {"SBM_REQUIRE_POSTGRES": "1", "SBM_REQUIRE_NODE_G0": "1"},
}


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


def assert_node_agent_shared_check_contract(
    config: dict, required_paths: set[str]
) -> None:
    assert set(config) == {"name", "on", "permissions", "concurrency", "jobs"}
    assert "env" not in config
    assert "defaults" not in config
    for event in ("push", "pull_request"):
        assert required_paths <= set(config["on"][event]["paths"])
    assert config["permissions"] == {"contents": "read"}
    job = config["jobs"]["node-g0"]
    assert set(job) == {"runs-on", "timeout-minutes", "steps"}
    for key in ("if", "continue-on-error", "permissions", "env", "defaults"):
        assert key not in job
    assert job["runs-on"] == "ubuntu-latest"
    oracle_step = next(
        step
        for step in job["steps"]
        if step.get("name") == NODE_AGENT_ORACLE_STEP["name"]
    )
    assert oracle_step == NODE_AGENT_ORACLE_STEP
    module_step = next(
        step for step in job["steps"] if step.get("name") == NODE_AGENT_GO_STEP["name"]
    )
    assert module_step == NODE_AGENT_GO_STEP
    export_step = next(
        step
        for step in job["steps"]
        if step.get("name") == NODE_AGENT_COUNTER_EXPORT_STEP["name"]
    )
    assert export_step == NODE_AGENT_COUNTER_EXPORT_STEP
    assert "PYTEST_ADDOPTS" not in export_step.get("env", {})
    assert "--ignore" not in export_step["run"]
    assert "--deselect" not in export_step["run"]
    steps = job["steps"]
    assert steps.index(module_step) < steps.index(export_step)


def test_node_agent_parity_paths_trigger_the_separate_go_module_checks() -> None:
    config = workflow("node-agent.yml")
    parity_paths = {
        "tests/fixtures/node-g2-counter-parity.json",
        "tests/test_node_g2_counter_parity.py",
        "tests/fixtures/node-accounting-calendar.json",
        "tests/test_node_accounting_calendar.py",
    }
    assert_node_agent_shared_check_contract(config, parity_paths)


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/node-traffic-v1.json",
        "tests/test_node_traffic_contract.py",
        "sing_box_manager/control_plane/traffic_contract.py",
    ],
)
def test_node_traffic_codec_paths_trigger_both_language_checks(path: str) -> None:
    config = workflow("node-agent.yml")
    required_paths = {
        path,
        "tests/fixtures/node-g2-counter-parity.json",
        "tests/test_node_g2_counter_parity.py",
        "tests/fixtures/node-accounting-calendar.json",
        "tests/test_node_accounting_calendar.py",
    }
    assert_node_agent_shared_check_contract(config, required_paths)


def test_counter_export_paths_trigger_the_real_postgres_composition() -> None:
    config = workflow("node-agent.yml")
    required_paths = {
        "tests/test_node_counter_export.py",
        "tests/node_counter_export/**",
        "tests/test_control_plane_integration.py",
        "sing_box_manager/control_plane/traffic_ingest.py",
        "sing_box_manager/control_plane/models.py",
        "sing_box_manager/control_plane/migrations/**",
        "sing_box_manager/control_plane/database.py",
        "sing_box_manager/control_plane/django_settings.py",
        "sing_box_manager/control_plane/runner.py",
        "sing_box_manager/cli.py",
        "sing_box_manager/__main__.py",
        "node-agent/**",
    }
    assert_node_agent_shared_check_contract(config, required_paths)


def test_node_agent_workflow_guard_rejects_counter_export_step_skipping() -> None:
    config = workflow("node-agent.yml")
    assert_node_agent_shared_check_contract(config, {"node-agent/**"})
    export_step = next(
        step
        for step in config["jobs"]["node-g0"]["steps"]
        if step.get("name") == NODE_AGENT_COUNTER_EXPORT_STEP["name"]
    )
    export_step["env"] = {
        "SBM_REQUIRE_POSTGRES": "1",
        "SBM_REQUIRE_NODE_G0": "1",
        "PYTEST_ADDOPTS": "--ignore=tests/test_node_counter_export.py",
    }
    with pytest.raises(AssertionError):
        assert_node_agent_shared_check_contract(config, {"node-agent/**"})


def test_node_agent_workflow_guard_rejects_counter_export_before_go_module_check() -> None:
    config = workflow("node-agent.yml")
    assert_node_agent_shared_check_contract(config, {"node-agent/**"})
    steps = config["jobs"]["node-g0"]["steps"]
    export_step = next(
        step
        for step in steps
        if step.get("name") == NODE_AGENT_COUNTER_EXPORT_STEP["name"]
    )
    steps.remove(export_step)
    module_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == NODE_AGENT_GO_STEP["name"]
    )
    steps.insert(module_index, export_step)
    with pytest.raises(AssertionError):
        assert_node_agent_shared_check_contract(config, {"node-agent/**"})


def test_node_agent_workflow_guard_rejects_job_continue_on_error() -> None:
    # Mutate only the loaded YAML object; the workflow file is never written.
    config = workflow("node-agent.yml")
    assert_node_agent_shared_check_contract(config, {"node-agent/**"})
    config["jobs"]["node-g0"]["continue-on-error"] = "true"
    with pytest.raises(AssertionError):
        assert_node_agent_shared_check_contract(config, {"node-agent/**"})


def test_node_agent_workflow_guard_rejects_oracle_pytest_addopts() -> None:
    # Mutate only the loaded YAML object; the workflow file is never written.
    config = workflow("node-agent.yml")
    assert_node_agent_shared_check_contract(config, {"node-agent/**"})
    oracle_step = next(
        step
        for step in config["jobs"]["node-g0"]["steps"]
        if step.get("name") == NODE_AGENT_ORACLE_STEP["name"]
    )
    oracle_step["env"] = {
        "PYTEST_ADDOPTS": "--ignore=tests/test_node_traffic_contract.py"
    }
    with pytest.raises(AssertionError):
        assert_node_agent_shared_check_contract(config, {"node-agent/**"})


def test_node_agent_workflow_guard_rejects_workflow_env_suppression() -> None:
    # Mutate only the loaded YAML object; the workflow file is never written.
    config = workflow("node-agent.yml")
    assert_node_agent_shared_check_contract(config, {"node-agent/**"})
    config["env"] = {"PYTEST_ADDOPTS": "--ignore=tests/test_node_traffic_contract.py"}
    with pytest.raises(AssertionError):
        assert_node_agent_shared_check_contract(config, {"node-agent/**"})


def test_control_plane_job_runs_registry_and_ingestion_schema_with_postgres() -> None:
    config = workflow("ci.yml")
    job = config["jobs"]["control-plane"]
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Check PostgreSQL portal and private HTTP lifecycle"
    )
    assert step["run"] == (
        "pixi run -e control-plane-test test tests/test_control_plane*.py "
        "tests/test_routes.py tests/test_deploy.py tests/test_provision.py "
        "tests/test_remote_config.py"
    )
    assert step["env"] == {"SBM_REQUIRE_POSTGRES": "1"}
    assert (ROOT / "tests" / "test_control_plane_node_registry.py").is_file()
    assert (ROOT / "tests" / "test_control_plane_traffic_ingest.py").is_file()
    assert "tests/test_control_plane*.py" in step["run"]
    assert "--ignore" not in step["run"]
    assert "--deselect" not in step["run"]
    assert "PYTEST_ADDOPTS" not in step["env"]
    for key in ("if", "continue-on-error"):
        assert key not in step
    for key in ("env", "if", "continue-on-error", "defaults"):
        assert key not in job
