"""Transactional fixture imports. No production collector or serving integration."""

from __future__ import annotations

import hashlib
import hmac
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from django.db import connection, transaction

from sing_box_manager.control_plane.import_files import (
    decode_json,
    publish_private,
    read_private,
)
from sing_box_manager.control_plane.import_parity import (
    verify_parity,
    verify_projection,
)
from sing_box_manager.control_plane.import_schema import (
    AccessRecords,
    ImportRejected,
    SealedReport,
    Snapshot,
    canonical,
    keyed_identity,
    validate_context,
)
from sing_box_manager.control_plane.models import (
    ImportedAccess,
    ImportRun,
    ImportSource,
    ImportTarget,
    LegacyAccess,
    ProxySubject,
)
from sing_box_manager.settings import Settings


def _target(*, lock: bool = False) -> ImportTarget:
    targets = (
        ImportTarget.objects.select_for_update() if lock else ImportTarget.objects.all()
    )
    target = targets.get(pk=1)
    from sing_box_manager.account_copy.models import AccountCopyAdmission

    if not target.fixture_enabled or AccountCopyAdmission.objects.exists():
        raise ImportRejected("fixture_target_required")
    return target


def database_identity(runtime: Settings, target: ImportTarget) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), oid FROM pg_database WHERE datname = current_database()"
        )
        database, oid = cursor.fetchone()
    actual = connection.settings_dict
    control = runtime.control_plane
    if (
        database != control.database_name
        or actual["HOST"] != str(control.database_socket_dir)
        or int(actual["PORT"]) != control.database_port
    ):
        raise ImportRejected("database_configuration_mismatch")
    return keyed_identity(
        runtime,
        "import-database-v1",
        {
            "identity": str(target.identity),
            "database": database,
            "oid": oid,
            "socket": actual["HOST"],
            "port": int(actual["PORT"]),
            "user": actual["USER"],
        },
    )


def load_snapshot(path: Path, runtime: Settings) -> tuple[Snapshot, str]:
    content = read_private(path)
    try:
        snapshot = Snapshot.model_validate(decode_json(content))
    except Exception:
        raise ImportRejected("snapshot_rejected") from None
    validate_context(snapshot, runtime)
    return snapshot, hashlib.sha256(content).hexdigest()


def source_for(namespace: UUID, source_identity: UUID) -> ImportSource | None:
    other = ImportSource.objects.filter(source_identity=source_identity).first()
    if other is not None and other.namespace != namespace:
        raise ImportRejected("source_namespace_conflict")
    source = ImportSource.objects.filter(namespace=namespace).first()
    if source is not None and source.source_identity != source_identity:
        raise ImportRejected("source_identity_conflict")
    return source


def _source(snapshot: Snapshot) -> ImportSource | None:
    return source_for(snapshot.namespace, snapshot.source_identity)


def database_state(source: ImportSource | None) -> dict:
    """Read all fields, including absent memberships and counter-only history."""
    if source is None:
        return {"head": None, "subjects": [], "access": [], "legacy": []}
    return {
        "head": {
            "source_identity": str(source.source_identity),
            "sequence": source.sequence,
            "revision": source.revision,
            "bundle_digest": source.bundle_digest,
        },
        "subjects": [
            {"id": str(row.pk), "username": row.username, "present": row.present}
            for row in ProxySubject.objects.filter(source=source).order_by("username")
        ],
        "access": [
            {
                "username": row.subject.username,
                "kind": row.kind,
                "present": row.present,
                "payload": row.payload,
            }
            for row in ImportedAccess.objects.filter(subject__source=source)
            .select_related("subject")
            .order_by("subject__username", "kind")
        ],
        "legacy": [
            {
                "username": row.token_username,
                "subject": str(row.subject_id) if row.subject_id else None,
                "counter": row.counter,
                "secret_identity": row.secret_identity,
            }
            for row in LegacyAccess.objects.filter(source=source).order_by(
                "token_username"
            )
        ],
    }


def validate_account_transition(
    records: AccessRecords, sequence: int, digest: str, state: dict
) -> None:
    head = state["head"]
    if head and (
        sequence < head["sequence"]
        or (sequence == head["sequence"] and digest != head["bundle_digest"])
    ):
        raise ImportRejected("stale_or_conflicting_capture")
    incoming = records.memberships()
    subjects = {row["username"]: row for row in state["subjects"]}
    for name in incoming:
        if name in subjects and not subjects[name]["present"]:
            raise ImportRejected("retired_username_reappearance")
    for row in state["legacy"]:
        if row["subject"] is None and row["username"] in incoming:
            raise ImportRejected("retired_username_reappearance")


