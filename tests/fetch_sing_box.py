"""Fetch the sing-box binary that the real-binary tests run.

`sing_box_version` in config/inventory/example.yaml names the release. The
download goes through the release builder, so it is verified against the digest
GitHub publishes and shares the release cache. Run it with
`pixi run fetch-sing-box`.
"""

import os
import platform
import shutil
import sys
from pathlib import Path

from sing_box_manager.release.builder import ReleaseBuilder
from sing_box_manager.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_INVENTORY = ROOT / "config/inventory/example.yaml"
BIN_DIR = ROOT / ".cache/sing-box-manager/bin"
TEST_BINARY = BIN_DIR / "sing-box"

_ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def pinned_version() -> str:
    return load_settings(EXAMPLE_INVENTORY).sing_box_version


def host_platform() -> str:
    system = platform.system().lower()
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if system not in {"linux", "darwin"} or architecture is None:
        sys.exit(
            f"No sing-box test binary for {platform.system()} {platform.machine()}"
        )
    return f"{system}-{architecture}"


def main() -> None:
    settings = load_settings(EXAMPLE_INVENTORY)
    if "--server" in sys.argv[1:]:
        from sing_box_manager.traffic_stats import build_custom_server_binary

        destination = ROOT / ".cache/sing-box-manager/server/sing-box"
        destination.parent.mkdir(parents=True, exist_ok=True)
        build_custom_server_binary(settings, destination)
        print(
            f"sing-box {settings.sing_box_version} with V2Ray counters: {destination}"
        )
        return
    host = host_platform()
    payload = ReleaseBuilder(settings).fetch_upstream_payload(host)
    # The naive outbound loads libcronet.so from beside the binary, so the whole
    # payload moves together. Hard links survive `sbm gc` removing the store
    # copy and cost no space.
    staged = BIN_DIR.with_name(f".{BIN_DIR.name}.tmp")
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    for source in payload.iterdir():
        try:
            os.link(source, staged / source.name)
        except OSError:
            shutil.copy2(source, staged / source.name)
    shutil.rmtree(BIN_DIR, ignore_errors=True)
    staged.rename(BIN_DIR)
    print(f"sing-box {settings.sing_box_version} for {host}: {TEST_BINARY}")


if __name__ == "__main__":
    main()
