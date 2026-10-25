from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThan, LessThanOrEqual
from django.utils import timezone

from sing_box_manager.settings import (
    SUPPORTED_PROTOCOLS,
    USERNAME_PATTERN,
    SnellUser,
    _validate_password_hash,
)


class User(AbstractUser):
    """Private Django operators. Proxy accounts remain separate."""

    class Meta:
        verbose_name = "operator"
        verbose_name_plural = "operators"


class Account(models.Model):
    """A proxy user account and its subscription state."""

    username = models.CharField(max_length=32, unique=True)
    enabled = models.BooleanField(default=True)
    portal_enabled = models.BooleanField(default=True)
    is_portal_admin = models.BooleanField(default=False)
    portal_password_hash = models.CharField(max_length=255, blank=True)
    subscription_counter = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    credentials: models.Manager[Credential]
    objects = models.Manager()

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("view_traffic", "Can view all traffic statistics"),
        ]

    def __str__(self) -> str:
        return str(self.username)

    def clean(self) -> None:
        if not USERNAME_PATTERN.fullmatch(str(self.username)):
            raise ValidationError(
                {"username": "Username must match ^[a-zA-Z0-9_-]{3,32}$."}
            )
        if self.portal_password_hash:
            try:
                _validate_password_hash(str(self.portal_password_hash))
            except ValueError as exc:
                raise ValidationError({"portal_password_hash": str(exc)}) from exc


class Node(models.Model):
    """Node identity metadata supplied by the node installer."""

    # The node installation supplies this ID. Dormant until an explicit
    # registration operation supplies it, so the database never invents one.
    id = models.UUIDField(primary_key=True, editable=False)
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    token_digest = models.CharField(
        max_length=64,
        unique=True,
        validators=[RegexValidator(r"^[0-9a-f]{64}$", "Must be lowercase hex.")],
    )
    token_updated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~models.Q(slug=""), name="node_slug_not_empty"
            ),
            models.CheckConstraint(
                condition=models.Q(("token_digest__regex", r"^[0-9a-f]{64}$")),
                name="node_token_digest_lower_hex",
            ),
        ]

    def __str__(self) -> str:
        return str(self.slug)


class TrafficSource(models.Model):
    """One registered local accounting source for a node."""

    # The node installation supplies this ID; the central schema never
    # generates one or creates a placeholder source.
    id = models.UUIDField(primary_key=True, editable=False)
    node = models.ForeignKey(
        Node, on_delete=models.PROTECT, related_name="traffic_sources"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["node"],
                condition=models.Q(retired_at__isnull=True),
                name="unique_active_traffic_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.node} ({self.id})"


class Credential(models.Model):
    """Per-protocol proxy credential for an account."""

    PROTOCOL_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("trojan", "Trojan"),
        ("hysteria2", "Hysteria2"),
        ("naive", "Naive"),
        ("snell", "Snell v6"),
    ]

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="credentials"
    )
    protocol = models.CharField(max_length=16, choices=PROTOCOL_CHOICES)
    secret = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    node = models.ForeignKey(
        Node,
        on_delete=models.PROTECT,
        related_name="credentials",
        null=True,
        blank=True,
        editable=False,
    )
    # NULL wire_name marks an unbound credential. The pair constraint
    # requires a non-empty label whenever node is set.
    wire_name = models.CharField(  # noqa: DJ001
        max_length=128, null=True, blank=True, editable=False
    )
    objects = models.Manager()

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["account", "protocol"], name="unique_account_protocol"
            ),
            models.UniqueConstraint(
                fields=["secret"],
                condition=models.Q(protocol="snell"),
                name="unique_snell_userkey",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(node__isnull=True, wire_name__isnull=True)
                    | (
                        models.Q(node__isnull=False)
                        & models.Q(wire_name__isnull=False)
                        & ~models.Q(wire_name="")
                    )
                ),
                name="credential_node_wire_pair",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account} ({self.protocol})"

    def clean(self) -> None:
        secret = str(self.secret)
        if str(self.protocol) not in SUPPORTED_PROTOCOLS:
            raise ValidationError({"protocol": "Unsupported protocol."})
        if str(self.protocol) == "snell":
            try:
                SnellUser.validate_userkey(secret)
            except ValueError as exc:
                raise ValidationError({"secret": str(exc)}) from exc
        if (
            not secret
            or secret.strip() != secret
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)
        ):
            raise ValidationError(
                {
                    "secret": "Secret must be a non-empty string without leading/trailing whitespace or control characters."
                }
            )


class TrafficSnapshot(models.Model):
    """One versioned absolute traffic bucket stored in PostgreSQL."""

    KIND_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("counter", "Counter"),
        ("attribution", "Attribution"),
    ]
    DETAIL_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("exact", "Exact"),
        ("compacted", "Compacted"),
        ("degraded", "Degraded"),
    ]

    source = models.ForeignKey(
        TrafficSource,
        on_delete=models.PROTECT,
        related_name="traffic_snapshots",
    )
    # The wire credential identity is the immutable UUID, not the local PK.
    credential = models.ForeignKey(
        Credential,
        to_field="uuid",
        on_delete=models.PROTECT,
        related_name="traffic_snapshots",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    accounting_date = models.DateField()
    version = models.PositiveBigIntegerField()
    digest = models.CharField(
        max_length=64,
        validators=[RegexValidator(r"^[0-9a-f]{64}$", "Must be lowercase hex.")],
    )
    # Exact canonical payload without the digest. BinaryField keeps names that
    # JSONB cannot represent, including U+0000 and other C0 controls.
    payload = models.BinaryField(editable=False)
    upload = models.PositiveBigIntegerField(null=True, blank=True)
    download = models.PositiveBigIntegerField(null=True, blank=True)
    detail = models.CharField(  # noqa: DJ001 - NULL is part of the counter shape.
        max_length=16, choices=DETAIL_CHOICES, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = models.Manager()

    class Meta:
        ordering: ClassVar[list[str]] = [
            "source_id",
            "credential_id",
            "kind",
            "accounting_date",
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["source", "credential", "kind", "accounting_date"],
                name="unique_traffic_snapshot_key",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="traffic_snapshot_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(digest__regex=r"^[0-9a-f]{64}$"),
                name="traffic_snapshot_digest_lower_hex",
            ),
            models.CheckConstraint(
                condition=(
                    GreaterThan(Length(models.F("payload")), 0)
                    & LessThanOrEqual(Length(models.F("payload")), 768 * 1024)
                ),
                name="traffic_snapshot_payload_bounds",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["counter", "attribution"]),
                name="traffic_snapshot_kind_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="counter",
                        upload__isnull=False,
                        download__isnull=False,
                        detail__isnull=True,
                    )
                    | models.Q(
                        kind="attribution",
                        upload__isnull=True,
                        download__isnull=True,
                        detail__isnull=False,
                        detail__in=["exact", "compacted", "degraded"],
                    )
                ),
                name="traffic_snapshot_kind_shape",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)
