import csv
from django.contrib import admin
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _

from .models import Participant, Quiz, Question, Choice, Attempt, Answer


# ✅ عناوين لوحة الإدارة
admin.site.site_header = "لوحة إدارة الاختبار"
admin.site.site_title = "إدارة الاختبار"
admin.site.index_title = "إدارة الموقع"


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("national_id", "full_name", "is_allowed", "has_taken_exam")
    search_fields = ("national_id", "full_name")
    list_filter = ("is_allowed", "has_taken_exam")


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 4


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("quiz", "order", "text")
    list_filter = ("quiz",)
    search_fields = ("text",)
    inlines = [ChoiceInline]


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ("title", "time_per_question_seconds", "is_active")
    list_filter = ("is_active",)


def export_attempts_csv(modeladmin, request, queryset):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="attempts.csv"'
    writer = csv.writer(response)
    writer.writerow(["السجل", "الاسم", "الاختبار", "الدرجة", "بدأ", "انتهى", "منتهي؟"])

    for a in queryset.select_related("participant", "quiz"):
        writer.writerow([
            a.participant.national_id,
            a.participant.full_name,
            a.quiz.title,
            a.score,
            a.started_at,
            a.finished_at,
            "نعم" if a.is_finished else "لا",
        ])
    return response


export_attempts_csv.short_description = _("تصدير المحدد CSV")


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("participant", "quiz", "score", "started_at", "finished_at", "is_finished")
    list_filter = ("quiz", "is_finished")
    search_fields = ("participant__national_id", "participant__full_name")
    actions = [export_attempts_csv]


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("attempt", "question", "selected_choice", "started_at", "answered_at", "is_late")
    list_filter = ("is_late", "attempt__quiz")
