import uuid
from typing import ClassVar

from django.db import models


class Ownership(models.Model):
    """Explicitly prepared ownership; no migration seeds or automatic activation."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    identity = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.OneToOneField(
        "control_plane.ImportSource", on_delete=models.PROTECT
    )
    key_identity = models.CharField(max_length=64)
    target_identity = models.CharField(max_length=64)
    active = models.BooleanField(default=False)
    revision = models.PositiveBigIntegerField(default=1)
    applied_revision = models.PositiveBigIntegerField(default=0)
    applied_context = models.CharField(max_length=64, blank=True)
    applied_config = models.CharField(max_length=64, blank=True)
    last_error = models.CharField(max_length=64, blank=True)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="managed_owner_singleton"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="managed_revision_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(applied_revision__lte=models.F("revision")),
                name="managed_revision_order",
            ),
        ]

    def __str__(self):
        return f"Ownership {self.identity}"


class LinkGeneration(models.Model):
    """New-family revocation state. This is not an imported legacy counter."""

    subject = models.OneToOneField(
        "control_plane.ProxySubject", primary_key=True, on_delete=models.PROTECT
    )
    generation = models.PositiveBigIntegerField(default=1)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=models.Q(generation__gte=1), name="managed_link_positive"
            ),
        ]

    def __str__(self):
        return f"Link generation {self.pk}"
