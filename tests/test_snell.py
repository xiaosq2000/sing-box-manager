"""Optional infrastructure, safe multi-user rendering and all client profiles."""

import copy

import pytest
from pydantic import ValidationError

from sing_box_manager.desktop_config import (
    build_desktop_config,
    build_linux_tun_config,
    build_mobile_config,
)
from sing_box_manager.release.platforms import MIXED_PROFILE, ROUTE_STRATEGIES
from sing_box_manager.release.profiles import build_client_profile, build_server_config
from sing_box_manager.release.renderers import apply_desktop_route_strategy
from sing_box_manager.release.validation import check_config
from sing_box_manager.settings import Settings, SnellInventory, SnellUser
from sing_box_manager.subscription import user_protocols
from tests.snell_fixtures import with_snell
from tests.test_m3_profiles import profile_inventory
from tests.test_sing_box_114 import binary as binary


def test_legacy_inventory_is_unchanged_and_snell_is_optional():
    inventory = profile_inventory()
    assert inventory.snell.psk is None
    assert inventory.deployment.snell_port is None
    assert [i["type"] for i in build_server_config(inventory)["inbounds"]] == [
        "trojan",
        "hysteria2",
        "naive",
    ]


@pytest.mark.parametrize("psk", ["", "short", "x" * 256, " x" * 12, "x" * 12 + "\n"])
def test_rejects_invalid_psk_without_disclosing_it(psk):
    with pytest.raises(ValidationError) as error:
        SnellInventory(psk=psk)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("key", ["", " leading", "trailing ", "control\x01", "界" * 86])
def test_rejects_invalid_userkeys(key):
    with pytest.raises(ValidationError):
        SnellUser(username="alice", password=key)


def test_psk_and_userkey_lengths_use_utf8_bytes():
    assert SnellInventory(psk="界" * 4).psk == "界" * 4
    assert SnellUser(username="alice", password="界" * 85).password == "界" * 85
    with pytest.raises(ValidationError):
        SnellInventory(psk="界" * 86)


@pytest.mark.parametrize(
    "change",
    [
        "psk-only",
        "port-only",
        "users-only",
        "old-core",
        "default-only",
        "duplicate-key",
        "trojan-port",
        "naive-port",
        "api-port",
        "control-port",
        "http-port",
        "https-port",
    ],
)
def test_rejects_incomplete_or_conflicting_snell_settings(change):
    inventory = with_snell(profile_inventory())
    data = inventory.model_dump()
    if change == "psk-only":
        data["deployment"]["snell_port"] = None
    elif change == "port-only":
        data["snell"] = {}
    elif change in {"users-only", "default-only"}:
        data["deployment"]["snell_port"] = None
        data["snell"]["psk"] = None
        if change == "default-only":
            data["snell"]["users"] = []
            data["default_protocol"] = "snell"
    elif change == "old-core":
        data["sing_box_version"] = "1.13.0"
    elif change == "duplicate-key":
        data["snell"]["users"][1]["password"] = data["snell"]["users"][0]["password"]
    elif change == "control-port":
        data["control_plane"]["enabled"] = True
        data["deployment"]["snell_port"] = data["control_plane"]["port"]
    else:
        data["deployment"]["snell_port"] = {
            "trojan-port": inventory.deployment.trojan_port,
            "naive-port": inventory.deployment.naive_port,
            "api-port": 19080,
            "http-port": 80,
            "https-port": 443,
        }[change]
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_server_uses_multi_user_v6_without_tls_and_strips_accounts():
    inventory = with_snell(profile_inventory())
    original = inventory.model_dump()
    config = build_server_config(inventory, inventory.traffic_stats)
    inbound = next(i for i in config["inbounds"] if i["type"] == "snell")
    assert inbound == {
        "type": "snell",
        "tag": "snell",
        "listen": "::",
        "listen_port": 10443,
        "version": 6,
        "psk": inventory.snell.psk,
        "mode": "default",
        "users": [
            {"name": "snell:alice", "userkey": "synthetic-snell-alice"},
            {"name": "snell:bob", "userkey": "synthetic-snell-bob"},
        ],
    }
    assert "snell:alice" in config["experimental"]["v2ray_api"]["stats"]["users"]
    assert inventory.model_dump() == original
    clean = inventory.without_accounts()
    assert clean.snell.psk == inventory.snell.psk
    assert clean.snell.users == []
    assert "snell" not in {i["type"] for i in build_server_config(clean)["inbounds"]}