def _validate_transition(snapshot: Snapshot, digest: str, state: dict) -> None:
    validate_account_transition(snapshot.access, snapshot.sequence, digest, state)
    counters = snapshot.counter_map()
    for row in state["legacy"]:
        if counters.get(row["username"], 0) < row["counter"]:
            raise ImportRejected("counter_regression")


def _project_access(incoming: dict, state: dict, counts: dict) -> dict:
    access = {(row["username"], row["kind"]): deepcopy(row) for row in state["access"]}
    old_presence = {key: row["present"] for key, row in access.items()}
    for (name, kind), row in access.items():
        if row["present"] and kind not in incoming.get(name, {}):
            counts["access_removed"] += 1
        row["present"] = kind in incoming.get(name, {})
    for name, memberships in incoming.items():
        for kind, payload in memberships.items():
            key = (name, kind)
            if key not in access:
                counts["access_added"] += 1
            elif access[key]["payload"] != payload or not old_presence[key]:
                counts["access_changed"] += 1
            access[key] = {
                "username": name,
                "kind": kind,
                "present": True,
                "payload": payload,
            }
    return access


def project_accounts(
    records: AccessRecords, state: dict
) -> tuple[dict, dict[str, int]]:
    projected = deepcopy(state)
    incoming = records.memberships()
    subjects = {row["username"]: row for row in projected["subjects"]}
    counts = dict.fromkeys(
        (
            "subjects_added",
            "subjects_removed",
            "access_added",
            "access_changed",
            "access_removed",
            "counters_changed",
        ),
        0,
    )
    for name, row in subjects.items():
        if row["present"] and name not in incoming:
            counts["subjects_removed"] += 1
        row["present"] = name in incoming
    for name in incoming:
        if name not in subjects:
            subjects[name] = {"id": "new", "username": name, "present": True}
            counts["subjects_added"] += 1
    access = _project_access(incoming, state, counts)
    projected["subjects"] = sorted(subjects.values(), key=lambda row: row["username"])
    projected["access"] = sorted(
        access.values(), key=lambda row: (row["username"], row["kind"])
    )
    return projected, counts


def _project(snapshot: Snapshot, state: dict) -> tuple[dict, dict[str, int]]:
    projected, counts = project_accounts(snapshot.access, state)
    incoming = snapshot.access.memberships()
    subjects = {row["username"]: row for row in projected["subjects"]}
    legacy = {row["username"]: row for row in projected["legacy"]}
    counters = snapshot.counter_map()
    for name in set(legacy) | set(incoming) | set(counters):
        counter = counters.get(name, 0)
        if name not in legacy or legacy[name]["counter"] != counter:
            counts["counters_changed"] += 1
        legacy[name] = {
            "username": name,
            "subject": subjects[name]["id"] if name in subjects else None,
            "counter": counter,
            "secret_identity": snapshot.secret_identity,
        }
    projected["legacy"] = sorted(legacy.values(), key=lambda row: row["username"])
    return projected, counts


def _binding(
    snapshot: Snapshot, digest: str, runtime: Settings, target: ImportTarget
) -> dict:
    return {
        "version": 1,
        "namespace": str(snapshot.namespace),
        "source_identity": str(snapshot.source_identity),
        "sequence": snapshot.sequence,
        "bundle_digest": digest,
        "context_identity": snapshot.context_identity,
        "secret_identity": snapshot.secret_identity,
        "target_identity": database_identity(runtime, target),
    }


def preview(snapshot_path: Path, report_path: Path, runtime: Settings) -> dict:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        target = _target()
        snapshot, digest = load_snapshot(snapshot_path, runtime)
        body = _binding(snapshot, digest, runtime, target)
        source = _source(snapshot)
        state = database_state(source)
        body.update(
            expected_revision=source.revision if source else 0,
            state_identity=keyed_identity(runtime, "import-state-v1", state),
        )
        try:
            _validate_transition(snapshot, digest, state)
            projection, counts = _project(snapshot, state)
            checks = verify_parity(snapshot, runtime, projection)
            body.update(status="ready", counts=counts, projection_checks=checks)
        except ImportRejected as error:
            body.update(status="rejected", error=str(error))
    sealed = {"body": body, "seal": keyed_identity(runtime, "import-report-v1", body)}
    publish_private(report_path, canonical(sealed) + b"\n")
    if body["status"] != "ready":
        raise ImportRejected("preview_rejected")
    return body


def _read_report(path: Path, runtime: Settings) -> tuple[dict, str]:
    content = read_private(path)
    try:
        report = SealedReport.model_validate(decode_json(content))
    except ValueError:
        raise ImportRejected("report_rejected") from None
    if (
        content != canonical({"body": report.body, "seal": report.seal}) + b"\n"
        or not hmac.compare_digest(
            report.seal, keyed_identity(runtime, "import-report-v1", report.body)
        )
        or report.body.get("status") != "ready"
    ):
        raise ImportRejected("report_rejected")
    return report.body, hashlib.sha256(content).hexdigest()


