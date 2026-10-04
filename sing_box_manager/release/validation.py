"""Validate rendered configs without exposing credentials or requiring VPS TLS files."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _check_certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "config-check.invalid")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "check.crt", directory / "check.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def check_config(
    binary: Path, config: dict, *, label: str, server: bool = False
) -> None:
    """Check structure and build features. The installer checks actual VPS TLS paths.

    CA certificate paths usually exist only on the node. Substitute an ephemeral
    ECDSA certificate in a private validation copy when those files are absent.
    Inline/self-signed material is always checked as rendered. Never print the
    config or sing-box output: either can contain credentials.
    """
    with tempfile.TemporaryDirectory(prefix="sbm-config-check-") as temporary:
        directory = Path(temporary)
        candidate = copy.deepcopy(config)
        if server:
            for inbound in candidate.get("inbounds", []):
                tls = inbound.get("tls", {})
                if (
                    tls.get("enabled")
                    and "certificate_path" in tls
                    and (
                        not Path(tls["certificate_path"]).is_file()
                        or not Path(tls["key_path"]).is_file()
                    )
                ):
                    certificate, key = _check_certificate(directory)
                    tls.update(certificate_path=str(certificate), key_path=str(key))
        path = directory / "config.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(candidate), encoding="utf-8")
        try:
            result = subprocess.run(
                [str(binary.resolve()), "check", "-D", str(directory), "-c", str(path)],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"Unable to validate rendered config {label}") from exc
        if result.returncode:
            raise ValueError(
                f"sing-box check failed for rendered config {label} (exit {result.returncode})"
            )
