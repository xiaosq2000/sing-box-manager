from django.urls import path

from sing_box_manager.managed_access import views

urlpatterns = [
    path("", views.login, name="portal-login"),
    path("login", views.login),
    path("files", views.files, name="portal-files"),
    path("logout", views.logout, name="portal-logout"),
    path("subscription/reset", views.reset, name="portal-reset"),
    path("api/sub", views.exchange),
    path("sub/<str:token>", views.subscription),
    path("sub/<str:token>/files/<path:name>", views.client_file),
    path("install.sh", views.installer),
    path("install/linux.sh", views.installer),
    path("install/macos.sh", views.installer),
    path("install.ps1", views.installer, {"windows": True}),
    path("install/windows.ps1", views.installer, {"windows": True}),
    path("health/", views.health),
]
