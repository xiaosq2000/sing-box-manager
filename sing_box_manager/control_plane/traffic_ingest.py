"""Durable PostgreSQL ingestion for the frozen node traffic wire codec.

The service accepts a caller-authenticated node UUID and the raw request body.
It intentionally implements no HTTP, authentication, node registration, policy
generation, local collection, exporter or history import behavior.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import cast
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from sing_box_manager.control_plane.models import (
    Credential,
    TrafficSnapshot,
    TrafficSource,
)
from sing_box_manager.control_plane.traffic_contract import (
    AttributionSnapshot,
    CounterSnapshot,
    Receipt,
    ReceiptResponse,
    Snapshot,
    TrafficRequest,
    canonical_payload_bytes,
    encode_receipt_response,
    parse_request,
)


class TrafficIngestErrorCode(StrEnum):
    """Fixed service errors. Messages never carry request or database data."""

    OWNERSHIP_MISMATCH = "ownership_mismatch"
    VERSION_CONFLICT = "version_conflict"
    COUNTER_REGRESSION = "counter_regression"


class TrafficIngestError(Exception):
    """Raised for an ownership, version or counter contract failure."""

    code: TrafficIngestErrorCode

    def __init__(self, code: TrafficIngestErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


SnapshotKeyRow = tuple[UUID, UUID, str, date]


def _snapshot_key(snapshot: Snapshot) -> SnapshotKeyRow:
    return (
        UUID(snapshot.source_id),
        UUID(snapshot.credential_id),
        snapshot.kind,
        snapshot.accounting_date,
    )


def _require_owned_identities(node_id: UUID, request: TrafficRequest) -> None:
    source_ids = sorted({UUID(snapshot.source_id) for snapshot in request.snapshots})
    credential_ids = sorted(
        {UUID(snapshot.credential_id) for snapshot in request.snapshots}
    )

    sources = list(
        TrafficSource._default_manager.select_for_update()  # noqa: SLF001
        .filter(node_id=node_id, id__in=source_ids)
        .order_by("id")
    )
    if {source.id for source in sources} != set(source_ids):
        raise TrafficIngestError(TrafficIngestErrorCode.OWNERSHIP_MISMATCH)

    credentials = list(
        Credential.objects.select_for_update()
        .filter(node_id=node_id, uuid__in=credential_ids)
        .only("id", "uuid", "node_id")
        .order_by("uuid")
    )
    if {credential.uuid for credential in credentials} != set(credential_ids):
        raise TrafficIngestError(TrafficIngestErrorCode.OWNERSHIP_MISMATCH)


def _load_current(request: TrafficRequest) -> dict[SnapshotKeyRow, TrafficSnapshot]:
    key_filter = Q()
    for snapshot in request.snapshots:
        source_id, credential_id, kind, accounting_date = _snapshot_key(snapshot)
        key_filter |= Q(
            source_id=source_id,
            credential_id=credential_id,
            kind=kind,
            accounting_date=accounting_date,
        )
    rows = (
        TrafficSnapshot.objects.select_for_update()
        .filter(key_filter)
        .defer("payload")
        .order_by("source_id", "credential_id", "kind", "accounting_date")
    )
    return {
        (row.source_id, row.credential_id, row.kind, row.accounting_date): row
        for row in rows
    }


def _scalar_projection(
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> tuple[int | None, int | None, str | None]:
    if isinstance(snapshot, CounterSnapshot):
        return snapshot.upload, snapshot.download, None
    return None, None, snapshot.detail


def _insert_snapshot(snapshot: CounterSnapshot | AttributionSnapshot) -> None:
    upload, download, detail = _scalar_projection(snapshot)
    TrafficSnapshot.objects.create(
        source_id=UUID(snapshot.source_id),
        credential_id=UUID(snapshot.credential_id),
        kind=snapshot.kind,
        accounting_date=snapshot.accounting_date,
        version=snapshot.version,
        digest=snapshot.digest,
        payload=canonical_payload_bytes(snapshot),
        upload=upload,
        download=download,
        detail=detail,
    )


def _replace_snapshot(
    current: TrafficSnapshot,
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> None:
    upload, download, detail = _scalar_projection(snapshot)
    current.kind = snapshot.kind
    current.accounting_date = snapshot.accounting_date
    current.version = snapshot.version
    current.digest = snapshot.digest
    current.payload = canonical_payload_bytes(snapshot)
    current.upload = upload
    current.download = download
    current.detail = detail
    current.save(
        update_fields=[
            "kind",
            "accounting_date",
            "version",
            "digest",
            "payload",
            "upload",
            "download",
            "detail",
            "updated_at",
        ]
    )


def _receipt(
    snapshot: CounterSnapshot | AttributionSnapshot,
    version: int,
    digest: str,
) -> Receipt:
    return Receipt(
        source_id=snapshot.source_id,
        credential_id=snapshot.credential_id,
        kind=snapshot.kind,
        accounting_date=snapshot.accounting_date,
        version=version,
        digest=digest,
    )


def _apply_snapshot(
    snapshot: CounterSnapshot | AttributionSnapshot,
    current: TrafficSnapshot | None,
) -> Receipt:
    if current is None:
        _insert_snapshot(snapshot)
        return _receipt(snapshot, snapshot.version, snapshot.digest)

    current_version = cast("int", current.version)
    current_digest = cast("str", current.digest)

    if snapshot.version < current_version:
        return _receipt(snapshot, current_version, current_digest)

    if snapshot.version == current_version:
        if snapshot.digest != current_digest:
            raise TrafficIngestError(TrafficIngestErrorCode.VERSION_CONFLICT)
        return _receipt(snapshot, current_version, current_digest)

    if isinstance(snapshot, CounterSnapshot) and (
        snapshot.upload < cast("int", current.upload)
        or snapshot.download < cast("int", current.download)
    ):
        raise TrafficIngestError(TrafficIngestErrorCode.COUNTER_REGRESSION)

    _replace_snapshot(current, snapshot)
    return _receipt(snapshot, snapshot.version, snapshot.digest)


def ingest_page(node_id: UUID, raw_body: bytes) -> bytes:
    """Ingest one validated page and return canonical receipts after commit.

    The caller must already have authenticated ``node_id``. A surrounding
    transaction is caller misuse: this service owns the durable commit boundary
    and must not acknowledge work that the caller can still roll back.
    """

    request = parse_request(raw_body)
    connection = transaction.get_connection()
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError(
            "traffic ingestion requires its own top-level durable transaction"
        )

    with transaction.atomic(durable=True):
        _require_owned_identities(node_id, request)
        current_by_key = _load_current(request)
        receipts = [
            _apply_snapshot(snapshot, current_by_key.get(_snapshot_key(snapshot)))
            for snapshot in request.snapshots
        ]
        response = ReceiptResponse(schema=1, receipts=tuple(receipts))
        receipt_bytes = encode_receipt_response(response, request)

    return receipt_bytes  # noqa: RET504 - return only after the durable block exits


__all__ = [
    "TrafficIngestError",
    "TrafficIngestErrorCode",
    "ingest_page",
]
