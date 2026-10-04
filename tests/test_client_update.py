"""Interactive-shell update notice behavior for the Unix client helper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


def _require_bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    return bash


def _seed_client(
    tmp_path: Path, *, policy: str, unreachable: bool = False
) -> tuple[dict[str, str], Path]:
    data_root = tmp_path / "data" / "sing-box"
    state_root = tmp_path / "state" / "sing-box"
    fake_bin = tmp_path / "bin"
    data_root.mkdir(parents=True)
    state_root.mkdir(parents=True)
    fake_bin.mkdir()
    (data_root / "client-version").write_text(
        "schema=1\n"
        f"client_build_id={'a' * 64}\n"
        "commit_sha=11111111\n"
        "dirty=false\n"
        "upstream_version=1.0.0\n"
        "portal_base_url=https://vpn.example.com\n",
        encoding="utf-8",
    )
    (data_root / "portal-base-url").write_text(
        "https://vpn.example.com\n", encoding="utf-8"
    )
    count_file = tmp_path / "curl-count"
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f"count_file={count_file!s}\n"
        "count=0\n"
        "[ ! -f \"$count_file\" ] || read -r count <\"$count_file\"\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" >\"$count_file\"\n"
        + ("exit 7\n" if unreachable else "")
        + "printf '%s\\n' "
        "'schema=1' "
        f"'client_build_id={'b' * 64}' "
        "'commit_sha=22222222' "
        "'dirty=false' "
        "'deployed_at=2026-08-24T09:30:00Z' "
        f"'policy={policy}' "
        f"'notice_id={'c' * 64}' "
        "'message=Routing format changed'\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return env, count_file


def _source(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_require_bash(), "--noprofile", "--norc", "-ic", f'source "{SETUP_SCRIPT}"'],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_suggested_update_is_checked_daily_and_shown_once(tmp_path: Path) -> None:
    env, count_file = _seed_client(tmp_path, policy="suggested")

    first = _source(env)
    second = _source(env)

    assert "client update is highly suggested" in first.stdout
    # The why belongs to `proxy check update`, not to every new shell.
    assert "Routing format changed" not in first.stdout
    assert "client update is highly suggested" not in second.stdout
    assert count_file.read_text(encoding="utf-8").strip() == "1"


def test_required_update_repeats_from_cache_on_every_shell(tmp_path: Path) -> None:
    env, count_file = _seed_client(tmp_path, policy="required")

    first = _source(env)
    second = _source(env)

    assert "client upgrade is required" in first.stdout
    assert "client upgrade is required" in second.stdout
    assert "Routing format changed" not in first.stdout
    assert count_file.read_text(encoding="utf-8").strip() == "1"


def test_unreachable_portal_is_still_throttled_to_one_check(tmp_path: Path) -> None:
    """A portal the client cannot reach must not re-probe on every new shell."""
    env, count_file = _seed_client(tmp_path, policy="suggested", unreachable=True)

    first = _source(env)
    second = _source(env)

    assert "client update is highly suggested" not in first.stdout
    assert "client update is highly suggested" not in second.stdout
    assert count_file.read_text(encoding="utf-8").strip() == "1"


def test_proxy_check_update_reports_both_versions(tmp_path: Path) -> None:
    env, _ = _seed_client(tmp_path, policy="suggested")
    result = subprocess.run(
        [
            _require_bash(),
            "--noprofile",
            "--norc",
            "-ic",
            f'source "{SETUP_SCRIPT}" >/dev/null; proxy check update',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "11111111" in result.stdout
    assert "22222222" in result.stdout
    assert "suggested" in result.stdout
    assert "Routing format changed" in result.stdout
