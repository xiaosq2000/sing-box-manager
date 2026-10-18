from typing import ClassVar, cast

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import F

from sing_box_manager.control_plane.models import Account, Credential, User
from sing_box_manager.settings import ARGON2ID_VERIFIER
from sing_box_manager.subscription import subscription_token


class AccountAdminForm(forms.ModelForm):
    new_portal_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Enter a plaintext password to hash with Argon2id, or leave blank to keep the existing hash.",
    )

    class Meta:
        model = Account
        fields: ClassVar[list[str]] = [
            "username",
            "enabled",
            "is_portal_admin",
            "new_portal_password",
            "portal_password_hash",
            "subscription_counter",
        ]

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        new_password = cleaned.get("new_portal_password")
        if isinstance(new_password, str) and new_password:
            cleaned["portal_password_hash"] = ARGON2ID_VERIFIER.hash(new_password)
            self.instance.portal_password_hash = str(cleaned["portal_password_hash"])
        return cleaned


class CredentialInline(admin.TabularInline):
    model = Credential
    extra = 0
    max_num = 3


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    form = AccountAdminForm
    inlines: ClassVar[list[type[admin.TabularInline]]] = [CredentialInline]
    list_display: ClassVar[tuple[str, ...]] = (
        "username",
        "enabled",
        "is_portal_admin",
        "protocols_summary",
        "subscription_counter",
        "updated_at",
    )
    list_filter: ClassVar[tuple[str, ...]] = (
        "enabled",
        "is_portal_admin",
        "credentials__protocol",
    )
    search_fields: ClassVar[tuple[str, ...]] = ("username",)
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "subscription_path",
        "created_at",
        "updated_at",
    )
    actions: ClassVar[list[str]] = ["reset_subscription_links"]

    @admin.display(description="Enabled protocols")
    def protocols_summary(self, obj: Account) -> str:
        enabled = [c.protocol for c in obj.credentials.all() if c.enabled]
        return ", ".join(enabled) if enabled else "-"

    @admin.display(description="Subscription path")
    def subscription_path(self, obj: Account) -> str:
        from sing_box_manager.control_plane import django_settings

        secret = django_settings.runtime.web.subscription_secret
        if not secret or not obj.enabled or not obj.portal_password_hash:
            return "-"
        token = subscription_token(
            secret, str(obj.username), cast("int", obj.subscription_counter)
        )
        return f"/sub/{token}"

    @admin.action(description="Reset subscription links for selected accounts")
    def reset_subscription_links(self, request, queryset) -> None:
        updated = queryset.update(subscription_counter=F("subscription_counter") + 1)
        self.message_user(
            request, f"Reset subscription links for {updated} account(s)."
        )


admin.site.register(User, UserAdmin)
admin.site.site_header = "Service administration"
admin.site.site_title = "Service administration"
