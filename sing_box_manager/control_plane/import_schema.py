"""Strict synthetic snapshots. Production provenance has no implementation yet."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, Literal, Self, cast
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from sing_box_manager.settings import (
    USERNAME_PATTERN,
    Hysteria2User,
    NaiveUser,
    Settings,
    StrictModel,
    TrojanUser,
    WebPortalUser,
)

KINDS = ("portal", "trojan", "hysteria2", "naive")
MAX_COUNTER = 2**63 - 1


class ImportRejectedError(ValueError):
    """A fixed error code, never an input value or a database exception."""


ImportRejected = ImportRejectedError


def _validate_postgres_text(value: object) -> None:
    if not isinstance(value, str):
        return
    if "\x00" in value:
        raise ValueError("unsupported_access_string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("unsupported_access_string") from None


class AccessRecords(StrictModel):
    portal: list[WebPortalUser] = Field(repr=False)
    trojan: list[TrojanUser] = Field(repr=False)
    hysteria2: list[Hysteria2User] = Field(repr=False)
    naive: list[NaiveUser] = Field(repr=False)

    @field_validator("portal", "trojan", "hysteria2", "naive", mode="before")
    @classmethod
    def complete_records(cls, value: object, info: ValidationInfo) -> object:
        expected = (
            {"username", "enabled", "password_hash", "admin"}
            if info.field_name == "portal"
            else {"username", "enabled", "password"}
        )
        if not isinstance(value, list):
            raise ValueError("invalid_membership_shape")
        for row in value:
            if not isinstance(row, dict):
                raise ValueError("invalid_membership_shape")
            mapping = cast("dict[str, Any]", row)
            if (
                (
                    len(mapping) != len(expected)
                    or any(key not in mapping for key in expected)
                )
                or type(mapping.get("enabled")) is not bool
                or (
                    info.field_name == "portal"
                    and type(mapping.get("admin")) is not bool
                )
            ):
                raise ValueError("invalid_membership_shape")
            for field in mapping.values():
                _validate_postgres_text(field)
        return value

    @model_validator(mode="after")
    def unique_memberships(self) -> Self:
        for kind in KINDS:
            names = [row.username for row in getattr(self, kind)]
            if len(names) != len(set(names)):
                raise ValueError("duplicate_membership")
        return self

    def memberships(self) -> dict[str, dict[str, dict]]:
        result: dict[str, dict[str, dict]] = {}
        for kind in KINDS:
            for row in getattr(self, kind):
                result.setdefault(row.username, {})[kind] = row.model_dump(mode="json")
        return result


class CounterRow(StrictModel):
    username: str = Field(strict=True, pattern=USERNAME_PATTERN.pattern)
    counter: int = Field(strict=True, ge=0, le=MAX_COUNTER)


class FixtureEvidence(StrictModel):
    """Fixture assertions, not proof of a real server's state."""

    loaded_runtime: Literal["synthetic-match"]
    loaded_auth: Literal["synthetic-match"]
    proxy_config: Literal["synthetic-match"]
    source_order: Literal["synthetic-coordinated"]
    username_continuity: Literal["synthetic-confirmed"]
    identity_changes: Literal["none"]
    initial_zero: Literal["synthetic-verified"] | None = None


class Snapshot(StrictModel):
    version: int = Field(strict=True, ge=1, le=1)
    purpose: Literal["synthetic-fixture"]
    namespace: UUID
    source_identity: UUID
    sequence: int = Field(strict=True, ge=1, le=MAX_COUNTER)
    captured_at: datetime
    evidence: FixtureEvidence
    context_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    secret_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    counter_state: Literal["complete-table", "initial-zero"]
    counters: list[CounterRow]
    access: AccessRecords = Field(repr=False)

    @field_validator("captured_at")
    @classmethod
    def aware_cutoff(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("cutoff_requires_timezone")
        return value

    @model_validator(mode="after")
    def valid_counter_evidence(self) -> Self:
        names = [row.username for row in self.counters]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_counter")
        if self.counter_state == "initial-zero" and (
            self.counters or self.evidence.initial_zero is None
        ):
            raise ValueError("initial_zero_requires_evidence")
        return self

    def counter_map(self) -> dict[str, int]:
        return {row.username: row.counter for row in self.counters}


class SealedReport(StrictModel):
    body: dict[str, Any] = Field(repr=False)
    seal: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def keyed_identity(runtime: Settings, domain: str, value: object) -> str:
    key = runtime.control_plane.secret_key
    if not key:
        raise ImportRejected("configuration_rejected")
    return hmac.new(
        key.encode(), domain.encode() + b"\0" + canonical(value), hashlib.sha256
    ).hexdigest()


def context_identity(runtime: Settings) -> str:
    # No roster or TLS file is loaded. Infrastructure stays outside imported rows.
    return keyed_identity(
        runtime,
        "import-context-v1",
        {
            "deployment": runtime.deployment.model_dump(mode="json"),
            "hysteria2": runtime.hysteria2.model_dump(mode="json", exclude={"users"}),
            "sing_box_version": runtime.sing_box_version,
            "default_protocol": runtime.default_protocol,
        },
    )


def secret_identity(runtime: Settings) -> str:
    return keyed_identity(
        runtime, "import-subscription-secret-v1", runtime.web.subscription_secret
    )


def validate_context(snapshot: Snapshot, runtime: Settings) -> None:
    if runtime.deployment.tls.self_signed_cert:
        # Avoid any TLS-file access in the fixture-only implementation.
        raise ImportRejected("unsupported_fixture_context")
    if not hmac.compare_digest(
        snapshot.context_identity, context_identity(runtime)
    ) or not hmac.compare_digest(snapshot.secret_identity, secret_identity(runtime)):
        raise ImportRejected("context_mismatch")
