"""Database-backed account projections, inventory import, and node sync."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from django.db import transaction

from sing_box_manager.control_plane.models import Account, Credential
from sing_box_manager.control_plane.runner import ControlPlaneError
from sing_box_manager.deploy import SERVICE_USER
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
    ProtocolUser,
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
    read_legacy_reset_counters,
    subscription_token,
    user_protocols,
)

DEFAULT_SERVER_CONFIG_PATH = Path("/usr/local/etc/sing-box/config.json")
DEFAULT_SING_BOX_BINARY_PATH = Path("/usr/local/bin/sing-box")
DEFAULT_SING_BOX_SERVICE = "sing-box.service"


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


def import_inventory(runtime: Settings) -> dict[str, int]:
    """Import inventory before PostgreSQL becomes authoritative."""
    if runtime.control_plane.authoritative:
        raise ControlPlaneError(
            "Inventory import is disabled while the control plane is authoritative. "
            "Manage accounts in Django Admin."
        )
    _ = runtime.deployment
    source = runtime
    sqlite_counters = read_legacy_reset_counters(runtime.web.subscription_database_path)

    portal_by_user = {u.username: u for u in source.web_portal.users}
    protocol_by_user: dict[str, dict[str, ProtocolUser]] = {
        protocol: {u.username: u for u in getattr(source, protocol).users}
        for protocol in SUPPORTED_PROTOCOLS
    }
    ordered_usernames: list[str] = []
    for username in [
        *(u.username for u in source.web_portal.users),
        *(
            u.username
            for protocol in SUPPORTED_PROTOCOLS
            for u in getattr(source, protocol).users
        ),
    ]:
        if username not in ordered_usernames:
            ordered_usernames.append(username)

    credential_count = 0
    with transaction.atomic():
        existing_accounts = {
            acc.username: acc for acc in Account.objects.select_for_update()
        }
        for username in ordered_usernames:
            portal = portal_by_user.get(username)
            existing = existing_accounts.get(username)
            counter = max(
                0,
                0 if existing is None else existing.subscription_counter,
                int(sqlite_counters.get(username, 0)),
            )
            account, _ = Account.objects.update_or_create(
                username=username,
                defaults={
                    "enabled": bool(portal and portal.enabled)
                    or any(
                        users[username].enabled
                        for users in protocol_by_user.values()
                        if username in users
                    ),
                    "portal_enabled": False if portal is None else portal.enabled,
                    "is_portal_admin": False if portal is None else portal.admin,
                    "portal_password_hash": ""
                    if portal is None
                    else portal.password_hash,
                    "subscription_counter": counter,
                },
            )
            account.full_clean()
            for protocol in SUPPORTED_PROTOCOLS:
                proto_user = protocol_by_user[protocol].get(username)
                if proto_user is None:
                    Credential.objects.filter(
                        account=account, protocol=protocol
                    ).delete()
                else:
                    cred, _ = Credential.objects.update_or_create(
                        account=account,
                        protocol=protocol,
                        defaults={
                            "secret": proto_user.password,
                            "enabled": proto_user.enabled,
                        },
                    )
                    cred.full_clean()
                    credential_count += 1

        # Retain removed accounts as disabled tombstones so subscription counters never regress.
        removed = Account.objects.exclude(username__in=ordered_usernames)
        Credential.objects.filter(account__in=removed).delete()
        removed.update(enabled=False, is_portal_admin=False, portal_password_hash="")

        projected = build_inventory_from_db(runtime)
        verify_inventory_parity(source, projected, runtime.traffic_stats)

    return {"accounts": len(ordered_usernames), "credentials": credential_count}


def ensure_default_credentials(account: Account) -> list[str]:
    """Populate random credentials for all supported protocols when none exist."""
    if account.credentials.exists():
        return []
    created: list[str] = []
    for protocol in SUPPORTED_PROTOCOLS:
        Credential.objects.create(
            account=account,
            protocol=protocol,
            secret=secrets.token_urlsafe(24),
            enabled=True,
        )
        created.append(protocol)
    return created


def backfill_snell_credentials(
    runtime: Settings, *, dry_run: bool = False
) -> dict[str, int]:
    """Add only missing Snell credentials. Preserve account and credential switches."""
    if not runtime.control_plane.authoritative:
        raise ControlPlaneError(
            "Snell backfill requires authoritative account management"
        )
    if not runtime.snell.psk or runtime.deployment.snell_port is None:
        raise ControlPlaneError(
            "Configure snell.psk and deployment.snell_port before Snell backfill"
        )
    with transaction.atomic():
        accounts = list(Account.objects.select_for_update().order_by("id"))
        existing = set(
            Credential.objects.filter(protocol="snell").values_list(
                "account_id", flat=True
            )
        )
        missing = [account for account in accounts if account.pk not in existing]
        if not dry_run:
            for account in missing:
                credential = Credential(
                    account=account, protocol="snell", secret=secrets.token_urlsafe(32)
                )
                credential.full_clean()
                credential.save()
            if missing:

                def trigger() -> None:
                    try:
                        request_node_sync(runtime)
                    except OSError as exc:
                        raise ControlPlaneError(
                            "Snell credentials were saved, but node sync could not be requested. "
                            "Run sudo sbm control-plane sync on the server."
                        ) from exc

                transaction.on_commit(trigger)
    return {
        "accounts": len(accounts),
        "existing": len(accounts) - len(missing),
        "missing" if dry_run else "created": len(missing),
    }


def request_node_sync(runtime: Settings, *, trigger_path: Path | None = None) -> Path:
    """Write the trigger file watched by sing-box-manager-sync.path."""
    path = trigger_path or (runtime.generated_root / "node-sync.trigger")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time_ns()}\n", encoding="utf-8")
    return path


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


def _build_inventory_as_db_user(runtime: Settings) -> RuntimeInventory:
    """Drop effective privileges to the DB peer role when invoked as root."""
    if os.geteuid() != 0:
        return build_inventory_from_db(runtime)
    import pwd

    from django.db import connections

    egid = os.getegid()
    pw = pwd.getpwnam(SERVICE_USER)
    connections.close_all()
    try:
        os.setegid(pw.pw_gid)
        os.seteuid(pw.pw_uid)
        return build_inventory_from_db(runtime)
    finally:
        try:
            connections.close_all()
        finally:
            os.seteuid(0)
            os.setegid(egid)


def sync_node_config(
    runtime: Settings,
    *,
    config_path: Path | None = None,
    binary_path: Path | None = None,
    service_name: str = DEFAULT_SING_BOX_SERVICE,
    restart_service: bool = True,
) -> dict[str, object]:
    """Serialize node updates, validate the candidate, and recover on failure."""
    import fcntl

    resolved_config = config_path or Path(
        os.environ.get("SBM_SING_BOX_CONFIG", str(DEFAULT_SERVER_CONFIG_PATH))
    )
    resolved_binary = binary_path or Path(
        os.environ.get("SBM_SING_BOX_BINARY", str(DEFAULT_SING_BOX_BINARY_PATH))
    )
    resolved_config.parent.mkdir(parents=True, exist_ok=True)
    # The server installer uses the same lock. Acquire it before reading the DB
    # so a waiting sync cannot apply an obsolete snapshot after a newer update.
    with (resolved_config.parent / ".sync.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync_node_config_locked(
            runtime, resolved_config, resolved_binary, service_name, restart_service
        )


def _restart_node_service(service_name: str) -> bool:
    try:
        restart = subprocess.run(
            ["systemctl", "restart", service_name], capture_output=True, check=False
        )
        if restart.returncode != 0:
            return False
        time.sleep(1)
        health = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True,
            check=False,
        )
        return health.returncode == 0
    except OSError:
        return False


def _sync_node_config_locked(
    runtime: Settings,
    resolved_config: Path,
    resolved_binary: Path,
    service_name: str,
    restart_service: bool,
) -> dict[str, object]:
    inventory = _build_inventory_as_db_user(runtime)
    server_config = build_server_config(inventory, runtime.traffic_stats)
    rendered = json.dumps(server_config, indent=2, sort_keys=True) + "\n"
    result: dict[str, object] = {
        "config_path": str(resolved_config),
        "inbounds": [inbound["tag"] for inbound in server_config["inbounds"]],
    }
    fd, temp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".json.tmp", dir=str(resolved_config.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        temp_path.chmod(0o600)

        if resolved_binary.is_file():
            check = subprocess.run(
                [str(resolved_binary), "check", "-c", str(temp_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                # Checker output can contain credentials from the candidate.
                raise ControlPlaneError(
                    "sing-box check failed. The active config is unchanged."
                )
        elif restart_service:
            raise ControlPlaneError("sing-box binary is missing. Node sync stopped.")

        previous_bytes = (
            resolved_config.read_bytes() if resolved_config.is_file() else None
        )
        try:
            unchanged = (
                previous_bytes is not None
                and json.loads(previous_bytes) == server_config
            )
        except (ValueError, UnicodeDecodeError):
            unchanged = False
        if unchanged and (
            not restart_service
            or subprocess.run(
                ["systemctl", "is-active", "--quiet", service_name],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ):
            return result
        os.replace(temp_path, resolved_config)
        if restart_service and not _restart_node_service(service_name):
            if previous_bytes is None:
                resolved_config.unlink()
                raise ControlPlaneError(
                    "Node start failed. No previous config is available."
                )
            # Restore atomically before attempting to recover the service.
            with temp_path.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(previous_bytes)
            os.replace(temp_path, resolved_config)
            if not _restart_node_service(service_name):
                raise ControlPlaneError(
                    "Node start and service recovery failed. The previous config is restored. "
                    "Check the node service on the server."
                )
            raise ControlPlaneError(
                "Node start failed. The previous config and service are restored."
            )
    finally:
        temp_path.unlink(missing_ok=True)

    return result
