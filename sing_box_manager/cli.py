"""CLI interface for sing-box-manager."""

from __future__ import annotations

import importlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import uvicorn

tyro: Any = importlib.import_module("tyro")

if TYPE_CHECKING:
    from sing_box_manager.settings import Settings


CLI_DESCRIPTION = "sing-box-manager: Deploy, package and distribute sing-box."


@dataclass
class ServeCommand:
    """Start the authenticated download web service."""

    port: int | None = None
    host: str | None = None


@dataclass
class ReleaseCommand:
    """Build release packages for all users and platforms."""


@dataclass
class DeployCommand:
    """Deploy the project code and the active release to a remote host."""

    hostname: str
    remote_root: str = "/opt/sing-box-manager"
    protocol: list[str] = field(default_factory=list)
    """Protocols to install. Defaults to all supported protocols."""
    bootstrap: bool = False
    """Set up a fresh host: provision the VPS (create user, directory, install
    nginx/certbot, configure firewall, ship config), sync code, install the
    pixi environment, and enable all managed systemd services."""
    skip_code: bool = False
    """Sync artifacts only, leaving the remote code tree as it is."""
    dry_run: bool = False
    """Print every command and let rsync report what it would transfer or
    delete, without changing anything on the remote."""
    certbot_email: str | None = None
    """Email for Let's Encrypt certificate notifications. Used only during
    bootstrap. Omit to register without an email address."""


@dataclass
class GcCommand:
    """Delete old releases and the store entries nothing points at."""

    keep: int = 3
    """How many recent releases to keep. The active one is always kept."""
    remote: str | None = None
    """Collect on this host instead of locally, using the code deploy shipped."""
    remote_root: str = "/opt/sing-box-manager"
    dry_run: bool = False
    """Report what would be deleted without deleting it."""


@dataclass
class StatsCollectCommand:
    """Collect local per-user traffic stats from sing-box services."""


@dataclass
class StatsStreamCommand:
    """Stream per-connection traffic stats from sing-box until stopped.

    Runs in the foreground; systemd owns its lifetime in production. Unlike
    stats-collect this cannot be run on a timer, because sing-box drops
    connection events whenever nobody is subscribed.
    """


Command = (
    Annotated[ServeCommand, tyro.conf.subcommand(name="serve")]
    | Annotated[ReleaseCommand, tyro.conf.subcommand(name="release")]
    | Annotated[DeployCommand, tyro.conf.subcommand(name="deploy")]
    | Annotated[GcCommand, tyro.conf.subcommand(name="gc")]
    | Annotated[StatsCollectCommand, tyro.conf.subcommand(name="stats-collect")]
    | Annotated[StatsStreamCommand, tyro.conf.subcommand(name="stats-stream")]
)


@dataclass
class Cli:
    command: Annotated[Command, tyro.conf.OmitSubcommandPrefixes]
    config: Path | None = None
    """Path to the unified config file (SOPS-encrypted or plaintext YAML).
    Falls back to the SBM_CONFIG environment variable."""


