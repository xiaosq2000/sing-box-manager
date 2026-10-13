from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Django operators only until the explicit proxy-user ownership cutover."""

    class Meta:
        verbose_name = "operator"
        verbose_name_plural = "operators"
