"""Default selection uses fixture infrastructure, never operator settings."""

import pytest
import yaml

from sing_box_manager.settings import Settings, load_settings
from tests.fetch_sing_box import EXAMPLE_INVENTORY
from tests.snell_fixtures import with_snell
from tests.test_m3_profiles import profile_inventory


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("explicit", [None, "trojan", "hysteria2", "naive", "snell"])
def test_default_protocol_precedence_and_round_trip(configured, explicit, tmp_path):
    inventory = profile_inventory()
    if configured:
        inventory = with_snell(inventory)
    data = inventory.model_dump(mode="json")
    data.pop("default_protocol")
    if explicit is not None:
        data["default_protocol"] = explicit
    if explicit == "snell" and not configured:
        with pytest.raises(
            ValueError, match="requires configured Snell infrastructure"
        ):
            Settings.from_dict(data)
        return
    expected = explicit or ("snell" if configured else "trojan")
    settings = Settings.from_dict(data)
    assert settings.default_protocol == expected
    assert Settings.model_validate(settings.model_dump()).default_protocol == expected
    assert settings.without_accounts().default_protocol == expected
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_settings(path).default_protocol == expected


def test_settings_without_inventory_keep_the_legacy_default():
    assert Settings().default_protocol == "trojan"
    assert (
        Settings.from_dict({"sing_box_version": "1.14.3"}).default_protocol == "trojan"
    )


def test_public_example_configures_the_snell_default():
    settings = load_settings(EXAMPLE_INVENTORY)
    assert settings.default_protocol == "snell"
    assert settings.deployment.snell_port is not None
    assert settings.snell.psk is not None
    data = settings.model_dump()
    data.pop("default_protocol")
    assert Settings.model_validate(data).default_protocol == "snell"
