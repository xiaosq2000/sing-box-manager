from django.contrib import admin
from django.urls import path

from sing_box_manager.control_plane import compat, portal
from sing_box_manager.control_plane.views import (
    health,
    install_ps1,
    install_sh,
    subscription,
    subscription_file,
    subscription_page,
)

urlpatterns = [
    path("health/", health, name="health"),
    path("admin/traffic/", admin.site.admin_view(portal.traffic), name="traffic"),
    path(
        "admin/traffic/connections/",
        admin.site.admin_view(portal.connections),
        name="connections",
    ),
    path(
        "admin/traffic/users/<str:username>/",
        admin.site.admin_view(portal.traffic_user),
        name="traffic_user",
    ),
    path("admin/", admin.site.urls),
    path("", compat.retired, name="home"),
    path("download", compat.retired),
    path("files", compat.retired),
    path("api/sub", compat.exchange_subscription),
    path("api/token", compat.machine_token),
    path("auth", compat.authenticate),
    path("api/usage", compat.usage),
    path("api/client-update", compat.client_update),
    path("api/vps-info", compat.vps_info),
    path("sub/<str:token>", subscription, name="subscription"),
    path("sub/<str:token>/", subscription_page, name="subscription_page"),
    path(
        "sub/<str:token>/files/<path:name>",
        subscription_file,
        name="subscription_file",
    ),
    path("install.sh", install_sh, name="install_sh"),
    path("install/linux.sh", install_sh, name="install_linux_sh"),
    path("install/macos.sh", install_sh, name="install_macos_sh"),
    path("install.ps1", install_ps1, name="install_ps1"),
    path("install/windows.ps1", install_ps1, name="install_windows_ps1"),
]
