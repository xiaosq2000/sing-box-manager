"""Release builder: downloads, customizes, and packages sing-box distributions."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar, cast

from sing_box_manager.desktop_config import UserProtocols, build_desktop_config
from sing_box_manager.release.artifacts import build_release_info, write_release_info
from sing_box_manager.release.client_downloads import (
    CLIENT_DIR_NAME,
    CLIENT_MANIFEST_NAME,
    CLIENT_PLATFORMS,
    CLIENT_SIGNATURE_NAME,
    build_sbc,
    client_manifest,
    sbc_version,
    sign,
)
from sing_box_manager.release.digest_cache import (
    SHA256_DIGEST_PREFIX,
    digest_sidecar_path,
    is_sha256_hex,
    load_cached_digest,
    sha256_for_path,
    write_cached_digest,
)
from sing_box_manager.release.errors import ReleaseStageError
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    RELEASE_DIR_PREFIX,
    RELEASE_ID_LENGTH,
    SERVER_DIR_NAME,
    Manifest,
    Member,
)
from sing_box_manager.release.models import ReleasePlan, ReleaseResult
from sing_box_manager.release.platforms import (
    MOBILE_TUN_PROFILE,
    PLATFORMS,
    ROUTE_STRATEGIES,
    Platform,
)
from sing_box_manager.release.profiles import (
    build_client_profile,
    build_server_config,
    client_base_config,
)
from sing_box_manager.release.renderers import (
    apply_desktop_route_strategy,
)
from sing_box_manager.release.rule_sets import desktop_rule_sets, prepare_rule_snapshots
from sing_box_manager.release.store import STORE_DIR_NAME, Store
from sing_box_manager.release.ui import PlainReleaseReporter, ReleaseReporter
from sing_box_manager.release.validation import check_config
from sing_box_manager.runtime_env import load_local_env
from sing_box_manager.settings import (
    Hysteria2User,
    NaiveUser,
    RuntimeInventory,
    Settings,
    TrojanUser,
    load_runtime_inventory,
)
from sing_box_manager.traffic_stats import build_custom_server_binary

T = TypeVar("T")

_github_module = import_module("github")
_github_exception_module = import_module("github.GithubException")

Auth: Any = _github_module.Auth
Github: Any = _github_module.Github
GitReleaseAsset = Any
BadCredentialsException = cast(
    "type[Exception]",
    _github_exception_module.BadCredentialsException,
)
GithubException = cast(
    "type[Exception]",
    _github_exception_module.GithubException,
)
RateLimitExceededException = cast(
    "type[Exception]",
    _github_exception_module.RateLimitExceededException,
)
UnknownObjectException = cast(
    "type[Exception]",
    _github_exception_module.UnknownObjectException,
)

DOWNLOAD_STAGE_TITLE = "Download upstream binaries"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
UPSTREAM_REPOSITORY = "SagerNet/sing-box"
UPSTREAM_USER_AGENT = "sing-box-manager"

STAGING_DIR_NAME = ".staging"

# Server configs can contain infrastructure secrets and TLS material.
RENDERED_FILE_MODE = 0o600
EXECUTABLE_ARCHIVE_MODE = 0o755
REGULAR_ARCHIVE_MODE = 0o644


class AssetIntegrityError(RuntimeError):
    """Raised when an upstream asset digest cannot be verified."""


class UnsafeArchiveError(RuntimeError):
    """Raised when an upstream archive contains unsafe members."""


def _write_file_with_mode(path: Path, payload: bytes, mode: int) -> None:
    """Write a file at an explicit mode instead of whatever the umask allows.

    The chmod after the open is not redundant: the mode passed to os.open is
    masked by the umask, and these files carry credentials, so the permissions
    have to be stated rather than inherited.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as file_obj:
        file_obj.write(payload)
    path.chmod(mode)


