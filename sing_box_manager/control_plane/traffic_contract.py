"""Pure wire codec for versioned node traffic snapshots and receipts.

This module implements the `node-traffic-v1` contract. It has no Django,
database, HTTP or other runtime side effects; the later Ninja endpoint reuses
these types and functions after it has read the bounded request body.

The canonical encoding rule is deliberately the standard-library JSON encoder
with `ensure_ascii=False`, sorted compact keys, and the two line separators
written as lowercase `\\u2028` / `\\u2029` escapes. Names are otherwise
preserved exactly: no case folding, NFC/NFD normalization, truncation or
aggregate rewriting happens here.

The codec enforces the 10,000 named-identity ceiling across both attribution
arrays. The five reserved aggregate array slots are excluded from that count;
names are otherwise preserved exactly.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

INT64_MAX = 2**63 - 1
MAX_RAW_BODY_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 768 * 1024
MAX_PAGE_ITEMS = 1024
MAX_NAMED_IDENTITIES = 10_000

# Exact aggregate array slots: three domain slots and two destination slots.
# __ip__ is a domain aggregate only; as a destination name it is a named identity.
_DOMAIN_AGGREGATE_NAMES = frozenset({"__other__", "__unattributed__", "__ip__"})
_DESTINATION_AGGREGATE_NAMES = frozenset({"__other__", "__unattributed__"})

type Kind = Literal["counter", "attribution"]
type Detail = Literal["exact", "compacted", "degraded"]

_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LINE_SEPARATOR_ESCAPES = {"\u2028": "\\u2028", "\u2029": "\\u2029"}


class TrafficErrorCode(StrEnum):
    """Fixed wire error codes. Messages never carry raw input."""

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DUPLICATE_SNAPSHOT_KEY = "duplicate_snapshot_key"
    DUPLICATE_NAME = "duplicate_name"
    SNAPSHOT_DIGEST_MISMATCH = "snapshot_digest_mismatch"
    REQUEST_TOO_LARGE = "request_too_large"
    SNAPSHOT_TOO_LARGE = "snapshot_too_large"
    RECEIPT_KEY_MISMATCH = "receipt_key_mismatch"


class TrafficContractError(Exception):
    """Raised for every invalid request or receipt on the wire.

    The exception carries only a fixed code. It intentionally does not retain
    raw payloads, validator output or field values.
    """

    code: TrafficErrorCode

    def __init__(self, code: TrafficErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, order=True)
class SnapshotKey:
    """The immutable identity of one versioned snapshot bucket."""

    source_id: str
    credential_id: str
    kind: Kind
    accounting_date: date


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        serialize_by_alias=True,
        strict=True,
    )


def _invalid() -> TrafficContractError:
    return TrafficContractError(TrafficErrorCode.INVALID_REQUEST)


def _require_utf8(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    return value


def _canonical_uuid(value: str) -> str:
    if _UUID_PATTERN.fullmatch(value) is None:
        raise _invalid()
    return value


def _canonical_digest(value: str) -> str:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise _invalid()
    return value


def _accounting_date(value: object) -> date:
    if type(value) is date:
        return value
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        raise _invalid()
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise _invalid() from None


class AttributionEntry(_ContractModel):
    name: str
    upload: int = Field(ge=0, le=INT64_MAX)
    download: int = Field(ge=0, le=INT64_MAX)
    connections: int = Field(ge=0, le=INT64_MAX)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value:
            raise _invalid()
        return _require_utf8(value)


class _SnapshotIdentityModel(_ContractModel):
    source_id: str
    credential_id: str
    accounting_date: date
    version: int = Field(ge=1, le=INT64_MAX)
    digest: str

    @field_validator("source_id", "credential_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("accounting_date", mode="before")
    @classmethod
    def validate_date(cls, value: object) -> date:
        return _accounting_date(value)

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _canonical_digest(value)


class CounterSnapshot(_SnapshotIdentityModel):
    kind: Literal["counter"]
    upload: int = Field(ge=0, le=INT64_MAX)
    download: int = Field(ge=0, le=INT64_MAX)


class AttributionSnapshot(_SnapshotIdentityModel):
    kind: Literal["attribution"]
    detail: Detail
    domains: tuple[AttributionEntry, ...]
    destinations: tuple[AttributionEntry, ...]

    @field_validator("domains", "destinations", mode="before")
    @classmethod
    def validate_entry_sequence(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value

    @field_validator("domains", "destinations")
    @classmethod
    def sort_and_deduplicate(
        cls, value: tuple[AttributionEntry, ...]
    ) -> tuple[AttributionEntry, ...]:
        names: set[str] = set()
        for entry in value:
            if entry.name in names:
                raise TrafficContractError(TrafficErrorCode.DUPLICATE_NAME)
            names.add(entry.name)
        return tuple(sorted(value, key=lambda entry: entry.name.encode("utf-8")))

    @model_validator(mode="after")
    def validate_named_identity_count(self) -> Self:
        named = sum(
            1 for entry in self.domains if entry.name not in _DOMAIN_AGGREGATE_NAMES
        )
        named += sum(
            1
            for entry in self.destinations
            if entry.name not in _DESTINATION_AGGREGATE_NAMES
        )
        if named > MAX_NAMED_IDENTITIES:
            raise _invalid()
        return self


type Snapshot = Annotated[
    CounterSnapshot | AttributionSnapshot,
    Field(discriminator="kind"),
]


class Receipt(_SnapshotIdentityModel):
    kind: Kind


def _as_tuple(value: object) -> object:
    return tuple(value) if type(value) is list else value


class _VersionedEnvelope(_ContractModel):
    schema_version: int = Field(alias="schema", ge=1, le=1)

    @property
    def schema(self) -> int:
        return self.schema_version


class TrafficRequest(_VersionedEnvelope):
    snapshots: tuple[Snapshot, ...]

    @field_validator("snapshots", mode="before")
    @classmethod
    def validate_snapshot_sequence(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("snapshots")
    @classmethod
    def validate_page_size(cls, value: tuple[Snapshot, ...]) -> tuple[Snapshot, ...]:
        if not 1 <= len(value) <= MAX_PAGE_ITEMS:
            raise _invalid()
        return value


class ReceiptResponse(_VersionedEnvelope):
    receipts: tuple[Receipt, ...]

    @field_validator("receipts", mode="before")
    @classmethod
    def validate_receipt_sequence(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("receipts")
    @classmethod
    def validate_page_size(cls, value: tuple[Receipt, ...]) -> tuple[Receipt, ...]:
        if not 1 <= len(value) <= MAX_PAGE_ITEMS:
            raise _invalid()
        return value


def snapshot_key(
    snapshot: CounterSnapshot | AttributionSnapshot | Receipt,
) -> SnapshotKey:
    """Return the key used for duplicate checks and receipt coverage."""

    return SnapshotKey(
        source_id=snapshot.source_id,
        credential_id=snapshot.credential_id,
        kind=snapshot.kind,
        accounting_date=snapshot.accounting_date,
    )


def _entry_document(entry: AttributionEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "upload": entry.upload,
        "download": entry.download,
        "connections": entry.connections,
    }


def snapshot_payload(
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> dict[str, object]:
    """Return the digest payload: the snapshot without its `digest` field."""

    payload: dict[str, object] = {
        "source_id": snapshot.source_id,
        "credential_id": snapshot.credential_id,
        "kind": snapshot.kind,
        "accounting_date": snapshot.accounting_date.isoformat(),
        "version": snapshot.version,
    }
    if isinstance(snapshot, CounterSnapshot):
        payload["upload"] = snapshot.upload
        payload["download"] = snapshot.download
    else:
        payload["detail"] = snapshot.detail
        payload["domains"] = [_entry_document(entry) for entry in snapshot.domains]
        payload["destinations"] = [
            _entry_document(entry) for entry in snapshot.destinations
        ]
    return payload


def _snapshot_document(
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> dict[str, object]:
    document = snapshot_payload(snapshot)
    document["digest"] = snapshot.digest
    return document


def _receipt_document(receipt: Receipt) -> dict[str, object]:
    return {
        "source_id": receipt.source_id,
        "credential_id": receipt.credential_id,
        "kind": receipt.kind,
        "accounting_date": receipt.accounting_date.isoformat(),
        "version": receipt.version,
        "digest": receipt.digest,
    }


def canonical_json_bytes(value: object) -> bytes:
    """Encode one JSON value with the shared canonical rule."""

    text = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for literal, escaped in _LINE_SEPARATOR_ESCAPES.items():
        text = text.replace(literal, escaped)
    return text.encode("utf-8")


def canonical_payload_bytes(
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> bytes:
    return canonical_json_bytes(snapshot_payload(snapshot))


def snapshot_digest(snapshot: CounterSnapshot | AttributionSnapshot) -> str:
    return sha256(canonical_payload_bytes(snapshot)).hexdigest()


def canonical_snapshot_bytes(
    snapshot: CounterSnapshot | AttributionSnapshot,
) -> bytes:
    return canonical_json_bytes(_snapshot_document(snapshot))


def canonical_request_bytes(request: TrafficRequest) -> bytes:
    return canonical_json_bytes(
        {
            "schema": request.schema,
            "snapshots": [
                _snapshot_document(snapshot) for snapshot in request.snapshots
            ],
        }
    )


def canonical_receipt_response_bytes(response: ReceiptResponse) -> bytes:
    return canonical_json_bytes(
        {
            "schema": response.schema,
            "receipts": [_receipt_document(receipt) for receipt in response.receipts],
        }
    )


def _pair_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _invalid()


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _invalid()
    return parsed


def _as_bytes(raw: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("raw must be bytes-like")
    return bytes(raw)


def _load_json(raw: bytes | bytearray | memoryview) -> object:
    data = _as_bytes(raw)
    if len(data) > MAX_RAW_BODY_BYTES:
        raise TrafficContractError(TrafficErrorCode.REQUEST_TOO_LARGE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    try:
        return json.loads(
            text,
            object_pairs_hook=_pair_hook,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except TrafficContractError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise _invalid() from None


def _validate_schema(document: object) -> None:
    if not isinstance(document, dict):
        raise _invalid()
    mapping = cast("dict[str, object]", document)
    if "schema" not in mapping:
        raise _invalid()
    schema = mapping["schema"]
    if type(schema) is not int:
        raise _invalid()
    if schema != 1:
        raise TrafficContractError(TrafficErrorCode.UNSUPPORTED_SCHEMA)


def _validate_snapshot(snapshot: CounterSnapshot | AttributionSnapshot) -> None:
    if len(canonical_snapshot_bytes(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise TrafficContractError(TrafficErrorCode.SNAPSHOT_TOO_LARGE)
    if snapshot_digest(snapshot) != snapshot.digest:
        raise TrafficContractError(TrafficErrorCode.SNAPSHOT_DIGEST_MISMATCH)


def _validate_request(request: TrafficRequest) -> None:
    keys = [snapshot_key(snapshot) for snapshot in request.snapshots]
    if len(set(keys)) != len(keys):
        raise TrafficContractError(TrafficErrorCode.DUPLICATE_SNAPSHOT_KEY)
    for snapshot in request.snapshots:
        _validate_snapshot(snapshot)
    if len(canonical_request_bytes(request)) > MAX_RAW_BODY_BYTES:
        raise TrafficContractError(TrafficErrorCode.REQUEST_TOO_LARGE)


def parse_request(raw: bytes | bytearray | memoryview) -> TrafficRequest:
    """Parse and fully validate one bounded traffic request body."""

    document = _load_json(raw)
    _validate_schema(document)
    try:
        request = TrafficRequest.model_validate(document)
    except ValidationError:
        raise _invalid() from None
    _validate_request(request)
    return request


def validate_receipt_coverage(
    request: TrafficRequest, response: ReceiptResponse
) -> None:
    """Require exact request/receipt key equality before any acknowledgement."""

    keys = [snapshot_key(snapshot) for snapshot in request.snapshots]
    if len(set(keys)) != len(keys):
        raise TrafficContractError(TrafficErrorCode.DUPLICATE_SNAPSHOT_KEY)
    expected = set(keys)
    seen: set[SnapshotKey] = set()
    for receipt in response.receipts:
        key = snapshot_key(receipt)
        if key in seen:
            raise TrafficContractError(TrafficErrorCode.RECEIPT_KEY_MISMATCH)
        seen.add(key)
    if seen != expected:
        raise TrafficContractError(TrafficErrorCode.RECEIPT_KEY_MISMATCH)


def parse_receipt_response(
    raw: bytes | bytearray | memoryview, request: TrafficRequest
) -> ReceiptResponse:
    """Parse a bounded receipt response and enforce exact key coverage."""

    document = _load_json(raw)
    _validate_schema(document)
    try:
        response = ReceiptResponse.model_validate(document)
    except ValidationError:
        raise _invalid() from None
    validate_receipt_coverage(request, response)
    if len(canonical_receipt_response_bytes(response)) > MAX_RAW_BODY_BYTES:
        raise TrafficContractError(TrafficErrorCode.REQUEST_TOO_LARGE)
    return response


def make_counter_snapshot(  # noqa: PLR0913
    *,
    source_id: str,
    credential_id: str,
    accounting_date: date | str,
    version: int,
    upload: int,
    download: int,
) -> CounterSnapshot:
    """Build a counter snapshot with its canonical digest."""

    try:
        snapshot = CounterSnapshot(
            source_id=source_id,
            credential_id=credential_id,
            kind="counter",
            accounting_date=_accounting_date(accounting_date),
            version=version,
            digest="0" * 64,
            upload=upload,
            download=download,
        )
    except ValidationError:
        raise _invalid() from None
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def make_attribution_snapshot(  # noqa: PLR0913
    *,
    source_id: str,
    credential_id: str,
    accounting_date: date | str,
    version: int,
    detail: Detail,
    domains: Sequence[AttributionEntry],
    destinations: Sequence[AttributionEntry],
) -> AttributionSnapshot:
    """Build an attribution snapshot with its canonical digest."""

    try:
        snapshot = AttributionSnapshot(
            source_id=source_id,
            credential_id=credential_id,
            kind="attribution",
            accounting_date=_accounting_date(accounting_date),
            version=version,
            digest="0" * 64,
            detail=detail,
            domains=tuple(domains),
            destinations=tuple(destinations),
        )
    except ValidationError:
        raise _invalid() from None
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def make_request(snapshots: Iterable[Snapshot]) -> TrafficRequest:
    """Build a validated request page from already-digested snapshots."""

    try:
        request = TrafficRequest(schema=1, snapshots=tuple(snapshots))
    except ValidationError:
        raise _invalid() from None
    _validate_request(request)
    return request


def encode_request(request: TrafficRequest) -> bytes:
    """Validate and return the canonical request envelope bytes."""

    _validate_request(request)
    return canonical_request_bytes(request)


def encode_receipt_response(
    response: ReceiptResponse, request: TrafficRequest
) -> bytes:
    """Validate exact coverage and return canonical receipt response bytes."""

    validate_receipt_coverage(request, response)
    encoded = canonical_receipt_response_bytes(response)
    if len(encoded) > MAX_RAW_BODY_BYTES:
        raise TrafficContractError(TrafficErrorCode.REQUEST_TOO_LARGE)
    return encoded


__all__ = [
    "INT64_MAX",
    "MAX_NAMED_IDENTITIES",
    "MAX_PAGE_ITEMS",
    "MAX_RAW_BODY_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "AttributionEntry",
    "AttributionSnapshot",
    "CounterSnapshot",
    "Detail",
    "Kind",
    "Receipt",
    "ReceiptResponse",
    "Snapshot",
    "SnapshotKey",
    "TrafficContractError",
    "TrafficErrorCode",
    "TrafficRequest",
    "canonical_json_bytes",
    "canonical_payload_bytes",
    "canonical_receipt_response_bytes",
    "canonical_request_bytes",
    "canonical_snapshot_bytes",
    "encode_receipt_response",
    "encode_request",
    "make_attribution_snapshot",
    "make_counter_snapshot",
    "make_request",
    "parse_receipt_response",
    "parse_request",
    "snapshot_digest",
    "snapshot_key",
    "snapshot_payload",
    "validate_receipt_coverage",
]
