import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Django operators only until the explicit proxy-user ownership cutover."""

    class Meta:
        verbose_name = "operator"
        verbose_name_plural = "operators"


class ImportTarget(models.Model):
    """Migration-created identity; fixture imports are disabled by default."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    identity = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    fixture_enabled = models.BooleanField(default=False)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="import_target_singleton"
            )
        ]

    def __str__(self) -> str:
        return f"ImportTarget {self.pk}"


class ImportSource(models.Model):
    namespace = models.UUIDField(primary_key=True)
    source_identity = models.UUIDField(unique=True)
    sequence = models.PositiveBigIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    bundle_digest = models.CharField(max_length=64, blank=True)
    objects = models.Manager()

    def __str__(self) -> str:
        return f"ImportSource {self.pk}"


class ProxySubject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT)
    username = models.CharField(max_length=32)
    present = models.BooleanField(default=True)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["source", "username"], name="import_subject_name"
            )
        ]

    def __str__(self) -> str:
        return f"ProxySubject {self.pk}"


class ImportedAccess(models.Model):
    subject = models.ForeignKey(ProxySubject, on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=10,
        choices=[(name, name) for name in ("portal", "trojan", "hysteria2", "naive")],
    )
    present = models.BooleanField(default=True)
    payload = models.JSONField()
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["subject", "kind"], name="import_access_kind"
            )
        ]

    def __str__(self) -> str:
        return f"ImportedAccess {self.pk}"


class LegacyAccess(models.Model):
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT)
    subject = models.ForeignKey(ProxySubject, on_delete=models.PROTECT, null=True)
    token_username = models.CharField(max_length=32)
    counter = models.PositiveBigIntegerField(default=0)
    secret_identity = models.CharField(max_length=64)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["source", "token_username"], name="import_legacy_name"
            )
        ]

    def __str__(self) -> str:
        return f"LegacyAccess {self.pk}"


class ImportRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT)
    report_digest = models.CharField(max_length=64, unique=True)
    bundle_digest = models.CharField(max_length=64)
    sequence = models.PositiveBigIntegerField()
    revision = models.PositiveBigIntegerField()
    counts = models.JSONField()
    objects = models.Manager()

    def __str__(self) -> str:
        return f"ImportRun {self.pk}"
