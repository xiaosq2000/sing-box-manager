from django.db import migrations, models


def preserve_access(apps, schema_editor):
    Account = apps.get_model("control_plane", "Account")
    Credential = apps.get_model("control_plane", "Credential")
    alias = schema_editor.connection.alias
    accounts = Account.objects.using(alias)
    # The old enabled field controlled subscriptions, not protocol access.
    accounts.update(portal_enabled=models.F("enabled"))
    accounts.filter(
        pk__in=Credential.objects.using(alias).filter(enabled=True).values("account_id")
    ).update(enabled=True)


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0002_accounts")]

    operations = [
        migrations.AddField(
            model_name="account",
            name="portal_enabled",
            field=models.BooleanField(default=True),
        ),
        # Reverting to the old model can restore revoked protocol access.
        # Use a protected backup for a deliberate rollback instead.
        migrations.RunPython(preserve_access),
    ]