@pytest.mark.parametrize("empty", [True, False])
def test_no_snell_listener_with_zero_enabled_users(empty):
    inventory = with_snell(profile_inventory())
    if empty:
        inventory.snell.users.clear()
    else:
        for user in inventory.snell.users:
            user.enabled = False
    config = build_server_config(inventory, inventory.traffic_stats)
    assert "snell" not in {i["type"] for i in config["inbounds"]}
    assert not any(
        name.startswith("snell:")
        for name in config["experimental"]["v2ray_api"]["stats"]["users"]
    )


@pytest.mark.parametrize(
    "platform", ["linux", "darwin", "windows", "mobile", "linux-tun"]
)
@pytest.mark.parametrize("mode", ROUTE_STRATEGIES)
def test_snell_profiles_on_all_platforms_and_modes(platform, mode, binary):
    inventory = with_snell(profile_inventory())
    protocols = user_protocols(inventory, "alice")
    options = {"default_protocol": "snell", "default_route": mode}
    if platform == "mobile":
        config = build_mobile_config(inventory.deployment, protocols, **options)
    elif platform == "linux-tun":
        config = build_linux_tun_config(inventory.deployment, protocols, **options)
    else:
        config = build_desktop_config(
            inventory.deployment, protocols, os_name=platform, **options
        )
    assert config["outbounds"][0]["outbounds"] == [
        "trojan",
        "hysteria2",
        "naive",
        "snell",
    ]
    outbound = next(o for o in config["outbounds"] if o["type"] == "snell")
    assert outbound["version"] == 6
    assert outbound["mode"] == "default"
    assert outbound["userkey"] == "synthetic-snell-alice"
    assert outbound["psk"] == inventory.snell.psk
    assert outbound["reuse"] is False
    assert not {"tls", "password", "username", "obfs_mode"} & outbound.keys()
    assert "synthetic-snell-bob" not in str(config)
    check_config(binary, config, label=f"snell/{platform}/{mode}")


@pytest.mark.parametrize("mode", ROUTE_STRATEGIES)
def test_single_route_snell_profiles(mode, binary):
    inventory = with_snell(profile_inventory())
    config = build_client_profile(
        inventory.deployment, user_protocols(inventory, "alice"), "snell", MIXED_PROFILE
    )
    config = apply_desktop_route_strategy(config, mode, os_name="linux")
    check_config(binary, config, label=f"snell/mixed/{mode}")


def test_disabled_snell_is_not_advertised_and_shared_settings_are_required():
    inventory = with_snell(profile_inventory())
    protocols = user_protocols(inventory, "alice")
    protocols = copy.deepcopy(protocols)
    protocols.snell.enabled = False
    config = build_mobile_config(
        inventory.deployment, protocols, default_protocol="trojan"
    )
    assert config["outbounds"][0]["outbounds"] == ["trojan", "hysteria2", "naive"]
    assert not any(o["type"] == "snell" for o in config["outbounds"])
    with pytest.raises(ValueError, match="default protocol"):
        build_mobile_config(inventory.deployment, protocols, default_protocol="snell")
    missing = user_protocols(inventory, "alice")
    object.__setattr__(missing, "snell_settings", None)
    with pytest.raises(ValueError, match="shared settings"):
        build_mobile_config(inventory.deployment, missing, default_protocol="trojan")
