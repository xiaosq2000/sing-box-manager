import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def allocate_credential_uuids(apps, schema_editor):
    Credential = apps.get_model("control_plane", "Credential")
    alias = schema_editor.connection.alias
    manager = Credential.objects.using(alias)
    # Preserve every identity already allocated by a direct replay. Only rows
    # without an identity receive a new one, and existing values still reserve
    # their UUIDs against a collision in this allocation pass.
    allocated = set(manager.exclude(uuid__isnull=True).values_list("uuid", flat=True))
    for pk in (
        manager.filter(uuid__isnull=True).order_by("pk").values_list("pk", flat=True)
    ):
        value = uuid.uuid4()
        while value in allocated:
            value = uuid.uuid4()
        manager.filter(pk=pk, uuid__isnull=True).update(uuid=value)
        allocated.add(value)


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0005_snell")]

    operations = [
        migrations.CreateModel(
            name="Node",
            fields=[
                (
                    "id",
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                ("slug", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                (
                    "token_digest",
                    models.CharField(
                        max_length=64,
                        unique=True,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[0-9a-f]{64}$", "Must be lowercase hex."
                            )
                        ],
                    ),
                ),
                (
                    "token_updated_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=~models.Q(slug=""),
                        name="node_slug_not_empty",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("token_digest__regex", "^[0-9a-f]{64}$")),
                        name="node_token_digest_lower_hex",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="TrafficSource",
            fields=[
                (
                    "id",
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                (
                    "node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="traffic_sources",
                        to="control_plane.node",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "retired_at",
                    models.DateTimeField(blank=True, null=True),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(retired_at__isnull=True),
                        fields=("node",),
                        name="unique_active_traffic_source",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="credential",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(allocate_credential_uuids),
        migrations.AlterField(
            model_name="credential",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name="credential",
            name="node",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credentials",
                to="control_plane.node",
            ),
        ),
        migrations.AddField(
            model_name="credential",
            name="wire_name",
            field=models.CharField(
                blank=True, editable=False, max_length=128, null=True
            ),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.CheckConstraint(
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
        ),
    ]
