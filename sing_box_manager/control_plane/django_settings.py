"""Production settings for the loopback-only, SSH-tunnelled admin service."""

import os

from sing_box_manager.settings import PROJECT_ROOT, load_settings

config_path = os.environ.get("SBM_CONFIG")
if not config_path:
    raise RuntimeError("SBM_CONFIG is required for the control plane")

runtime = load_settings(config_path)
control = runtime.control_plane
if not control.enabled:
    raise RuntimeError("The control plane is disabled")
if not control.secret_key:
    raise RuntimeError("control_plane.secret_key is required to start Django")

SECRET_KEY = control.secret_key
DEBUG = False
ALLOWED_HOSTS = list(
    dict.fromkeys(
        [
            "127.0.0.1",
            "localhost",
            *(
                [runtime.deployment_data.host]
                if runtime.deployment_data is not None
                else []
            ),
            *runtime.web.allowed_hosts,
        ]
    )
)
ROOT_URLCONF = "sing_box_manager.control_plane.urls"
WSGI_APPLICATION = "sing_box_manager.control_plane.wsgi.application"
AUTH_USER_MODEL = "control_plane.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "sing_box_manager.control_plane.apps.ControlPlaneConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "sing_box_manager.control_plane.views.LoopbackAdminMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": control.database_name,
        "USER": "sbm",
        "HOST": str(control.database_socket_dir),
        "PORT": control.database_port,
        "OPTIONS": {"connect_timeout": 5},
        "CONN_MAX_AGE": 60,
    },
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{name}"}
    for name in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = str(
    (PROJECT_ROOT / runtime.generated_root / "control-plane-static").resolve()
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
WHITENOISE_USE_FINDERS = False
WHITENOISE_AUTOREFRESH = False
FILE_UPLOAD_PERMISSIONS = 0o640
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o750
# HTTP exists only on loopback. SSH supplies encryption for remote operators.
# Use distinct cookies so the legacy portal session remains independent.
SESSION_COOKIE_NAME = "sbm_control_session"
CSRF_COOKIE_NAME = "sbm_control_csrf"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_CONTENT_TYPE_NOSNIFF = True
# no-referrer can make browser form submissions send Origin: null.
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
