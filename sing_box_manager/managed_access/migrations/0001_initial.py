import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("account_copy", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="Ownership",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, primary_key=True, serialize=False
                    ),
                ),
                (
                    "identity",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("key_identity", models.CharField(max_length=64)),
                ("target_identity", models.CharField(max_length=64)),
                ("active", models.BooleanField(default=False)),
                ("revision", models.PositiveBigIntegerField(default=1)),
                ("applied_revision", models.PositiveBigIntegerField(default=0)),
                ("applied_context", models.CharField(blank=True, max_length=64)),
                ("applied_config", models.CharField(blank=True, max_length=64)),
                ("last_error", models.CharField(blank=True, max_length=64)),
                (
                    "source",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="control_plane.importsource",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(id=1), name="managed_owner_singleton"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(revision__gte=1),
                        name="managed_revision_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(applied_revision__lte=models.F("revision")),
                        name="managed_revision_order",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="LinkGeneration",
            fields=[
                (
                    "subject",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        serialize=False,
                        to="control_plane.proxysubject",
                    ),
                ),
                ("generation", models.PositiveBigIntegerField(default=1)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(generation__gte=1),
                        name="managed_link_positive",
                    )
                ]
            },
        ),
    ]
