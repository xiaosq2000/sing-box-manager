"""The native CI harness must fail closed outside CI and avoid SSH transport."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def harness():
    path = (
        Path(__file__).resolve().parents[1] / "scripts/dev/test-control-plane-native.py"
    )
    spec = importlib.util.spec_from_file_location("native_foundation_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_harness_refuses_non_ci_execution(harness, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("SBM_CP_NATIVE_E2E", raising=False)
    with pytest.raises(RuntimeError, match="disposable GitHub runner"):
        harness.main()


def test_native_transport_uses_root_shell_for_input_redirection(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(
        harness.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )
    harness.command("cat < /private/fixture.sql")
    assert calls == [
        (["sudo", "bash", "-eu", "-c", "cat < /private/fixture.sql"], {"check": True})
    ]


def test_native_harness_keeps_all_host_commands_local(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SBM_CP_NATIVE_E2E", "1")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(harness, "ROOT", tmp_path / "native")
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/fake/pixi")
    commands = []
    monkeypatch.setattr(harness, "command", commands.append)
    arguments = []

    def run(args, **_kwargs):
        arguments.append(args)
        assert args[0] != "ssh"
        status = 1 if "test" in args or "is-active" in args else 0
        return subprocess.CompletedProcess(args, status)

    def forbid_process(*_args, **_kwargs):
        pytest.fail("The native harness attempted an unmocked process")

    monkeypatch.setattr(harness.subprocess, "run", run)
    monkeypatch.setattr(harness.subprocess, "Popen", forbid_process)
    monkeypatch.setattr(
        harness.subprocess, "check_output", lambda *_args, **_kwargs: "1\n"
    )
    harness.main()
    assert any("--no-hard-links" in args for args in arguments)
    assert sum("control-plane --action health" in value for value in commands) == 2
    assert any("systemctl disable --now" in value for value in commands)
    assert any("postgresql-client" in value for value in commands)
