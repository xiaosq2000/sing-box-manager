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
PRESERVED_SECRETS = [("web", name) for name in PRESERVED_WEB_SECRETS] + [
    ("control_plane", "secret_key"),
    ("control_plane", "subscription_secret"),
]


def _secret(config: dict[str, object], section: str, name: str) -> str | None:
    web = config.get(section)
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
    *,
    managed: bool = False,
) -> None:
    """Validate and atomically publish ``staged_path`` as ``target_path``.

    A deploy may generate secrets that exist only on the server. Preserve them
    across later deploys so browser sessions, installer machine tokens and
    subscription links remain valid.
    """
    from sing_box_manager.managed_access.storage import legacy_blocked

    if legacy_blocked() != managed:
        raise ValueError("Runtime publication mode does not match host ownership")
    if managed:
        from sing_box_manager.account_copy.export import load_source_settings

        incoming = load_source_settings(staged_path).model_dump(
            mode="json", by_alias=True
        )
        current = load_source_settings(target_path).model_dump(
            mode="json", by_alias=True
        )
        _retain_managed_state(incoming, current)
    else:
        incoming = _load_mapping(staged_path)
        current = _load_mapping(target_path) if target_path.is_file() else {}

    reference_path = owner_reference_path
    if target_path.is_file():
        for section, name in PRESERVED_SECRETS:
            existing_secret = _secret(current, section, name)
            if existing_secret is None:
                continue
            incoming_web = incoming.get(section)
            if section == "control_plane" and incoming_web is None:
                incoming_web = incoming[section] = {"enabled": False}
            if not isinstance(incoming_web, dict):
                raise ValueError(f"Runtime config {section} setting must be a mapping")
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


def _retain_managed_state(incoming: dict, current: dict) -> None:
    from sing_box_manager.settings import Settings

    before, after = Settings.from_dict(current), Settings.from_dict(incoming)
    if not after.control_plane.enabled or not after.control_plane.portal_enabled:
        raise ValueError("Managed runtime must retain both Django services")
    for name in ("database_name", "database_socket_dir", "database_port"):
        if getattr(before.control_plane, name) != getattr(after.control_plane, name):
            raise ValueError("Managed deployment cannot change the database binding")
    if (
        before.deployment.host != after.deployment.host
        or before.control_plane.portal_port != after.control_plane.portal_port
    ):
        raise ValueError(
            "Public host and listener changes require a separate routing migration"
        )
    # These retained rosters are recovery/collector inputs, never live authority.
    # Do not replace them with inventory values or export new database passwords.
    for name in ("web_portal", "trojan", "hysteria2", "naive"):
        incoming[name]["users"] = current[name]["users"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("staged_path", type=Path)
    parser.add_argument("target_path", type=Path)
    parser.add_argument("owner_reference_path", type=Path)
    parser.add_argument("--managed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        publish_runtime_config(
            args.staged_path,
            args.target_path,
            args.owner_reference_path,
            managed=args.managed,
        )
    finally:
        if not args.managed:
            args.staged_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
