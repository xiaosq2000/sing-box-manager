"""Operator-only account forms. Passwords and verifiers are never displayed."""

from argon2 import PasswordHasher
from django import forms
from django.contrib import admin
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from sing_box_manager.control_plane.import_schema import KINDS, AccessRecords
from sing_box_manager.control_plane.models import ImportedAccess, ProxySubject
from sing_box_manager.managed_access import service
from sing_box_manager.managed_access.models import Ownership


def runtime():
    from sing_box_manager.control_plane.django_settings import runtime as value

    return value


def application_state(value):
    try:
        value = service.owner(runtime(), active=bool(value.active))
    except Exception:
        return "Blocked: check the ownership binding"
    state = (
        "Prepared, not active"
        if not value.active
        else "Recorded applied"
        if service.ready(value, runtime())
        else "Pending or failed"
    )
    return f"{state}. Desired revision {value.revision}, applied revision {value.applied_revision}."


class AccountForm(forms.ModelForm):
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    revoke_link = forms.BooleanField(
        required=False,
        label="Revoke the current subscription URL",
        help_text="Deliver the replacement URL privately. This does not revoke proxy credentials or disconnect clients.",
    )
    retire_account = forms.BooleanField(
        required=False,
        label="Retire this account permanently",
        help_text="Disable every membership and reserve the username. Apply proxy changes separately to disconnect clients.",
    )

    class Meta:
        model = ProxySubject
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = service.owner(runtime()).revision
        self.adding = self.instance._state.adding  # noqa: SLF001 -- Django's documented model lifecycle
        self.previous = (
            {}
            if self.adding
            else {
                row.kind: row.payload
                for row in ImportedAccess.objects.filter(
                    subject=self.instance, present=True
                )
            }
        )
        if not self.adding:
            self.fields["username"].disabled = True
        for kind in KINDS:
            old = self.previous.get(kind)
            self.fields[f"{kind}_member"] = forms.BooleanField(
                required=False, label=f"{kind}: membership", initial=old is not None
            )
            self.fields[f"{kind}_enabled"] = forms.BooleanField(
                required=False,
                label=f"{kind}: enabled",
                initial=old is not None and old["enabled"],
            )
            self.fields[f"{kind}_password"] = forms.CharField(
                required=False,
                min_length=8,
                max_length=1024,
                strip=False,
                widget=forms.PasswordInput(render_value=False),
                label=f"{kind}: new password",
                help_text="Leave empty to retain the current credential. Required for a new membership.",
            )
        self.fields["portal_admin"] = forms.BooleanField(
            required=False,
            initial=self.previous.get("portal", {}).get("admin", False),
            label="Retained portal admin flag",
            help_text="This flag grants no Django operator permission.",
        )

    def clean(self):
        cleaned = super().clean()
        value = service.owner(runtime(), active=True)
        if cleaned.get("expected_revision") != value.revision:
            raise forms.ValidationError(
                "Accounts changed. Reload this form before you submit it again."
            )
        username = cleaned.get("username")
        if not username:
            return cleaned
        if (
            self.adding
            and ProxySubject.objects.filter(
                source=value.source, username=username
            ).exists()
        ):
            raise forms.ValidationError("This username is already reserved.")
        if cleaned.get("retire_account"):
            if self.adding:
                raise forms.ValidationError("A new account cannot be retired.")
            return cleaned
        self.records = self._records(cleaned, username)
        return cleaned

    def _records(self, cleaned, username):
        data = {kind: [] for kind in KINDS}
        for kind in KINDS:
            if not cleaned.get(f"{kind}_member"):
                continue
            old = self.previous.get(kind, {})
            password = cleaned.get(f"{kind}_password", "")
            key = "password_hash" if kind == "portal" else "password"
            if password and password == runtime().control_plane.subscription_secret:
                raise forms.ValidationError(
                    "A password must differ from the subscription signing key."
                )
            credential = (
                PasswordHasher().hash(password)
                if kind == "portal" and password
                else password or old.get(key)
            )
            if credential is None:
                raise forms.ValidationError("Each new membership requires a password.")
            payload = {
                "username": username,
                "enabled": bool(cleaned.get(f"{kind}_enabled")),
                key: credential,
            }
            if kind == "portal":
                payload["admin"] = bool(cleaned.get("portal_admin"))
            data[kind] = [payload]
        try:
            records = AccessRecords.model_validate(data)
        except ValueError:
            raise forms.ValidationError(
                "Use a valid username and at least one valid membership."
            ) from None
        if set(records.memberships()) != {username}:
            raise forms.ValidationError("At least one membership is required.")
        return records


