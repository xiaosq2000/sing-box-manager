import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "sing_box_manager.control_plane.django_settings"
)
application = get_wsgi_application()
