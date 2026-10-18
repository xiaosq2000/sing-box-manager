"""Display filters shared by the subscription and operator pages."""

from django import template

from sing_box_manager.web.charts import domain_display_name
from sing_box_manager.web.formatting import format_byte_count

register = template.Library()
register.filter("bytes", format_byte_count)
register.filter("domain_name", domain_display_name)
