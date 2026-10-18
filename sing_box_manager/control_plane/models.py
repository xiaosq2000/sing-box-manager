from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from sing_box_manager.settings import (
    SUPPORTED_PROTOCOLS,
    USERNAME_PATTERN,
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


class Credential(models.Model):
    """Per-protocol proxy credential for an account."""

    PROTOCOL_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("trojan", "Trojan"),
        ("hysteria2", "Hysteria2"),
        ("naive", "Naive"),
    ]

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="credentials"
    )
    protocol = models.CharField(max_length=16, choices=PROTOCOL_CHOICES)
    secret = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    objects = models.Manager()

    class Meta:
        ordering: ClassVar[list[str]] = ["id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["account", "protocol"], name="unique_account_protocol"
            )
        ]

    def __str__(self) -> str:
        return f"{self.account} ({self.protocol})"

    def clean(self) -> None:
        secret = str(self.secret)
        if str(self.protocol) not in SUPPORTED_PROTOCOLS:
            raise ValidationError({"protocol": "Unsupported protocol."})
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
