"""Safely publish a staged runtime config on a deployed server."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path
from typing import cast

import yaml


def _load_mapping(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Runtime config must be a mapping: {path}")
    return cast("dict[str, object]", data)


# Deploy generates these on the server when the inventory has none, and
# replacing one would sign users out or revoke their subscription links.
PRESERVED_WEB_SECRETS = ("session_secret", "subscription_secret")


def _web_secret(config: dict[str, object], name: str) -> str | None:
    web = config.get("web")
    if not isinstance(web, dict):
        return None

    web_data = cast("dict[str, object]", web)
    secret = web_data.get(name)
    if not isinstance(secret, str) or not secret.strip():
        return None
    return secret


def publish_runtime_config(
    staged_path: Path,
    target_path: Path,
    owner_reference_path: Path,
) -> None:
    """Validate and atomically publish ``staged_path`` as ``target_path``.

    A deploy may generate secrets that exist only on the server. Preserve them
    across later deploys so browser sessions, installer machine tokens and
    subscription links remain valid.
    """
    incoming = _load_mapping(staged_path)

    reference_path = owner_reference_path
    if target_path.is_file():
        current = _load_mapping(target_path)
        for name in PRESERVED_WEB_SECRETS:
            existing_secret = _web_secret(current, name)
            if existing_secret is None:
                continue
            incoming_web = incoming.get("web")
            if not isinstance(incoming_web, dict):
                raise ValueError("Runtime config web setting must be a mapping")
            cast("dict[str, object]", incoming_web)[name] = existing_secret
        reference_path = target_path

    reference_stat = reference_path.stat()
    staged_path.write_text(
        yaml.dump(incoming, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    # A new file gets group read so the service account can load it.
    mode = stat.S_IMODE(reference_stat.st_mode) if target_path.exists() else 0o640
    staged_path.chmod(mode)
    os.chown(staged_path, reference_stat.st_uid, reference_stat.st_gid)
    os.replace(staged_path, target_path)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("staged_path", type=Path)
    parser.add_argument("target_path", type=Path)
    parser.add_argument("owner_reference_path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        publish_runtime_config(
            args.staged_path,
            args.target_path,
            args.owner_reference_path,
        )
    finally:
        args.staged_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
