"""HTTPS portal settings. This URL configuration never mounts the operator admin."""

from urllib.parse import urlsplit

from sing_box_manager.control_plane import django_settings as base

# Share models and database configuration, not the private HTTP cookie policy.
globals().update({name: value for name, value in vars(base).items() if name.isupper()})
runtime = base.runtime
SBM_RUNTIME = runtime
if not runtime.control_plane.portal_enabled or not runtime.control_plane.portal_origin:
    raise RuntimeError("The public Django portal is disabled")
ROOT_URLCONF = "sing_box_manager.managed_access.urls"
WSGI_APPLICATION = "sing_box_manager.managed_access.wsgi.application"
ALLOWED_HOSTS = [urlsplit(runtime.control_plane.portal_origin).hostname]
CSRF_TRUSTED_ORIGINS = [runtime.control_plane.portal_origin]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_NAME = "sbm_portal_session"
CSRF_COOKIE_NAME = "sbm_portal_csrf"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_AGE = 3600
SECURE_REFERRER_POLICY = "same-origin"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16384
DATA_UPLOAD_MAX_NUMBER_FIELDS = 12
MIDDLEWARE = [
    "sing_box_manager.managed_access.views.PublicBoundaryMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
# Django's request error messages can include the token-bearing path.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"discard": {"class": "logging.NullHandler"}},
    "loggers": {"django": {"handlers": ["discard"], "propagate": False}},
}
