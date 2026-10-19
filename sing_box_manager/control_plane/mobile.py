"""Local QR images and the official sing-box remote-profile import URI."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote, urlencode

import segno

PROTOCOL_LABELS = {"trojan": "Trojan", "hysteria2": "Hysteria2", "naive": "Naive"}


@lru_cache(maxsize=128)
def _qr_image(import_uri: str) -> str:
    # Reuse images in memory. The page still authorizes every request.
    return segno.make_qr(import_uri, error="m").png_data_uri(scale=5, border=4)


def mobile_imports(
    subscription_url: str, username: str, protocols: list[str]
) -> list[dict[str, str]]:
    """Encode authorized profile URLs, not profile contents or browser-page URLs."""
    profiles = []
    for protocol in protocols:
        label = PROTOCOL_LABELS[protocol]
        remote_url = f"{subscription_url}?{urlencode({'format': 'mobile', 'protocol': protocol})}"
        name = f"{username} - {label}"
        # libbox stores the display name in the fragment, not a name query field.
        import_uri = (
            f"sing-box://import-remote-profile?{urlencode({'url': remote_url})}"
            f"#{quote(name, safe='')}"
        )
        profiles.append(
            {
                "protocol": protocol,
                "label": label,
                "url": remote_url,
                "import_uri": import_uri,
                "qr": _qr_image(import_uri),
            }
        )
    return profiles
