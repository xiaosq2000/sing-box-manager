"""UI helpers for the release command."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, TextIO

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from sing_box_manager.release.models import ReleasePlan, ReleaseResult


class ReleaseReporter(Protocol):
    """Presentation interface for release progress and summaries."""

    def start(self, plan: ReleasePlan) -> None:
        """Render the initial release plan."""

    def start_stage(self, key: str, title: str, total: int | None = None) -> None:
        """Start rendering a release stage."""

    def advance_stage(
        self, key: str, advance: int = 1, detail: str | None = None
    ) -> None:
        """Advance stage progress."""

    def complete_stage(self, key: str, summary: str | None = None) -> None:
        """Mark a stage as completed."""

    def finish(self, result: ReleaseResult) -> None:
        """Render the final release result."""

    def warn(self, message: str) -> None:
        """Render a non-fatal release warning."""

    def fail(self, message: str) -> None:
        """Render a fatal release error."""


def create_release_reporter(
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> ReleaseReporter:
    """Create a TTY-aware release reporter."""
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    if hasattr(output_stream, "isatty") and output_stream.isatty():
        return RichReleaseReporter(stdout=output_stream, stderr=error_stream)
    return PlainReleaseReporter(stdout=output_stream, stderr=error_stream)


class PlainReleaseReporter:
    """Minimal text reporter used for tests and non-TTY output."""

    def __init__(
        self, *, stdout: TextIO | None = None, stderr: TextIO | None = None
    ) -> None:
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._stage_titles: dict[str, str] = {}

    def start(self, plan: ReleasePlan) -> None:
        print(f"Building sing-box v{plan.version} release", file=self._stdout)
        print(f"Release directory: {plan.release_dir}", file=self._stdout)
        print(f"Config: {plan.config_path}", file=self._stdout)
        print(f"Auth snapshot: {plan.auth_snapshot_path}", file=self._stdout)
        print(
            (
                "Plan: "
                f"{plan.enabled_release_users} release users, "
                f"{plan.enabled_portal_users} portal users, "
                f"default {plan.default_protocol}, "
                f"{len(plan.platforms)} platforms, "
                f"{len(plan.protocols)} protocols, "
                f"{plan.total_expected_archives} archives"
            ),
            file=self._stdout,
        )

    def start_stage(self, key: str, title: str, total: int | None = None) -> None:
        self._stage_titles[key] = title
        print(f"-> {title}", file=self._stdout)

    def advance_stage(
        self, key: str, advance: int = 1, detail: str | None = None
    ) -> None:
        del advance
        del key
        del detail

    def complete_stage(self, key: str, summary: str | None = None) -> None:
        title = self._stage_titles.get(key, key)
        if summary:
            print(f"   done: {title} ({summary})", file=self._stdout)
            return
        print(f"   done: {title}", file=self._stdout)

    def finish(self, result: ReleaseResult) -> None:
        print("Release complete.", file=self._stdout)
        if result.reused_existing_release:
            print("Nothing changed; reused the existing release.", file=self._stdout)
        print(f"Release id: {result.release_id}", file=self._stdout)
        print(f"User archives: {result.user_archive_count}", file=self._stdout)
        print(
            f"Shared payloads: {result.prefixes_built} built, "
            f"{result.prefixes_reused} reused",
            file=self._stdout,
        )
        print(f"Release directory: {result.release_dir}", file=self._stdout)
        print(f"Auth snapshot: {result.auth_snapshot_path}", file=self._stdout)
        print(f"Server package: {result.server_package_dir}", file=self._stdout)
        print(f"Elapsed: {result.elapsed_seconds:.1f}s", file=self._stdout)

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=self._stdout)

    def fail(self, message: str) -> None:
        print(f"Release failed: {message}", file=self._stderr)


class RichReleaseReporter:
    """Rich-powered reporter for interactive release runs."""

    def __init__(
        self, *, stdout: TextIO | None = None, stderr: TextIO | None = None
    ) -> None:
        self._console = Console(file=stdout or sys.stdout)
        self._error_console = Console(file=stderr or sys.stderr)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self._console,
            transient=True,
        )
        self._stage_tasks: dict[str, TaskID] = {}
        self._progress_started = False

    def start(self, plan: ReleasePlan) -> None:
        plan_table = Table.grid(padding=(0, 2))
        plan_table.add_column(style="bold cyan")
        plan_table.add_column()
        plan_table.add_row("Release", f"sing-box v{plan.version}")
        plan_table.add_row("Output", self._format_path(plan.release_dir))
        plan_table.add_row("Config", self._format_path(plan.config_path))
        plan_table.add_row("Auth", self._format_path(plan.auth_snapshot_path))
        plan_table.add_row(
            "Users",
            (
                f"{plan.enabled_release_users} release / "
                f"{plan.enabled_portal_users} portal"
            ),
        )
        plan_table.add_row(
            "Default",
            plan.default_protocol,
        )
        plan_table.add_row(
            "Protocols",
            (
                f"{', '.join(plan.protocols)} "
                f"({plan.enabled_trojan_users} trojan, "
                f"{plan.enabled_hysteria2_users} hysteria2, "
                f"{plan.enabled_naive_users} naive)"
            ),
        )
        plan_table.add_row("Platforms", ", ".join(plan.platforms))
        plan_table.add_row("Packages", str(plan.total_expected_archives))
        self._console.print(
            Panel.fit(plan_table, title="Release Plan", border_style="cyan")
        )
        self._ensure_progress_started()

    def start_stage(self, key: str, title: str, total: int | None = None) -> None:
        task_total = 1 if total is None or total <= 0 else total
        self._ensure_progress_started()
        self._stage_tasks[key] = self._progress.add_task(title, total=task_total)

    def advance_stage(
        self, key: str, advance: int = 1, detail: str | None = None
    ) -> None:
        task_id = self._stage_tasks[key]
        if detail is not None:
            self._progress.update(task_id, advance=advance, description=detail)
            return

        self._progress.update(task_id, advance=advance)

    def complete_stage(self, key: str, summary: str | None = None) -> None:
        task_id = self._stage_tasks[key]
        task = self._progress.tasks[task_id]
        description = summary or task.description
        self._progress.update(task_id, completed=task.total, description=description)

    def finish(self, result: ReleaseResult) -> None:
        self._stop_progress()
        result_table = Table.grid(padding=(0, 2))
        result_table.add_column(style="bold green")
        result_table.add_column()
        result_table.add_row("Release id", result.release_id)
        result_table.add_row("User archives", str(result.user_archive_count))
        result_table.add_row(
            "Shared payloads",
            f"{result.prefixes_built} built, {result.prefixes_reused} reused",
        )
        result_table.add_row("Release dir", self._format_path(result.release_dir))
        result_table.add_row(
            "Auth snapshot", self._format_path(result.auth_snapshot_path)
        )
        result_table.add_row(
            "Server package", self._format_path(result.server_package_dir)
        )
        result_table.add_row("Elapsed", f"{result.elapsed_seconds:.1f}s")
        title = (
            "Release Unchanged"
            if result.reused_existing_release
            else "Release Complete"
        )
        self._console.print(Panel.fit(result_table, title=title, border_style="green"))

    def warn(self, message: str) -> None:
        self._console.print(f"[yellow]Warning:[/yellow] {message}")

    def fail(self, message: str) -> None:
        self._stop_progress()
        self._error_console.print(
            Panel.fit(message, title="Release Failed", border_style="red")
        )

    def _ensure_progress_started(self) -> None:
        if self._progress_started:
            return
        self._progress.start()
        self._progress_started = True

    def _stop_progress(self) -> None:
        if not self._progress_started:
            return
        self._progress.stop()
        self._progress_started = False

    def _format_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)
