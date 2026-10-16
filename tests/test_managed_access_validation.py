"""Configuration and CLI validation without runtime inventory or database access."""

from pathlib import Path
from uuid import UUID

import pytest

from sing_box_manager.managed_access.command import ManagedAccessCommand
from sing_box_manager.settings import Settings


def settings(**changes):
    return Settings.from_dict(
        {
            "sing_box_version": "1.14.2",
            "control_plane": {
                "enabled": True,
                "portal_enabled": True,
                "portal_origin": "https://vpn.example.test",
                **changes,
            },
        }
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://vpn.example.test",
        "https://user:password@vpn.example.test",
        "https://vpn.example.test/path",
        "https://vpn.example.test?token=synthetic",
        "https://vpn.example.test#fragment",
        "https://vpn.example.test:0",
        "https://vpn.example.test:",
        "https://vpn.example.test:65536",
        "https://vpn.example.test\n",
        "https://非.example.test",
        None,
    ],
)
def test_origin_requires_a_valid_https_authority(origin):
    with pytest.raises(ValueError):
        settings(portal_origin=origin)


@pytest.mark.parametrize("port", [True, 0, 80, 47070, 47071, 65536])
def test_public_listener_does_not_collide_with_other_tcp_services(port):
    with pytest.raises(ValueError):
        settings(portal_port=port)


def test_public_settings_keep_a_separate_hidden_signing_key():
    value = settings(
        subscription_secret="synthetic-independent-signing-key-" + "x" * 32,
        portal_origin="https://vpn.example.test/",
    )
    assert value.control_plane.portal_origin == "https://vpn.example.test"
    assert value.control_plane.subscription_secret not in repr(value.control_plane)
    assert Settings().control_plane.portal_enabled is False
    with pytest.raises(ValueError):
        settings(enabled=False)


@pytest.mark.parametrize(
    "secret", ["synthetic-short-value", "x" * 129, "x" * 60 + " ", "x" * 60 + "\n"]
)
def test_new_signing_key_has_a_bounded_ascii_encoding(secret):
    with pytest.raises(ValueError) as error:
        settings(subscription_secret=secret)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "options",
    [
        {"action": "prepare"},
        {"namespace": UUID(int=1)},
        {"action": "node-apply"},
        {"binary": Path("synthetic-binary")},
        {"run_dir": Path("synthetic-run")},
        {"acknowledge_interrupt": True},
        {"action": "activate", "require_active": True},
    ],
)
def test_invalid_command_options_stop_before_configuration(options):
    with pytest.raises(ValueError):
        ManagedAccessCommand(**options).validate()
