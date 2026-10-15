"""Export selected stored source bytes. Never inspect counters or running processes."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml
from yaml.resolver import BaseResolver

from sing_box_manager.account_copy.files import publish_private, read_source
from sing_box_manager.account_copy.schema import (
    POLICY,
    PURPOSE,
    AccountSnapshot,
    copy_context_identity,
)
from sing_box_manager.control_plane.import_files import decode_json
from sing_box_manager.control_plane.import_schema import ImportRejected, canonical
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import AuthSnapshot, Settings, resolve_settings_path


class _UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ImportRejected("source_rejected")
        return super().compose_node(parent, index)


def _mapping(loader, node):
    result = {}
    for key, value in node.value:
        if key.tag == "tag:yaml.org,2002:merge":
            raise ImportRejected("source_rejected")
        name = loader.construct_object(key)
        if name in result:
            raise ImportRejected("source_rejected")
        result[name] = loader.construct_object(value)
    return result


_UniqueLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _parse_source(content: bytes, path: Path) -> Settings:
    try:
        loader = _UniqueLoader(content)
        try:
            value = loader.get_single_data()
        finally:
            loader.dispose()
        return Settings.from_dict(value, config_path=path)
    except Exception:
        raise ImportRejected("source_rejected") from None


def load_source_settings(path: Path) -> Settings:
    """Read bounded, protected plaintext before the first configuration parse."""
    resolved = resolve_settings_path(path)
    if resolved.name.endswith((".sops.yaml", ".sops.yml")):
        raise ImportRejected("plaintext_source_required")
    return _parse_source(read_source(resolved), resolved)


def _selected(originals: dict, runtime: Settings) -> Settings:
    selected = _parse_source(originals["runtime"], runtime.require_config_path())
    if selected.model_dump() != runtime.model_dump():
        raise ImportRejected("source_changed")
    copy_context_identity(selected)
    auth = AuthSnapshot.model_validate(decode_json(originals["auth"]))
    if {row.username: row.model_dump() for row in auth.users} != {
        row.username: row.model_dump() for row in selected.web_portal.users
    }:
        raise ImportRejected("source_auth_mismatch")
    if canonical(decode_json(originals["proxy"])) != canonical(
        build_server_config(selected, selected.traffic_stats)
    ):
        raise ImportRejected("source_proxy_mismatch")
    return selected


def export_snapshot(  # noqa: PLR0913 -- explicit source paths and operator-issued identities
    runtime: Settings,
    output: Path,
    auth_path: Path,
    proxy_path: Path,
    *,
    namespace: UUID,
    source_identity: UUID,
    sequence: int,
) -> dict:
    config_path = runtime.require_config_path()
    if config_path.name.endswith((".sops.yaml", ".sops.yml")):
        raise ImportRejected("plaintext_source_required")
    paths = {"runtime": config_path, "auth": auth_path, "proxy": proxy_path}
    originals = {name: read_source(path) for name, path in paths.items()}
    try:
        selected = _selected(originals, runtime)
        snapshot = AccountSnapshot.model_validate(
            {
                "version": 1,
                "purpose": PURPOSE,
                "link_policy": POLICY,
                "namespace": str(namespace),
                "source_identity": str(source_identity),
                "sequence": sequence,
                "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "context_identity": copy_context_identity(selected),
                "source_files": {
                    name: hashlib.sha256(raw).hexdigest()
                    for name, raw in originals.items()
                },
                "legacy_counters": "not-imported",
                "identity_history": "not-established",
                "access": {
                    kind: [
                        row.model_dump(mode="json")
                        for row in getattr(
                            selected,
                            "web_portal" if kind == "portal" else kind,
                        ).users
                    ]
                    for kind in ("portal", "trojan", "hysteria2", "naive")
                },
            }
        )
    except ImportRejected:
        raise
    except Exception:
        raise ImportRejected("source_rejected") from None
    if any(read_source(paths[name]) != content for name, content in originals.items()):
        raise ImportRejected("source_changed")
    publish_private(output, canonical(snapshot.model_dump(mode="json")) + b"\n")
    return {"exported": True, "subjects": len(snapshot.access.memberships())}
