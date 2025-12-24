from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),

    # ✅ مهم جداً: تفعيل namespace = quiz
    path("", include(("quiz.urls", "quiz"), namespace="quiz")),
]
