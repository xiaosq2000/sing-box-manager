"""Helpers for loading optional local environment variables."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_local_env(path: Path | None = None) -> None:
    """Load the repo-local .env file without overriding exported variables."""
    load_dotenv(dotenv_path=path or DEFAULT_DOTENV_PATH, override=False)
