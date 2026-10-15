"""Account-only wire format. Operator custody supplies trust, not fixture claims."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sing_box_manager.control_plane.import_schema import (
    MAX_COUNTER,
    AccessRecords,
    ImportRejected,
    context_identity,
    keyed_identity,
)
from sing_box_manager.settings import Settings

PURPOSE = "production-account-copy"
POLICY = "fresh-links-at-cutover"


class Record(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    def __repr_args__(self):
        return []


class SourceFiles(Record):
    runtime: str = Field(pattern=r"^[0-9a-f]{64}$")
    auth: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy: str = Field(pattern=r"^[0-9a-f]{64}$")


class AccountSnapshot(Record):
    version: Literal[1]
    purpose: Literal["production-account-copy"]
    link_policy: Literal["fresh-links-at-cutover"]
    namespace: str
    source_identity: str
    sequence: int = Field(ge=1, le=MAX_COUNTER)
    captured_at: str
    context_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_files: SourceFiles
    legacy_counters: Literal["not-imported"]
    identity_history: Literal["not-established"]
    access: AccessRecords

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid_version")
        return value

    @field_validator("namespace", "source_identity")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("invalid_identity")
        return value

    @field_validator("captured_at")
    @classmethod
    def canonical_time(cls, value: str) -> str:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
            raise ValueError("invalid_time")
        return value


def copy_context_identity(runtime: Settings) -> str:
    if runtime.deployment.tls.self_signed_cert:
        raise ImportRejected("account_copy_context_rejected")
    return keyed_identity(
        runtime,
        "account-copy-context-v1",
        {
            "render_context": context_identity(runtime),
            "traffic_settings": runtime.traffic_stats.model_dump(mode="json"),
        },
    )
