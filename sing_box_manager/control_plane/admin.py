from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from sing_box_manager.control_plane.models import User

admin.site.register(User, UserAdmin)
admin.site.site_header = "Service administration"
admin.site.site_title = "Service administration"
