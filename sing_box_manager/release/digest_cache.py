"""Shared sha256 helpers for the content-addressed local caches.

Both the upstream release-asset cache and the custom server-binary cache record a
verified digest in a ``<name>.sha256`` sidecar so a later run can skip the work.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
SHA256_DIGEST_PREFIX = "sha256:"
SHA256_HEX_DIGITS = frozenset("0123456789abcdef")
SHA256_HEX_LENGTH = 64


def is_sha256_hex(value: str) -> bool:
    """Return whether a string is a well-formed lowercase sha256 hex digest."""
    return len(value) == SHA256_HEX_LENGTH and all(
        character in SHA256_HEX_DIGITS for character in value
    )


def sha256_for_path(path: Path) -> str:
    """Compute the sha256 digest of a file without loading it into memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_for_bytes(payload: bytes) -> str:
    """Compute the sha256 digest of an in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def sha256_for_text(value: str) -> str:
    """Compute the sha256 digest of a UTF-8 string."""
    return sha256_for_bytes(value.encode("utf-8"))


def digest_sidecar_path(path: Path) -> Path:
    """Return the sidecar file used to cache a verified digest for a path."""
    return path.with_name(f"{path.name}.sha256")


def load_cached_digest(path: Path) -> str | None:
    """Read a cached digest, discarding malformed sidecars."""
    sidecar = digest_sidecar_path(path)
    if not sidecar.is_file():
        return None

    digest = sidecar.read_text(encoding="utf-8").strip().lower()
    if not is_sha256_hex(digest):
        sidecar.unlink(missing_ok=True)
        return None

    return digest


def write_cached_digest(path: Path, digest: str) -> None:
    """Persist a verified digest for future cached reuse."""
    digest_sidecar_path(path).write_text(f"{digest}\n", encoding="utf-8")
