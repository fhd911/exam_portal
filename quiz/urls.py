from django.urls import path
from . import views

app_name = "quiz"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("q/", views.question_view, name="question"),
    path("finish/", views.finish_view, name="finish"),

    # =========================
    # إدارة VIP (Staff only)
    # =========================
    path("staff/", views.staff_manage_view, name="staff_manage"),

    path("staff/attempt/<int:attempt_id>/", views.staff_attempt_detail_view, name="staff_attempt_detail"),
    path("staff/attempt/<int:attempt_id>/reset/", views.staff_reset_attempt_view, name="staff_attempt_reset"),
    path("staff/attempt/<int:attempt_id>/finish/", views.staff_force_finish_attempt_view, name="staff_attempt_finish"),

    path("staff/export/results.csv", views.staff_export_results_csv, name="staff_export_csv"),
    path("staff/export/results.xlsx", views.staff_export_results_xlsx, name="staff_export_xlsx"),
    path("staff/export/results.pdf", views.staff_export_results_pdf, name="staff_export_pdf"),

    # ✅ الاستيراد
    path("staff/import/questions/", views.staff_import_questions_view, name="staff_import"),
    path("staff/import/participants/", views.staff_import_participants_view, name="staff_import_participants"),
]
