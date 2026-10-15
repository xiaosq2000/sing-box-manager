import uuid
from typing import ClassVar

from django.db import models


class AccountCopyAdmission(models.Model):
    """Separate admission for account copying, never for fixture or token import."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.OneToOneField(
        "control_plane.ImportTarget", on_delete=models.PROTECT
    )
    namespace = models.UUIDField(unique=True)
    source_identity = models.UUIDField(unique=True)
    enabled = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    objects = models.Manager()

    class Meta:
        constraints: ClassVar[list] = [
            models.CheckConstraint(
                condition=~models.Q(enabled=True, closed=True),
                name="account_copy_closed_disables",
            ),
        ]

    def __str__(self) -> str:
        return f"AccountCopyAdmission {self.pk}"
