"""Helpers for resolving user-facing release artifacts."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from sing_box_manager.release.digest_cache import is_sha256_hex
from sing_box_manager.release.platforms import PLATFORMS
from sing_box_manager.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORM_PACKAGE_EXTENSIONS = {
    platform.name: platform.package_ext for platform in PLATFORMS
}
RELEASE_INFO_FILENAME = "release-info.json"
DEPLOYMENT_INFO_FILENAME = "deployment-info.json"


@dataclass(frozen=True)
class ReleaseInfo:
    """Persisted metadata for locating the active release directory."""

    release_dir_name: str
    commit_sha: str
    dirty: bool
    date: str
    upstream_version: str
    deployment_host: str | None = None
    # The content hash the release directory is named after. The commit sha
    # beside it is provenance only -- what built this -- and no longer decides
    # what may be deployed.
    release_id: str | None = None
    client_build_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "release_dir_name": self.release_dir_name,
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "date": self.date,
            "upstream_version": self.upstream_version,
        }

        if self.deployment_host is not None:
            payload["deployment_host"] = self.deployment_host
        if self.release_id is not None:
            payload["release_id"] = self.release_id
        if self.client_build_id is not None:
            payload["client_build_id"] = self.client_build_id

        return payload

    @classmethod
    def from_dict(cls, value: object) -> ReleaseInfo:
        if not isinstance(value, dict):
            raise ValueError("release info must be a mapping")

        payload = cast("dict[str, object]", value)

        release_dir_name = payload.get("release_dir_name")
        commit_sha = payload.get("commit_sha")
        dirty = payload.get("dirty")
        date = payload.get("date")
        upstream_version = payload.get("upstream_version")
        deployment_host = payload.get("deployment_host")
        release_id = payload.get("release_id")
        client_build_id = payload.get("client_build_id")

        if not isinstance(release_dir_name, str) or not release_dir_name:
            raise ValueError("release_dir_name must be a non-empty string")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("commit_sha must be a non-empty string")
        if not isinstance(dirty, bool):
            raise ValueError("dirty must be a boolean")
        if not isinstance(date, str) or not date:
            raise ValueError("date must be a non-empty string")
        if not isinstance(upstream_version, str) or not upstream_version:
            raise ValueError("upstream_version must be a non-empty string")
        if deployment_host is not None and (
            not isinstance(deployment_host, str) or not deployment_host
        ):
            raise ValueError("deployment_host must be a non-empty string")
        if release_id is not None and (
            not isinstance(release_id, str) or not release_id
        ):
            raise ValueError("release_id must be a non-empty string")
        if client_build_id is not None and (
            not isinstance(client_build_id, str) or not is_sha256_hex(client_build_id)
        ):
            raise ValueError("client_build_id must be a sha256 digest")

        return cls(
            release_dir_name=release_dir_name,
            commit_sha=commit_sha,
            dirty=dirty,
            date=date,
            upstream_version=upstream_version,
            deployment_host=deployment_host,
            release_id=release_id,
            client_build_id=client_build_id,
        )


@dataclass(frozen=True)
class DeploymentInfo:
    """Metadata published only after a remote deployment succeeds."""

    release_id: str
    commit_sha: str
    dirty: bool
    deployed_at: str
    client_build_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "release_id": self.release_id,
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "deployed_at": self.deployed_at,
        }
        if self.client_build_id is not None:
            payload["client_build_id"] = self.client_build_id
        return payload

    @classmethod
    def from_dict(cls, value: object) -> DeploymentInfo:
        if not isinstance(value, dict):
            raise ValueError("deployment info must be a mapping")
        payload = cast("dict[str, object]", value)
        release_id = payload.get("release_id")
        commit_sha = payload.get("commit_sha")
        dirty = payload.get("dirty")
        deployed_at = payload.get("deployed_at")
        client_build_id = payload.get("client_build_id")
        if not isinstance(release_id, str) or not is_sha256_hex(release_id):
            raise ValueError("deployment release_id must be a sha256 digest")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("deployment commit_sha must be a non-empty string")
        if not isinstance(dirty, bool):
            raise ValueError("deployment dirty must be a boolean")
        if not isinstance(deployed_at, str) or not deployed_at:
            raise ValueError("deployment deployed_at must be a non-empty string")
        if client_build_id is not None and (
            not isinstance(client_build_id, str) or not is_sha256_hex(client_build_id)
        ):
            raise ValueError("deployment client_build_id must be a sha256 digest")
        return cls(
            release_id=release_id,
            commit_sha=commit_sha,
            dirty=dirty,
            deployed_at=deployed_at,
            client_build_id=client_build_id,
        )


def build_release_info(
    version: str, *, project_root: Path = PROJECT_ROOT
) -> ReleaseInfo:
    """Build the release metadata from the current repo state."""
    commit_sha = _git_stdout(
        ["git", "rev-parse", "--short=8", "HEAD"],
        project_root=project_root,
    )
    dirty = bool(
        _git_stdout(
            ["git", "status", "--short"],
            project_root=project_root,
        )
    )
    date = datetime.now().strftime("%Y%m%d")
    dirty_suffix = "_dirty" if dirty else ""
    return ReleaseInfo(
        release_dir_name=f"{date}-{commit_sha}{dirty_suffix}",
        commit_sha=commit_sha,
        dirty=dirty,
        date=date,
        upstream_version=version,
    )


def read_release_info(path: Path) -> ReleaseInfo:
    """Load persisted release metadata from disk."""
    return ReleaseInfo.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_release_info(path: Path, release_info: ReleaseInfo) -> Path:
    """Persist release metadata for later serve and deploy runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(release_info.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_deployment_info(path: Path) -> DeploymentInfo:
    return DeploymentInfo.from_dict(json.loads(path.read_text(encoding="utf-8")))


def write_deployment_info(path: Path, deployment_info: DeploymentInfo) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(deployment_info.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def resolve_release_info_path(settings: Settings) -> Path:
    """Resolve the generated release-info path from the project root."""
    return PROJECT_ROOT / settings.release_info_path


def resolve_release_dir(settings: Settings) -> Path:
    """Resolve the configured release directory from the project root."""
    release_info_path = resolve_release_info_path(settings)
    if release_info_path.is_file():
        return (
            PROJECT_ROOT
            / settings.releases_root
            / read_release_info(release_info_path).release_dir_name
        )

    return PROJECT_ROOT / settings.release_dir


def resolve_deployment_host(settings: Settings) -> str | None:
    """Resolve the persisted deployment host if release metadata exists."""
    release_info_path = resolve_release_info_path(settings)
    if not release_info_path.is_file():
        return None

    return read_release_info(release_info_path).deployment_host


def _git_stdout(command: list[str], *, project_root: Path) -> str:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    return completed.stdout.strip()


def is_user_archive(filename: str, username: str) -> bool:
    """Return whether a filename is a valid top-level user archive name."""
    if any(token in filename for token in ("..", "/", "\\")):
        return False

    if filename.endswith("-server.tar.gz"):
        return False

    return any(
        filename.endswith(f"-{username}-{platform_name}{package_ext}")
        for platform_name, package_ext in PLATFORM_PACKAGE_EXTENSIONS.items()
    )


def resolve_user_archive_owner(
    filename: str,
    usernames: Iterable[str],
) -> str | None:
    """Resolve the owning username for an archive using the known username set."""
    matches: list[str] = [
        username for username in usernames if is_user_archive(filename, username)
    ]
    if not matches:
        return None

    longest_match = matches[0]
    for match in matches[1:]:
        if len(match) > len(longest_match):
            longest_match = match

    return longest_match


def list_user_archives(
    release_dir: Path,
    username: str,
    known_usernames: Iterable[str] | None = None,
) -> list[Path]:
    """List direct child archives belonging to a user."""
    return sorted(
        [
            path
            for path in release_dir.iterdir()
            if path.is_file()
            and (
                resolve_user_archive_owner(path.name, known_usernames) == username
                if known_usernames is not None
                else is_user_archive(path.name, username)
            )
        ]
    )


def resolve_user_archive(
    release_dir: Path,
    username: str,
    filename: str,
    known_usernames: Iterable[str] | None = None,
) -> Path | None:
    """Resolve a specific user archive if it is safe and exists."""
    if known_usernames is not None:
        if resolve_user_archive_owner(filename, known_usernames) != username:
            return None
    elif not is_user_archive(filename, username):
        return None

    candidate = release_dir / filename
    if not candidate.is_file():
        return None

    return candidate


def resolve_user_archive_for_platform(
    release_dir: Path,
    username: str,
    platform: str,
    known_usernames: Iterable[str] | None = None,
) -> Path | None:
    """Resolve a specific user archive by exact platform name."""
    package_ext = PLATFORM_PACKAGE_EXTENSIONS.get(platform)
    if package_ext is None:
        return None

    expected_suffix = f"-{platform}{package_ext}"
    for path in list_user_archives(release_dir, username, known_usernames):
        if path.name.endswith(expected_suffix):
            return path

    return None
