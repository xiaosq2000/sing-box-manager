"""Tests for structured settings parsing."""

from pathlib import Path

import pytest

from sing_box_manager.settings import (
    DEFAULT_TRAFFIC_STATS_DATABASE_PATH,
    Settings,
    WebSettings,
)


def test_traffic_stats_database_defaults_outside_the_deploy_root() -> None:
    """An unset database_path must not land inside the disposable deploy root.

    The remote root can be wiped and reprovisioned, so a relative default would
    quietly take the usage history with it on the next redeploy.
    """
    settings = Settings.from_dict(
        {"sing_box_version": "1.12.24", "traffic_stats": {"enabled": True}}
    )

    assert settings.traffic_stats.database_path == DEFAULT_TRAFFIC_STATS_DATABASE_PATH
    assert settings.traffic_stats.database_path.is_absolute()
    assert Path("/var/lib/sing-box-manager") in (
        settings.traffic_stats.database_path.parents
    )


def test_settings_from_dict_parses_traffic_stats_block() -> None:
    settings = Settings.from_dict(
        {
            "sing_box_version": "1.12.24",
            "traffic_stats": {
                "enabled": True,
                "database_path": "data/monthly-traffic.sqlite3",
                "timezone": "Asia/Shanghai",
                "relay_multiplier": 2.1,
                "trojan_api_listen": "127.0.0.1:29080",
                "hysteria2_api_listen": "127.0.0.1:29081",
                "naive_api_listen": "127.0.0.1:29082",
            },
        }
    )

    assert settings.traffic_stats.enabled is True
    assert settings.traffic_stats.database_path == Path("data/monthly-traffic.sqlite3")
    assert settings.traffic_stats.timezone == "Asia/Shanghai"
    assert settings.traffic_stats.relay_multiplier == 2.1
    assert settings.traffic_stats.api_listen == "127.0.0.1:29080"
    assert "trojan_api_listen" not in settings.traffic_stats.model_dump()


def test_settings_from_dict_parses_connection_api_block() -> None:
    settings = Settings.from_dict(
        {
            "sing_box_version": "1.14.0",
            "traffic_stats": {
                "enabled": True,
                "trojan_connection_api_listen": "127.0.0.1:29090",
                "hysteria2_connection_api_listen": "127.0.0.1:29091",
                "naive_connection_api_listen": "127.0.0.1:29092",
                "connection_api_secret": "  s3cr3t  ",
                "domain_top_n": 25,
                "show_user_domains": True,
            },
        }
    )

    stats = settings.traffic_stats
    assert stats.connection_api_listen == "127.0.0.1:29090"
    assert stats.connection_api_secret == "s3cr3t"
    assert stats.domain_top_n == 25
    assert stats.show_user_domains is True


def test_connection_api_defaults_do_not_collide_with_the_stats_api() -> None:
    """The single node's two API listeners must use different ports."""
    stats = Settings.from_dict({"sing_box_version": "1.14.0"}).traffic_stats

    ports = [stats.api_listen, stats.connection_api_listen]

    assert len(set(ports)) == len(ports)


def test_connection_api_secret_defaults_to_empty() -> None:
    """Empty disables auth in sing-box, which is safe only on loopback.

    The default listen addresses are all 127.0.0.1, so an unset secret is not a
    remote exposure; binding elsewhere without setting one would be.
    """
    stats = Settings.from_dict({"sing_box_version": "1.14.0"}).traffic_stats

    assert stats.connection_api_secret == ""
    assert stats.connection_api_listen.startswith("127.0.0.1:")


@pytest.mark.parametrize("value", [0, -1, True, "10", 2.5])
def test_settings_from_dict_rejects_invalid_domain_top_n(value: object) -> None:
    with pytest.raises(ValueError, match="traffic_stats.domain_top_n"):
        Settings.from_dict(
            {
                "sing_box_version": "1.14.0",
                "traffic_stats": {"domain_top_n": value},
            }
        )


def test_settings_from_dict_rejects_invalid_connection_api_listen() -> None:
    with pytest.raises(ValueError, match="traffic_stats.connection_api_listen"):
        Settings.from_dict(
            {
                "sing_box_version": "1.14.0",
                "traffic_stats": {"trojan_connection_api_listen": "127.0.0.1"},
            }
        )


def test_split_listen_address_drops_the_ipv6_brackets() -> None:
    """The brackets exist to keep the combined form unambiguous, nothing else.

    sing-box's `listen` field wants a bare address, so carrying them through
    would render a services block the binary rejects.
    """
    from sing_box_manager.settings import split_listen_address

    assert split_listen_address("[::1]:19090") == ("::1", 19090)
    assert split_listen_address("127.0.0.1:19090") == ("127.0.0.1", 19090)


