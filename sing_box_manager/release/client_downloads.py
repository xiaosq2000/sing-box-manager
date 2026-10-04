"""The files sbc downloads, and the signed manifest that vouches for them.

sbc installs a download only when its SHA-256 matches a manifest signed with
`client_signing_key`, an ed25519 key that stays on the build machine. A server
that is broken into can serve other files, but it cannot sign for them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sing_box_manager.release.artifacts import ReleaseInfo
from sing_box_manager.release.manifest import MANIFEST_FILENAME, Manifest

CLIENT_DIR_NAME = "client"
CLIENT_MANIFEST_NAME = "manifest.json"
CLIENT_SIGNATURE_NAME = "manifest.json.sig"
CLIENT_MANIFEST_VERSION = 1
# The desktop platforms sbc runs on. Every platform's sbc is served as
# `sbc/<platform>/sbc`; the Windows installer saves it as sbc.exe.
CLIENT_PLATFORMS = (
    "linux-amd64",
    "linux-arm64",
    "darwin-amd64",
    "darwin-arm64",
    "windows-amd64",
)
_SEED_BYTES = 32


def _private_key(seed_b64: str) -> Ed25519PrivateKey:
    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except ValueError as exc:
        raise ValueError("client_signing_key must be base64") from exc
    if len(seed) != _SEED_BYTES:
        raise ValueError("client_signing_key must hold 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key(seed_b64: str) -> str:
    """Return the base64 public key sbc carries to check signatures."""
    raw = (
        _private_key(seed_b64).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    return base64.b64encode(raw).decode("ascii")


def sign(seed_b64: str, payload: bytes) -> str:
    """Return the base64 ed25519 signature of `payload`.

    ed25519 signatures are deterministic, so unchanged content signs to the
    same bytes and an unchanged release keeps its id.
    """
    return base64.b64encode(_private_key(seed_b64).sign(payload)).decode("ascii")


def generate_key() -> str:
    """Return a new base64 private key for `client_signing_key`."""
    return base64.b64encode(os.urandom(_SEED_BYTES)).decode("ascii")


def sbc_version(release_info: ReleaseInfo) -> str:
    """Name an sbc build after the commit it was built from."""
    version = release_info.commit_sha[:12]
    return f"{version}-dirty" if release_info.dirty else version


def build_sbc(project_root: Path, platform: str, version: str, output: Path) -> None:
    """Cross-compile sbc for one platform.

    `-trimpath` and a fixed toolchain make the output depend only on the
    source, so an unchanged build keeps its digest.
    """
    goos, goarch = platform.split("-", 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "go",
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-ldflags",
            f"-s -w -X main.version={version}",
            "-o",
            str(output),
            "./cmd/sbc",
        ],
        cwd=project_root,
        env={
            **os.environ,
            "GOOS": goos,
            "GOARCH": goarch,
            "CGO_ENABLED": "0",
            "GOTOOLCHAIN": "local",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"go build for {platform} failed: {result.stderr.strip() or result.stdout}"
        )


def client_manifest(
    files: list[tuple[str, int, str]], *, sbc: str, sing_box: str
) -> bytes:
    """Return the manifest sbc checks, listing each (path, size, sha256)."""
    payload = {
        "version": CLIENT_MANIFEST_VERSION,
        "sbc": sbc,
        "sing_box": sing_box,
        "files": [
            {"path": path, "size": size, "sha256": digest}
            for path, size, digest in sorted(files)
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ClientDownloads:
    """The client files a published release serves, by the path sbc asks for."""

    release_dir: Path
    files: dict[str, str]
    sbc_version: str
    # The SHA-256 of the client manifest, which names this build of sbc,
    # sing-box and the rule sets.
    build_id: str

    def path_for(self, name: str) -> Path | None:
        source = self.files.get(name)
        return None if source is None else self.release_dir / source


def load_client_downloads(release_dir: Path) -> ClientDownloads | None:
    """Return the release's client downloads, or None when it has none."""
    manifest_path = release_dir / MANIFEST_FILENAME
    client_manifest_path = release_dir / CLIENT_DIR_NAME / CLIENT_MANIFEST_NAME
    if not manifest_path.is_file() or not client_manifest_path.is_file():
        return None
    manifest = Manifest.from_json(manifest_path.read_text(encoding="utf-8"))
    if not manifest.client_members:
        return None
    client_manifest = client_manifest_path.read_bytes()
    return ClientDownloads(
        release_dir=release_dir,
        files={member.arcname: member.src for member in manifest.client_members},
        sbc_version=json.loads(client_manifest)["sbc"],
        build_id=hashlib.sha256(client_manifest).hexdigest(),
    )
