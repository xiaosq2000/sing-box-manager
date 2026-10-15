"""Explicit account-copy admission and report-bound transactions. No token state."""

import hashlib
import hmac
from pathlib import Path
from uuid import UUID

from django.db import connection, transaction

from sing_box_manager.account_copy.files import publish_private, read_private
from sing_box_manager.account_copy.models import AccountCopyAdmission
from sing_box_manager.account_copy.schema import (
    POLICY,
    PURPOSE,
    AccountSnapshot,
    copy_context_identity,
)
from sing_box_manager.control_plane import import_service as shared
from sing_box_manager.control_plane.import_files import decode_json
from sing_box_manager.control_plane.import_parity import (
    verify_account_parity,
    verify_projection,
)
from sing_box_manager.control_plane.import_schema import (
    ImportRejected,
    SealedReport,
    canonical,
    keyed_identity,
)
from sing_box_manager.control_plane.models import (
    ImportRun,
    ImportSource,
    ImportTarget,
    LegacyAccess,
    ProxySubject,
)
from sing_box_manager.settings import Settings


def _target(runtime: Settings, *, lock: bool = False) -> ImportTarget:
    rows = (
        ImportTarget.objects.select_for_update() if lock else ImportTarget.objects.all()
    )
    target = rows.get(pk=1)
    if target.fixture_enabled:
        raise ImportRejected("production_target_required")
    shared.database_identity(runtime, target)
    return target


def authorize(runtime: Settings, namespace: UUID, source_identity: UUID) -> dict:
    """The explicit operator command is the authorization, not export or preview."""
    with transaction.atomic():
        target = _target(runtime, lock=True)
        admission = (
            AccountCopyAdmission.objects.select_for_update()
            .filter(target=target)
            .first()
        )
        if admission is not None:
            if (
                admission.namespace != namespace
                or admission.source_identity != source_identity
            ):
                raise ImportRejected("admission_binding_mismatch")
            if admission.closed or not admission.enabled:
                raise ImportRejected("admission_closed")
            return {"authorized": True, "noop": True}
        if (
            ImportSource.objects.exists()
            or LegacyAccess.objects.exists()
            or ProxySubject.objects.exists()
        ):
            raise ImportRejected("empty_copy_target_required")
        AccountCopyAdmission.objects.create(
            target=target,
            namespace=namespace,
            source_identity=source_identity,
            enabled=True,
        )
    return {"authorized": True, "noop": False}


def close(runtime: Settings, namespace: UUID, source_identity: UUID) -> dict:
    with transaction.atomic():
        target = _target(runtime, lock=True)
        admission = (
            AccountCopyAdmission.objects.select_for_update()
            .filter(target=target)
            .first()
        )
        if (
            admission is None
            or admission.namespace != namespace
            or admission.source_identity != source_identity
        ):
            raise ImportRejected("admission_binding_mismatch")
        admission.enabled = False
        admission.closed = True
        admission.save(update_fields=["enabled", "closed"])
    return {"closed": True}


def _gate(runtime: Settings, snapshot: AccountSnapshot, *, lock: bool = False):
    target = _target(runtime, lock=lock)
    rows = (
        AccountCopyAdmission.objects.select_for_update()
        if lock
        else AccountCopyAdmission.objects.all()
    )
    admission = rows.filter(target=target).first()
    if admission is None or not admission.enabled or admission.closed:
        raise ImportRejected("account_copy_not_authorized")
    if (
        str(admission.namespace) != snapshot.namespace
        or str(admission.source_identity) != snapshot.source_identity
    ):
        raise ImportRejected("admission_binding_mismatch")
    if (
        LegacyAccess.objects.exists()
        or ImportSource.objects.exclude(namespace=admission.namespace).exists()
    ):
        raise ImportRejected("account_copy_state_rejected")
    return target, admission


def load_snapshot(path: Path, runtime: Settings) -> tuple[AccountSnapshot, str]:
    raw = read_private(path)
    try:
        snapshot = AccountSnapshot.model_validate(decode_json(raw))
        valid = raw == canonical(snapshot.model_dump(mode="json")) + b"\n"
    except Exception:
        raise ImportRejected("snapshot_rejected") from None
    if not valid:
        raise ImportRejected("snapshot_rejected")
    if not hmac.compare_digest(
        snapshot.context_identity, copy_context_identity(runtime)
    ):
        raise ImportRejected("context_mismatch")
    return snapshot, hashlib.sha256(raw).hexdigest()


def _binding(snapshot, digest, runtime, target, admission):
    return {
        "version": 1,
        "purpose": PURPOSE,
        "link_policy": POLICY,
        "admission": str(admission.pk),
        "namespace": snapshot.namespace,
        "source_identity": snapshot.source_identity,
        "sequence": snapshot.sequence,
        "bundle_digest": digest,
        "context_identity": snapshot.context_identity,
        "target_identity": shared.database_identity(runtime, target),
    }


