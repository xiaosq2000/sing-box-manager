"""Release-specific error types."""

from __future__ import annotations


class ReleaseError(RuntimeError):
    """Base error raised for user-facing release failures."""


class ReleaseStageError(ReleaseError):
    """A failure that occurred while running a named release stage."""

    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}")
