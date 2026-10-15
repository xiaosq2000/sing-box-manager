"""Compare copied fields and renderer outputs without serving any clients."""

from copy import deepcopy
from pathlib import Path

from sing_box_manager.control_plane.import_schema import (
    KINDS,
    AccessRecords,
    ImportRejected,
    Snapshot,
    canonical,
)
from sing_box_manager.release.profiles import build_server_config
from sing_box_manager.settings import (
    NaiveInventory,
    Settings,
    TrojanInventory,
    WebPortalInventory,
)
from sing_box_manager.subscription import (
    ResetCounters,
    SubscriptionService,
    build_envelope,
    encode_envelope,
    find_subscriber,
    subscription_token,
)


def inventory_from_records(runtime: Settings, records: AccessRecords) -> Settings:
    """Only infrastructure comes from runtime; no runtime roster is consulted."""
    return runtime.model_copy(
        update={
            "web_portal_data": WebPortalInventory(users=records.portal),
            "trojan_data": TrojanInventory(users=records.trojan),
            "hysteria2_data": runtime.hysteria2.model_copy(
                update={"users": records.hysteria2}
            ),
            "naive_data": NaiveInventory(users=records.naive),
        }
    )


def records_from_state(state: dict) -> AccessRecords:
    active = {row["username"] for row in state["subjects"] if row["present"]}
    rows = {kind: [] for kind in KINDS}
    for row in state["access"]:
        if row["present"] and row["username"] in active:
            rows[row["kind"]].append(row["payload"])
    for values in rows.values():
        values.sort(key=lambda value: value["username"])
    return AccessRecords.model_validate(rows)


def verify_projection(expected: dict, actual: dict) -> None:
    """Check all retained rows and IDs; resolve IDs only for genuinely new subjects."""
    projected = deepcopy(expected)
    expected_subjects = {row["username"]: row for row in projected["subjects"]}
    actual_subjects = {row["username"]: row for row in actual["subjects"]}
    if expected_subjects.keys() != actual_subjects.keys():
        raise ImportRejected("projection_parity_failed")
    for name, row in expected_subjects.items():
        if row["id"] == "new":
            row["id"] = actual_subjects[name]["id"]
    for row in projected["legacy"]:
        if row["subject"] == "new":
            row["subject"] = expected_subjects[row["username"]]["id"]
    for kind in ("subjects", "access", "legacy"):
        expected_rows = sorted(
            projected[kind], key=lambda row: (row["username"], row.get("kind", ""))
        )
        actual_rows = sorted(
            actual[kind], key=lambda row: (row["username"], row.get("kind", ""))
        )
        if canonical(expected_rows) != canonical(actual_rows):
            raise ImportRejected("projection_parity_failed")


def _service(runtime: Settings, records: AccessRecords) -> SubscriptionService:
    return SubscriptionService(
        secret=runtime.web.subscription_secret or "",
        inventory=inventory_from_records(runtime, records),
        default_protocol=runtime.default_protocol,
        sing_box_version=runtime.sing_box_version,
        # build_envelope never reads this object. No counter file is opened.
        counters=ResetCounters(Path("unused-fixture-counter-path")),
    )


def _verify_links(snapshot: Snapshot, state: dict) -> None:
    subjects = {row["username"]: row["id"] for row in state["subjects"]}
    incoming = snapshot.access.memberships()
    counters = snapshot.counter_map()
    for row in state["access"]:
        if row["payload"].get("username") != row["username"] or row["present"] != (
            row["kind"] in incoming.get(row["username"], {})
        ):
            raise ImportRejected("membership_binding_failed")
    for row in state["legacy"]:
        if (
            row["subject"] != subjects.get(row["username"])
            or row["counter"] != counters.get(row["username"], 0)
            or row["secret_identity"] != snapshot.secret_identity
        ):
            raise ImportRejected("legacy_binding_failed")


