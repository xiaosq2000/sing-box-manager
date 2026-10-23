"""Check G0 safety and fixture construction without starting a VM or service."""

import json
import subprocess
import sys
from pathlib import Path

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