def test_relay_multiplier_defaults_to_two_for_bidirectional_billing() -> None:
    """A relayed byte crosses the VPS interface twice.

    Hosts that bill inbound plus outbound therefore charge about double what the
    per-user counters record, and that is the common case, so the default has to
    be 2.0 rather than a no-op 1.0.
    """
    settings = Settings.from_dict({"sing_box_version": "1.12.24"})

    assert settings.traffic_stats.relay_multiplier == 2.0


@pytest.mark.parametrize("value", [0, 0.5, -2, "2", True, float("inf")])
def test_settings_from_dict_rejects_invalid_relay_multiplier(value: object) -> None:
    """Below 1.0 would report less traffic than sing-box actually measured.

    An egress-only host bills each relayed byte exactly once, which is the floor;
    anything under that describes no real billing model.
    """
    with pytest.raises(ValueError, match="traffic_stats.relay_multiplier"):
        Settings.from_dict(
            {
                "sing_box_version": "1.12.24",
                "traffic_stats": {"relay_multiplier": value},
            }
        )


def test_settings_from_dict_parses_default_protocol() -> None:
    settings = Settings.from_dict(
        {
            "sing_box_version": "1.12.24",
            "default_protocol": "naive",
        }
    )

    assert settings.default_protocol == "naive"


def test_settings_from_dict_parses_client_upgrade_policy() -> None:
    settings = Settings.from_dict(
        {
            "sing_box_version": "1.12.24",
            "client_upgrade": {
                "policy": "required",
                "message": "Routing format changed",
            },
        }
    )

    assert settings.client_upgrade.policy == "required"
    assert settings.client_upgrade.message == "Routing format changed"


def test_client_upgrade_defaults_to_suggested() -> None:
    settings = Settings.from_dict({"sing_box_version": "1.12.24"})

    assert settings.client_upgrade.policy == "suggested"


@pytest.mark.parametrize("policy", ["recommended", "mandatory", ""])
def test_settings_rejects_invalid_client_upgrade_policy(policy: str) -> None:
    with pytest.raises(ValueError, match="client_upgrade.policy"):
        Settings.from_dict(
            {
                "sing_box_version": "1.12.24",
                "client_upgrade": {"policy": policy},
            }
        )


def test_settings_rejects_multiline_client_upgrade_message() -> None:
    with pytest.raises(ValueError, match="client_upgrade.message"):
        Settings.from_dict(
            {
                "sing_box_version": "1.12.24",
                "client_upgrade": {"message": "line one\nline two"},
            }
        )


def test_settings_from_dict_rejects_invalid_default_protocol() -> None:
    with pytest.raises(ValueError, match="default_protocol"):
        Settings.from_dict(
            {
                "sing_box_version": "1.12.24",
                "default_protocol": "vmess",
            }
        )


def test_settings_from_dict_rejects_invalid_traffic_stats_timezone() -> None:
    with pytest.raises(ValueError, match="traffic_stats.timezone"):
        Settings.from_dict(
            {
                "sing_box_version": "1.12.24",
                "traffic_stats": {"timezone": "Mars/Olympus_Mons"},
            }
        )


def test_require_config_path_returns_configured_path() -> None:
    config_path = Path("config/inventory/runtime.yaml")
    settings = Settings(sing_box_version="1.12.24", config_path=config_path)

    assert settings.require_config_path() == config_path


def test_require_config_path_rejects_missing_path() -> None:
    settings = Settings(sing_box_version="1.12.24")

    with pytest.raises(ValueError, match="config path is required"):
        settings.require_config_path()


def test_session_secret_is_unset_unless_configured() -> None:
    assert WebSettings.from_dict({}).session_secret is None
    assert WebSettings.from_dict({"session_secret": " kept "}).session_secret == "kept"


def test_subscription_secret_is_unset_unless_configured() -> None:
    assert WebSettings.from_dict({}).subscription_secret is None
    web = WebSettings.from_dict({"subscription_secret": " kept "})
    assert web.subscription_secret == "kept"
    with pytest.raises(ValueError, match="web.subscription_secret must be"):
        WebSettings.from_dict({"subscription_secret": "  "})


def test_subscription_counters_live_in_the_state_directory_by_default() -> None:
    assert WebSettings.from_dict({}).subscription_database_path == Path(
        "/var/lib/sing-box-manager/subscriptions.sqlite3"
    )
    web = WebSettings.from_dict({"subscription_database_path": "/tmp/subs.db"})
    assert web.subscription_database_path == Path("/tmp/subs.db")


def test_client_signing_key_is_a_top_level_setting() -> None:
    assert Settings.from_dict({"sing_box_version": "1.14.2"}).client_signing_key is None
    settings = Settings.from_dict(
        {"sing_box_version": "1.14.2", "client_signing_key": " key "}
    )
    assert settings.client_signing_key == "key"
    with pytest.raises(ValueError, match="client_signing_key must be"):
        Settings.from_dict({"sing_box_version": "1.14.2", "client_signing_key": ""})
