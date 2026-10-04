"""Remote deployment: ship the code and the artifacts in one command.

The VPS used to be a git checkout that had to be hand-updated to exactly the
commit a release was built from before deploy would proceed, on a box whose
whole purpose is working around network interference -- and it needed a
credential that could read a private repo to do it. Artifacts were one prebuilt
archive per user per platform, in a directory named after the commit sha, so
every commit created a brand-new remote directory and rsync re-transferred all
1.2 GB from scratch even for a docs typo.

Now one command ships both halves:

* **code**, over rsync from an explicit allowlist, so the VPS needs no git, no
  LFS and no repo credential -- the deploy key can be revoked;
* **artifacts**, as a shared content-addressed store plus a small
  content-addressed release directory, so a payload that did not change
  transfers nothing at all.

The allowlist is the dangerous part and is deliberately narrow. ``--delete`` is
only ever scoped inside directories wholly owned by git. Server-local state is
never a delete target. The derived runtime config is the sole exception to the
otherwise append-only state rule: it is staged, validated and atomically
published while retaining the server's existing session secret.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from sing_box_manager.settings import RuntimeInventory

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from sing_box_manager import ssh
from sing_box_manager.release import artifacts
from sing_box_manager.release.digest_cache import sha256_for_path
from sing_box_manager.release.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    SERVER_DIR_NAME,
)
from sing_box_manager.release.store import (
    PREFIX_DIR_NAME,
    STORE_DIR_NAME,
    UPSTREAM_DIR_NAME,
)
from sing_box_manager.settings import Settings

SUPPORTED_PROTOCOLS = ("trojan", "hysteria2", "naive")
# The account the portal and the collectors run as. It reads the deployed tree
# through its group and writes only its systemd StateDirectory.
SERVICE_USER = "sbm"
WEB_SERVICE_NAME = "sing-box-manager-web.service"
TRAFFIC_STATS_SERVICE_NAME = "sing-box-manager-traffic-stats.service"
TRAFFIC_STREAM_SERVICE_NAME = "sing-box-manager-traffic-stream.service"
RSYNC_PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d{1,3})%(?!\d)")
RSYNC_VERSION_PATTERN = re.compile(r"version\s+(\d+\.\d+\.\d+)")
REMOTE_PIXI_RSYNC_RELATIVE_PATH = ".pixi/envs/default/bin/rsync"
REMOTE_PIXI_PYTHON_RELATIVE_PATH = ".pixi/envs/default/bin/python"
REMOTE_CONFIG_RELATIVE_PATH = "config/inventory/runtime.yaml"

# Directories that are 100% git-tracked, and therefore the only places
# `--delete` is allowed to run. Anything holding server-local state is absent
# from both lists on purpose.
CODE_TREE_PATHS = (
    "sing_box_manager",
    "scripts",
    "docs",
    # The portal reads the AI rule catalog when it renders a subscription.
    "config/rules",
)
# Tracked files that live next to untracked siblings. Synced individually and
# never with `--delete`, because their parent directories are mixed.
CODE_FILE_PATHS = (
    "pixi.toml",
    "pixi.lock",
    "pyproject.toml",
    "config/inventory/example.yaml",
)
# CPython invalidates bytecode on source mtime and size, so neither pushing nor
# deleting these matters -- but deleting them under `--delete` would be churn.
CODE_SYNC_EXCLUDES = ("__pycache__/",)
PIXI_LOCK_RELATIVE_PATH = "pixi.lock"

# Large files kept in Git LFS. A clone without `git lfs pull` holds pointer
# stubs, and rsyncing those would silently replace the portal's fonts with
# 130-byte text files.
LFS_TRACKED_PATTERNS = ("sing_box_manager/web/static/fonts/*.ttf",)
LFS_POINTER_HEADER = b"version https://git-lfs"
LFS_MIN_OBJECT_SIZE = 1024 * 1024

VERSION_FILENAME = "VERSION"
STORE_PARTIAL_DIR = ".rsync-partial"


class DeploymentError(RuntimeError):
    """Raised when a deployment step fails."""


@dataclass(frozen=True, slots=True)
class DeployOptions:
    """How a deployment behaves, as opposed to what it targets."""

    show_progress: bool = False
    bootstrap: bool = False
    skip_code: bool = False
    dry_run: bool = False
    certbot_email: str | None = None


@dataclass(frozen=True, slots=True)
class _Transfer:
    """One rsync invocation: what moves where, and under which flags."""

    source: Path
    destination: str
    options: tuple[str, ...]
    error_message: str
    description: str = "Sync"


@dataclass(frozen=True, slots=True)
class _Remote:
    """Resolved remote paths for one deployment."""

    hostname: str
    root: str
    releases_root: str
    release_dir: str
    store_dir: str
    server_dir: str
    generated_dir: str
    auth_snapshot: str
    release_info: str
    deployment_info: str
    version_file: str
    # Empty while bootstrapping, before `pixi install` has produced one.
    rsync_path: str = ""

    @property
    def pixi_rsync_path(self) -> str:
        return os.fspath(Path(self.root) / REMOTE_PIXI_RSYNC_RELATIVE_PATH)

    @property
    def pixi_python_path(self) -> str:
        return os.fspath(Path(self.root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH)


def deploy_release(
    hostname: str,
    protocols: Sequence[str] | None,
    remote_root: str,
    settings: Settings,
    options: DeployOptions = DeployOptions(),
) -> None:
    """Deploy the active release, and the code that serves it, to a remote host."""
    normalized_hostname = _normalize_hostname(hostname)
    normalized_protocols = _normalize_protocols(protocols)
    protocol_description = ", ".join(normalized_protocols) or "all packaged inbounds"
    normalized_remote_root = _normalize_remote_root(remote_root)

    auth_snapshot_path = _resolve_local_project_path(settings.auth_snapshot_path)
    release_info_path = artifacts.resolve_release_info_path(settings)
    release_info = _load_release_info(release_info_path)
    releases_root = _resolve_local_project_path(settings.releases_root)
    release_dir = releases_root / release_info.release_dir_name
    store_dir = releases_root / STORE_DIR_NAME

    _require_local_artifacts(
        auth_snapshot_path=auth_snapshot_path,
        release_dir=release_dir,
        store_dir=store_dir,
    )
    inventory = (
        _load_deploy_inventory(settings)
        if options.bootstrap or not options.skip_code
        else None
    )

    remote = _build_remote(
        normalized_hostname,
        normalized_remote_root,
        settings,
        release_dir_name=release_dir.name,
    )

    # One connection for the whole run. Each remote step used to pay for
    # its own handshake, and on a path where those get reset the deploy
    # died partway through more often than it finished.
    with ssh.multiplexed_session(normalized_hostname):
        print(f"Deploying release {release_dir.name} to {normalized_hostname}")
        print(f"Remote root: {normalized_remote_root}")
        print(f"Protocols: {protocol_description}")
        if options.dry_run:
            print("Dry run: no remote state will be modified.")
        if release_info.dirty:
            print("Warning: release was built from a dirty worktree.")

        if not options.skip_code:
            # Before any remote contact: a pointer-file font is a local mistake and
            # there is no reason to open an ssh connection to discover it.
            _check_lfs_objects(artifacts.PROJECT_ROOT)

        _provision_if_bootstrapping(
            normalized_hostname,
            normalized_remote_root,
            settings,
            inventory,
            options,
        )

        print("Checking remote host...")
        remote = _preflight(remote, options)

        if options.skip_code:
            print("Skipping code sync (--skip-code).")
        else:
            remote = _sync_code(remote, artifacts.PROJECT_ROOT, options)

        _ensure_manifest_compatibility(remote, options)

        print("Syncing shared payload store...")
        _rsync_directory(
            _Transfer(
                source=store_dir,
                destination=_rsync_remote_target(remote.hostname, remote.store_dir),
                # Both `prefix/` and `payload/` have to travel: the server package's
                # sing-box binary is a relative symlink into `payload/`, so shipping
                # only the prefixes would leave it dangling on the remote.
                # `upstream/` is a build input and stays on the build host.
                #
                # Deliberately no `--delete`: the store is shared across releases, so
                # "not in this release" and "unreferenced" are different questions.
                # `sbm gc` answers the second one. `--partial-dir` lets an interrupted
                # blob resume instead of restarting; `--ignore-existing` would be wrong
                # here for the same reason -- it would skip that partial blob forever.
                options=(
                    "-a",
                    "-z",
                    "-s",
                    f"--exclude={UPSTREAM_DIR_NAME}/",
                    f"--partial-dir={STORE_PARTIAL_DIR}",
                ),
                error_message=f"Failed to sync the payload store to {remote.hostname}.",
                description="Sync payload store",
            ),
            remote,
            options,
        )

        print("Syncing active release directory...")
        _rsync_directory(
            _Transfer(
                source=release_dir,
                destination=_rsync_remote_target(remote.hostname, remote.release_dir),
                # No `-z`: the payload here is already compressed or tiny. `--delete`
                # is safe because the directory is named after a hash of its contents.
                # `-a` implies `-p`, so the rendered configs keep the 0600 and the
                # `users/` directory keeps the 0700 the builder gave them -- the
                # receiving umask never gets a say.
                options=("-a", "-s", "--delete"),
                error_message=(
                    f"Failed to sync release directory {release_dir.name} "
                    f"to {remote.hostname}."
                ),
                description="Sync active release",
            ),
            remote,
            options,
        )

        print("Uploading generated portal metadata...")
        _upload_metadata(remote, options, auth_snapshot_path, release_info_path)

        # Before anything below runs code or installs a binary from the tree.
        print("Locking down the deployed tree...")
        _lock_down_remote_tree(remote, options)

        _install_services(remote, normalized_protocols, options)

        print("Publishing generated portal metadata...")
        _publish_metadata(remote, options)

        _sync_remote_config(remote, settings, inventory, options)

        _install_managed_units(
            normalized_hostname,
            normalized_remote_root,
            settings,
            options,
        )

        _install_nginx_log_redaction(normalized_hostname, options)

        _restart_web_service(remote, options)
        _verify_traffic_stats_collector(remote, settings, options)
        _publish_deployment_info(remote, options, release_info)

        print(
            "Deployment complete: "
            f"{release_dir.name} is active on {remote.hostname} "
            f"({protocol_description})."
        )


def _build_remote(
    hostname: str,
    remote_root: str,
    settings: Settings,
    *,
    release_dir_name: str,
) -> _Remote:
    releases_root = _resolve_remote_project_path(remote_root, settings.releases_root)
    auth_snapshot = _resolve_remote_project_path(
        remote_root, settings.auth_snapshot_path
    )
    release_dir = os.fspath(Path(releases_root) / release_dir_name)
    return _Remote(
        hostname=hostname,
        root=remote_root,
        releases_root=releases_root,
        release_dir=release_dir,
        store_dir=os.fspath(Path(releases_root) / STORE_DIR_NAME),
        server_dir=os.fspath(Path(release_dir) / SERVER_DIR_NAME),
        generated_dir=os.fspath(Path(auth_snapshot).parent),
        auth_snapshot=auth_snapshot,
        release_info=_resolve_remote_project_path(
            remote_root, settings.release_info_path
        ),
        deployment_info=_resolve_remote_project_path(
            remote_root, settings.deployment_info_path
        ),
        version_file=os.fspath(Path(remote_root) / VERSION_FILENAME),
    )


def _require_local_artifacts(
    *,
    auth_snapshot_path: Path,
    release_dir: Path,
    store_dir: Path,
) -> None:
    _require_file(
        auth_snapshot_path,
        "Portal auth snapshot not found. Run `sbm release` first.",
    )
    _require_directory(
        release_dir,
        f"Release directory not found: {release_dir}. Run `sbm release` first.",
    )
    _require_file(
        release_dir / MANIFEST_FILENAME,
        f"Release manifest not found in {release_dir}. Run `sbm release` first.",
    )
    _require_directory(
        release_dir / SERVER_DIR_NAME,
        f"Server package not found in {release_dir}. Run `sbm release` first.",
    )
    _require_directory(
        store_dir / PREFIX_DIR_NAME,
        f"Payload store not found: {store_dir}. Run `sbm release` first.",
    )


def _preflight(remote: _Remote, options: DeployOptions) -> _Remote:
    """Check the remote can be deployed to, and resolve its rsync binary.

    Bootstrapping a fresh host is the one case where the pixi environment is
    not there yet, so the code sync has to fall back to the system rsync and
    ``pixi install`` creates the managed one before anything else uses it.
    """
    required_dirs = [
        remote.releases_root,
        remote.store_dir,
        remote.generated_dir,
    ]
    checks = [
        f"test -d {shlex.quote(remote.root)}",
        f"test -w {shlex.quote(remote.root)}",
        "command -v systemctl >/dev/null",
        "command -v sudo >/dev/null",
    ]

    if options.bootstrap:
        checks.append("command -v rsync >/dev/null")
        checks.append("command -v pixi >/dev/null")
        failure = (
            f"Remote host {remote.hostname} is not ready to bootstrap at "
            f"{remote.root}. Expected a writable directory plus rsync, pixi, "
            "sudo and systemctl on PATH."
        )
    else:
        pixi_toml = os.fspath(Path(remote.root) / "pixi.toml")
        checks.append(f"test -f {shlex.quote(pixi_toml)}")
        checks.append(f"test -x {shlex.quote(remote.pixi_rsync_path)}")
        failure = (
            f"Remote root not ready at {remote.root} on {remote.hostname}. "
            f"Expected pixi.toml, a pixi-managed rsync at {remote.pixi_rsync_path} "
            "(deploy once with --bootstrap), plus sudo and systemctl."
        )

    checks.append(f"mkdir -p {' '.join(shlex.quote(path) for path in required_dirs)}")
    _run_remote_command(remote.hostname, " && ".join(checks), failure)

    if options.bootstrap:
        # No `--rsync-path`: the managed binary does not exist yet.
        return replace(remote, rsync_path="")

    _ensure_rsync_versions_match(remote.hostname, remote.pixi_rsync_path)
    return replace(remote, rsync_path=remote.pixi_rsync_path)


def _sync_code(remote: _Remote, project_root: Path, options: DeployOptions) -> _Remote:
    """Push the tracked code, then rebuild the environment only if it moved."""
    lock_changed = options.bootstrap or _remote_lock_differs(remote, project_root)

    print("Syncing project code...")
    for tree in CODE_TREE_PATHS:
        source = project_root / tree
        _require_directory(source, f"Code path missing from the working tree: {source}")
        _rsync_directory(
            _Transfer(
                source=source,
                destination=_rsync_remote_target(
                    remote.hostname, os.fspath(Path(remote.root) / tree)
                ),
                # `--delete` is confined to this subtree, which git owns
                # entirely. `--delay-updates` means an interrupted run never
                # leaves a half-written code tree behind.
                options=(
                    "-a",
                    "-z",
                    "-s",
                    "--delete",
                    "--delay-updates",
                    *[f"--exclude={pattern}" for pattern in CODE_SYNC_EXCLUDES],
                ),
                error_message=f"Failed to sync {tree} to {remote.hostname}.",
                description=f"Sync {tree}",
            ),
            remote,
            options,
        )

    for name in CODE_FILE_PATHS:
        source = project_root / name
        _require_file(source, f"Code file missing from the working tree: {source}")
        _rsync_file(
            _Transfer(
                source=source,
                destination=_rsync_remote_target(
                    remote.hostname, os.fspath(Path(remote.root) / name)
                ),
                options=("-a", "-z", "-s"),
                error_message=f"Failed to sync {name} to {remote.hostname}.",
            ),
            remote,
            options,
        )

    if not lock_changed:
        print("pixi.lock unchanged; skipping remote environment install.")
        return remote

    print("Installing remote pixi environment (pixi.lock changed)...")
    _run_remote_command(
        remote.hostname,
        f"cd {shlex.quote(remote.root)} && pixi install --locked",
        f"Failed to install the pixi environment on {remote.hostname}.",
        dry_run=options.dry_run,
    )

    if options.dry_run:
        return remote

    # `pixi install` can replace the rsync binary this deploy is about to use
    # for the artifact transfers, so both the path and the version check have to
    # be redone rather than carried over from preflight.
    _run_remote_command(
        remote.hostname,
        f"test -x {shlex.quote(remote.pixi_rsync_path)}",
        f"No pixi-managed rsync at {remote.pixi_rsync_path} on {remote.hostname} "
        "after `pixi install`.",
    )
    _ensure_rsync_versions_match(remote.hostname, remote.pixi_rsync_path)
    return replace(remote, rsync_path=remote.pixi_rsync_path)


def _check_lfs_objects(project_root: Path) -> None:
    """Fail before the wire if LFS-managed files are still pointer stubs.

    A fresh clone without `git lfs pull` holds 130-byte text stubs. Syncing
    those would replace the portal's fonts with pointer files and the breakage
    would only show up in a browser.
    """
    for pattern in LFS_TRACKED_PATTERNS:
        matches = sorted(project_root.glob(pattern))
        if not matches:
            raise DeploymentError(
                f"No files match the LFS-tracked pattern `{pattern}`. "
                "Run `git lfs pull` before deploying."
            )
        for path in matches:
            with path.open("rb") as file_obj:
                header = file_obj.read(len(LFS_POINTER_HEADER))
            if (
                header == LFS_POINTER_HEADER
                or path.stat().st_size < LFS_MIN_OBJECT_SIZE
            ):
                raise DeploymentError(
                    f"{path} is a Git LFS pointer, not the real object. "
                    "Run `git lfs pull` before deploying."
                )


def _remote_lock_differs(remote: _Remote, project_root: Path) -> bool:
    """Whether the remote pixi.lock is about to change.

    Solving an environment costs minutes on a weak CPU, so skipping it when
    nothing moved is most of the win on an ordinary deploy. Comparing before the
    sync means one remote round trip instead of two.
    """
    local_digest = sha256_for_path(project_root / PIXI_LOCK_RELATIVE_PATH)
    remote_path = os.fspath(Path(remote.root) / PIXI_LOCK_RELATIVE_PATH)
    output = _run_remote_stdout(
        remote.hostname,
        f"sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1",
        f"Failed to read the remote pixi.lock digest from {remote.hostname}.",
    )
    return output != local_digest


def _ensure_manifest_compatibility(remote: _Remote, options: DeployOptions) -> None:
    """Check the remote code can read the manifest this release writes.

    Only interesting when the code sync was skipped -- otherwise the remote is
    running the code that just shipped. The interpreter is addressed by absolute
    path because `_run_remote_command` uses a non-login shell, where `pixi run`
    is unreliable.
    """
    if options.dry_run or options.bootstrap:
        return

    output = _run_remote_stdout(
        remote.hostname,
        (
            f"cd {shlex.quote(remote.root)} && "
            f"{shlex.quote(remote.pixi_python_path)} -c "
            "'import sing_box_manager.release.manifest as m; "
            "print(m.MANIFEST_VERSION)'"
        ),
        f"Failed to read the remote manifest version from {remote.hostname}.",
    )
    if output != str(MANIFEST_VERSION):
        raise DeploymentError(
            f"Remote code reads manifest version {output}, but this release "
            f"writes version {MANIFEST_VERSION}. Deploy without --skip-code."
        )


def _upload_metadata(
    remote: _Remote,
    options: DeployOptions,
    auth_snapshot_path: Path,
    release_info_path: Path,
) -> None:
    """Stage metadata beside its final path, to be published only on success."""
    release_info = artifacts.read_release_info(release_info_path)
    with tempfile.TemporaryDirectory() as staging:
        version_path = Path(staging) / VERSION_FILENAME
        version_path.write_text(_version_file_contents(release_info), encoding="utf-8")
        uploads = (
            (auth_snapshot_path, remote.auth_snapshot, "auth snapshot"),
            (release_info_path, remote.release_info, "release metadata"),
            (version_path, remote.version_file, "version marker"),
        )
        for source, destination, label in uploads:
            _rsync_file(
                _Transfer(
                    source=source,
                    destination=_rsync_remote_target(
                        remote.hostname, f"{destination}.tmp"
                    ),
                    options=("-a", "-z", "-s"),
                    error_message=f"Failed to upload {label} to {remote.hostname}.",
                ),
                remote,
                options,
            )


def _version_file_contents(release_info: artifacts.ReleaseInfo) -> str:
    """What replaces `git log` for forensics on a host that has no git."""
    fields = {
        "release_id": release_info.release_id or "",
        "release_dir": release_info.release_dir_name,
        "commit_sha": release_info.commit_sha,
        "dirty": "true" if release_info.dirty else "false",
        "date": release_info.date,
        "upstream_version": release_info.upstream_version,
        "client_build_id": release_info.client_build_id or "",
    }
    return "".join(f"{key}={value}\n" for key, value in fields.items())


def _provision_if_bootstrapping(
    hostname: str,
    remote_root: str,
    settings: Settings,
    inventory: RuntimeInventory | None,
    options: DeployOptions,
) -> None:
    """Bring a bare host up to the point where a release can land on it.

    Imported lazily to avoid the provisioning module's dependency on this one.
    """
    if not options.bootstrap:
        return

    from sing_box_manager.provision import provision_remote

    if inventory is None:
        raise DeploymentError("Bootstrap requires a runtime config path.")

    provision_remote(
        hostname=hostname,
        remote_root=remote_root,
        settings=settings,
        inventory=inventory,
        certbot_email=options.certbot_email,
        dry_run=options.dry_run,
    )


def _load_deploy_inventory(settings: Settings) -> RuntimeInventory | None:
    if settings.config_path is None:
        return None

    from sing_box_manager.settings import load_runtime_inventory

    return load_runtime_inventory(_resolve_local_project_path(settings.config_path))


def _sync_remote_config(
    remote: _Remote,
    settings: Settings,
    inventory: RuntimeInventory | None,
    options: DeployOptions,
) -> None:
    """Publish the local derived config without rotating the server secret."""
    if inventory is None:
        return

    from sing_box_manager.provision import _derive_remote_config

    target_path = os.fspath(Path(remote.root) / REMOTE_CONFIG_RELATIVE_PATH)
    staged_path = os.fspath(Path(remote.root) / ".sbm-runtime-config.deploy.tmp")

    print("Synchronizing server runtime configuration...")
    with tempfile.TemporaryDirectory() as staging:
        local_path = Path(staging) / "runtime.yaml"
        local_path.write_text(
            _derive_remote_config(settings, inventory), encoding="utf-8"
        )
        local_path.chmod(0o600)
        _rsync_file(
            _Transfer(
                source=local_path,
                destination=_rsync_remote_target(remote.hostname, staged_path),
                options=("-a", "-z", "-s"),
                error_message=(
                    f"Failed to stage runtime configuration on {remote.hostname}."
                ),
            ),
            remote,
            options,
        )

    command = (
        f"cd {shlex.quote(remote.root)} && sudo "
        f"{shlex.quote(remote.pixi_python_path)} "
        "-m sing_box_manager.remote_config "
        f"{shlex.quote(staged_path)} {shlex.quote(target_path)} "
        f"{shlex.quote(remote.root)}"
    )
    _run_remote_command(
        remote.hostname,
        command,
        f"Failed to publish runtime configuration on {remote.hostname}.",
        dry_run=options.dry_run,
    )


def _install_services(
    remote: _Remote, protocols: Sequence[str], options: DeployOptions
) -> None:
    service_name = "sing-box.service"
    flags = "".join(f" -p {shlex.quote(protocol)}" for protocol in protocols)
    print("Installing single sing-box node service...")
    _run_remote_command(
        remote.hostname,
        f"cd {shlex.quote(remote.server_dir)} && ./server-install.sh{flags}",
        f"Failed to install sing-box on {remote.hostname}.",
        dry_run=options.dry_run,
    )
    _run_remote_command(
        remote.hostname,
        f"sudo systemctl is-active --quiet {service_name}",
        f"{service_name} is not active after deployment on {remote.hostname}.",
        dry_run=options.dry_run,
    )


def _publish_metadata(remote: _Remote, options: DeployOptions) -> None:
    moves = " && ".join(
        f"mv -f {shlex.quote(f'{path}.tmp')} {shlex.quote(path)}"
        for path in (remote.auth_snapshot, remote.release_info, remote.version_file)
    )
    _run_remote_command(
        remote.hostname,
        moves,
        f"Failed to publish generated metadata on {remote.hostname}.",
        dry_run=options.dry_run,
    )


def _lock_down_remote_tree(remote: _Remote, options: DeployOptions) -> None:
    """Make root own the remote root, readable but not writable by the service.

    Root runs code from this tree during a deploy (the runtime config publisher,
    `server-install.sh`, a remote `gc`) and installs the sing-box binary from its
    store, which the protocol services then run as root. If the service account
    could write here, a compromise of the internet-facing portal would become
    root at the next deploy.

    Bytecode caches are deleted rather than re-owned. The code sync excludes
    them, so one the service account wrote while it owned the tree would outlive
    the sync and be imported by root.
    """
    root = shlex.quote(remote.root)
    package = shlex.quote(os.fspath(Path(remote.root) / "sing_box_manager"))
    _run_remote_command(
        remote.hostname,
        (
            f"sudo find {package} -name __pycache__ -type d -prune "
            "-exec rm -rf {} + && "
            f"sudo chown -R root:{SERVICE_USER} {root} && "
            f"sudo chmod -R u+rwX,g+rX,g-w,o-rwx {root}"
        ),
        f"Failed to lock down the deployed tree on {remote.hostname}.",
        dry_run=options.dry_run,
    )


def _install_managed_units(
    hostname: str,
    remote_root: str,
    settings: Settings,
    options: DeployOptions,
) -> None:
    """Refresh the systemd units this project owns on the remote.

    Imported lazily to avoid the provisioning module's dependency on this one.
    """
    from sing_box_manager.provision import install_managed_units

    install_managed_units(
        hostname=hostname,
        remote_root=remote_root,
        settings=settings,
        dry_run=options.dry_run,
    )


def _install_nginx_log_redaction(hostname: str, options: DeployOptions) -> None:
    """Keep subscription tokens out of nginx's access log, on every deploy.

    Imported lazily to avoid the provisioning module's dependency on this one.
    """
    from sing_box_manager.provision import install_nginx_log_redaction

    install_nginx_log_redaction(hostname, dry_run=options.dry_run)


def _restart_web_service(remote: _Remote, options: DeployOptions) -> None:
    if options.dry_run or not _remote_unit_exists(remote.hostname, WEB_SERVICE_NAME):
        return

    print(f"Restarting {WEB_SERVICE_NAME}...")
    _run_remote_command(
        remote.hostname,
        f"sudo systemctl restart {shlex.quote(WEB_SERVICE_NAME)}",
        f"Failed to restart {WEB_SERVICE_NAME} on {remote.hostname}.",
    )
    _run_remote_command(
        remote.hostname,
        f"sudo systemctl is-active --quiet {shlex.quote(WEB_SERVICE_NAME)}",
        f"{WEB_SERVICE_NAME} is not active after deployment on {remote.hostname}.",
    )


def _verify_traffic_stats_collector(
    remote: _Remote, settings: Settings, options: DeployOptions
) -> None:
    """Run one collection under systemd and fail the deploy if it does not pass.

    The collector is a timer-driven oneshot, so nothing else in a deploy ever
    exercises it: the portal comes up healthy while the collection that feeds
    it has been failing every five minutes since the sync. Starting the unit
    here runs it the way the timer will -- same user, same sandboxing, same
    state directory -- and `systemctl start` on a oneshot waits for the exit
    code, so a broken collector surfaces now instead of in the journal.
    """
    if (
        options.dry_run
        or not settings.traffic_stats.enabled
        or not _remote_unit_exists(remote.hostname, TRAFFIC_STATS_SERVICE_NAME)
    ):
        return

    print("Verifying the traffic stats collector...")
    _run_remote_command(
        remote.hostname,
        f"sudo systemctl start {shlex.quote(TRAFFIC_STATS_SERVICE_NAME)}",
        (
            f"{TRAFFIC_STATS_SERVICE_NAME} failed on {remote.hostname}. "
            f"Run `journalctl -u {TRAFFIC_STATS_SERVICE_NAME} -n 50` there "
            "for the collector's own error."
        ),
    )
    _verify_traffic_stream_daemon(remote, settings, options)


def _verify_traffic_stream_daemon(
    remote: _Remote, settings: Settings, options: DeployOptions
) -> None:
    """Restart the stream daemon and confirm it stays up.

    The oneshot above cannot cover this one: a long-running unit that crashes on
    startup is restarted forever by systemd without ever failing a deploy, so
    the domain breakdown would quietly stop filling while everything else looks
    healthy. Restarting also picks up the new code, which a running daemon would
    otherwise keep ignoring until the next reboot.
    """
    if (
        options.dry_run
        or not settings.traffic_stats.enabled
        or not _remote_unit_exists(remote.hostname, TRAFFIC_STREAM_SERVICE_NAME)
    ):
        return

    print("Verifying the connection stats stream...")
    _run_remote_command(
        remote.hostname,
        f"sudo systemctl restart {shlex.quote(TRAFFIC_STREAM_SERVICE_NAME)}",
        f"Failed to restart {TRAFFIC_STREAM_SERVICE_NAME} on {remote.hostname}.",
    )
    # Give it long enough to fail on a bad config rather than catching it in the
    # window before its first connection attempt.
    _run_remote_command(
        remote.hostname,
        f"sleep 5 && sudo systemctl is-active --quiet "
        f"{shlex.quote(TRAFFIC_STREAM_SERVICE_NAME)}",
        (
            f"{TRAFFIC_STREAM_SERVICE_NAME} is not active on {remote.hostname}. "
            f"Run `journalctl -u {TRAFFIC_STREAM_SERVICE_NAME} -n 50` there "
            "for the daemon's own error."
        ),
    )


def _publish_deployment_info(
    remote: _Remote,
    options: DeployOptions,
    release_info: artifacts.ReleaseInfo,
) -> None:
    """Publish the timestamp only after every deployment health check passed."""
    if options.dry_run:
        return
    if release_info.release_id is None:
        # `release_id` is provenance, and older release-info.json files predate
        # it. Everything has already been synced, installed and restarted by the
        # time this runs, so a missing id must not turn a healthy deploy into a
        # failure -- the portal already hides the timestamp when it cannot match
        # the marker to the served release.
        print(
            "Skipping deployment metadata: release metadata has no release_id. "
            "Rebuild with `sbm release` to record the last-deployed timestamp."
        )
        return

    deployment_info = artifacts.DeploymentInfo(
        release_id=release_info.release_id,
        commit_sha=release_info.commit_sha,
        dirty=release_info.dirty,
        deployed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        client_build_id=release_info.client_build_id,
    )
    with tempfile.TemporaryDirectory() as staging:
        local_path = Path(staging) / artifacts.DEPLOYMENT_INFO_FILENAME
        artifacts.write_deployment_info(local_path, deployment_info)
        _rsync_file(
            _Transfer(
                source=local_path,
                destination=_rsync_remote_target(
                    remote.hostname, f"{remote.deployment_info}.tmp"
                ),
                options=("-a", "-z", "-s"),
                error_message=(
                    f"Failed to upload deployment metadata to {remote.hostname}."
                ),
            ),
            remote,
            options,
        )
    staged = shlex.quote(f"{remote.deployment_info}.tmp")
    _run_remote_command(
        remote.hostname,
        (
            # This lands after the tree was locked down, so it gets the same
            # ownership explicitly instead of the uploader's.
            f"sudo chown root:{SERVICE_USER} {staged} && "
            f"sudo chmod 0640 {staged} && "
            f"mv -f {staged} {shlex.quote(remote.deployment_info)}"
        ),
        f"Failed to publish deployment metadata on {remote.hostname}.",
    )


def _resolve_local_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return artifacts.PROJECT_ROOT / candidate


def _resolve_remote_project_path(remote_root: str, path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return os.fspath(candidate)
    return os.fspath(Path(remote_root) / candidate)


def _load_release_info(path: Path) -> artifacts.ReleaseInfo:
    _require_file(path, f"Release metadata not found: {path}. Run `sbm release` first.")
    try:
        return artifacts.read_release_info(path)
    except (OSError, ValueError) as exc:
        raise DeploymentError(f"Failed to read release metadata: {path}") from exc


def _require_file(path: Path, error_message: str) -> None:
    if not path.is_file():
        raise DeploymentError(error_message)


def _require_directory(path: Path, error_message: str) -> None:
    if not path.is_dir():
        raise DeploymentError(error_message)


def _normalize_hostname(hostname: str) -> str:
    normalized = hostname.strip()
    if not normalized:
        raise DeploymentError("Remote hostname must be a non-empty string.")
    return normalized


def _normalize_remote_root(remote_root: str) -> str:
    normalized = remote_root.strip()
    if not normalized:
        raise DeploymentError("Remote root must be a non-empty path.")
    return normalized


def _normalize_protocols(protocols: Sequence[str] | None) -> tuple[str, ...]:
    # Leave the installer default intact: a release may omit unused Naive.
    requested = protocols or ()
    normalized_protocols: list[str] = []
    for protocol in requested:
        normalized = protocol.strip()
        if normalized not in SUPPORTED_PROTOCOLS:
            supported = ", ".join(SUPPORTED_PROTOCOLS)
            raise DeploymentError(
                f"Unsupported protocol `{protocol}`. Supported protocols: {supported}."
            )
        if normalized not in normalized_protocols:
            normalized_protocols.append(normalized)

    return tuple(normalized_protocols)


def _parse_rsync_version(output: str) -> str:
    match = RSYNC_VERSION_PATTERN.search(output)
    if match is None:
        raise DeploymentError(f"Failed to parse rsync version from output: {output!r}")
    return match.group(1)


def _local_rsync_version() -> str:
    output = _run_command_stdout(
        ["rsync", "--version"],
        "Failed to read local rsync version.",
    )
    return _parse_rsync_version(output)


def _remote_rsync_version(hostname: str, remote_rsync_path: str) -> str:
    output = _run_remote_stdout(
        hostname,
        f"{shlex.quote(remote_rsync_path)} --version",
        f"Failed to read remote rsync version from {hostname}.",
    )
    return _parse_rsync_version(output)


def _ensure_rsync_versions_match(hostname: str, remote_rsync_path: str) -> None:
    print("Verifying rsync versions match...")
    local_version = _local_rsync_version()
    remote_version = _remote_rsync_version(hostname, remote_rsync_path)
    if local_version != remote_version:
        raise DeploymentError(
            f"Local rsync ({local_version}) does not match remote rsync "
            f"({remote_version}) at {remote_rsync_path} on {hostname}. "
            "Deploy with --bootstrap, or run `pixi install` on the remote, "
            "to align versions."
        )


def _remote_unit_exists(hostname: str, unit_name: str) -> bool:
    output = _run_remote_stdout(
        hostname,
        (
            f"if systemctl cat {shlex.quote(unit_name)} >/dev/null 2>&1; "
            "then printf 1; else printf 0; fi"
        ),
        f"Failed to inspect {unit_name} on {hostname}.",
    )
    return output == "1"


def _rsync_remote_target(hostname: str, remote_path: str) -> str:
    return f"{hostname}:{remote_path}"


def remote_python_command(remote_root: str, arguments: Sequence[str]) -> str:
    """Run an `sbm` subcommand through the remote's own interpreter.

    Addressed by absolute path rather than through `pixi run`: remote commands
    go through a non-login shell where pixi may not be on PATH. `cd` first so
    relative paths resolve against the project root.
    """
    interpreter = os.fspath(Path(remote_root) / REMOTE_PIXI_PYTHON_RELATIVE_PATH)
    invocation = " ".join(
        shlex.quote(part)
        for part in (
            interpreter,
            "-m",
            "sing_box_manager",
            "--config",
            REMOTE_CONFIG_RELATIVE_PATH,
            *arguments,
        )
    )
    return f"cd {shlex.quote(remote_root)} && {invocation}"


def run_remote_command(hostname: str, remote_command: str, error_message: str) -> None:
    """Run one command on the remote, raising DeploymentError if it fails."""
    _run_remote_command(hostname, remote_command, error_message)


def _run_remote_command(
    hostname: str,
    remote_command: str,
    error_message: str,
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"  [dry-run] ssh {hostname} {remote_command}")
        return
    _run_command(ssh.command(hostname, remote_command, tty=True), error_message)


def _run_remote_stdout(hostname: str, remote_command: str, error_message: str) -> str:
    return _run_command_stdout(ssh.command(hostname, remote_command), error_message)


def _run_command(command: list[str], error_message: str) -> None:
    """Run a command, retrying handshakes that died before the remote ran.

    stderr is captured only so `ssh.with_connection_retry` can classify it, and
    replayed either way -- an ssh warning the user would have seen live before
    must not vanish into the classifier.
    """

    def attempt() -> None:
        completed = subprocess.run(
            command, check=True, stderr=subprocess.PIPE, text=True
        )
        ssh.echo_stderr(completed.stderr)

    try:
        ssh.with_connection_retry(attempt)
    except FileNotFoundError as exc:
        raise DeploymentError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(error_message) from exc


def _rsync_command(
    transfer: _Transfer, remote: _Remote, options: DeployOptions
) -> list[str]:
    command: list[str] = [
        "rsync",
        *transfer.options,
        "-e",
        ssh.rsync_transport(remote.hostname),
    ]
    if remote.rsync_path:
        command.extend(["--rsync-path", remote.rsync_path])
    if options.dry_run:
        # `--dry-run` alongside `--delete` is how you confirm no delete rule
        # reaches server-local state; `--stats` turns that into a byte estimate.
        command.extend(["--dry-run", "--stats"])
    elif options.show_progress:
        command.append("--info=progress2")
    return command


def _rsync_directory(
    transfer: _Transfer, remote: _Remote, options: DeployOptions
) -> None:
    command = _rsync_command(transfer, remote, options)
    command.extend([f"{transfer.source}/", f"{transfer.destination}/"])

    if options.dry_run:
        print(f"  [dry-run] {shlex.join(command)}")
        _run_command(command, transfer.error_message)
        return

    if options.show_progress:
        _run_rsync_directory_with_progress(
            command, transfer.error_message, transfer.description
        )
        return

    _run_command(command, transfer.error_message)


def _rsync_file(transfer: _Transfer, remote: _Remote, options: DeployOptions) -> None:
    command = _rsync_command(transfer, remote, options)
    command.extend([str(transfer.source), transfer.destination])
    if options.dry_run:
        print(f"  [dry-run] {shlex.join(command)}")
    _run_command(command, transfer.error_message)


def should_show_deploy_progress(stdout: TextIO | None = None) -> bool:
    """Return whether deploy should render interactive progress."""
    output_stream = stdout or sys.stdout
    return output_stream.isatty()


def _run_rsync_directory_with_progress(
    command: list[str], error_message: str, description: str
) -> None:
    def attempt() -> None:
        _stream_rsync_progress(command, description)

    try:
        ssh.with_connection_retry(attempt)
    except FileNotFoundError as exc:
        raise DeploymentError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(error_message) from exc


def _stream_rsync_progress(command: list[str], description: str) -> None:
    """One rsync run, rendering `--info=progress2` as a bar.

    stderr goes to a temporary file rather than a second pipe: the loop below
    drains stdout a byte at a time until EOF, and a pipe filling up unread in
    the meantime would deadlock the transfer. It is collected rather than
    inherited so a failed run can be classified as a lost handshake or not.
    """
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace"
    ) as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            bufsize=1,
        )

        stdout = process.stdout
        if stdout is None:
            raise subprocess.CalledProcessError(1, command, stderr="")

        console = Console(file=sys.stdout)
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("[bold cyan]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task(description, total=100)
            buffer = ""
            while chunk := stdout.read(1):
                if chunk in {"\r", "\n"}:
                    percent = _parse_rsync_progress_percent(buffer)
                    if percent is not None:
                        progress.update(task_id, completed=percent)
                    buffer = ""
                    continue

                buffer += chunk
                percent = _parse_rsync_progress_percent(buffer)
                if percent is not None:
                    progress.update(task_id, completed=percent)

            percent = _parse_rsync_progress_percent(buffer)
            if percent is not None:
                progress.update(task_id, completed=percent)

            returncode = process.wait()
            errors.seek(0)
            stderr = errors.read()
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, command, stderr=stderr)
            progress.update(task_id, completed=100)

    ssh.echo_stderr(stderr)


def _parse_rsync_progress_percent(text: str) -> int | None:
    match = RSYNC_PROGRESS_PATTERN.search(text)
    if match is None:
        return None

    return min(int(match.group(1)), 100)


def _run_command_stdout(command: list[str], error_message: str) -> str:
    def attempt() -> str:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    try:
        return ssh.with_connection_retry(attempt)
    except FileNotFoundError as exc:
        raise DeploymentError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        ssh.echo_stderr(exc.stderr)
        raise DeploymentError(error_message) from exc