class ReleaseBuilder:
    """Orchestrates the full release pipeline."""

    def __init__(
        self,
        settings: Settings,
        reporter: ReleaseReporter | None = None,
        project_root: Path | None = None,
    ) -> None:
        self._settings = settings
        # Relative settings paths resolve against this, so it has to be set
        # before any of them are.
        self._root_dir = project_root or Path(__file__).resolve().parents[2]
        self._release_info = build_release_info(
            settings.sing_box_version,
            project_root=self._root_dir,
        )
        self._config_path = settings.require_config_path()
        self._releases_root = self._resolve_path(settings.releases_root)
        # The release directory is named after a hash of what it contains, so
        # it cannot be known until everything has been rendered. Work happens
        # in a staging directory that is renamed into place at the end.
        self._staging_dir = self._releases_root / STAGING_DIR_NAME
        self._release_dir = self._releases_root
        self._store = Store(self._releases_root / STORE_DIR_NAME)
        self._upstream_cache_root = self._resolve_path(settings.upstream_cache_root)
        self._reporter = reporter or PlainReleaseReporter()
        self._download_paths: dict[str, Path] = {}
        self._custom_server_binary_path: Path | None = None
        self._payload_digests: set[str] = set()
        self._rule_snapshot_paths: list[Path] = []

    def _resolve_path(self, path: str | Path) -> Path:
        """Resolve a settings path relative to the project root."""
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self._root_dir / candidate

    def build(self) -> ReleaseResult:
        """Run the full release pipeline."""
        original_cwd = Path.cwd()
        started_at = perf_counter()
        self._custom_server_binary_path = None
        self._payload_digests = set()
        self._rule_snapshot_paths = []

        try:
            os.chdir(self._root_dir)
            inventory = self._run_stage(
                key="inventory",
                title="Load runtime inventory",
                total=1,
                action=self._load_inventory,
                summary=lambda _: self._resolve_path(self._config_path).name,
            )
            self._release_info = replace(
                self._release_info,
                deployment_host=inventory.deployment.host,
            )
            plan = self._build_plan()
            self._reporter.start(plan)
            self._reset_staging_dir()

            self._run_stage(
                key="download",
                title=DOWNLOAD_STAGE_TITLE,
                total=len(PLATFORMS),
                action=self._download_binaries,
                summary=lambda counts: f"{sum(counts)} platforms ready",
            )
            self._run_stage(
                key="extract",
                title="Extract upstream archives",
                total=len(PLATFORMS),
                action=self._extract_archives,
                summary=lambda extracted: f"{extracted} archives extracted",
            )
            if self._settings.traffic_stats.enabled:
                self._custom_server_binary_path = self._run_stage(
                    key="custom_server_binary",
                    title="Build custom server binary",
                    total=1,
                    action=self._build_custom_server_binary,
                    summary=lambda path: path.name,
                )
            self._run_stage(
                key="rules",
                title="Prepare initial rule sets",
                action=self._prepare_rule_sets,
                summary=lambda count: f"{count} rule files ready",
            )
            self._validate_client_configs(inventory)
            server_members = self._run_stage(
                key="server",
                title="Build server package",
                total=1,
                action=lambda: self._build_server_release(inventory),
                summary=lambda members: f"{len(members)} files staged",
            )
            client_members: tuple[Member, ...] = ()
            if self._settings.client_signing_key is None:
                self._reporter.warn(
                    "client_signing_key is unset, so the release carries no sbc "
                    "downloads."
                )
            else:
                client_members = self._run_stage(
                    key="client",
                    title="Build client downloads",
                    total=1,
                    action=self._build_client_downloads,
                    summary=lambda members: f"{len(members)} files signed",
                )
            published = self._run_stage(
                key="publish",
                title="Publish release",
                total=1,
                action=lambda: self._publish_release(server_members, client_members),
                summary=lambda outcome: (
                    outcome[1].name + (" (unchanged)" if outcome[2] else "")
                ),
            )
            manifest, release_dir, reused_existing = published

            result = ReleaseResult(
                release_dir=release_dir,
                release_id=manifest.release_id,
                server_package_dir=release_dir / SERVER_DIR_NAME,
                reused_existing_release=reused_existing,
                elapsed_seconds=perf_counter() - started_at,
            )
            self._reporter.finish(result)
            return result
        finally:
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            os.chdir(original_cwd)

    def _build_plan(self) -> ReleasePlan:
        """Release artifacts are independent of the live account roster."""
        return ReleasePlan(
            version=self._settings.sing_box_version,
            release_dir=self._release_dir,
            config_path=self._resolve_path(self._config_path),
            platforms=tuple(CLIENT_PLATFORMS),
        )

    def _load_inventory(self) -> RuntimeInventory:
        """Load runtime inventory."""
        return load_runtime_inventory(self._resolve_path(self._config_path))

    def _write_release_info(self) -> Path:
        """Persist release metadata for runtime consumers."""
        return write_release_info(
            self._resolve_path(self._settings.release_info_path),
            self._release_info,
        )

    def _run_stage(
        self,
        *,
        key: str,
        title: str,
        action: Callable[[], T],
        total: int | None = None,
        summary: str | Callable[[T], str] | None = None,
    ) -> T:
        """Run a named stage with consistent UI and error reporting."""
        self._reporter.start_stage(key, title, total)
        try:
            result = action()
        except ReleaseStageError:
            raise
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise ReleaseStageError(title, detail) from exc

        if isinstance(summary, str) or summary is None:
            completion_summary = summary
        else:
            completion_summary = summary(result)
        self._reporter.complete_stage(key, completion_summary)
        return result

    def fetch_upstream_payload(self, platform_name: str) -> Path:
        """Download, verify and extract one platform's upstream release files.

        Returns the directory holding the sing-box binary and the runtime
        library the naive outbound loads beside it. Tests and CI use this to run
        the binary a release ships without building a release. It shares the
        download cache and the store with `build`.
        """
        platform = next(
            (candidate for candidate in PLATFORMS if candidate.name == platform_name),
            None,
        )
        if platform is None:
            raise ValueError(f"No upstream platform is named {platform_name}")
        self._download_binaries([platform])
        self._extract_archives([platform])
        return self._upstream_payload_dir(platform)

    def _download_binaries(
        self, platforms: list[Platform] | None = None
    ) -> tuple[int, int]:
        """Download prebuilt binaries from the upstream GitHub release."""
        platforms = PLATFORMS if platforms is None else platforms
        downloaded = 0
        reused = 0
        version = self._settings.sing_box_version
        verified_cached_platforms: set[str] = set()
        platforms_needing_release_assets: list[Platform] = []

        for platform in platforms:
            download_path = self._cache_path_for_platform(platform, version)
            self._download_paths[platform.name] = download_path

            if not download_path.exists():
                platforms_needing_release_assets.append(platform)
                continue

            cached_digest = self._load_cached_archive_digest(download_path)
            if cached_digest is None:
                platforms_needing_release_assets.append(platform)
                continue

            try:
                self._verify_archive_digest(download_path, cached_digest)
            except AssetIntegrityError:
                self._digest_path_for_archive(download_path).unlink(missing_ok=True)
                platforms_needing_release_assets.append(platform)
            else:
                verified_cached_platforms.add(platform.name)

        release_assets: dict[str, GitReleaseAsset] = {}
        if platforms_needing_release_assets:
            release_assets = self._load_release_assets(
                version, platforms_needing_release_assets
            )

        for platform in platforms:
            download_path = self._download_paths[platform.name]

            if platform.name in verified_cached_platforms:
                reused += 1
            else:
                asset_name = platform.local_filename(version)
                if self._ensure_verified_release_asset(
                    release_assets[asset_name], download_path
                ):
                    downloaded += 1
                else:
                    reused += 1

            self._reporter.advance_stage(
                "download",
                detail=f"{DOWNLOAD_STAGE_TITLE} ({platform.name})",
            )

        return downloaded, reused

    def _load_release_assets(
        self, version: str, missing_platforms: list[Platform]
    ) -> dict[str, GitReleaseAsset]:
        """Resolve the upstream release assets needed for missing local downloads."""
        token = self._github_token()
        if token is None:
            self._reporter.warn(
                "GITHUB_TOKEN is not set; using anonymous GitHub API access for "
                f"{UPSTREAM_REPOSITORY}. This works for public releases but has low "
                "rate limits. Maintainers should export GITHUB_TOKEN before running "
                "`pixi run release`."
            )

        github_client = self._create_github_client(token)
        release_tag = self._release_tag(version)

        try:
            release = github_client.get_repo(UPSTREAM_REPOSITORY).get_release(
                release_tag
            )
            assets_by_name = {asset.name: asset for asset in release.get_assets()}
        except UnknownObjectException as exc:
            raise ReleaseStageError(
                DOWNLOAD_STAGE_TITLE,
                f"GitHub release tag {release_tag} was not found in {UPSTREAM_REPOSITORY}",
            ) from exc
        except BadCredentialsException as exc:
            raise ReleaseStageError(
                DOWNLOAD_STAGE_TITLE,
                "GITHUB_TOKEN was rejected by GitHub",
            ) from exc
        except RateLimitExceededException as exc:
            if token is None:
                detail = (
                    "GitHub API rate limit exceeded for anonymous access; export "
                    "GITHUB_TOKEN and retry"
                )
            else:
                detail = (
                    "GitHub API rate limit exceeded while fetching release metadata"
                )
            raise ReleaseStageError(DOWNLOAD_STAGE_TITLE, detail) from exc
        except GithubException as exc:
            raise ReleaseStageError(
                DOWNLOAD_STAGE_TITLE,
                self._format_github_error(exc),
            ) from exc
        finally:
            github_client.close()

        required_assets = {
            platform.local_filename(version) for platform in missing_platforms
        }
        missing_assets = sorted(required_assets - set(assets_by_name))
        if missing_assets:
            joined = ", ".join(missing_assets)
            raise ReleaseStageError(
                DOWNLOAD_STAGE_TITLE,
                f"GitHub release {release_tag} is missing required assets: {joined}",
            )

        return {name: assets_by_name[name] for name in required_assets}

    def _cache_path_for_platform(self, platform: Platform, version: str) -> Path:
        """Return the cached upstream archive path for a platform release asset."""
        return (
            self._upstream_cache_root
            / "SagerNet"
            / "sing-box"
            / self._release_tag(version)
            / platform.local_filename(version)
        )

    def _digest_path_for_archive(self, archive_path: Path) -> Path:
        """Return the sidecar file used to cache a verified archive digest."""
        return digest_sidecar_path(archive_path)

    def _load_cached_archive_digest(self, archive_path: Path) -> str | None:
        """Read a cached archive digest, discarding malformed sidecars."""
        return load_cached_digest(archive_path)

    def _write_cached_archive_digest(self, archive_path: Path, digest: str) -> None:
        """Persist the verified archive digest for future cached reuse."""
        write_cached_digest(archive_path, digest)

    def _asset_sha256_digest(self, asset: GitReleaseAsset) -> str:
        """Return the normalized sha256 digest published by GitHub for an asset."""
        digest = getattr(asset, "digest", None)
        asset_name = getattr(asset, "name", "unknown asset")
        if not isinstance(digest, str) or not digest.startswith(SHA256_DIGEST_PREFIX):
            raise AssetIntegrityError(
                f"GitHub asset {asset_name} did not include a sha256 digest"
            )

        normalized = digest.removeprefix(SHA256_DIGEST_PREFIX).strip().lower()
        if not is_sha256_hex(normalized):
            raise AssetIntegrityError(
                f"GitHub asset {asset_name} published an invalid sha256 digest"
            )

        return normalized

    def _sha256_for_path(self, path: Path) -> str:
        """Compute the sha256 digest for a local archive file."""
        return sha256_for_path(path)

    def _verify_archive_digest(self, archive_path: Path, expected_digest: str) -> None:
        """Raise if a local archive does not match the expected sha256 digest."""
        actual_digest = self._sha256_for_path(archive_path)
        if actual_digest != expected_digest:
            raise AssetIntegrityError(
                f"Upstream asset {archive_path.name} failed sha256 verification"
            )

    def _ensure_verified_release_asset(
        self, asset: GitReleaseAsset, destination: Path
    ) -> bool:
        """Verify an existing asset or download a fresh verified copy."""
        expected_digest = self._asset_sha256_digest(asset)
        if destination.is_file():
            try:
                self._verify_archive_digest(destination, expected_digest)
            except AssetIntegrityError:
                destination.unlink(missing_ok=True)
                self._digest_path_for_archive(destination).unlink(missing_ok=True)
            else:
                self._write_cached_archive_digest(destination, expected_digest)
                return False

        self._download_release_asset(asset, destination)
        return True

    def _validate_archive_member_path(
        self, destination_root: Path, member_name: str
    ) -> None:
        """Reject archive members that would escape the release directory."""
        if not member_name:
            raise UnsafeArchiveError("Archive contains an empty member name")

        target_path = (destination_root / member_name).resolve(strict=False)
        if not target_path.is_relative_to(destination_root):
            raise UnsafeArchiveError(
                f"Archive entry {member_name!r} would extract outside release directory"
            )

    def _safe_extract_tar_archive(self, archive_path: Path, destination: Path) -> None:
        """Extract a tar.gz archive after validating member paths and types."""
        destination_root = destination.resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                self._validate_archive_member_path(destination_root, member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise UnsafeArchiveError(
                        f"Tar archive entry {member.name!r} uses an unsupported link or device type"
                    )
            archive.extractall(destination, members=members, filter="data")

    def _safe_extract_zip_archive(self, archive_path: Path, destination: Path) -> None:
        """Extract a zip archive after validating member paths and types."""
        destination_root = destination.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            for member in members:
                self._validate_archive_member_path(destination_root, member.filename)
                member_type = stat.S_IFMT(member.external_attr >> 16)
                if member_type not in {0, stat.S_IFDIR, stat.S_IFREG}:
                    raise UnsafeArchiveError(
                        f"Zip archive entry {member.filename!r} uses an unsupported file type"
                    )
            archive.extractall(destination)

    def _download_release_asset(
        self, asset: GitReleaseAsset, destination: Path
    ) -> None:
        """Download a release asset to disk with cleanup for partial files."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_digest = self._asset_sha256_digest(asset)
        tmp_file = None
        try:
            tmp_file = tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            )
            tmp_path = Path(tmp_file.name)
            tmp_file.close()
            asset.download_asset(
                path=str(tmp_path),
                chunk_size=DOWNLOAD_CHUNK_SIZE,
            )
            self._verify_archive_digest(tmp_path, expected_digest)
            tmp_path.replace(destination)
            self._write_cached_archive_digest(destination, expected_digest)
        except Exception:
            if tmp_file is not None:
                Path(tmp_file.name).unlink(missing_ok=True)
            raise

    def _github_token(self) -> str | None:
        """Return the configured GitHub token if one is set."""
        load_local_env()
        token = os.getenv(GITHUB_TOKEN_ENV, "").strip()
        return token or None

    def _create_github_client(self, token: str | None) -> Github:
        """Create a GitHub API client for release metadata lookups."""
        auth = Auth.Token(token) if token is not None else None
        return Github(auth=auth, user_agent=UPSTREAM_USER_AGENT)

    def _release_tag(self, version: str) -> str:
        """Return the upstream git tag used for the requested release version."""
        return f"v{version}"

    def _format_github_error(self, exc: Exception) -> str:
        """Render a compact user-facing error for GitHub API failures."""
        message = (
            getattr(exc, "message", None) or str(exc) or "GitHub API request failed"
        )
        return f"GitHub API request failed: {message}"

    def _upstream_asset_digest(self, platform: Platform) -> str:
        """The verified sha256 of a platform's upstream archive."""
        download_path = self._download_paths[platform.name]
        digest = self._load_cached_archive_digest(download_path)
        if digest is None:
            raise ReleaseStageError(
                "Extract upstream archives",
                f"missing verified digest for {download_path.name}",
            )
        return digest

    def _upstream_payload_dir(self, platform: Platform) -> Path:
        """Where a platform's extracted upstream files live inside the store."""
        root = self._store.upstream_dir_for(
            platform.name, self._upstream_asset_digest(platform)
        )
        nested = platform.extraction_dir(
            root / platform.local_filename(self._settings.sing_box_version)
        )
        return nested if nested is not None else root

    def _extract_archives(self, platforms: list[Platform] | None = None) -> int:
        """Extract upstream archives into the store, skipping what is already there.

        The destination is keyed by the digest just verified against the one
        GitHub published, so an existing directory is known-good: it needs
        neither re-extraction nor re-verification. Previously every run
        re-extracted all six archives even on a full cache hit.
        """
        platforms = PLATFORMS if platforms is None else platforms
        extracted = 0

        for platform in platforms:
            download_path = self._download_paths[platform.name]

            if platform.download_format in {"tar.gz", "zip"}:
                for attempt in range(2):
                    # Recomputed every attempt: a re-download can land different
                    # bytes, and the digest of those bytes is what names the
                    # destination.
                    destination = self._store.upstream_dir_for(
                        platform.name, self._upstream_asset_digest(platform)
                    )
                    if self._upstream_payload_dir(platform).is_dir():
                        break

                    try:
                        shutil.rmtree(destination, ignore_errors=True)
                        destination.mkdir(parents=True, exist_ok=True)
                        if platform.download_format == "tar.gz":
                            self._safe_extract_tar_archive(download_path, destination)
                        else:
                            self._safe_extract_zip_archive(download_path, destination)
                        extracted += 1
                        break
                    except (
                        EOFError,
                        OSError,
                        UnsafeArchiveError,
                        tarfile.TarError,
                        zipfile.BadZipFile,
                    ):
                        if attempt == 1:
                            raise
                        shutil.rmtree(destination, ignore_errors=True)
                        download_path.unlink(missing_ok=True)
                        self._digest_path_for_archive(download_path).unlink(
                            missing_ok=True
                        )
                        release_assets = self._load_release_assets(
                            self._settings.sing_box_version, [platform]
                        )
                        asset_name = platform.local_filename(
                            self._settings.sing_box_version
                        )
                        self._download_release_asset(
                            release_assets[asset_name], download_path
                        )

            self._reporter.advance_stage(
                "extract",
                detail=f"Extract upstream archives ({platform.name})",
            )

        return extracted

    def _reset_staging_dir(self) -> None:
        """Start every run from an empty staging directory."""
        shutil.rmtree(self._staging_dir, ignore_errors=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)

    def _archive_mode(self, path: Path) -> int:
        """Normalise a source file's mode to what belongs in an archive.

        Only the executable bit carries meaning for these files, and using the
        raw stat mode would let a local umask or a stray chmod move a prefix's
        content hash without changing anything a client actually receives.
        """
        if path.stat().st_mode & 0o111:
            return EXECUTABLE_ARCHIVE_MODE
        return REGULAR_ARCHIVE_MODE

    def _prepare_rule_sets(self) -> int:
        if self._settings.client_signing_key is None:
            return 0
        protocols = ("trojan", "hysteria2", "naive")
        configs = (
            apply_desktop_route_strategy(
                client_base_config(protocol),
                strategy,
                # Only the rule sets are read back here, and those are the same
                # on every platform.
                os_name="linux",
            )
            for protocol in protocols
            for strategy in ROUTE_STRATEGIES
        )
        rules = desktop_rule_sets(configs)
        linux = next(
            platform for platform in PLATFORMS if platform.name == "linux-amd64"
        )
        self._rule_snapshot_paths = prepare_rule_snapshots(
            rules,
            self._staging_dir / "rules",
            self._upstream_payload_dir(linux) / "sing-box",
            self._github_token(),
        )
        return len(rules)

    def _check_config(self, config: dict, label: str, *, server: bool = False) -> None:
        linux = next(
            (platform for platform in PLATFORMS if platform.name == "linux-amd64"), None
        )
        if linux is None:
            raise ValueError("Release validation requires the Linux amd64 binary")
        binary = (
            self._custom_server_binary_path
            if server and self._custom_server_binary_path
            else self._upstream_payload_dir(linux) / "sing-box"
        )
        check_config(binary, config, label=label, server=server)

    def _validate_client_configs(self, inventory: RuntimeInventory) -> None:
        # Validate profiles without reading or publishing any live user credentials.
        values = {"username": "validation", "password": "synthetic-validation-only"}
        protocols = UserProtocols(
            trojan=TrojanUser.model_validate(values),
            hysteria2=Hysteria2User.model_validate(values),
            naive=NaiveUser.model_validate(values),
            hysteria2_settings=inventory.hysteria2,
        )
        for os_name in ("linux", "darwin", "windows"):
            config = build_desktop_config(
                inventory.deployment,
                protocols,
                os_name=os_name,
                default_protocol=self._settings.default_protocol,
                default_route="china-direct",
            )
            self._check_config(config, f"validation/{os_name}/sbc")
        for protocol in ("trojan", "hysteria2", "naive"):
            self._check_config(
                build_client_profile(
                    inventory.deployment, protocols, protocol, MOBILE_TUN_PROFILE
                ),
                f"validation/mobile/{protocol}",
            )

    def _build_custom_server_binary(self) -> Path:
        """Build the traffic-stats server binary from upstream source."""
        destination = self._staging_dir / "sing-box-linux-amd64-server"
        destination.unlink(missing_ok=True)
        return build_custom_server_binary(self._settings, destination)

    def _link_payload_into(self, server_dir: Path, source: Path, name: str) -> str:
        """Adopt a large file into the store and point the server package at it.

        The sing-box binary is ~58 MB and changes only with the upstream
        version, so copying it into every release directory would put it back
        on the wire every time a user is added. A relative symlink resolves
        identically on the build host and the VPS because the staging directory
        and the published release directories sit at the same depth, and both
        rsync and server-install.sh handle symlinks natively.
        """
        stored, digest = self._store.ensure_payload(source, name)
        self._payload_digests.add(digest)
        server_dir.mkdir(parents=True, exist_ok=True)
        link = server_dir / name
        link.unlink(missing_ok=True)
        link.symlink_to(os.path.relpath(stored, server_dir))
        return digest

    def _build_client_downloads(self) -> tuple[Member, ...]:
        """Build sbc, collect what it downloads, and sign a manifest of it all.

        The sing-box archives are the ones already checked against GitHub's
        digests, kept in the store like the server binary. The rule files are
        the snapshots this release took.
        """
        key = self._settings.client_signing_key
        if key is None:
            raise RuntimeError("client_signing_key is required")
        client_dir = self._staging_dir / CLIENT_DIR_NAME
        version = sbc_version(self._release_info)
        members: list[Member] = []

        def add(path: Path, name: str, mode: int, digest: str | None = None) -> None:
            members.append(
                Member(
                    arcname=name,
                    mode=mode,
                    size=path.stat().st_size,
                    sha256=digest or sha256_for_path(path),
                    src=str(path.relative_to(self._staging_dir)),
                )
            )

        for platform_name in (
            platform.name for platform in PLATFORMS if platform.name in CLIENT_PLATFORMS
        ):
            binary = client_dir / "sbc" / platform_name / "sbc"
            build_sbc(self._root_dir, platform_name, version, binary)
            add(binary, f"sbc/{platform_name}/sbc", EXECUTABLE_ARCHIVE_MODE)
            archive = self._download_paths[platform_name]
            digest = self._link_payload_into(
                client_dir / "sing-box", archive, archive.name
            )
            add(
                client_dir / "sing-box" / archive.name,
                f"sing-box/{archive.name}",
                REGULAR_ARCHIVE_MODE,
                digest,
            )
        for snapshot in self._rule_snapshot_paths:
            if snapshot.suffix == ".srs":
                add(snapshot, f"rules/{snapshot.name}", REGULAR_ARCHIVE_MODE)

        manifest = client_manifest(
            [(member.arcname, member.size, member.sha256) for member in members],
            sbc=version,
            sing_box=self._settings.sing_box_version,
        )
        manifest_path = client_dir / CLIENT_MANIFEST_NAME
        signature_path = client_dir / CLIENT_SIGNATURE_NAME
        _write_file_with_mode(manifest_path, manifest, REGULAR_ARCHIVE_MODE)
        _write_file_with_mode(
            signature_path,
            (sign(key, manifest) + "\n").encode("ascii"),
            REGULAR_ARCHIVE_MODE,
        )
        add(manifest_path, CLIENT_MANIFEST_NAME, REGULAR_ARCHIVE_MODE)
        add(signature_path, CLIENT_SIGNATURE_NAME, REGULAR_ARCHIVE_MODE)
        return tuple(members)

    def _build_server_release(self, inventory: RuntimeInventory) -> tuple[Member, ...]:
        """Stage the server package: small rendered files plus links to the store."""
        server_dir = self._staging_dir / SERVER_DIR_NAME
        server_dir.mkdir(parents=True, exist_ok=True)

        linux_platform = next(
            (platform for platform in PLATFORMS if platform.name == "linux-amd64"),
            None,
        )
        if linux_platform is not None:
            for file_path in sorted(
                self._upstream_payload_dir(linux_platform).iterdir()
            ):
                if not file_path.is_file():
                    continue
                if (
                    self._custom_server_binary_path is not None
                    and file_path.name == "sing-box"
                ):
                    continue
                self._link_payload_into(server_dir, file_path, file_path.name)

        if self._custom_server_binary_path is not None:
            self._link_payload_into(
                server_dir, self._custom_server_binary_path, "sing-box"
            )

        if self._settings.control_plane.authoritative:
            inventory = inventory.without_accounts()
        rendered = build_server_config(inventory, self._settings.traffic_stats)
        self._check_config(rendered, "server/config.json", server=True)
        _write_file_with_mode(
            server_dir / "config.json",
            json.dumps(rendered, indent=4).encode("utf-8"),
            RENDERED_FILE_MODE,
        )

        scripts_dir = self._root_dir / "scripts"
        shutil.copy(scripts_dir / "server-install.sh", server_dir)
        shutil.copy(scripts_dir / "server-uninstall.sh", server_dir)
        lib_dir = server_dir / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(scripts_dir / "lib" / "ui.sh", lib_dir)
        shutil.copy(scripts_dir / "sing-box.service", server_dir)

        self._reporter.advance_stage("server")
        return self._describe_staged_tree(server_dir, SERVER_DIR_NAME)

    def _describe_staged_tree(self, root: Path, prefix: str) -> tuple[Member, ...]:
        """Record what a staged directory contains, for the manifest."""
        return tuple(
            sorted(
                (
                    Member(
                        arcname=str(path.relative_to(root)),
                        mode=self._archive_mode(path),
                        size=path.stat().st_size,
                        sha256=sha256_for_path(path),
                        src=f"{prefix}/{path.relative_to(root)}",
                    )
                    for path in root.rglob("*")
                    if path.is_file()
                ),
                key=lambda member: member.arcname,
            )
        )

    def _publish_release(
        self,
        server_members: tuple[Member, ...],
        client_members: tuple[Member, ...] = (),
    ) -> tuple[Manifest, Path, bool]:
        """Name the release after its contents and move it into place.

        Because the name is a hash of everything in it, a rebuild that changes
        nothing lands on a directory that already exists -- which is what makes
        `nothing changed` cost zero bytes to deploy.
        """
        manifest = Manifest(
            upstream_version=self._settings.sing_box_version,
            prefixes=(),
            archives=(),
            server_members=server_members,
            payload_digests=tuple(sorted(self._payload_digests)),
            client_members=client_members,
        )
        release_dir = self._releases_root / (
            f"{RELEASE_DIR_PREFIX}{manifest.release_id[:RELEASE_ID_LENGTH]}"
        )

        reused_existing = (release_dir / MANIFEST_FILENAME).is_file()
        if not reused_existing:
            # A directory without a manifest is a run that died before
            # publishing, so it has no claim on the name.
            shutil.rmtree(release_dir, ignore_errors=True)
            _write_file_with_mode(
                self._staging_dir / MANIFEST_FILENAME,
                manifest.to_json().encode("utf-8"),
                REGULAR_ARCHIVE_MODE,
            )
            self._staging_dir.replace(release_dir)

        self._release_dir = release_dir
        self._release_info = replace(
            self._release_info,
            release_dir_name=release_dir.name,
            release_id=manifest.release_id,
        )
        self._write_release_info()
        self._reporter.advance_stage("publish")
        return manifest, release_dir, reused_existing
