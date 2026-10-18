"""Build inputs and outputs for shared, account-independent artifacts."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleasePlan:
    version: str
    release_dir: Path
    config_path: Path
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseResult:
    release_dir: Path
    release_id: str
    server_package_dir: Path
    reused_existing_release: bool
    elapsed_seconds: float