def _configure_logging(log_format: str) -> None:
    """Log at INFO, except for httpx.

    httpx logs every request URL at INFO, and the KiwiVM API takes its key in
    the query string, so its request lines would put that key in the journal.
    """
    logging.basicConfig(
        level=logging.INFO, format=log_format, handlers=[logging.StreamHandler()]
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _run_serve(command: ServeCommand, settings: Settings) -> None:
    _configure_logging("%(asctime)s - %(levelname)s - %(message)s")

    from sing_box_manager.subscription import TokenRedactingFilter
    from sing_box_manager.web.app import create_app

    # uvicorn logs each request path, and a subscription link's path is its
    # credential. Its logging setup keeps filters already on the logger.
    logging.getLogger("uvicorn.access").addFilter(TokenRedactingFilter())

    web_app = create_app(settings)
    uvicorn.run(
        web_app,
        host=command.host or settings.web.host,
        port=command.port or settings.web.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


def _run_release(_: ReleaseCommand, settings: Settings) -> None:
    from sing_box_manager.release.builder import ReleaseBuilder
    from sing_box_manager.release.errors import ReleaseError
    from sing_box_manager.release.ui import create_release_reporter

    reporter = create_release_reporter()

    try:
        ReleaseBuilder(settings, reporter=reporter).build()
    except ReleaseError as exc:
        reporter.fail(str(exc))
        raise SystemExit(1) from exc


def _run_deploy(command: DeployCommand, settings: Settings) -> None:
    from sing_box_manager.deploy import (
        DeploymentError,
        DeployOptions,
        deploy_release,
        should_show_deploy_progress,
    )

    try:
        deploy_release(
            hostname=command.hostname,
            protocols=command.protocol or None,
            remote_root=command.remote_root,
            settings=settings,
            options=DeployOptions(
                show_progress=should_show_deploy_progress(),
                bootstrap=command.bootstrap,
                skip_code=command.skip_code,
                dry_run=command.dry_run,
                certbot_email=command.certbot_email,
            ),
        )
    except DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _run_gc(command: GcCommand, settings: Settings) -> None:
    from sing_box_manager.release.gc import GarbageCollectionError, collect_garbage

    if command.remote is not None:
        _run_remote_gc(command, command.remote)
        return

    try:
        result = collect_garbage(settings, keep=command.keep, dry_run=command.dry_run)
    except GarbageCollectionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    prefix = "Would remove" if result.dry_run else "Removed"
    for note in result.notes:
        print(note)
    print(f"Kept releases: {', '.join(result.kept_releases) or 'none'}")
    print(f"{prefix} releases: {', '.join(result.removed_releases) or 'none'}")
    print(f"{prefix} {len(result.removed_store_entries)} store entries")
    if result.removed_upstream:
        print(f"{prefix} {len(result.removed_upstream)} extracted upstream trees")
    print(f"{prefix} {result.reclaimed_bytes / 1_000_000:.1f} MB of release data")


def _run_remote_gc(command: GcCommand, hostname: str) -> None:
    """Run the same collection on the remote, using the code deploy shipped.

    The remote holds the same store and the same release directories, and it is
    the host that actually runs out of disk. Since `sbm deploy` now puts this
    code there, the honest implementation is to run the identical verb rather
    than reimplement the reference walk over ssh.
    """
    from sing_box_manager.deploy import (
        DeploymentError,
        remote_python_command,
        run_remote_command,
    )

    arguments = ["gc", "--keep", str(command.keep)]
    if command.dry_run:
        arguments.append("--dry-run")

    try:
        run_remote_command(
            hostname,
            remote_python_command(command.remote_root, arguments),
            f"Failed to collect garbage on {hostname}.",
        )
    except DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _run_stats_collect(_: StatsCollectCommand, settings: Settings) -> None:
    from sing_box_manager.traffic_stats import TrafficStatsError, collect_traffic_stats

    try:
        result = collect_traffic_stats(settings)
    except TrafficStatsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if result.failed_services:
        print("Partial traffic stats collection:", file=sys.stderr)
        for failure in result.failed_services:
            print(f"- {failure}", file=sys.stderr)

    successful = ", ".join(result.successful_services) or "0 services"
    print(
        f"Collected traffic stats into {result.database_path} ({successful})",
        file=sys.stdout,
    )


def _run_stats_stream(_: StatsStreamCommand, settings: Settings) -> None:
    from sing_box_manager.connection_stats import collect_connection_stream
    from sing_box_manager.traffic_stats import TrafficStatsError

    # systemd captures stdout, and the daemon's only output is its log.
    _configure_logging("%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        collect_connection_stream(settings)
    except TrafficStatsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _dispatch(command: Command, settings: Settings) -> None:
    if isinstance(command, ServeCommand):
        _run_serve(command, settings)
        return

    if isinstance(command, ReleaseCommand):
        _run_release(command, settings)
        return

    if isinstance(command, GcCommand):
        _run_gc(command, settings)
        return

    if isinstance(command, StatsCollectCommand):
        _run_stats_collect(command, settings)
        return

    if isinstance(command, StatsStreamCommand):
        _run_stats_stream(command, settings)
        return

    _run_deploy(command, settings)


def _resolve_config_path(cli_config: Path | None) -> Path:
    if cli_config is not None:
        return cli_config

    env_config = os.environ.get("SBM_CONFIG")
    if env_config:
        return Path(env_config)

    print(
        "Error: --config flag or SBM_CONFIG environment variable is required.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    cli = tyro.cli(
        Cli,
        args=argv,
        prog="sbm",
        description=CLI_DESCRIPTION,
    )
    config_path = _resolve_config_path(cli.config)
    from sing_box_manager.settings import load_settings

    try:
        settings = load_settings(config_path)
    except Exception as exc:
        print(f"Failed to load settings: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    _dispatch(cli.command, settings)


if __name__ == "__main__":
    main()
