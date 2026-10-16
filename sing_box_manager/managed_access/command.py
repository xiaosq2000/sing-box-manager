"""Explicit managed-access operations. No credentials are command arguments."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID


@dataclass
class ManagedAccessCommand:
    """Manage database ownership and the minimal public portal."""

    action: Literal[
        "status",
        "prepare",
        "activate",
        "fence",
        "node-plan",
        "node-apply",
        "node-apply-current",
        "serve",
        "health",
    ] = "status"
    namespace: UUID | None = None
    run_dir: Path | None = None
    binary: Path | None = None
    acknowledge_interrupt: bool = False
    """Allow the selected operation to stop the old portal or restart the proxy."""
    require_active: bool = False

    def validate(self) -> None:
        if (self.action == "prepare") != (self.namespace is not None):
            raise ValueError("namespace_requires_prepare")
        if self.action == "node-apply" and self.run_dir is None:
            raise ValueError("node_plan_directory_required")
        if self.binary is not None and self.action not in {
            "node-plan",
            "node-apply-current",
        }:
            raise ValueError("binary_requires_node_plan")
        if self.run_dir is not None and self.action not in {
            "node-plan",
            "node-apply",
            "node-apply-current",
        }:
            raise ValueError("unexpected_run_directory")
        if self.acknowledge_interrupt and self.action not in {
            "node-apply",
            "node-apply-current",
            "fence",
        }:
            raise ValueError("unexpected_interruption_acknowledgement")
        if self.require_active and self.action != "status":
            raise ValueError("active_check_requires_status")
