"""Structured models for the release pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleasePlan:
    """The resolved release plan shown before packaging starts."""

    version: str
    release_dir: Path
    config_path: Path
    auth_snapshot_path: Path
    default_protocol: str
    enabled_portal_users: int
    enabled_release_users: int
    enabled_trojan_users: int
    enabled_hysteria2_users: int
    enabled_naive_users: int
    platforms: tuple[str, ...]
    protocols: tuple[str, ...]
    expected_user_archives: int
    expected_server_archives: int = 1

    @property
    def total_expected_archives(self) -> int:
        """Total distributable archives expected from the run."""
        return self.expected_user_archives + self.expected_server_archives


@dataclass(frozen=True)
class ReleaseResult:
    """The final outputs produced by the release pipeline.

    Archives are described rather than written: each one is a shared prefix in
    the store plus a few KB of per-user config, assembled on demand. So there
    are counts and names here, not paths to multi-megabyte files.
    """

    release_dir: Path
    auth_snapshot_path: Path
    release_id: str
    archive_names: tuple[str, ...]
    server_package_dir: Path
    prefixes_built: int
    prefixes_reused: int
    reused_existing_release: bool
    elapsed_seconds: float

    @property
    def user_archive_count(self) -> int:
        """Number of user archives this release can serve."""
        return len(self.archive_names)

    @property
    def prefix_count(self) -> int:
        """Number of shared prefixes backing those archives."""
        return self.prefixes_built + self.prefixes_reused
