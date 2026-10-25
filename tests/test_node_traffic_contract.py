"""Frozen node-traffic-v1 vectors, canonical bytes and size boundaries."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from sing_box_manager.control_plane import traffic_contract as contract

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "node-traffic-v1.json"
FIXTURE: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CANONICAL_VECTORS: list[dict[str, Any]] = FIXTURE["canonical_vectors"]
EQUIVALENCE_VECTORS: list[dict[str, Any]] = FIXTURE["equivalence_vectors"]
REQUEST_REJECTIONS: list[dict[str, Any]] = FIXTURE["request_rejections"]
RECEIPT_VECTORS: list[dict[str, Any]] = FIXTURE["receipt_vectors"]
RECEIPT_REJECTIONS: list[dict[str, Any]] = FIXTURE["receipt_rejections"]
REQUEST_VECTORS: list[dict[str, Any]] = FIXTURE["request_vectors"]
BOUNDS: dict[str, Any] = FIXTURE["bounds"]
NAMED_IDENTITY_BOUNDARIES: dict[str, Any] = FIXTURE["named_identity_boundaries"]
AGGREGATE_SLOTS: dict[str, list[str]] = BOUNDS["aggregate_slots"]
IDENTITIES: dict[str, str] = FIXTURE["identities"]

SOURCE_ID = IDENTITIES["source_id"]
CREDENTIAL_ID = IDENTITIES["credential_id"]
ACCOUNTING_DATE = IDENTITIES["accounting_date"]


def _vector(name: str) -> dict[str, Any]:
    return next(vector for vector in CANONICAL_VECTORS if vector["name"] == name)


def _snapshot(
    vector: dict[str, Any],
) -> contract.CounterSnapshot | contract.AttributionSnapshot:
    document = vector["snapshot"]
    if document["kind"] == "counter":
        return contract.CounterSnapshot.model_validate(document)
    assert document["kind"] == "attribution"
    return contract.AttributionSnapshot.model_validate(document)


def _request_bytes(*snapshots: dict[str, Any]) -> bytes:
    return contract.canonical_json_bytes({"schema": 1, "snapshots": list(snapshots)})


def _raw(case: dict[str, Any]) -> bytes:
    if "raw_hex" in case:
        return bytes.fromhex(case["raw_hex"])
    return case["raw"].encode("utf-8")


def _assert_error(call: Any, code: str) -> contract.TrafficContractError:
    with pytest.raises(contract.TrafficContractError) as exc_info:
        call()
    assert exc_info.value.code == code
    assert str(exc_info.value) == code
    return exc_info.value


def _receipt(
    snapshot: contract.CounterSnapshot | contract.AttributionSnapshot,
) -> contract.Receipt:
    return contract.Receipt(
        source_id=snapshot.source_id,
        credential_id=snapshot.credential_id,
        kind=snapshot.kind,
        accounting_date=snapshot.accounting_date,
        version=snapshot.version,
        digest=snapshot.digest,
    )


def _generated_identity_names(count: int) -> list[str]:
    spec = NAMED_IDENTITY_BOUNDARIES["generated_name"]
    prefix = spec["prefix"]
    width = int(spec["width"])
    return [f"{prefix}{index:0{width}d}" for index in range(count)]


def _identity_entries(names: list[str]) -> tuple[contract.AttributionEntry, ...]:
    return tuple(
        sorted(
            (
                contract.AttributionEntry(
                    name=name,
                    upload=0,
                    download=0,
                    connections=0,
                )
                for name in names
            ),
            key=lambda entry: entry.name.encode("utf-8"),
        )
    )


def _boundary_snapshot(case: dict[str, Any]) -> contract.AttributionSnapshot:
    domains = _identity_entries(
        _generated_identity_names(int(case["generated_domains"]))
        + list(case["extra_domains"])
    )
    destinations = _identity_entries(
        _generated_identity_names(int(case["generated_destinations"]))
        + list(case["extra_destinations"])
    )
    snapshot = contract.AttributionSnapshot.model_construct(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        kind="attribution",
        accounting_date=date.fromisoformat(ACCOUNTING_DATE),
        version=1,
        digest="0" * 64,
        detail="exact",
        domains=domains,
        destinations=destinations,
    )
    return snapshot.model_copy(update={"digest": contract.snapshot_digest(snapshot)})


def _named_identity_count(
    domains: tuple[contract.AttributionEntry, ...],
    destinations: tuple[contract.AttributionEntry, ...],
) -> int:
    domain_slots = set(AGGREGATE_SLOTS["domains"])
    destination_slots = set(AGGREGATE_SLOTS["destinations"])
    return sum(entry.name not in domain_slots for entry in domains) + sum(
        entry.name not in destination_slots for entry in destinations
    )


def _snapshot_at_size(
    size: int, *, accounting_date: str
) -> contract.AttributionSnapshot:
    """Build one deterministic attribution snapshot of exactly `size` bytes."""

    def build(name: str) -> contract.AttributionSnapshot:
        return contract.make_attribution_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=accounting_date,
            version=1,
            detail="exact",
            domains=[
                contract.AttributionEntry(
                    name=name,
                    upload=1,
                    download=2,
                    connections=3,
                )
            ],
            destinations=[],
        )

    probe = build("a")
    name_length = 1 + size - len(contract.canonical_snapshot_bytes(probe))
    snapshot = build("a" * name_length)
    assert len(contract.canonical_snapshot_bytes(snapshot)) == size
    return snapshot


def _envelope_size(
    first: contract.AttributionSnapshot, second: contract.AttributionSnapshot
) -> int:
    request = contract.TrafficRequest(schema=1, snapshots=(first, second))
    return len(contract.canonical_request_bytes(request))


def test_fixture_is_versioned_and_records_shared_bounds() -> None:
    assert FIXTURE["schema"] == 1
    assert FIXTURE["contract"] == "node-traffic-v1"
    assert "never regenerate" in FIXTURE["usage"]
    assert "response overflow uses request_too_large" in FIXTURE["usage"]
    assert BOUNDS == {
        "raw_body_bytes": contract.MAX_RAW_BODY_BYTES,
        "snapshot_bytes": contract.MAX_SNAPSHOT_BYTES,
        "page_items": contract.MAX_PAGE_ITEMS,
        "counter_v1_payload_bytes": 197,
        "counter_v1_snapshot_bytes": 273,
        "counter_max_snapshot_bytes": 303,
        "named_identities": contract.MAX_NAMED_IDENTITIES,
        "aggregate_slots": {
            "domains": ["__other__", "__unattributed__", "__ip__"],
            "destinations": ["__other__", "__unattributed__"],
        },
        "response_overflow_code": "request_too_large",
    }
    assert contract.MAX_NAMED_IDENTITIES == 10_000
    assert NAMED_IDENTITY_BOUNDARIES["generated_name"] == {
        "prefix": "n",
        "width": 5,
    }
    assert len({vector["name"] for vector in CANONICAL_VECTORS}) == len(
        CANONICAL_VECTORS
    )
    assert len({case["name"] for case in REQUEST_REJECTIONS}) == len(REQUEST_REJECTIONS)
    assert len({case["name"] for case in RECEIPT_REJECTIONS}) == len(RECEIPT_REJECTIONS)


def test_counter_anchor_matches_the_parent_digest() -> None:
    vector = _vector("counter_v1")
    assert vector["snapshot"]["digest"] == (
        "f86a29ebf7d81ff621359780b4917566d49f2d606dd4940942d75f22ec02d28b"
    )
    assert len(vector["payload_canonical"].encode()) == 197
    assert len(vector["snapshot_canonical"].encode()) == 273
    assert len(_vector("counter_max_v1")["snapshot_canonical"].encode()) == 303


@pytest.mark.parametrize(
    "vector", CANONICAL_VECTORS, ids=[vector["name"] for vector in CANONICAL_VECTORS]
)
def test_canonical_vector_round_trips(vector: dict[str, Any]) -> None:
    snapshot = _snapshot(vector)
    assert (
        contract.canonical_payload_bytes(snapshot).decode("utf-8")
        == vector["payload_canonical"]
    )
    assert (
        contract.canonical_snapshot_bytes(snapshot).decode("utf-8")
        == vector["snapshot_canonical"]
    )
    assert contract.snapshot_digest(snapshot) == snapshot.digest

    request_bytes = _request_bytes(vector["snapshot"])
    request = contract.parse_request(request_bytes)
    assert request.snapshots == (snapshot,)


@pytest.mark.parametrize(
    "case", REQUEST_VECTORS, ids=[case["name"] for case in REQUEST_VECTORS]
)
def test_request_envelope_matches_frozen_canonical_bytes(case: dict[str, Any]) -> None:
    snapshots = tuple(_snapshot(_vector(name)) for name in case["snapshots"])
    request = contract.TrafficRequest(schema=1, snapshots=snapshots)
    expected = case["request_canonical"].encode("utf-8")
    assert contract.encode_request(request) == expected
    assert contract.parse_request(expected).snapshots == snapshots


@pytest.mark.parametrize(
    "case",
    EQUIVALENCE_VECTORS,
    ids=[case["name"] for case in EQUIVALENCE_VECTORS],
)
def test_equivalent_encodings_canonicalize_to_the_frozen_vector(
    case: dict[str, Any],
) -> None:
    target = _vector(case["vector"])
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    assert len(request.snapshots) == 1
    snapshot = request.snapshots[0]
    assert (
        contract.canonical_payload_bytes(snapshot).decode("utf-8")
        == target["payload_canonical"]
    )
    assert (
        contract.canonical_snapshot_bytes(snapshot).decode("utf-8")
        == target["snapshot_canonical"]
    )
    assert snapshot.digest == target["snapshot"]["digest"]


@pytest.mark.parametrize(
    "case", REQUEST_REJECTIONS, ids=[case["name"] for case in REQUEST_REJECTIONS]
)
def test_request_rejection_uses_the_frozen_code(case: dict[str, Any]) -> None:
    _assert_error(lambda: contract.parse_request(_raw(case)), case["code"])


def test_request_errors_do_not_echo_the_payload() -> None:
    case = next(
        case for case in REQUEST_REJECTIONS if case["name"] == "digest_mismatch"
    )
    raw = _raw(case)
    error = _assert_error(
        lambda: contract.parse_request(raw), "snapshot_digest_mismatch"
    )
    message = str(error)
    assert message == "snapshot_digest_mismatch"
    assert SOURCE_ID not in message
    assert "f86a29eb" not in message
    assert raw.decode("utf-8") not in message


@pytest.mark.parametrize(
    "case", RECEIPT_VECTORS, ids=[case["name"] for case in RECEIPT_VECTORS]
)
def test_receipt_vector_round_trips(case: dict[str, Any]) -> None:
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    response = contract.parse_receipt_response(
        case["response_raw"].encode("utf-8"), request
    )
    contract.validate_receipt_coverage(request, response)
    assert (
        contract.canonical_receipt_response_bytes(response).decode("utf-8")
        == case["response_canonical"]
    )
    assert contract.encode_receipt_response(response, request) == (
        case["response_canonical"].encode("utf-8")
    )
    assert {contract.snapshot_key(receipt) for receipt in response.receipts} == {
        contract.snapshot_key(snapshot) for snapshot in request.snapshots
    }


@pytest.mark.parametrize(
    "case", RECEIPT_REJECTIONS, ids=[case["name"] for case in RECEIPT_REJECTIONS]
)
def test_receipt_rejection_uses_the_frozen_code(case: dict[str, Any]) -> None:
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    _assert_error(
        lambda: contract.parse_receipt_response(
            case["response_raw"].encode("utf-8"), request
        ),
        case["code"],
    )


def test_stored_receipts_do_not_have_to_echo_the_submitted_bucket() -> None:
    case = RECEIPT_VECTORS[0]
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    submission = next(
        snapshot for snapshot in request.snapshots if snapshot.kind == "counter"
    )
    response = contract.parse_receipt_response(
        case["response_raw"].encode("utf-8"), request
    )
    receipt = next(item for item in response.receipts if item.kind == "counter")
    assert submission.version == 1
    assert receipt.version == 2
    assert receipt.digest != submission.digest
    assert submission.version == 1
    assert submission.digest == _vector("counter_v1")["snapshot"]["digest"]


def test_receipt_coverage_rejects_directly_constructed_duplicate_snapshot_keys() -> (
    None
):
    counter = contract.make_counter_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        upload=1,
        download=2,
    )
    request = contract.TrafficRequest(schema=1, snapshots=(counter, counter))
    response = contract.ReceiptResponse(schema=1, receipts=(_receipt(counter),))
    _assert_error(
        lambda: contract.validate_receipt_coverage(request, response),
        "duplicate_snapshot_key",
    )
    _assert_error(
        lambda: contract.encode_receipt_response(response, request),
        "duplicate_snapshot_key",
    )
    _assert_error(
        lambda: contract.parse_receipt_response(
            contract.canonical_receipt_response_bytes(response), request
        ),
        "duplicate_snapshot_key",
    )


def test_names_preserve_unicode_forms_controls_and_punctuation() -> None:
    vector = _vector("attribution_unicode_v1")
    snapshot = _snapshot(vector)
    assert isinstance(snapshot, contract.AttributionSnapshot)
    names = [entry.name for entry in snapshot.domains]
    assert names == sorted(names, key=lambda name: name.encode("utf-8"))
    assert "café.example" in names
    assert "cafe\u0301.example" in names
    assert 'he said "hi"\\ok' in names
    assert "a&b<c>d.example" in names
    assert "line\u2028sep\u2029end" in names
    assert "nul\x00name" in names
    assert "\U0001f600" in names[-1]
    assert "Case.example" in names
    assert "case.example" in names

    canonical = contract.canonical_snapshot_bytes(snapshot).decode("utf-8")
    assert "\\u2028" in canonical
    assert "\\u2029" in canonical
    assert "\u2028" not in canonical
    assert "\u2029" not in canonical
    assert "a&b<c>d.example" in canonical
    assert "\\u003c" not in canonical
    assert "\\u003e" not in canonical
    assert "\\u0026" not in canonical
    assert r"he said \"hi\"\\ok" in canonical
    assert "\\u0000" in canonical
    assert "\U0001f600" in canonical

    composed = contract.make_attribution_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        detail="exact",
        domains=[
            contract.AttributionEntry(
                name="café.example", upload=1, download=2, connections=3
            )
        ],
        destinations=[],
    )
    decomposed = contract.make_attribution_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        detail="exact",
        domains=[
            contract.AttributionEntry(
                name="cafe\u0301.example", upload=1, download=2, connections=3
            )
        ],
        destinations=[],
    )
    assert composed.domains[0].name == "café.example"
    assert decomposed.domains[0].name == "cafe\u0301.example"
    assert contract.snapshot_digest(composed) != contract.snapshot_digest(decomposed)


def test_names_are_sets_sorted_by_utf8_and_duplicate_names_are_rejected() -> None:
    upper = contract.AttributionEntry(
        name="A.example", upload=1, download=2, connections=3
    )
    lower = contract.AttributionEntry(
        name="a.example", upload=4, download=5, connections=6
    )
    snapshot = contract.make_attribution_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        detail="exact",
        domains=[lower, upper],
        destinations=[],
    )
    assert [entry.name for entry in snapshot.domains] == ["A.example", "a.example"]

    duplicate = contract.AttributionEntry(
        name="a.example", upload=7, download=8, connections=9
    )
    _assert_error(
        lambda: contract.make_attribution_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=ACCOUNTING_DATE,
            version=1,
            detail="exact",
            domains=[lower, duplicate],
            destinations=[],
        ),
        "duplicate_name",
    )


def test_detail_does_not_imply_aggregate_presence() -> None:
    exact = _snapshot(_vector("attribution_exact_with_aggregate_v1"))
    compacted = _snapshot(_vector("attribution_compacted_without_aggregate_v1"))
    assert isinstance(exact, contract.AttributionSnapshot)
    assert isinstance(compacted, contract.AttributionSnapshot)
    assert exact.detail == "exact"
    assert compacted.detail == "compacted"
    assert "__other__" in [entry.name for entry in exact.domains]
    assert "__other__" not in [entry.name for entry in compacted.domains]


@pytest.mark.parametrize(
    "case",
    NAMED_IDENTITY_BOUNDARIES["cases"],
    ids=[case["name"] for case in NAMED_IDENTITY_BOUNDARIES["cases"]],
)
def test_named_identity_boundary(case: dict[str, Any]) -> None:
    snapshot = _boundary_snapshot(case)
    assert _named_identity_count(snapshot.domains, snapshot.destinations) == int(
        case["expected_named_identities"]
    )
    snapshot_bytes = contract.canonical_snapshot_bytes(snapshot)
    assert len(snapshot_bytes) == int(case["expected_snapshot_bytes"])
    assert len(snapshot_bytes) < contract.MAX_SNAPSHOT_BYTES

    request = contract.TrafficRequest.model_construct(
        schema_version=1, snapshots=(snapshot,)
    )
    request_bytes = contract.canonical_request_bytes(request)
    assert len(request_bytes) == int(case["expected_request_bytes"])
    assert len(request_bytes) < contract.MAX_RAW_BODY_BYTES

    if case["expected_result"] == "accepted":
        assert contract.parse_request(request_bytes).snapshots == (snapshot,)
        built = contract.make_attribution_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=ACCOUNTING_DATE,
            version=1,
            detail="exact",
            domains=snapshot.domains,
            destinations=snapshot.destinations,
        )
        assert built == snapshot
        return

    assert case["expected_result"] == "invalid_request"
    _assert_error(lambda: contract.parse_request(request_bytes), "invalid_request")
    _assert_error(
        lambda: contract.make_attribution_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=ACCOUNTING_DATE,
            version=1,
            detail="exact",
            domains=snapshot.domains,
            destinations=snapshot.destinations,
        ),
        "invalid_request",
    )


def test_canonical_json_rule_uses_compact_sorted_utf8_and_line_separator_escapes() -> (
    None
):
    value = {"b": "\u00e9<&>", "a": "\u2028\u2029", "n": 1}
    assert contract.canonical_json_bytes(value) == (
        b'{"a":"\\u2028\\u2029","b":"\xc3\xa9<&>","n":1}'
    )
    assert contract.canonical_json_bytes({"c": "\x00\x01\b\f\n\r\t"}) == (
        b'{"c":"\\u0000\\u0001\\b\\f\\n\\r\\t"}'
    )
    with pytest.raises(ValueError, match="Out of range float values"):
        contract.canonical_json_bytes({"nonfinite": math.nan})


def test_wire_fields_do_not_accept_python_model_field_names() -> None:
    request_bytes = REQUEST_VECTORS[0]["request_canonical"].encode("utf-8")
    raw = request_bytes.replace(b'"schema":1', b'"schema":1,"schema_version":1', 1)
    _assert_error(lambda: contract.parse_request(raw), "invalid_request")

    case = RECEIPT_VECTORS[0]
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    response = (
        case["response_raw"]
        .encode("utf-8")
        .replace(b'"schema":1', b'"schema":1,"schema_version":1', 1)
    )
    _assert_error(
        lambda: contract.parse_receipt_response(response, request),
        "invalid_request",
    )


def test_snapshot_payload_excludes_digest_and_metadata_timestamps() -> None:
    counter = _snapshot(_vector("counter_v1"))
    attribution = _snapshot(_vector("attribution_v1"))
    assert set(contract.snapshot_payload(counter)) == {
        "source_id",
        "credential_id",
        "kind",
        "accounting_date",
        "version",
        "upload",
        "download",
    }
    assert set(contract.snapshot_payload(attribution)) == {
        "source_id",
        "credential_id",
        "kind",
        "accounting_date",
        "version",
        "detail",
        "domains",
        "destinations",
    }
    assert "digest" not in contract.snapshot_payload(counter)
    assert "received_at" not in contract.snapshot_payload(counter)


def test_producer_builders_round_trip_through_the_codec() -> None:
    counter = contract.make_counter_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        upload=1024,
        download=2048,
    )
    attribution = contract.make_attribution_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        detail="exact",
        domains=[
            contract.AttributionEntry(
                name="b.example.com", upload=5, download=6, connections=1
            ),
            contract.AttributionEntry(
                name="a.example.com", upload=100, download=200, connections=2
            ),
        ],
        destinations=[],
    )
    assert counter.digest == _vector("counter_v1")["snapshot"]["digest"]
    assert [entry.name for entry in attribution.domains] == [
        "a.example.com",
        "b.example.com",
    ]

    request = contract.make_request((counter, attribution))
    encoded = contract.encode_request(request)
    parsed = contract.parse_request(encoded)
    assert parsed == request

    response = contract.ReceiptResponse(
        schema=1,
        receipts=tuple(_receipt(snapshot) for snapshot in parsed.snapshots),
    )
    encoded_response = contract.encode_receipt_response(response, parsed)
    assert contract.parse_receipt_response(encoded_response, parsed) == response


def test_producer_rejects_duplicate_snapshot_keys_and_bad_digests() -> None:
    counter = contract.make_counter_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=ACCOUNTING_DATE,
        version=1,
        upload=1,
        download=2,
    )
    _assert_error(
        lambda: contract.make_request((counter, counter)), "duplicate_snapshot_key"
    )
    bad_digest = counter.model_copy(update={"digest": "0" * 64})
    _assert_error(
        lambda: contract.make_request((bad_digest,)), "snapshot_digest_mismatch"
    )


def test_producer_validates_real_dates_and_int64_bounds() -> None:
    for accounting_date in ("0001-01-01", "2024-02-29", "9999-12-31"):
        snapshot = contract.make_counter_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=accounting_date,
            version=contract.INT64_MAX,
            upload=contract.INT64_MAX,
            download=contract.INT64_MAX,
        )
        assert snapshot.accounting_date == date.fromisoformat(accounting_date)

    for accounting_date in (
        "0000-01-01",
        "2023-02-29",
        "2026-13-01",
        "2026-10-23T00:00:00Z",
    ):
        _assert_error(
            lambda value=accounting_date: contract.make_counter_snapshot(
                source_id=SOURCE_ID,
                credential_id=CREDENTIAL_ID,
                accounting_date=value,
                version=1,
                upload=1,
                download=2,
            ),
            "invalid_request",
        )


def test_raw_request_and_receipt_body_bounds() -> None:
    request_bytes = REQUEST_VECTORS[0]["request_canonical"].encode("utf-8")
    at_limit = request_bytes + b" " * (contract.MAX_RAW_BODY_BYTES - len(request_bytes))
    assert len(at_limit) == contract.MAX_RAW_BODY_BYTES
    contract.parse_request(at_limit)
    _assert_error(lambda: contract.parse_request(at_limit + b" "), "request_too_large")

    case = RECEIPT_VECTORS[0]
    request = contract.parse_request(case["request_raw"].encode("utf-8"))
    response_bytes = case["response_raw"].encode("utf-8")
    at_limit = response_bytes + b" " * (
        contract.MAX_RAW_BODY_BYTES - len(response_bytes)
    )
    assert len(at_limit) == contract.MAX_RAW_BODY_BYTES
    contract.parse_receipt_response(at_limit, request)
    _assert_error(
        lambda: contract.parse_receipt_response(at_limit + b" ", request),
        "request_too_large",
    )


def test_snapshot_byte_boundary() -> None:
    at_limit = _snapshot_at_size(
        contract.MAX_SNAPSHOT_BYTES, accounting_date="2026-10-23"
    )
    request = contract.make_request((at_limit,))
    encoded = contract.encode_request(request)
    assert contract.parse_request(encoded).snapshots == (at_limit,)

    over_limit = _snapshot_at_size(
        contract.MAX_SNAPSHOT_BYTES + 1, accounting_date="2026-10-23"
    )
    _assert_error(lambda: contract.make_request((over_limit,)), "snapshot_too_large")
    over_request = contract.TrafficRequest(schema=1, snapshots=(over_limit,))
    _assert_error(
        lambda: contract.parse_request(contract.canonical_request_bytes(over_request)),
        "snapshot_too_large",
    )


def test_full_canonical_envelope_boundary() -> None:
    first = _snapshot_at_size(contract.MAX_SNAPSHOT_BYTES, accounting_date="2026-10-23")
    probe_second = _snapshot_at_size(400, accounting_date="2026-10-24")
    overhead = _envelope_size(first, probe_second) - 400
    second = _snapshot_at_size(
        contract.MAX_RAW_BODY_BYTES - overhead, accounting_date="2026-10-24"
    )
    assert _envelope_size(first, second) == contract.MAX_RAW_BODY_BYTES

    request = contract.make_request((first, second))
    encoded = contract.encode_request(request)
    assert len(encoded) == contract.MAX_RAW_BODY_BYTES
    assert contract.parse_request(encoded).snapshots == (first, second)

    over_second = _snapshot_at_size(
        contract.MAX_RAW_BODY_BYTES - overhead + 1, accounting_date="2026-10-24"
    )
    _assert_error(
        lambda: contract.make_request((first, over_second)), "request_too_large"
    )


def test_page_count_boundary_for_snapshots_and_receipts() -> None:
    start = date(2026, 1, 1)
    snapshots = tuple(
        contract.make_counter_snapshot(
            source_id=SOURCE_ID,
            credential_id=CREDENTIAL_ID,
            accounting_date=start + timedelta(days=index),
            version=1,
            upload=index,
            download=index,
        )
        for index in range(contract.MAX_PAGE_ITEMS)
    )
    request = contract.make_request(snapshots)
    assert len(request.snapshots) == contract.MAX_PAGE_ITEMS

    extra = contract.make_counter_snapshot(
        source_id=SOURCE_ID,
        credential_id=CREDENTIAL_ID,
        accounting_date=start + timedelta(days=contract.MAX_PAGE_ITEMS),
        version=1,
        upload=1,
        download=2,
    )
    _assert_error(lambda: contract.make_request((*snapshots, extra)), "invalid_request")

    receipts = tuple(_receipt(snapshot) for snapshot in snapshots)
    response = contract.ReceiptResponse(schema=1, receipts=receipts)
    encoded = contract.encode_receipt_response(response, request)
    assert contract.parse_receipt_response(encoded, request) == response
    _assert_error(
        lambda: contract.ReceiptResponse(
            schema=1, receipts=(*receipts, _receipt(extra))
        ),
        "invalid_request",
    )


def test_codec_import_has_no_django_or_database_side_effects() -> None:
    code = """
import sys
import sing_box_manager.control_plane.traffic_contract
forbidden = sorted(
    name
    for name in sys.modules
    if name == "django" or name.startswith("django.") or name.startswith("psycopg")
)
assert not forbidden, forbidden
"""
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
