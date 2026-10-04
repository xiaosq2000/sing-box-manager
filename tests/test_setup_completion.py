"""Tests for setup.sh shell completion registration."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


def _require_shell(name: str) -> str:
    shell_path = shutil.which(name)
    if shell_path is None:
        pytest.skip(f"{name} is not available")

    return shell_path


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_require_shell("bash"), "--noprofile", "--norc", "-ic", script],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_zsh(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_require_shell("zsh"), "-fic", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_bash_completion_registers_immediately_and_schedules_retry() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            if [[ "$(complete -p proxy 2>/dev/null || true)" == *"_proxy_completion_bash"* ]]; then
                printf 'complete\n'
            fi
            case ";${{PROMPT_COMMAND:-}};" in
                *";_proxy_register_completion_prompt_bash;"*)
                    printf 'hook\n'
                    ;;
            esac
            """
        ).strip(),
    )

    assert result.stdout.splitlines() == ["complete", "hook"]


def test_bash_prompt_retry_restores_completion_and_clears_itself() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _dummy_complete() {{ :; }}
            complete -F _dummy_complete proxy
            _proxy_register_completion_prompt_bash
            if [[ "$(complete -p proxy 2>/dev/null || true)" == *"_proxy_completion_bash"* ]]; then
                printf 'restored\n'
            fi
            case ";${{PROMPT_COMMAND:-}};" in
                *";_proxy_register_completion_prompt_bash;"*)
                    printf 'hook-present\n'
                    ;;
                *)
                    printf 'hook-cleared\n'
                    ;;
            esac
            """
        ).strip(),
    )

    assert result.stdout.splitlines() == ["restored", "hook-cleared"]


def test_zsh_completion_registers_immediately_and_schedules_retry() -> None:
    result = _run_zsh(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            if [[ "${{_comps[proxy]-}}" == "_proxy_completion_zsh" ]]; then
                print -r -- complete
            fi
            hook_state=missing
            for hook_name in "${{precmd_functions[@]:-}}"; do
                if [[ "$hook_name" == "_proxy_register_completion_prompt_zsh" ]]; then
                    hook_state=hook
                    break
                fi
            done
            print -r -- "$hook_state"
            """
        ).strip(),
    )

    assert result.stdout.splitlines() == ["complete", "hook"]


def test_zsh_prompt_retry_restores_completion_and_clears_itself() -> None:
    result = _run_zsh(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            typeset -gA _comps
            _comps[proxy]=_dummy_complete
            _proxy_register_completion_prompt_zsh
            if [[ "${{_comps[proxy]-}}" == "_proxy_completion_zsh" ]]; then
                print -r -- restored
            fi
            hook_state=missing
            for hook_name in "${{precmd_functions[@]:-}}"; do
                if [[ "$hook_name" == "_proxy_register_completion_prompt_zsh" ]]; then
                    hook_state=hook
                    break
                fi
            done
            print -r -- "$hook_state"
            """
        ).strip(),
    )

    assert result.stdout.splitlines() == ["restored", "missing"]


def test_check_completion_includes_quota() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_complete_candidates check
            """
        ).strip()
    )

    candidates = result.stdout.splitlines()
    assert "quota" in candidates
    assert "vps" not in candidates
    assert "usage" not in candidates


@pytest.mark.parametrize("command", ["protocol", "version"])
def test_top_level_completion_includes_command(command: str) -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_complete_candidates
            """
        ).strip()
    )

    assert command in result.stdout.splitlines()


def test_version_completion_has_no_arguments() -> None:
    result = _run_bash(
        f'source "{SETUP_SCRIPT}" >/dev/null 2>&1; _proxy_complete_candidates version'
    )
    assert result.stdout == ""


def test_top_level_completion_includes_port() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_complete_candidates
            """
        ).strip()
    )

    assert "port" in result.stdout.splitlines()


def test_protocol_completion_includes_supported_protocols() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_complete_candidates protocol
            """
        ).strip()
    )

    assert result.stdout.splitlines() == ["trojan", "hysteria2", "naive"]


def test_route_completion_includes_supported_strategies() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_complete_candidates route
            """
        ).strip()
    )

    assert result.stdout.splitlines() == ["china", "gfw", "ai", "global"]


def test_tun_completion_is_offered_on_linux() -> None:
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_is_linux() {{ return 0; }}
            _proxy_complete_candidates
            printf -- '---\n'
            _proxy_complete_candidates tun
            printf -- '---\n'
            _proxy_complete_candidates tun on
            printf -- '---\n'
            _proxy_complete_candidates check
            """
        ).strip()
    )

    top, tun, tun_on, check = (
        section.split() for section in result.stdout.split("---")
    )
    assert "tun" in top
    assert tun == ["on", "off"]
    assert tun_on == ["--persist"]
    assert "tun" in check


def test_tun_completion_is_not_advertised_on_macos() -> None:
    """A Linux-only command must not show up in macOS completion."""
    result = _run_bash(
        textwrap.dedent(
            f"""
            source "{SETUP_SCRIPT}" >/dev/null 2>&1
            _proxy_is_linux() {{ return 1; }}
            _proxy_complete_candidates
            printf -- '---\n'
            _proxy_complete_candidates tun
            printf -- '---\n'
            _proxy_complete_candidates check
            """
        ).strip()
    )

    top, tun, check = (section.split() for section in result.stdout.split("---"))
    assert "tun" not in top
    assert tun == []
    assert "tun" not in check


def test_uninstall_completion() -> None:
    result = _run_bash(f'''
        source "{SETUP_SCRIPT}" >/dev/null 2>&1
        _proxy_complete_candidates | command grep -x uninstall
        _proxy_complete_candidates uninstall
        _proxy_complete_candidates uninstall --shell
    ''')
    assert result.stdout.splitlines() == [
        "uninstall",
        "-y",
        "--yes",
        "--shell",
        "--no-rc",
        "--preserve-tun",
        "-h",
        "--help",
        "bash",
        "zsh",
    ]
