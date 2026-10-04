"""Shared data models."""

from dataclasses import dataclass


@dataclass
class User:
    """A sing-box user with credentials."""

    name: str
    password: str