def lock_source(namespace: UUID, source_identity: UUID) -> None:
    # Lock both bindings to serialize first imports, including namespace conflicts.
    keys = sorted(
        {
            int.from_bytes(
                hashlib.sha256(label.encode()).digest()[:8], "big", signed=True
            )
            for label in (
                f"namespace:{namespace}",
                f"source:{source_identity}",
            )
        }
    )
    with connection.cursor() as cursor:
        for key in keys:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def _lock(snapshot: Snapshot) -> None:
    lock_source(snapshot.namespace, snapshot.source_identity)


def copy_accounts(records: AccessRecords, source: ImportSource) -> dict:
    incoming = records.memberships()
    subjects = {row.username: row for row in ProxySubject.objects.filter(source=source)}
    ProxySubject.objects.filter(source=source).update(present=False)
    ImportedAccess.objects.filter(subject__source=source).update(present=False)
    for name, memberships in incoming.items():
        subject = subjects.get(name)
        if subject is None:
            subject = ProxySubject.objects.create(source=source, username=name)
            subjects[name] = subject
        else:
            subject.present = True
            subject.save(update_fields=["present"])
        for kind, payload in memberships.items():
            ImportedAccess.objects.update_or_create(
                subject=subject,
                kind=kind,
                defaults={"present": True, "payload": payload},
            )
    return subjects


def _copy(snapshot: Snapshot, source: ImportSource) -> None:
    subjects = copy_accounts(snapshot.access, source)
    incoming = snapshot.access.memberships()
    counters = snapshot.counter_map()
    previous = set(
        LegacyAccess.objects.filter(source=source).values_list(
            "token_username", flat=True
        )
    )
    for name in previous | set(incoming) | set(counters):
        LegacyAccess.objects.update_or_create(
            source=source,
            token_username=name,
            defaults={
                "subject": subjects.get(name),
                "counter": counters.get(name, 0),
                "secret_identity": snapshot.secret_identity,
            },
        )


def _record_receipt(
    source: ImportSource, report_digest: str, bundle_digest: str, counts: dict
) -> None:
    ImportRun.objects.create(
        source=source,
        report_digest=report_digest,
        bundle_digest=bundle_digest,
        sequence=source.sequence,
        revision=source.revision,
        counts=counts,
    )


def apply(snapshot_path: Path, report_path: Path, runtime: Settings) -> dict:
    _target()
    snapshot, digest = load_snapshot(snapshot_path, runtime)
    body, report_digest = _read_report(report_path, runtime)
    with transaction.atomic():
        _lock(snapshot)
        target = _target(lock=True)
        expected = _binding(snapshot, digest, runtime, target)
        if any(body.get(key) != value for key, value in expected.items()):
            raise ImportRejected("report_binding_mismatch")
        source = _source(snapshot)
        receipt = ImportRun.objects.filter(report_digest=report_digest).first()
        if receipt is not None:
            if (
                receipt.source_id != snapshot.namespace
                or receipt.bundle_digest != digest
            ):
                raise ImportRejected("receipt_binding_mismatch")
            return {
                "counts": receipt.counts,
                "noop": True,
                "superseded": source is not None
                and source.revision != receipt.revision,
            }
        state = database_state(source)
        if body.get("expected_revision") != (
            source.revision if source else 0
        ) or not hmac.compare_digest(
            body.get("state_identity", ""),
            keyed_identity(runtime, "import-state-v1", state),
        ):
            raise ImportRejected("preview_invalidated")
        _validate_transition(snapshot, digest, state)
        if source is not None and source.sequence == snapshot.sequence:
            verify_parity(snapshot, runtime, state)
            _record_receipt(source, report_digest, digest, body["counts"])
            return {"counts": body["counts"], "noop": True, "superseded": False}
        projection, counts = _project(snapshot, state)
        if source is None:
            source = ImportSource.objects.create(
                namespace=snapshot.namespace, source_identity=snapshot.source_identity
            )
        _copy(snapshot, source)
        copied = database_state(source)
        verify_projection(projection, copied)
        verify_parity(snapshot, runtime, copied)
        source.sequence = snapshot.sequence
        source.revision = (state["head"]["revision"] if state["head"] else 0) + 1
        source.bundle_digest = digest
        source.save(update_fields=["sequence", "revision", "bundle_digest"])
        _record_receipt(source, report_digest, digest, counts)
        return {"counts": counts, "noop": False, "superseded": False}
