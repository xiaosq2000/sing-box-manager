"""Read-only database account projections and subscription payloads."""

from __future__ import annotations

import hmac
import json

from sing_box_manager.control_plane.models import Account
from sing_box_manager.desktop_config import (
    UserProtocols,
    build_desktop_config,
    build_linux_tun_config,
    build_mobile_config,
)
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.release.renderers import SUPPORTED_CLIENT_OS
from sing_box_manager.settings import (
    SUPPORTED_PROTOCOLS,
    Hysteria2Inventory,
    Hysteria2User,
    NaiveInventory,
    NaiveUser,
    RuntimeInventory,
    Settings,
    SnellInventory,
    SnellUser,
    TrafficStatsSettings,
    TrojanInventory,
    TrojanUser,
    WebPortalInventory,
    WebPortalUser,
)
from sing_box_manager.subscription import (
    DEFAULT_ROUTE,
    ENVELOPE_VERSION,
    PROTOCOLS,
    TOKEN_PATTERN,
    subscription_token,
    user_protocols,
)


def build_inventory_from_db(runtime: Settings) -> RuntimeInventory:
    """Project database accounts and credentials into a RuntimeInventory."""
    _ = runtime.deployment
    accounts = list(Account.objects.prefetch_related("credentials").order_by("id"))
    portal_users = [
        WebPortalUser(
            username=account.username,
            password_hash=account.portal_password_hash,
            enabled=account.enabled and account.portal_enabled,
            admin=account.is_portal_admin,
        )
        for account in accounts
        if account.portal_password_hash
    ]
    trojan_users: list[TrojanUser] = []
    hysteria2_users: list[Hysteria2User] = []
    naive_users: list[NaiveUser] = []
    snell_users: list[SnellUser] = []
    for account in accounts:
        by_protocol = {cred.protocol: cred for cred in account.credentials.all()}
        if (cred := by_protocol.get("trojan")) is not None:
            trojan_users.append(
                TrojanUser(
                    username=account.username,
                    password=cred.secret,
                    enabled=account.enabled and cred.enabled,
                )
            )
        if (cred := by_protocol.get("hysteria2")) is not None:
            hysteria2_users.append(
                Hysteria2User(
                    username=account.username,
                    password=cred.secret,
                    enabled=account.enabled and cred.enabled,
                )
            )
        if (cred := by_protocol.get("naive")) is not None:
            naive_users.append(
                NaiveUser(
                    username=account.username,
                    password=cred.secret,
                    enabled=account.enabled and cred.enabled,
                )
            )
        if runtime.snell.psk and (cred := by_protocol.get("snell")) is not None:
            snell_users.append(
                SnellUser(
                    username=account.username,
                    password=cred.secret,
                    enabled=account.enabled and cred.enabled,
                )
            )
    return runtime.model_copy(
        update={
            "web_portal_data": WebPortalInventory(users=portal_users),
            "trojan_data": TrojanInventory(users=trojan_users),
            "hysteria2_data": Hysteria2Inventory(
                obfs_password=runtime.hysteria2.obfs_password,
                up_mbps=runtime.hysteria2.up_mbps,
                down_mbps=runtime.hysteria2.down_mbps,
                users=hysteria2_users,
            ),
            "naive_data": NaiveInventory(users=naive_users),
            "snell": SnellInventory(psk=runtime.snell.psk, users=snell_users),
        }
    )


def _normalize_server_config(config: dict) -> dict:
    normalized = json.loads(json.dumps(config))
    for inbound in normalized.get("inbounds", []):
        if "users" in inbound:
            inbound["users"] = sorted(
                inbound["users"],
                key=lambda item: item.get("name") or item.get("username") or "",
            )
    stats = normalized.get("experimental", {}).get("v2ray_api", {}).get("stats")
    if isinstance(stats, dict) and "users" in stats:
        stats["users"] = sorted(stats["users"])
    return normalized


