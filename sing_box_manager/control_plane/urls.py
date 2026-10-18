from django.contrib import admin
from django.urls import path

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
    path("admin/", admin.site.urls),
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
