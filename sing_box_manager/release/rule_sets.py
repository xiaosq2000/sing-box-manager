"""Prepare the initial rule files shipped with desktop clients."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

RULE_FILES_MANIFEST = "files.txt"


def snapshot_filename(rule_set: dict) -> str:
    """Use the URL as the identity, independently of a rule's display tag."""
    url = rule_set.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Remote rule-set requires a URL")
    if rule_set.get("format", "binary") != "binary":
        raise ValueError("Bundled desktop rule-sets must use binary format")
    return hashlib.sha256(url.encode()).hexdigest() + ".srs"


def desktop_rule_sets(configs: Iterable[dict]) -> dict[str, dict]:
    """Collect the unique remote files needed by the rendered desktop modes."""
    rules: dict[str, dict] = {}
    for config in configs:
        for rule_set in config.get("route", {}).get("rule_set", []):
            if rule_set.get("type") == "remote":
                rules[snapshot_filename(rule_set)] = rule_set
    return dict(sorted(rules.items()))


def _github_source(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    parts = parsed.path.lstrip("/").split("/", 3)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "raw.githubusercontent.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 4
        or any(not part or part in {".", ".."} for part in parsed.path.split("/")[1:])
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:3])
    ):
        raise ValueError("Bundled rule sources must be GitHub Raw HTTPS URLs")
    owner, repository, revision, path = parts
    return f"{owner}/{repository}", revision, path


def _validate_snapshot(binary: Path, path: Path) -> None:
    # `check` loads local rule-sets, but not a remote rule's initial_path.
    # Decompilation cannot validate binary AdGuard rules, which are valid SRS
    # files that cannot be represented as source JSON.
    output = path.with_suffix(".check.json")
    try:
        output.write_text(
            json.dumps(
                {
                    "route": {
                        "rule_set": [
                            {
                                "type": "local",
                                "tag": "validate",
                                "format": "binary",
                                "path": str(path.resolve()),
                            }
                        ],
                        "rules": [{"rule_set": "validate", "action": "reject"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(binary), "check", "-c", str(output)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"Invalid bundled rule-set: {path.name}")
    finally:
        output.unlink(missing_ok=True)


def prepare_rule_snapshots(
    rules: dict[str, dict],
    destination: Path,
    binary: Path,
    github_token: str | None = None,
) -> list[Path]:
    """Fetch one consistent revision per source and validate before packaging.

    Every build resolves current source revisions. There is no stale-cache
    fallback; a failed download must not publish an incomplete new release.
    Authentication is sent only to the GitHub API, never to raw file URLs.
    """
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / RULE_FILES_MANIFEST
    manifest.unlink(missing_ok=True)
    revisions: dict[tuple[str, str], str] = {}
    paths: list[Path] = []
    headers = {"User-Agent": "sing-box-manager"}
    api_headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        for filename, rule_set in sorted(rules.items()):
            if filename != snapshot_filename(rule_set):
                raise ValueError("Invalid rule snapshot filename")
            repository, revision, source_path = _github_source(rule_set["url"])
            source = (repository, revision)
            if source not in revisions:
                response = client.get(
                    f"https://api.github.com/repos/{repository}/commits/{quote(revision, safe='')}",
                    headers=api_headers,
                )
                response.raise_for_status()
                commit = response.json().get("sha")
                if not isinstance(commit, str) or not re.fullmatch(
                    r"[0-9a-f]{40}", commit
                ):
                    raise ValueError("Invalid rule-source commit returned by GitHub")
                revisions[source] = commit
            response = client.get(
                f"https://raw.githubusercontent.com/{repository}/{revisions[source]}/{source_path}"
            )
            response.raise_for_status()
            path = destination / filename
            path.write_bytes(response.content)
            _validate_snapshot(binary, path)
            paths.append(path)
    manifest.write_text("".join(f"{path.name}\n" for path in paths), encoding="ascii")
    return [*paths, manifest]
