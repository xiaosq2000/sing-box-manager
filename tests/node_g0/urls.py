"""Real Ninja/Pydantic endpoint fixture. No enrollment or operator HTTP API."""

import hashlib
import hmac
import json
import re
from datetime import date
from typing import Annotated
from uuid import UUID

from django.db import transaction
from django.http import HttpResponse
from django.urls import path
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.parser import Parser
from ninja.security import HttpBearer
from pydantic import ConfigDict, Field

from .models import Node, Usage


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class StrictParser(Parser):
    def parse_body(self, request):
        return json.loads(request.body, object_pairs_hook=unique_object)


class NodeAuth(HttpBearer):
    def authenticate(self, request, token):
        # Fixed local fault injection for the isolated reliability fixture only.
        from django.conf import settings

        if (settings.RUN_DIR / "deny-auth").exists():
            return None
        if not re.fullmatch(r"[a-f0-9-]{36}\.[a-f0-9]{64}", token):
            return None
        node_id, secret = token.split(".")
        try:
            node = Node.objects.get(pk=UUID(node_id))
        except (Node.DoesNotExist, ValueError):
            return None
        actual = hashlib.sha256(secret.encode()).hexdigest()
        return node if hmac.compare_digest(node.token_digest, actual) else None


api = NinjaAPI(
    title="Disposable node G0 fixture",
    version="1",
    auth=NodeAuth(),
    parser=StrictParser(),
    docs_url=None,
    openapi_url=None,
)
Counter = Annotated[int, Field(strict=True, ge=0, le=(1 << 63) - 1)]
Version = Annotated[int, Field(strict=True, ge=1, le=(1 << 63) - 1)]


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid")


class Snapshot(StrictSchema):
    source_id: UUID
    credential_id: UUID
    usage_date: date
    version: Version
    upload: Counter
    download: Counter
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Batch(StrictSchema):
    snapshots: Annotated[list[Snapshot], Field(max_length=100)]


class Status(StrictSchema):
    generation: Version
    config_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    invocation: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    coverage_gaps: Counter = 0


@api.get("/config")
def configuration(request):
    node = request.auth
    etag = f'"{node.generation}:{node.bundle["config_sha256"]}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304, headers={"ETag": etag})
    return HttpResponse(
        canonical(node.bundle), content_type="application/json", headers={"ETag": etag}
    )


@api.post("/status")
def status(request, value: Status):
    with transaction.atomic():
        node = Node.objects.select_for_update().get(pk=request.auth.pk)
        if (
            value.generation != node.generation
            or value.config_sha256 != node.bundle["config_sha256"]
        ):
            raise HttpError(409, "generation conflict")
        node.applied_generation = value.generation
        node.invocation = value.invocation
        node.coverage_gaps = value.coverage_gaps
        node.save(update_fields=["applied_generation", "invocation", "coverage_gaps"])
    return {"accepted_generation": value.generation}


@api.post("/traffic")
def traffic(request, batch: Batch):
    receipts = []
    with transaction.atomic():
        # Serializing one node also protects concurrent inserts of a new key.
        node = Node.objects.select_for_update().get(pk=request.auth.pk)
        permitted = {entry["credential_id"] for entry in node.bundle["roster"]}
        seen = set()
        for row in batch.snapshots:
            if (
                row.source_id != node.source_id
                or str(row.credential_id) not in permitted
            ):
                raise HttpError(403, "snapshot ownership mismatch")
            raw = row.model_dump(mode="json", exclude={"digest"})
            if digest(raw) != row.digest:
                raise HttpError(409, "snapshot digest mismatch")
            key = (row.credential_id, row.usage_date)
            if key in seen:
                raise HttpError(400, "duplicate snapshot key")
            seen.add(key)
            stored = Usage.objects.filter(
                node=node, credential_id=key[0], usage_date=key[1]
            ).first()
            if stored is not None:
                if row.version == stored.version and row.digest != stored.digest:
                    raise HttpError(409, "snapshot version conflict")
                if row.version > stored.version and (
                    row.upload < stored.upload or row.download < stored.download
                ):
                    raise HttpError(409, "counter regression")
            if stored is None or row.version > stored.version:
                values = row.model_dump(
                    exclude={"source_id", "credential_id", "usage_date"}
                )
                stored, _ = Usage.objects.update_or_create(
                    node=node, credential_id=key[0], usage_date=key[1], defaults=values
                )
            receipts.append({"version": stored.version, "digest": stored.digest})
    return {"receipts": receipts}


urlpatterns = [path("node-g0/v1/", api.urls)]
