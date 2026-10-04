"""Reject operational files in the public index and reachable Git history."""

# ruff: noqa: INP001 -- this is a standalone development script, not a package

from __future__ import annotations

import argparse
import subprocess
from pathlib import PurePosixPath

PUBLIC_INVENTORY = "config/inventory/example.yaml"
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3")
PRIVATE_DIRECTORIES = {
    ".cache",
    ".pixi",
    ".worktrees",
    "__pycache__",
}


def forbidden_path(path: str) -> bool:
    """Identify forbidden paths without reading or printing their contents."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1].lower()
    return any(
        (
            name.startswith(".env") and name != ".env.example",
            name == ".sops.yaml" or name.endswith(".sops.yaml"),
            name.endswith(PRIVATE_SUFFIXES) or ".sqlite3-" in name,
            any(part in PRIVATE_DIRECTORIES for part in parts),
            parts[0] in {"data", "releases"},
            parts[:2] == ("config", "generated"),
            parts[:2] == ("config", "inventory") and path != PUBLIC_INVENTORY,
        )
    )


def tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    )
    return set(result.stdout.split("\0")) - {""}


def history_paths() -> set[str]:
    result = subprocess.run(
        ["git", "log", "--all", "--format=", "--name-only", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {path.lstrip("\n") for path in result.stdout.split("\0") if path.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history", action="store_true", help="Also check all Git refs"
    )
    args = parser.parse_args()
    paths = tracked_paths()
    if args.history:
        paths.update(history_paths())
    forbidden = sorted(path for path in paths if forbidden_path(path))
    if forbidden:
        print("Operational files are forbidden in public source control:")
        for path in forbidden:
            print(f"  {path}")
        return 1
    print(f"Public-file policy passed ({len(paths)} paths checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
