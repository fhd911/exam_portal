# quiz/urls.py
from django.urls import path
from . import views

app_name = "quiz"

urlpatterns = [
    # Public
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("question/", views.question_view, name="question"),
    path("finish/", views.finish_view, name="finish"),

    # Staff
    path("staff/", views.staff_manage_view, name="staff_manage"),

    # Import
    path("staff/import/questions/", views.staff_import_questions_view, name="staff_import_questions"),
    path("staff/import/participants/", views.staff_import_participants_view, name="staff_import_participants"),

    # Export
    path("staff/export/csv/", views.staff_export_csv_view, name="staff_export_csv"),
    path("staff/export/xlsx/", views.staff_export_xlsx_view, name="staff_export_xlsx"),

    # Attempt actions
    path("staff/attempt/<int:attempt_id>/", views.staff_attempt_detail_view, name="staff_attempt_detail"),
    path("staff/attempt/<int:attempt_id>/finish/", views.staff_force_finish_attempt_view, name="staff_attempt_finish"),
    path("staff/attempt/<int:attempt_id>/reset/", views.staff_reset_attempt_view, name="staff_attempt_reset"),
]
