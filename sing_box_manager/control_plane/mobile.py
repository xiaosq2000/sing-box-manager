"""Local QR images and the official sing-box remote-profile import URI."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote, urlencode

import segno


@lru_cache(maxsize=128)
def _qr_image(import_uri: str) -> str:
    # Reuse images in memory. The page still authorizes every request.
    return segno.make_qr(import_uri, error="m").png_data_uri(scale=5, border=4)


def mobile_import(subscription_url: str, username: str) -> dict[str, str]:
    """Encode the unified remote-profile URL, not configuration contents."""
    remote_url = f"{subscription_url}?{urlencode({'format': 'mobile'})}"
    name = f"{username} - sing-box"
    # libbox stores the display name in the fragment, not a name query field.
    import_uri = (
        f"sing-box://import-remote-profile?{urlencode({'url': remote_url})}"
        f"#{quote(name, safe='')}"
    )
    return {
        "url": remote_url,
        "import_uri": import_uri,
        "qr": _qr_image(import_uri),
    }