def _source(snapshot):
    return shared.source_for(UUID(snapshot.namespace), UUID(snapshot.source_identity))


def _projection(snapshot, digest, runtime, state):
    shared.validate_account_transition(
        snapshot.access, snapshot.sequence, digest, state
    )
    projected, counts = shared.project_accounts(snapshot.access, state)
    checks = verify_account_parity(snapshot.access, runtime, projected)
    return projected, counts, checks


def preview(snapshot_path: Path, report_path: Path, runtime: Settings) -> dict:
    snapshot, digest = load_snapshot(snapshot_path, runtime)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        target, admission = _gate(runtime, snapshot)
        source = _source(snapshot)
        state = shared.database_state(source)
        body = _binding(snapshot, digest, runtime, target, admission)
        body.update(
            expected_revision=source.revision if source else 0,
            state_identity=keyed_identity(runtime, "account-copy-state-v1", state),
        )
        try:
            _, counts, checks = _projection(snapshot, digest, runtime, state)
            body.update(status="ready", counts=counts, projection_checks=checks)
        except ImportRejected as error:
            body.update(status="rejected", error=str(error))
    sealed = {
        "body": body,
        "seal": keyed_identity(runtime, "account-copy-report-v1", body),
    }
    publish_private(report_path, canonical(sealed) + b"\n")
    if body["status"] != "ready":
        raise ImportRejected("preview_rejected")
    return body


def _report(path: Path, runtime: Settings) -> tuple[dict, str]:
    raw = read_private(path)
    try:
        report = SealedReport.model_validate(decode_json(raw))
        rejected = (
            raw != canonical({"body": report.body, "seal": report.seal}) + b"\n"
            or report.body.get("purpose") != PURPOSE
            or report.body.get("link_policy") != POLICY
            or report.body.get("status") != "ready"
            or not hmac.compare_digest(
                report.seal,
                keyed_identity(runtime, "account-copy-report-v1", report.body),
            )
        )
    except Exception:
        raise ImportRejected("report_rejected") from None
    if rejected:
        raise ImportRejected("report_rejected")
    return report.body, hashlib.sha256(raw).hexdigest()


def _receipt(source, report_digest, digest, counts):
    ImportRun.objects.create(
        source=source,
        report_digest=report_digest,
        bundle_digest=digest,
        sequence=source.sequence,
        revision=source.revision,
        counts=counts,
    )


def apply(snapshot_path: Path, report_path: Path, runtime: Settings) -> dict:
    snapshot, digest = load_snapshot(snapshot_path, runtime)
    body, report_digest = _report(report_path, runtime)
    with transaction.atomic():
        shared.lock_source(UUID(snapshot.namespace), UUID(snapshot.source_identity))
        target, admission = _gate(runtime, snapshot, lock=True)
        expected = _binding(snapshot, digest, runtime, target, admission)
        if any(body.get(key) != value for key, value in expected.items()):
            raise ImportRejected("report_binding_mismatch")
        source = _source(snapshot)
        receipt = ImportRun.objects.filter(report_digest=report_digest).first()
        if receipt is not None:
            if (
                receipt.source_id != UUID(snapshot.namespace)
                or receipt.bundle_digest != digest
                or receipt.sequence != snapshot.sequence
            ):
                raise ImportRejected("receipt_binding_mismatch")
            return {
                "counts": receipt.counts,
                "noop": True,
                "superseded": source is not None
                and source.revision != receipt.revision,
            }
        state = shared.database_state(source)
        if body.get("expected_revision") != (
            source.revision if source else 0
        ) or body.get("state_identity") != keyed_identity(
            runtime, "account-copy-state-v1", state
        ):
            raise ImportRejected("preview_invalidated")
        projected, counts, checks = _projection(snapshot, digest, runtime, state)
        if body.get("counts") != counts or body.get("projection_checks") != checks:
            raise ImportRejected("report_binding_mismatch")
        if source is not None and source.sequence == snapshot.sequence:
            verify_account_parity(snapshot.access, runtime, state)
            _receipt(source, report_digest, digest, counts)
            return {"counts": counts, "noop": True, "superseded": False}
        if source is None:
            source = ImportSource.objects.create(
                namespace=UUID(snapshot.namespace),
                source_identity=UUID(snapshot.source_identity),
            )
        shared.copy_accounts(snapshot.access, source)
        copied = shared.database_state(source)
        verify_projection(projected, copied)
        verify_account_parity(snapshot.access, runtime, copied)
        source.sequence = snapshot.sequence
        source.revision = (state["head"]["revision"] if state["head"] else 0) + 1
        source.bundle_digest = digest
        source.save(update_fields=["sequence", "revision", "bundle_digest"])
        _receipt(source, report_digest, digest, counts)
        return {"counts": counts, "noop": False, "superseded": False}
