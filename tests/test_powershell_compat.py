"""Guards that keep the Windows client installer runnable on stock Windows PowerShell.

Every Windows 10/11 machine ships Windows PowerShell 5.1 and nothing newer, so
5.1 is the floor for the hosted install script (install.ps1).

This file checks constraints that PSScriptAnalyzer or real-host tests may miss:
5.1 semantics that parse fine under 7 ($IsWindows), basic parsing for Web requests,
function naming conventions, UTF-8 BOM rules, and absence of exit in in-memory
script blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Scripts that get installed onto, or streamed to, a client machine.
CLIENT_FACING_SCRIPTS = (
    "sing_box_manager/web/client/install.ps1",
)

# Each of these is invoked from text through the call operator somewhere in the
# supported install flow. `exit` would terminate its parent script or the user's
# interactive PowerShell instance instead of returning to the caller.
IN_MEMORY_ENTRY_POINTS = (
    "sing_box_manager/web/client/install.ps1",
)

POWERSHELL_7_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    (r"\?\?=?[\s(]", "null-coalescing (?? / ??=), PowerShell 7.0+"),
    (
        r"(?<![&|])(&&|\|\|)(?![&|])",
        "pipeline chain operators (&& / ||), PowerShell 7.0+",
    ),
    # The single most common 5.1 breakage: these are simply undefined there, so
    # `if ($IsWindows)` silently evaluates false instead of failing loudly.
    (r"\$Is(Windows|Linux|MacOS)\b", "$IsWindows/$IsLinux/$IsMacOS, undefined on 5.1"),
    (
        r"ConvertFrom-Json[^\r\n]*-AsHashtable",
        "ConvertFrom-Json -AsHashtable, PowerShell 6+",
    ),
    (r"\bForEach-Object\b[^\r\n]*-Parallel", "ForEach-Object -Parallel, PowerShell 7+"),
    (r"\$PSStyle\b", "$PSStyle, PowerShell 7.2+"),
    (
        r"-(SkipHttpErrorCheck|AllowUnencryptedAuthentication|Authentication)\b",
        "Invoke-* parameters added in PowerShell 6/7",
    ),
    (r"Split-Path[^\r\n]*-LeafBase", "Split-Path -LeafBase, PowerShell 6+"),
    (r"\b(Join-String|Get-Error|Test-Json)\b", "cmdlets added in PowerShell 6/7"),
    (
        r"ConvertFrom-SecureString[^\r\n]*-AsPlainText",
        "ConvertFrom-SecureString -AsPlainText, PowerShell 7+",
    ),
    (r"`u\{", "Unicode escape sequence, PowerShell 6+"),
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
@pytest.mark.parametrize(("pattern", "description"), POWERSHELL_7_CONSTRUCTS)
def test_no_powershell_7_constructs(relative_path: str, pattern: str, description: str):
    compiled = re.compile(pattern)
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if compiled.search(line)
    ]
    assert not offenders, (
        f"{description} is not available in Windows PowerShell 5.1:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("relative_path", CLIENT_FACING_SCRIPTS)
def test_web_requests_use_basic_parsing(relative_path: str):
    """Without -UseBasicParsing, 5.1 throws on hosts with no Internet Explorer engine."""
    lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
    offenders = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") or "Invoke-WebRequest" not in line:
            continue
        if "-UseBasicParsing" in line:
            continue
        # Splatted calls carry the switch in the hashtable instead, so look back
        # for the assignment rather than demanding it on the call line.
        if re.search(r"Invoke-WebRequest\s+@\w+", line):
            window = "\n".join(lines[max(0, index - 25) : index])
            if re.search(r"UseBasicParsing\s*=\s*\$true", window):
                continue
        offenders.append(f"{relative_path}:{index + 1}: {line.strip()}")

    assert not offenders, (
        "Invoke-WebRequest needs -UseBasicParsing on 5.1, which otherwise relies on "
        "the Internet Explorer engine and fails on hardened or Core installs:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("relative_path", CLIENT_FACING_SCRIPTS)
def test_add_type_calls_are_guarded(relative_path: str):
    """Add-Type throws when the type already exists in the session."""
    lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
    offenders = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") or "Add-Type" not in line:
            continue
        if "-AssemblyName" in line:
            continue
        window = "\n".join(lines[max(0, index - 3) : index])
        if "-as [type]" not in window:
            offenders.append(f"{relative_path}:{index + 1}: {line.strip()}")
    assert not offenders, (
        "Add-Type throws if the type is already loaded, so each call needs an "
        "`-as [type]` existence guard within the preceding 3 lines:\n"
        + "\n".join(offenders)
    )


def test_bootstrapper_defines_translation_catalog():
    text = (ROOT / "sing_box_manager" / "web" / "client" / "install.ps1").read_text(
        encoding="utf-8"
    )
    assert "function Get-SbcText" in text
    assert "'Subscription link' = '订阅链接'" in text


@pytest.mark.parametrize("relative_path", CLIENT_FACING_SCRIPTS)
def test_function_names_follow_the_client_convention(relative_path: str):
    """Sbc prefix keeps function names consistent and avoids conflicts."""
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    allowed = re.compile(r"^[A-Z][a-zA-Z]+-Sbc[A-Za-z0-9]*$")
    offenders = [
        name
        for name in re.findall(r"^\s*function\s+(\S+)", text, flags=re.MULTILINE)
        if not allowed.match(name)
    ]
    assert not offenders, (
        f"{relative_path}: every function must be named "
        f"<ApprovedVerb>-Sbc<Noun>: {offenders}"
    )


@pytest.mark.parametrize("relative_path", CLIENT_FACING_SCRIPTS)
def test_encoding_matches_the_byte_order_mark(relative_path: str):
    """5.1 decodes a BOM-less .ps1 as ANSI, so non-ASCII without a BOM is mojibake."""
    payload = (ROOT / relative_path).read_bytes()
    has_bom = payload.startswith(b"\xef\xbb\xbf")
    body = payload[3:] if has_bom else payload
    is_ascii = body.decode("utf-8").isascii()

    if is_ascii:
        assert not has_bom, (
            f"{relative_path} is pure ASCII and must not carry a UTF-8 BOM"
        )
    else:
        assert has_bom, (
            f"{relative_path} contains non-ASCII characters, so it needs a UTF-8 BOM; "
            "Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI and would mangle them"
        )


@pytest.mark.parametrize("relative_path", IN_MEMORY_ENTRY_POINTS)
def test_in_memory_entry_points_never_exit(relative_path: str):
    offenders = [
        f"{relative_path}:{number}: {line.strip()}"
        for number, line in _code_lines(relative_path)
        if re.match(r"\s*exit(?:\s|$)", line)
    ]
    assert not offenders, (
        "in-memory entry points must return or throw; `exit` terminates the parent "
        "installer or the user's PowerShell instance:\n" + "\n".join(offenders)
    )
