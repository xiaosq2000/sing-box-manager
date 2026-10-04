"""Reclaim space from releases and store entries nothing points at any more.

The content-addressed store is what makes a rebuild cheap, and it is append-only
during a build precisely so a crash can never corrupt it. The cost of that is
that nothing ever deletes: without this, every upstream bump leaves its ~190 MB
of prefix blobs behind forever, on the build host and on the VPS.

This is deliberately a separate verb rather than an rsync ``--delete``. The store
is shared across releases, so "not in the newest release" and "unreferenced" are
different questions, and only the second one is safe to act on. Answering it
means reading the manifest of *every* release being kept -- which is also why a
manifest that cannot be parsed aborts the whole run: a parse failure would make
its payloads look unreferenced and delete bytes that are still in use.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sing_box_manager.release import artifacts
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    RELEASE_DIR_PREFIX,
    Manifest,
    ManifestError,
)
from sing_box_manager.release.store import STORE_DIR_NAME, Store, prefix_id_of
from sing_box_manager.settings import Settings


class GarbageCollectionError(RuntimeError):
    """Raised when the store cannot be collected safely."""


@dataclass(frozen=True, slots=True)
class GcResult:
    """What a collection kept and what it reclaimed."""

    kept_releases: tuple[str, ...] = ()
    removed_releases: tuple[str, ...] = ()
    removed_store_entries: tuple[str, ...] = ()
    removed_upstream: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    dry_run: bool = False
    notes: tuple[str, ...] = ()


def collect_garbage(
    settings: Settings, *, keep: int = 3, dry_run: bool = False
) -> GcResult:
    """Keep the newest ``keep`` releases plus the active one, drop the rest."""
    if keep < 1:
        raise GarbageCollectionError("--keep must be at least 1.")

    releases_root = _resolve_local_path(settings.releases_root)
    if not releases_root.is_dir():
        return GcResult(dry_run=dry_run, notes=(f"No releases at {releases_root}.",))

    candidates = _release_dirs(releases_root)
    retained, removable = _partition(
        candidates, keep=keep, active=_active_name(settings)
    )

    manifests = _read_manifests(retained)
    referenced_prefixes = {
        prefix.prefix_id for manifest in manifests for prefix in manifest.prefixes
    }
    referenced_payloads = {
        digest for manifest in manifests for digest in manifest.payload_digests
    }
    referenced_versions = {manifest.upstream_version for manifest in manifests}

    reclaimed = sum(_tree_size(path) for path in removable)
    store = Store(releases_root / STORE_DIR_NAME)

    if dry_run:
        removed_entries = _unreferenced_store_entries(store, referenced_prefixes)
    else:
        removed_entries = [
            path.name for path in store.gc(referenced_prefixes, referenced_payloads)
        ]

    removed_upstream: list[str] = []
    if not dry_run:
        removed_upstream = [
            f"{path.parent.name}/{path.name}"
            for path in store.prune_upstream(referenced_versions)
        ]
        for path in removable:
            shutil.rmtree(path)

    return GcResult(
        kept_releases=tuple(path.name for path in retained),
        removed_releases=tuple(path.name for path in removable),
        removed_store_entries=tuple(sorted(removed_entries)),
        removed_upstream=tuple(sorted(removed_upstream)),
        reclaimed_bytes=reclaimed,
        dry_run=dry_run,
    )


def _resolve_local_path(path: Path) -> Path:
    return path if path.is_absolute() else artifacts.PROJECT_ROOT / path


def _active_name(settings: Settings) -> str | None:
    """The release the portal is serving right now, which is never collectable."""
    info_path = artifacts.resolve_release_info_path(settings)
    if not info_path.is_file():
        return None
    try:
        return artifacts.read_release_info(info_path).release_dir_name
    except (OSError, ValueError):
        return None


def _release_dirs(releases_root: Path) -> list[Path]:
    """Published releases, newest first."""
    return sorted(
        (
            path
            for path in releases_root.glob(f"{RELEASE_DIR_PREFIX}*")
            if path.is_dir() and (path / MANIFEST_FILENAME).is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _partition(
    candidates: list[Path], *, keep: int, active: str | None
) -> tuple[list[Path], list[Path]]:
    """Keep the newest ``keep``, and the active release however old it is."""
    retained = list(candidates[:keep])
    active_dir = next((path for path in candidates if path.name == active), None)
    if active_dir is not None and active_dir not in retained:
        retained.append(active_dir)

    removable = [path for path in candidates if path not in retained]
    return retained, removable


def _read_manifests(release_dirs: list[Path]) -> list[Manifest]:
    manifests: list[Manifest] = []
    for path in release_dirs:
        manifest_path = path / MANIFEST_FILENAME
        try:
            manifests.append(
                Manifest.from_json(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, ManifestError, ValueError) as exc:
            # Refusing here is the point: a manifest we cannot read is a set of
            # references we cannot see, and deleting on that basis is how a
            # content-addressed store loses bytes it still needs.
            raise GarbageCollectionError(
                f"Cannot read {manifest_path}; refusing to collect. "
                "Remove that release directory by hand if it is truly dead."
            ) from exc
    return manifests


def _unreferenced_store_entries(store: Store, referenced: set[str]) -> list[str]:
    """What `Store.gc` would delete, without deleting it."""
    if not store.prefix_dir.is_dir():
        return []
    return [
        path.name
        for path in sorted(store.prefix_dir.iterdir())
        if path.is_file() and prefix_id_of(path.name) not in referenced
    ]


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
