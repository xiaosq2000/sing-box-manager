"""Platform layouts and code-built client profiles."""

from pathlib import Path

import pytest

from sing_box_manager.release.platforms import (
    ARCHIVE_PLATFORMS,
    MIXED_PROFILE,
    MOBILE_TUN_PROFILE,
    PLATFORMS,
    PROTOCOLS,
    client_config_filename,
    route_client_config_filename,
)
from sing_box_manager.release.profiles import client_base_config

ROOT = Path(__file__).resolve().parents[1]


def test_supported_platforms() -> None:
    assert {platform.name for platform in PLATFORMS} == {
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
        "android-arm64",
    }
    assert [platform.name for platform in ARCHIVE_PLATFORMS] == ["android-arm64"]


@pytest.mark.parametrize("platform", PLATFORMS, ids=lambda platform: platform.name)
def test_platform_layout(platform) -> None:
    assert platform.os_name == platform.name.split("-", 1)[0]
    filename = platform.local_filename("1.14.2")
    assert filename == f"sing-box-1.14.2-{platform.name}" + (
        ".zip" if platform.os_name == "windows" else ".tar.gz"
    )
    assert platform.download_format == (
        "zip" if platform.os_name == "windows" else "tar.gz"
    )
    assert platform.package_ext == (
        ".zip" if platform.os_name in {"windows", "android"} else ".tar.gz"
    )
    extension = ".zip" if platform.download_format == "zip" else ".tar.gz"
    assert platform.extraction_dir(Path(filename)) == Path(
        filename.removesuffix(extension)
    )
    for extra in platform.extras:
        assert (ROOT / extra).is_file()
    for extras in platform.protocol_extras.values():
        for extra in extras:
            assert (ROOT / extra).is_file()


@pytest.mark.parametrize("protocol", [protocol.name for protocol in PROTOCOLS])
@pytest.mark.parametrize("profile", [MIXED_PROFILE, MOBILE_TUN_PROFILE])
def test_profile_builder_emits_the_correct_inbound(protocol: str, profile) -> None:
    config = client_base_config(protocol, profile)
    inbound = config["inbounds"][0]
    assert config["outbounds"][0]["type"] == protocol
    if profile == MOBILE_TUN_PROFILE:
        assert inbound["type"] == "tun"
        assert "set_system_proxy" not in inbound
    else:
        assert inbound["listen"] == "127.0.0.1"
        assert inbound["set_system_proxy"] is False
    assert (
        client_config_filename(protocol, profile)
        == f"{protocol}{profile.filename_infix}-client.json"
    )


@pytest.mark.parametrize("route", ["china", "gfw", "ai", "global"])
def test_route_filenames(route: str) -> None:
    assert route_client_config_filename("trojan", route) == (
        "trojan-client.json" if route == "china" else f"trojan-{route}-client.json"
    )


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported route"):
        route_client_config_filename("trojan", "unknown")
