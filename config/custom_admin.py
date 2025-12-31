# config/custom_admin.py
from __future__ import annotations

from django.contrib import admin


class CustomAdminSite(admin.AdminSite):
    site_header = "لوحة إدارة الاختبار"
    site_title = "لوحة إدارة الاختبار"
    index_title = "إدارة الموقع"


custom_admin_site = CustomAdminSite(name="custom_admin")
