"""Synthetic Snell v6 infrastructure. Never load operator inventory."""

from sing_box_manager.settings import Settings


def with_snell(inventory: Settings) -> Settings:
    data = inventory.model_dump()
    data["deployment"]["snell_port"] = 10443
    data["snell"] = {
        "psk": "synthetic-shared-snell-psk",
        "users": [
            {"username": "alice", "password": "synthetic-snell-alice"},
            {"username": "bob", "password": "synthetic-snell-bob"},
            {
                "username": "disabled-user",
                "password": "synthetic-snell-disabled",
                "enabled": False,
            },
        ],
    }
    return Settings.model_validate(data)
