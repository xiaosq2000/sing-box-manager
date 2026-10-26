"""Inventory import and account credential operations."""

from __future__ import annotations

import secrets

from django.db import transaction

from sing_box_manager.control_plane.models import Account, Credential
from sing_box_manager.control_plane.node_sync import request_node_sync
from sing_box_manager.control_plane.projections import (
    build_inventory_from_db,
    verify_inventory_parity,
)
from sing_box_manager.control_plane.runner import ControlPlaneError
from sing_box_manager.settings import SUPPORTED_PROTOCOLS, ProtocolUser, Settings
from sing_box_manager.subscription import read_legacy_reset_counters


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
