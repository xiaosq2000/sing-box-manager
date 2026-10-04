"""Compatibility imports for the unified settings model (remove after M3)."""

# Older callers can use these names for one release. Parsing and validation
# live only in settings.py; RuntimeInventory and Settings are the same model.
from sing_box_manager.settings import (  # noqa: F401
    AuthSnapshot,
    AuthSnapshotUser,
    DeploymentConfig,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveInventory,
    NaiveUser,
    ProtocolUser,
    RuntimeInventory,
    StrictModel,
    TlsConfig,
    TrojanInventory,
    TrojanUser,
    VpsInfoConfig,
    WebPortalInventory,
    WebPortalUser,
    load_auth_snapshot,
    load_config_text,
    load_runtime_inventory,
    normalize_domain,
    os,
    subprocess,
    write_auth_snapshot,
)