def verify_inventory_parity(
    source: RuntimeInventory,
    target: RuntimeInventory,
    traffic_stats: TrafficStatsSettings | None = None,
) -> None:
    """Verify that database-projected accounts and rendered configs match source."""
    source_portal = {
        u.username: (u.password_hash, u.enabled, u.admin)
        for u in source.web_portal.users
    }
    target_portal = {
        u.username: (u.password_hash, u.enabled, u.admin)
        for u in target.web_portal.users
    }
    if source_portal != target_portal:
        raise ValueError("Portal user records do not match source inventory")

    for protocol in SUPPORTED_PROTOCOLS:
        source_section = {
            u.username: (u.password, u.enabled) for u in getattr(source, protocol).users
        }
        target_section = {
            u.username: (u.password, u.enabled) for u in getattr(target, protocol).users
        }
        if source_section != target_section:
            raise ValueError(f"{protocol} credentials do not match source inventory")

    if _normalize_server_config(
        build_server_config(source, traffic_stats)
    ) != _normalize_server_config(build_server_config(target, traffic_stats)):
        raise ValueError("Rendered server config does not match source inventory")

    usernames = sorted(
        {u.username for u in source.web_portal.users}
        | {
            u.username
            for protocol in SUPPORTED_PROTOCOLS
            for u in getattr(source, protocol).users
        }
    )
    for username in usernames:
        src_protocols = user_protocols(source, username)
        dst_protocols = user_protocols(target, username)
        held = [name for name in PROTOCOLS if getattr(src_protocols, name) is not None]
        if held != [
            name for name in PROTOCOLS if getattr(dst_protocols, name) is not None
        ]:
            raise ValueError(f"Enabled protocols differ for {username}")
        if not held:
            continue
        for os_name in SUPPORTED_CLIENT_OS:
            if build_desktop_config(
                source.deployment,
                src_protocols,
                os_name=os_name,
                default_protocol=held[0],
                default_route=DEFAULT_ROUTE,
            ) != build_desktop_config(
                target.deployment,
                dst_protocols,
                os_name=os_name,
                default_protocol=held[0],
                default_route=DEFAULT_ROUTE,
            ):
                raise ValueError(
                    f"Rendered {os_name} desktop config differs for {username}"
                )


def find_db_subscriber(secret: str, token: str) -> Account | None:
    """Find the enabled portal account matching token in constant time."""
    if TOKEN_PATTERN.fullmatch(token) is None:
        return None
    matched: Account | None = None
    for account in Account.objects.filter(enabled=True, portal_enabled=True):
        expected = subscription_token(
            secret, account.username, account.subscription_counter
        )
        if hmac.compare_digest(expected, token):
            matched = account
    return matched


def account_protocols(account: Account, runtime: Settings) -> UserProtocols:
    """Return UserProtocols for one database Account."""
    username = str(account.username)
    by_proto = {
        str(cred.protocol): str(cred.secret)
        for cred in account.credentials.filter(enabled=True)
        if account.enabled
    }
    return UserProtocols(
        trojan=None
        if "trojan" not in by_proto
        else TrojanUser(username=username, password=by_proto["trojan"], enabled=True),
        hysteria2=None
        if "hysteria2" not in by_proto
        else Hysteria2User(
            username=username, password=by_proto["hysteria2"], enabled=True
        ),
        naive=None
        if "naive" not in by_proto
        else NaiveUser(username=username, password=by_proto["naive"], enabled=True),
        hysteria2_settings=runtime.hysteria2,
        snell=SnellUser(username=username, password=by_proto["snell"])
        if "snell" in by_proto and runtime.snell.psk
        else None,
        snell_settings=runtime.snell,
    )


def build_account_envelope(
    account: Account,
    runtime: Settings,
    *,
    os_name: str,
    sbc_version: str | None = None,
) -> dict | None:
    """Build the sbc subscription envelope for a database Account."""
    protocols = account_protocols(account, runtime)
    held = [name for name in PROTOCOLS if getattr(protocols, name) is not None]
    if not held:
        return None
    default_protocol = (
        runtime.default_protocol if runtime.default_protocol in held else held[0]
    )
    node: dict[str, object] = {
        "name": runtime.deployment.host,
        "protocols": held,
    }
    if runtime.deployment.client_region is not None:
        node["region"] = runtime.deployment.client_region.model_dump()
    return {
        "version": ENVELOPE_VERSION,
        "default_node": runtime.deployment.host,
        "default_route": DEFAULT_ROUTE,
        "default_protocol": default_protocol,
        "nodes": [node],
        "latest": {
            "sing_box": runtime.sing_box_version,
            **({} if sbc_version is None else {"sbc": sbc_version}),
        },
        "config": build_desktop_config(
            runtime.deployment,
            protocols,
            os_name=os_name,
            default_protocol=default_protocol,
            default_route=DEFAULT_ROUTE,
        ),
        "config_tun": build_linux_tun_config(
            runtime.deployment,
            protocols,
            default_protocol=default_protocol,
            default_route=DEFAULT_ROUTE,
        ),
    }


def build_account_mobile_profile(
    account: Account,
    runtime: Settings,
) -> dict | None:
    """Build one mobile TUN config with every enabled account protocol."""
    protocols = account_protocols(account, runtime)
    held = [name for name in PROTOCOLS if getattr(protocols, name) is not None]
    if not held:
        return None
    default_protocol = (
        runtime.default_protocol if runtime.default_protocol in held else held[0]
    )
    return build_mobile_config(
        runtime.deployment,
        protocols,
        default_protocol=default_protocol,
        default_route=DEFAULT_ROUTE,
    )