def _verify_fields(snapshot: Snapshot, copied: AccessRecords, state: dict) -> None:
    _verify_links(snapshot, state)
    if canonical(copied.memberships()) != canonical(snapshot.access.memberships()):
        raise ImportRejected("field_parity_failed")
    expected_names = set(snapshot.access.memberships())
    actual_names = {row["username"] for row in state["subjects"] if row["present"]}
    if expected_names != actual_names:
        raise ImportRejected("subject_parity_failed")
    stored = {row["username"]: row for row in state["legacy"]}
    for name in expected_names | set(snapshot.counter_map()):
        if (
            name not in stored
            or stored[name]["counter"] != snapshot.counter_map().get(name, 0)
            or stored[name]["secret_identity"] != snapshot.secret_identity
        ):
            raise ImportRejected("counter_parity_failed")


def _verify_renders(
    records: AccessRecords, runtime: Settings, copied: AccessRecords
) -> None:
    left = _service(runtime, records)
    right = _service(runtime, copied)
    for name in records.memberships():
        for platform in ("linux", "darwin", "windows"):
            first = build_envelope(left, name, os_name=platform)
            second = build_envelope(right, name, os_name=platform)
            if first != second:
                raise ImportRejected("render_parity_failed")
            if (
                first is not None
                and second is not None
                and encode_envelope(first) != encode_envelope(second)
            ):
                raise ImportRejected("render_parity_failed")


def _verify_tokens(
    snapshot: Snapshot, runtime: Settings, copied: AccessRecords, state: dict
) -> None:
    secret = runtime.web.subscription_secret
    if not secret:
        return
    eligible = [row.username for row in copied.portal if row.enabled]
    counters = {row["username"]: row["counter"] for row in state["legacy"]}
    for name in snapshot.access.memberships():
        counter = snapshot.counter_map().get(name, 0)
        token = subscription_token(secret, name, counter)
        if find_subscriber(secret, token, eligible, counters) != (
            name if name in eligible else None
        ):
            raise ImportRejected("token_parity_failed")
        if (
            counter
            and find_subscriber(
                secret,
                subscription_token(secret, name, counter - 1),
                eligible,
                counters,
            )
            is not None
        ):
            raise ImportRejected("revocation_parity_failed")


def _account_checks(records: AccessRecords, runtime: Settings, state: dict) -> None:
    if runtime.deployment.tls.self_signed_cert or state["legacy"]:
        raise ImportRejected("account_copy_context_rejected")
    copied = records_from_state(state)
    incoming = records.memberships()
    if canonical(copied.memberships()) != canonical(incoming):
        raise ImportRejected("field_parity_failed")
    if set(incoming) != {
        row["username"] for row in state["subjects"] if row["present"]
    }:
        raise ImportRejected("subject_parity_failed")
    for row in state["access"]:
        if row["payload"].get("username") != row["username"] or row["present"] != (
            row["kind"] in incoming.get(row["username"], {})
        ):
            raise ImportRejected("membership_binding_failed")
    _verify_renders(records, runtime, copied)
    # The database reads in username order. Normalize source order too.
    ordered = AccessRecords.model_validate(
        {
            kind: sorted(
                [row.model_dump(mode="json") for row in getattr(records, kind)],
                key=lambda row: row["username"],
            )
            for kind in KINDS
        }
    )
    if build_server_config(
        inventory_from_records(runtime, ordered), runtime.traffic_stats
    ) != build_server_config(
        inventory_from_records(runtime, copied), runtime.traffic_stats
    ):
        raise ImportRejected("render_parity_failed")


def verify_account_parity(
    records: AccessRecords, runtime: Settings, state: dict
) -> dict[str, bool]:
    """Check account copies without deriving tokens or consulting reset counters."""
    try:
        _account_checks(records, runtime, state)
    except ImportRejected:
        raise
    except Exception:
        raise ImportRejected("parity_validation_failed") from None
    return {"fields": True, "configs_and_etags": True, "server_config": True}


def verify_parity(
    snapshot: Snapshot, runtime: Settings, state: dict
) -> dict[str, bool]:
    """Fail on field loss even when disabled credentials do not affect rendering."""
    try:
        copied = records_from_state(state)
        _verify_fields(snapshot, copied, state)
        _verify_renders(snapshot.access, runtime, copied)
        _verify_tokens(snapshot, runtime, copied, state)
    except ImportRejected:
        raise
    except Exception:
        raise ImportRejected("parity_validation_failed") from None
    return {
        "fields": True,
        "configs_and_etags": True,
        "current_and_revoked_tokens": True,
    }
