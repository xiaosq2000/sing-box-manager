"""Transactional account ownership, edits, and independent subscription links."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import cast
from uuid import UUID

from django.db import transaction

from sing_box_manager.account_copy.models import AccountCopyAdmission
from sing_box_manager.account_copy.schema import copy_context_identity
from sing_box_manager.control_plane.import_parity import (
    inventory_from_records,
    records_from_state,
    verify_account_parity,
)
from sing_box_manager.control_plane.import_schema import (
    KINDS,
    MAX_COUNTER,
    AccessRecords,
    canonical,
)
from sing_box_manager.control_plane.import_service import (
    database_identity,
    database_state,
)
from sing_box_manager.control_plane.models import (
    ImportedAccess,
    ImportSource,
    ImportTarget,
    LegacyAccess,
    ProxySubject,
)
from sing_box_manager.managed_access.errors import AccessRejected
from sing_box_manager.managed_access.models import LinkGeneration, Ownership
from sing_box_manager.settings import USERNAME_PATTERN, Settings
from sing_box_manager.subscription import TOKEN_PATTERN, user_protocols


def signing_key(runtime: Settings) -> bytes:
    value = runtime.control_plane.subscription_secret
    protocol_secrets = {
        row.password
        for kind in ("trojan", "hysteria2", "naive")
        for row in getattr(runtime, kind).users
    }
    if (
        not value
        or value == runtime.hysteria2.obfs_password
        or value in protocol_secrets
        or value
        in (
            runtime.control_plane.secret_key,
            runtime.web.session_secret,
            runtime.web.subscription_secret,
        )
    ):
        raise AccessRejected("independent_subscription_key_required")
    return value.encode()


def key_identity(runtime: Settings) -> str:
    return hashlib.sha256(b"managed-key-v1\0" + signing_key(runtime)).hexdigest()


def owner(runtime: Settings, *, active: bool = False, lock: bool = False) -> Ownership:
    rows = Ownership.objects.select_for_update() if lock else Ownership.objects.all()
    value = rows.get(pk=1)
    admission = AccountCopyAdmission.objects.get(namespace=value.source_id)
    target = ImportTarget.objects.get(pk=1)
    if (
        target.fixture_enabled
        or admission.enabled
        or not admission.closed
        or admission.target_id != target.pk
        or admission.source_identity != value.source.source_identity
        or LegacyAccess.objects.exists()
        or value.key_identity != key_identity(runtime)
        or value.target_identity != database_identity(runtime, target)
    ):
        raise AccessRejected("ownership_state_rejected")
    if active:
        from sing_box_manager.managed_access.storage import require_fence

        if not value.active:
            raise AccessRejected("ownership_not_active")
        require_fence(value.identity)
    return value


def accounts(value: Ownership) -> AccessRecords:
    state = database_state(cast("ImportSource", value.source))
    active = {row["username"] for row in state["subjects"] if row["present"]}
    if any(
        row["payload"].get("username") != row["username"]
        or (row["present"] and row["username"] not in active)
        for row in state["access"]
    ):
        raise AccessRejected("account_binding_rejected")
    result = records_from_state(state)
    if set(result.memberships()) != active:
        raise AccessRejected("account_memberships_rejected")
    return result


def runtime_accounts(runtime: Settings) -> AccessRecords:
    return AccessRecords.model_validate(
        {
            kind: [
                row.model_dump(mode="json")
                for row in getattr(
                    runtime, "web_portal" if kind == "portal" else kind
                ).users
            ]
            for kind in KINDS
        }
    )


def prepare(runtime: Settings, namespace: UUID) -> dict:
    """Require the closed production-copy gate and current stored-account parity."""
    with transaction.atomic():
        target = ImportTarget.objects.select_for_update().get(pk=1)
        admission = AccountCopyAdmission.objects.select_for_update().get(target=target)
        if (
            target.fixture_enabled
            or admission.namespace != namespace
            or admission.enabled
            or not admission.closed
            or LegacyAccess.objects.exists()
            or ImportSource.objects.count() != 1
        ):
            raise AccessRejected("closed_production_copy_required")
        source = ImportSource.objects.get(
            namespace=namespace, source_identity=admission.source_identity
        )
        if (
            source.revision < 1
            or not source.importrun_set.filter(
                revision=source.revision,
                sequence=source.sequence,
                bundle_digest=source.bundle_digest,
            ).exists()
        ):
            raise AccessRejected("committed_copy_required")
        existing = Ownership.objects.filter(pk=1).first()
        if existing is not None:
            if existing.source_id != namespace:
                raise AccessRejected("ownership_binding_mismatch")
            owner(runtime)
            return {"prepared": True, "noop": True}
        # No inventory account may silently appear or disappear at handover.
        records = runtime_accounts(runtime)
        _validate_verifiers(records)
        verify_account_parity(records, runtime, database_state(source))
        value = Ownership.objects.create(
            source=source,
            key_identity=key_identity(runtime),
            target_identity=database_identity(runtime, target),
        )
        LinkGeneration.objects.bulk_create(
            [
                LinkGeneration(subject=row)
                for row in ProxySubject.objects.filter(source=source)
            ]
        )
        return {"prepared": True, "noop": False, "identity": str(value.identity)}


def ready(value: Ownership, runtime: Settings) -> bool:
    return (
        value.applied_revision == value.revision
        and value.applied_context == copy_context_identity(runtime)
        and not value.last_error
    )


def activate(runtime: Settings) -> dict:
    from sing_box_manager.managed_access.storage import require_fence

    with transaction.atomic():
        value = owner(runtime, lock=True)
        require_fence(UUID(str(value.identity)))
        if not ready(value, runtime):
            raise AccessRejected("node_application_required")
        if not value.active:
            verify_account_parity(
                runtime_accounts(runtime),
                runtime,
                database_state(cast("ImportSource", value.source)),
            )
        value.active = True
        value.save(update_fields=["active"])
    return {"active": True}


def _advance(value: Ownership) -> None:
    if value.revision >= MAX_COUNTER:
        raise AccessRejected("revision_exhausted")
    value.revision = cast("int", value.revision) + 1
    value.save(update_fields=["revision"])


def _validate_verifiers(records: AccessRecords) -> None:
    from sing_box_manager.managed_access.auth import verifier_supported

    if any(not verifier_supported(row.password_hash) for row in records.portal):
        raise AccessRejected("portal_verifier_cost_unsupported")


def _save_memberships(subject: ProxySubject, incoming: dict) -> None:
    for row in ImportedAccess.objects.filter(subject=subject):
        if row.kind not in incoming and row.present:
            row.present = False
            row.save(update_fields=["present"])
    for kind, payload in incoming.items():
        ImportedAccess.objects.update_or_create(
            subject=subject, kind=kind, defaults={"present": True, "payload": payload}
        )


def save_account(
    runtime: Settings,
    username: str,
    records: AccessRecords,
    *,
    subject_id: UUID | None,
    expected_revision: int,
) -> ProxySubject:
    _validate_verifiers(records)
    if not USERNAME_PATTERN.fullmatch(username) or set(records.memberships()) != {
        username
    }:
        raise AccessRejected("invalid_account_memberships")
    if any(
        row.password == signing_key(runtime).decode()
        for kind in KINDS
        if kind != "portal"
        for row in getattr(records, kind)
    ):
        raise AccessRejected("independent_subscription_key_required")
    with transaction.atomic():
        value = owner(runtime, active=True, lock=True)
        if value.revision != expected_revision:
            raise AccessRejected("account_edit_stale")
        if subject_id is None:
            if ProxySubject.objects.filter(
                source=value.source, username=username
            ).exists():
                raise AccessRejected("username_already_reserved")
            subject = ProxySubject.objects.create(
                source=value.source, username=username
            )
            LinkGeneration.objects.create(subject=subject)
        else:
            subject = ProxySubject.objects.get(
                pk=subject_id, source=value.source, present=True
            )
            if subject.username != username:
                raise AccessRejected("username_is_immutable")
        incoming = records.memberships()[username]
        if subject_id is not None and canonical(incoming) == canonical(
            {
                row.kind: row.payload
                for row in ImportedAccess.objects.filter(subject=subject, present=True)
            }
        ):
            return subject
        _save_memberships(subject, incoming)
        _advance(value)
        return subject


def retire(runtime: Settings, subject_id: UUID, *, expected_revision: int) -> None:
    with transaction.atomic():
        value = owner(runtime, active=True, lock=True)
        if value.revision != expected_revision:
            raise AccessRejected("account_edit_stale")
        subject = ProxySubject.objects.get(
            pk=subject_id, source=value.source, present=True
        )
        _rotate(subject.pk)
        subject.present = False
        subject.save(update_fields=["present"])
        ImportedAccess.objects.filter(subject=subject).update(present=False)
        _advance(value)


def token(runtime: Settings, subject_id: UUID, generation: int) -> str:
    raw = hmac.new(
        signing_key(runtime),
        b"managed-subscription-v1\0"
        + str(subject_id).encode()
        + b"\0"
        + str(generation).encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(raw[:16]).rstrip(b"=").decode("ascii")


def portal_account(value: Ownership, subject_id: UUID):
    subject = ProxySubject.objects.filter(
        pk=subject_id, source=value.source, present=True
    ).first()
    if subject is None:
        return None
    rows = accounts(value)
    portal = next(
        (
            row
            for row in rows.portal
            if row.username == subject.username and row.enabled
        ),
        None,
    )
    return None if portal is None else (subject, portal, rows)


def holds_protocols(runtime: Settings, records: AccessRecords, username: str) -> bool:
    protocols = user_protocols(inventory_from_records(runtime, records), username)
    return any(
        getattr(protocols, kind) is not None for kind in KINDS if kind != "portal"
    )


def resolve(runtime: Settings, value: Ownership, supplied: str):
    if TOKEN_PATTERN.fullmatch(supplied) is None:
        return None
    records = accounts(value)
    eligible = {row.username for row in records.portal if row.enabled}
    found = None
    for link in LinkGeneration.objects.filter(
        subject__source=value.source, subject__present=True
    ).select_related("subject"):
        if link.subject.username in eligible and hmac.compare_digest(
            token(runtime, link.subject_id, link.generation), supplied
        ):
            found = link.subject
    return None if found is None else (found, records)


def link_for(runtime: Settings, value: Ownership, subject_id: UUID) -> str | None:
    if runtime.control_plane.portal_origin is None:
        return None
    account = portal_account(value, subject_id)
    if account is None or not holds_protocols(runtime, account[2], account[0].username):
        return None
    link = LinkGeneration.objects.get(subject_id=subject_id)
    return f"{runtime.control_plane.portal_origin}/sub/{token(runtime, subject_id, link.generation)}"


def reset_link(runtime: Settings, subject_id: UUID) -> None:
    with transaction.atomic():
        value = owner(runtime, active=True, lock=True)
        account = portal_account(value, subject_id)
        if account is None or not holds_protocols(
            runtime, account[2], account[0].username
        ):
            raise AccessRejected("account_not_eligible")
        _rotate(subject_id)


def _rotate(subject_id: UUID) -> None:
    link = LinkGeneration.objects.select_for_update().get(subject_id=subject_id)
    if link.generation >= MAX_COUNTER:
        raise AccessRejected("link_generation_exhausted")
    link.generation += 1
    link.save(update_fields=["generation"])


def revoke_link(runtime: Settings, subject_id: UUID) -> None:
    """Operator action, including accounts that cannot currently log in."""
    with transaction.atomic():
        value = owner(runtime, active=True, lock=True)
        subject = ProxySubject.objects.get(
            pk=subject_id, source=value.source, present=True
        )
        _rotate(subject.pk)


def session_identity(runtime: Settings, subject_id: UUID, verifier: str) -> str:
    key = runtime.control_plane.secret_key
    if not key:
        raise AccessRejected("control_plane_key_required")
    return hmac.new(
        key.encode(),
        b"managed-portal-session-v1\0" + canonical([str(subject_id), verifier]),
        hashlib.sha256,
    ).hexdigest()


def node_snapshot(runtime: Settings) -> dict:
    """Private IPC data only. The command layer must not print this payload."""
    from django.db import connection

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        value = owner(runtime)
        if not value.active:
            verify_account_parity(
                runtime_accounts(runtime),
                runtime,
                database_state(cast("ImportSource", value.source)),
            )
        return {
            "active": value.active,
            "identity": str(value.identity),
            "revision": value.revision,
            "context": copy_context_identity(runtime),
            "access": accounts(value).model_dump(mode="json"),
        }


def begin_application(
    runtime: Settings, identity: UUID, revision: int, context: str
) -> None:
    with transaction.atomic():
        value = owner(runtime, lock=True)
        if (
            value.identity != identity
            or value.revision != revision
            or copy_context_identity(runtime) != context
        ):
            raise AccessRejected("node_plan_stale")
        value.last_error = "node_application_in_progress"
        value.save(update_fields=["last_error"])


def record_application(  # noqa: PLR0913 -- explicit node receipt binding
    runtime: Settings,
    identity: UUID,
    revision: int,
    context: str,
    digest: str,
    *,
    failed: bool,
) -> dict:
    with transaction.atomic():
        value = owner(runtime, lock=True)
        if value.identity != identity or not 1 <= revision <= value.revision:
            raise AccessRejected("node_receipt_binding_mismatch")
        if revision < value.applied_revision:
            raise AccessRejected("stale_node_receipt")
        if failed:
            value.last_error = "node_application_failed"
            value.save(update_fields=["last_error"])
        else:
            if (
                value.last_error != "node_application_in_progress"
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise AccessRejected("node_application_not_started")
            value.applied_revision, value.applied_context, value.applied_config = (
                revision,
                context,
                digest,
            )
            value.last_error = ""
            value.save(
                update_fields=[
                    "applied_revision",
                    "applied_context",
                    "applied_config",
                    "last_error",
                ]
            )
        return {
            "revision": value.revision,
            "applied_revision": value.applied_revision,
            "pending": not ready(value, runtime),
        }
