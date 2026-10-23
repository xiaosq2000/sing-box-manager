"""Private PostgreSQL fixture settings, independent of operator SBM_CONFIG."""

import json
import os
from pathlib import Path

RUN_DIR = Path(os.environ["SBM_NODE_G0_DIR"])
fixture = json.loads((RUN_DIR / "control.json").read_text())
SECRET_KEY = fixture["django_secret"]
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "10.0.2.100", "localhost", "testserver"]
ROOT_URLCONF = "tests.node_g0.urls"
INSTALLED_APPS = ["tests.node_g0"]
MIDDLEWARE = []
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "node_g0",
        "USER": "postgres",
        "HOST": fixture["socket_dir"],
        "PORT": 5432,
        "OPTIONS": {"connect_timeout": 5},
    }
}
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 1 << 20