@admin.register(ProxySubject)
class AccountAdmin(admin.ModelAdmin):
    form = AccountForm
    list_display = ("username", "present", "application", "id")
    search_fields = ("username",)
    list_filter = ("present",)
    actions = None
    readonly_fields = ("subscription_link", "application")
    # Fields added in Form.__init__ must also be included by the admin layout.
    fields = (
        "username",
        "application",
        "subscription_link",
        "expected_revision",
        *tuple(
            field
            for kind in KINDS
            for field in (f"{kind}_member", f"{kind}_enabled", f"{kind}_password")
        ),
        "portal_admin",
        "revoke_link",
        "retire_account",
    )

    def get_fields(self, request, obj=None):
        visible = (
            obj is not None
            and obj.present
            and request.user.has_perm("control_plane.change_proxysubject")
        )
        return tuple(
            name for name in self.fields if name != "subscription_link" or visible
        )

    @admin.display(description="Node application")
    def application(self, obj):
        return application_state(Ownership.objects.get(pk=1))

    @admin.display(description="Subscription URL (credential)")
    def subscription_link(self, obj):
        return (
            service.link_for(runtime(), service.owner(runtime()), obj.pk)
            or "No current subscription URL"
        )

    def get_form(self, request, obj=None, change=False, **kwargs):  # noqa: FBT002 -- Django's override signature
        # Django's model-form factory rejects dynamically declared non-model fields.
        return self.form

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return super().has_add_permission(request) and self._active()

    def has_change_permission(self, request, obj=None):
        return (
            super().has_change_permission(request, obj)
            and self._active()
            and (obj is None or obj.present)
        )

    @staticmethod
    def _active():
        try:
            service.owner(runtime(), active=True)
            return True
        except Exception:
            return False

    def get_queryset(self, request):
        value = Ownership.objects.filter(pk=1).first()
        rows = super().get_queryset(request)
        return rows.none() if value is None else rows.filter(source=value.source)

    @method_decorator(sensitive_post_parameters())
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # Hold the same lock from stale-form validation through save and admin log.
        if request.method == "POST":
            with transaction.atomic():
                service.owner(runtime(), active=True, lock=True)
                return super().changeform_view(
                    request, object_id, form_url, extra_context
                )
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        if form.cleaned_data["retire_account"]:
            service.retire(
                runtime(),
                obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
            )
            obj.refresh_from_db()
        else:
            saved = service.save_account(
                runtime(),
                obj.username,
                form.records,
                subject_id=obj.pk if change else None,
                expected_revision=form.cleaned_data["expected_revision"],
            )
            obj.pk, obj.source, obj.present = saved.pk, saved.source, saved.present
            obj._state = saved._state  # noqa: SLF001 -- preserve Django's saved-instance state
            if form.cleaned_data["revoke_link"]:
                service.revoke_link(runtime(), saved.pk)


@admin.register(Ownership)
class OwnershipAdmin(admin.ModelAdmin):
    list_display = ("identity", "state", "revision", "applied_revision", "last_error")
    readonly_fields = (
        "identity",
        "state",
        "active",
        "revision",
        "applied_revision",
        "applied_context",
        "applied_config",
        "last_error",
    )
    fields = readonly_fields
    actions = None

    @admin.display(description="State")
    def state(self, obj):
        return application_state(obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
