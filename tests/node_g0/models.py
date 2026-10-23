"""G0-only schema. It cannot migrate or query the production account database."""

from django.db import models


class Node(models.Model):
    id = models.UUIDField(primary_key=True)
    source_id = models.UUIDField(unique=True)
    token_digest = models.CharField(max_length=64)
    bundle = models.JSONField()
    generation = models.PositiveBigIntegerField(default=1)
    applied_generation = models.PositiveBigIntegerField(default=0)
    invocation = models.CharField(max_length=64, default="")


class Usage(models.Model):
    node = models.ForeignKey(Node, on_delete=models.PROTECT)
    credential_id = models.UUIDField()
    usage_date = models.DateField()
    version = models.PositiveBigIntegerField()
    upload = models.PositiveBigIntegerField()
    download = models.PositiveBigIntegerField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["node", "credential_id", "usage_date"], name="g0_usage_key"
            )
        ]
