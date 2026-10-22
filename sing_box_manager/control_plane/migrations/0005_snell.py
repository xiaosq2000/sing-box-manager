from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0004_traffic_permission")]

    operations = [
        migrations.AlterField(
            model_name="credential",
            name="protocol",
            field=models.CharField(
                max_length=16,
                choices=[
                    ("trojan", "Trojan"),
                    ("hysteria2", "Hysteria2"),
                    ("naive", "Naive"),
                    ("snell", "Snell v6"),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.UniqueConstraint(
                fields=["secret"],
                condition=models.Q(protocol="snell"),
                name="unique_snell_userkey",
            ),
        ),
    ]
