"""Public-source safeguards must also reject accidentally force-added files."""

from __future__ import annotations

import importlib.util
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "public_tree", ROOT / "scripts/dev/check-public-tree.py"
)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.backup",
        "nested/.env.local",
        ".sops.yaml",
        "config/inventory/runtime.yaml",
        "config/inventory/runtime.sops.yaml",
        "config/inventory/operator/users.yaml",
        "elsewhere/inventory.sops.yaml",
        "config/generated/auth-users.json",
        "releases/server/config.json",
        "data/traffic-stats.sqlite3",
        "traffic.sqlite3-wal",
        "private.pem",
        "private.key",
        "certificate.p12",
        "certificate.pfx",
        "state.db",
        ".pixi/envs/default/bin/python",
        ".cache/output.json",
        ".worktrees/private/README.md",
        "tests/__pycache__/test.pyc",
    ],
)
def test_policy_rejects_operational_files(path: str) -> None:
    assert policy.forbidden_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "config/inventory/example.yaml",
        "config/rules/ai-services.json",
        ".sops.example.yaml",
        ".env.example",
        "nested/.env.example",
        "tests/fixtures/profiles/server-stats-true.json",
        "tests/fixtures/traffic-v2.sql",
        "internal/trust/trust.go",
        "sing_box_manager/web/static/fonts/OFL.txt",
    ],
)
def test_policy_allows_public_source(path: str) -> None:
    assert not policy.forbidden_path(path)


def test_fixture_allowlists_are_rule_scoped_and_value_specific() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"]["useDefault"] is True
    for allowlist in config["allowlists"]:
        assert allowlist["targetRules"] == ["generic-api-key"]
        assert allowlist["condition"] == "AND"
        assert allowlist["paths"] and allowlist["regexes"]
        assert allowlist["regexTarget"] == "secret"
        assert all(
            regex.startswith("^") and regex.endswith("$")
            for regex in allowlist["regexes"]
        )


def test_public_index_contains_no_operational_files() -> None:
    assert not [path for path in policy.tracked_paths() if policy.forbidden_path(path)]


@pytest.mark.parametrize(
    "path",
    [
        ".env.backup",
        ".sops.yaml",
        "config/inventory/runtime.yaml",
        "config/inventory/runtime.sops.yaml",
        "config/generated/auth-users.json",
        "releases/server/config.json",
        "data/traffic-stats.sqlite3",
        "private.pem",
        "state.db",
    ],
)
def test_operational_files_are_gitignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", path], cwd=ROOT, capture_output=True
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    "path", ["config/inventory/example.yaml", ".sops.example.yaml", ".env.example"]
)
def test_public_examples_are_not_gitignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", path], cwd=ROOT, capture_output=True
    )
    assert result.returncode == 1


def test_policy_checks_deleted_operational_files_in_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Fixture"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "fixture@example.com"],
        check=True,
    )
    path = tmp_path / "runtime.sops.yaml"
    path.write_text("synthetic encrypted fixture\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "runtime.sops.yaml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Fixture",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "rm", "-q", "runtime.sops.yaml"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Remove fixture",
        ],
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert "runtime.sops.yaml" in policy.history_paths()
    assert policy.forbidden_path("runtime.sops.yaml")
