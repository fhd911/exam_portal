# config/urls.py
from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "إدارة جانغو"
admin.site.site_title = "نظام الاختبارات"
admin.site.index_title = "لوحة التحكم"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("quiz.urls")),
]
