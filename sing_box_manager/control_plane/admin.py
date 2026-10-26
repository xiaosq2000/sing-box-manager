from typing import ClassVar, cast

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import transaction
from django.db.models import F

from sing_box_manager.control_plane.account_services import (
    ensure_default_credentials,
)
from sing_box_manager.control_plane.models import Account, Credential, User
from sing_box_manager.control_plane.node_sync import request_node_sync
from sing_box_manager.settings import ARGON2ID_VERIFIER
from sing_box_manager.subscription import subscription_token


class AccountAdminForm(forms.ModelForm):
    new_portal_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Optional legacy client password. Leave blank to preserve the hash. Subscription links do not require a password.",
    )

    class Meta:
        model = Account
        fields: ClassVar[list[str]] = [
            "username",
            "enabled",
            "portal_enabled",
            "new_portal_password",
            "portal_password_hash",
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
        "portal_enabled",
        "protocols_summary",
        "user_page_url",
        "subscription_counter",
        "updated_at",
    )
    list_filter: ClassVar[tuple[str, ...]] = (
        "enabled",
        "portal_enabled",
        "credentials__protocol",
    )
    search_fields: ClassVar[tuple[str, ...]] = ("username",)
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "user_page_url",
        "subscription_path",
        "subscription_counter",
        "created_at",
        "updated_at",
    )
    actions: ClassVar[list[str]] = ["reset_subscription_links", "sync_node_now"]

    @admin.display(description="Enabled protocols")
    def protocols_summary(self, obj: Account) -> str:
        enabled = [
            c.protocol for c in obj.credentials.all() if obj.enabled and c.enabled
        ]
        return ", ".join(enabled) if enabled else "-"

    @admin.display(description="Subscription path")
    def subscription_path(self, obj: Account) -> str:
        from sing_box_manager.control_plane import django_settings

        secret = django_settings.runtime.web.subscription_secret
        if not secret or not obj.enabled or not obj.portal_enabled:
            return "-"
        token = subscription_token(
            secret, str(obj.username), cast("int", obj.subscription_counter)
        )
        return f"/sub/{token}"

    @admin.display(description="User page URL")
    def user_page_url(self, obj: Account) -> str:
        from sing_box_manager.control_plane import django_settings

        path = self.subscription_path(obj)
        if path == "-":
            return "-"
        try:
            host = django_settings.runtime.deployment.host
        except ValueError:
            return f"{path}/"
        if host and host not in {"example.com", "127.0.0.1", "localhost"}:
            return f"https://{host}{path}/"
        return f"{path}/"

    def _maybe_trigger_node_sync(self, request, *, force: bool = False) -> None:
        from sing_box_manager.control_plane import django_settings

        runtime = django_settings.runtime
        if not force and not runtime.control_plane.authoritative:
            return

        def sync_after_commit() -> None:
            try:
                request_node_sync(runtime)
            except OSError:
                self.message_user(
                    request,
                    "Changes saved, but the node sync request failed. "
                    "Run sudo sbm control-plane sync on the server.",
                    level="error",
                )
            else:
                self.message_user(
                    request,
                    "Node sync requested. Check sing-box-manager-sync.service for the result.",
                )

        transaction.on_commit(sync_after_commit)

    def save_related(self, request, form, formsets, change) -> None:
        super().save_related(request, form, formsets, change)
        created = ensure_default_credentials(form.instance) if not change else []
        if created:
            self.message_user(
                request,
                f"Generated random credentials for {', '.join(created)}.",
            )
        self._maybe_trigger_node_sync(request)

    def delete_model(self, request, obj) -> None:
        super().delete_model(request, obj)
        self._maybe_trigger_node_sync(request)

    def delete_queryset(self, request, queryset) -> None:
        super().delete_queryset(request, queryset)
        self._maybe_trigger_node_sync(request)

    @admin.action(description="Reset subscription links for selected accounts")
    def reset_subscription_links(self, request, queryset) -> None:
        updated = queryset.update(subscription_counter=F("subscription_counter") + 1)
        self.message_user(
            request, f"Reset subscription links for {updated} account(s)."
        )

    @admin.action(description="Sync sing-box node config now")
    def sync_node_now(self, request, queryset) -> None:
        del queryset
        self._maybe_trigger_node_sync(request, force=True)


admin.site.register(User, UserAdmin)
admin.site.site_header = "Service administration"
admin.site.site_title = "Service administration"
