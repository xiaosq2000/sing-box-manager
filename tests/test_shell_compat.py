"""Guards that keep client-facing shell scripts runnable on stock macOS bash.

macOS ships bash 3.2 as /bin/bash. Anything that lands on a client machine has to
run there, so the bash 4 constructs below stay banned. `_t` in `lib/ui.sh` resolves
translations through a `_translate_zh` hook rather than an associative array for the
same reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Scripts that get installed onto, or streamed to, a client machine.
CLIENT_FACING_SCRIPTS = (
    "scripts/client-install.sh",
    "scripts/client-uninstall.sh",
    "scripts/setup.sh",
    "scripts/lib/ui.sh",
    "scripts/lib/platform.sh",
    "scripts/lib/system-proxy.sh",
    "scripts/lib/tun.sh",
)

# Every script that sources lib/ui.sh and therefore depends on the _t contract.
UI_LIBRARY_CONSUMERS = CLIENT_FACING_SCRIPTS + (
    "scripts/server-install.sh",
    "scripts/server-uninstall.sh",
)

# Scripts carrying their own domain translations.
TRANSLATED_SCRIPTS = (
    "scripts/client-install.sh",
    "scripts/client-uninstall.sh",
    "scripts/setup.sh",
    "scripts/server-install.sh",
    "scripts/server-uninstall.sh",
)

# setup.sh and the libraries it sources are sourced into the user's interactive
# shell, which is often zsh. zsh either makes these read-only or ties them to
# shell state, so `local <name>` there aborts the function or silently rewires
# the shell for the length of the call.
ZSH_RESERVED_NAMES = (
    "status",
    "path",
    "cdpath",
    "fpath",
    "manpath",
    "mailpath",
    "module_path",
    "argv",
    "options",
    "signals",
    "histchars",
    "prompt",
    "psvar",
    "watch",
    "commands",
    "functions",
    "aliases",
    "parameters",
    "dirstack",
    "jobstates",
    "jobtexts",
    "jobdirs",
    "historywords",
    "reply",
    "funcstack",
    "zsh_eval_context",
    "EUID",
    "UID",
    "GID",
    "EGID",
    "PPID",
    "SECONDS",
    "RANDOM",
    "LINENO",
    "IFS",
    "PATH",
    "HOME",
    "PWD",
    "OLDPWD",
    "COLUMNS",
    "LINES",
)

# The scripts a user sources into their own interactive shell.
SOURCED_INTO_USER_SHELL = (
    "scripts/setup.sh",
    "scripts/lib/ui.sh",
    "scripts/lib/platform.sh",
    "scripts/lib/system-proxy.sh",
    "scripts/lib/tun.sh",
)

LOCAL_DECLARATION = re.compile(r"^\s*(?:local|declare|typeset)\s+((?:-[A-Za-z]+\s+)*)(.*)$")

BASH_4_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    (r"\btypeset\s+-A\b", "associative arrays (typeset -A)"),
    (r"\bdeclare\s+-A\b", "associative arrays (declare -A)"),
    (r"\bmapfile\b", "mapfile"),
    (r"\breadarray\b", "readarray"),
    (r"\$\{[A-Za-z_][A-Za-z_0-9]*(\[[^\]]*\])?(,,?|\^\^?)\}", "case-modifying expansion"),
    (r"&>>", "&>> append redirection"),
    (r"\bcoproc\b", "coproc"),
    (r"\b(local|declare)\s+-n\b", "namerefs"),
    (r"\bwait\s+-n\b", "wait -n"),
)


def _code_lines(relative_path: str) -> list[tuple[int, str]]:
    """Return numbered lines with whole-line comments removed."""
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("relative_path", CLIENT_FACING_SCRIPTS)
@pytest.mark.parametrize(("pattern", "description"), BASH_4_CONSTRUCTS)
def test_no_bash_4_constructs(relative_path: str, pattern: str, description: str):
    compiled = re.compile(pattern)
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if compiled.search(line)
    ]
    assert not offenders, (
        f"{description} is not available in bash 3.2 (stock macOS /bin/bash):\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("relative_path", UI_LIBRARY_CONSUMERS)
def test_no_dead_translations_table(relative_path: str):
    """A TRANSLATIONS array would be silently ignored by the current _t."""
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if "TRANSLATIONS" in line
    ]
    assert not offenders, (
        "_t resolves translations through _translate_zh; a TRANSLATIONS array is "
        "dead code and its entries would never be shown:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("relative_path", TRANSLATED_SCRIPTS)
def test_translated_scripts_define_the_hook(relative_path: str):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "_translate_zh() {" in text, (
        f"{relative_path} has user-facing strings but no _translate_zh override, "
        "so zh_CN output would silently fall back to English"
    )


def test_ui_library_defines_a_default_hook():
    text = (ROOT / "scripts" / "lib" / "ui.sh").read_text(encoding="utf-8")
    assert "_translate_zh() {" in text, (
        "lib/ui.sh must define a default _translate_zh so _t works for scripts "
        "that supply no translations"
    )


@pytest.mark.parametrize("relative_path", SOURCED_INTO_USER_SHELL)
def test_no_zsh_reserved_locals(relative_path: str):
    """`local status=...` aborts the function in zsh; `local path=...` eats PATH."""
    offenders = []
    for number, line in _code_lines(relative_path):
        match = LOCAL_DECLARATION.match(line)
        if match is None:
            continue
        for word in match.group(2).split():
            name = word.split("=", 1)[0]
            if name in ZSH_RESERVED_NAMES:
                offenders.append(f"{relative_path}:{number}: {name}")

    assert not offenders, (
        "these names are read-only or tied to shell state in zsh, and this file "
        "is sourced into the user's interactive shell:\n" + "\n".join(offenders)
    )
