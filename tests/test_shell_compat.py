"""Bash 3.2 compatibility and translation guards for shared server scripts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH_SCRIPTS = (
    "scripts/lib/ui.sh",
    "scripts/server-install.sh",
    "scripts/server-uninstall.sh",
)
TRANSLATED_SCRIPTS = BASH_SCRIPTS[1:]

BASH_4_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    (r"\btypeset\s+-A\b", "associative arrays (typeset -A)"),
    (r"\bdeclare\s+-A\b", "associative arrays (declare -A)"),
    (r"\bmapfile\b", "mapfile"),
    (r"\breadarray\b", "readarray"),
    (
        r"\$\{[A-Za-z_][A-Za-z_0-9]*(\[[^\]]*\])?(,,?|\^\^?)\}",
        "case-modifying expansion",
    ),
    (r"&>>", "&>> append redirection"),
    (r"\bcoproc\b", "coproc"),
    (r"\b(local|declare)\s+-n\b", "namerefs"),
    (r"\bwait\s+-n\b", "wait -n"),
)


def _code_lines(relative_path: str) -> list[tuple[int, str]]:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("relative_path", BASH_SCRIPTS)
@pytest.mark.parametrize(("pattern", "description"), BASH_4_CONSTRUCTS)
def test_no_bash_4_constructs(relative_path: str, pattern: str, description: str):
    compiled = re.compile(pattern)
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if compiled.search(line)
    ]
    assert not offenders, f"{description} is not available in bash 3.2:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize("relative_path", BASH_SCRIPTS)
def test_no_dead_translations_table(relative_path: str):
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if "TRANSLATIONS" in line
    ]
    assert not offenders, (
        "_t uses _translate_zh, not a TRANSLATIONS array:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("relative_path", TRANSLATED_SCRIPTS)
def test_translated_scripts_define_the_hook(relative_path: str):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "_translate_zh() {" in text, (
        f"{relative_path} needs _translate_zh for Chinese messages"
    )


def test_ui_library_defines_a_default_hook():
    text = (ROOT / "scripts/lib/ui.sh").read_text(encoding="utf-8")
    assert "_translate_zh() {" in text
