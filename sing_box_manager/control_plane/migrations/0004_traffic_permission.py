from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0003_account_portal_enabled")]

    operations = [
        migrations.AlterModelOptions(
            name="account",
            options={
                "ordering": ["id"],
                "permissions": [("view_traffic", "Can view all traffic statistics")],
            },
        ),
    ]
