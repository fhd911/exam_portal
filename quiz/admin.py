# quiz/admin.py
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import Quiz, Question, Choice, Participant, Attempt, Answer


# ======================================================
# Inlines
# ======================================================
class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 0
    min_num = 2
    max_num = 6
    fields = ("text", "is_correct")
    ordering = ("id",)


# ======================================================
# Quiz
# ======================================================
@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "is_active", "time_per_question_seconds")
    list_filter = ("is_active",)
    search_fields = ("title",)
    ordering = ("id",)
    list_per_page = 50


# ======================================================
# Question
# ======================================================
@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "order", "short_text")
    list_filter = ("quiz",)
    search_fields = ("text",)
    autocomplete_fields = ("quiz",)
    ordering = ("quiz_id", "order")
    inlines = [ChoiceInline]
    list_per_page = 50

    @admin.display(description="نص السؤال")
    def short_text(self, obj: Question) -> str:
        t = (obj.text or "").strip()
        return (t[:80] + "…") if len(t) > 80 else t


# ======================================================
# Choice
# ======================================================
@admin.register(Choice)
class ChoiceAdmin(admin.ModelAdmin):
    list_display = ("id", "question", "text", "is_correct")
    list_filter = ("is_correct", "question__quiz")
    search_fields = ("text", "question__text")
    autocomplete_fields = ("question",)
    ordering = ("id",)
    list_per_page = 50


# ======================================================
# Participant
# ======================================================
@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "full_name", "national_id", "phone_last4", "is_allowed", "has_taken_exam")
    list_filter = ("is_allowed", "has_taken_exam")
    search_fields = ("full_name", "national_id")
    ordering = ("id",)
    list_per_page = 50

    # حماية بسيطة: لا تجعل الهوية تتغير بعد إنشاء السجل (للاستقرار)
    readonly_fields = ()

    def get_readonly_fields(self, request, obj=None):
        if obj:  # بعد الإنشاء
            return ("national_id",)
        return ()


# ======================================================
# Attempt
# ======================================================
@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "participant_nid",
        "participant_name",
        "quiz",
        "score",
        "current_index",
        "finish_badge",
        "started_at",
        "finished_at",
        "ip_short",
    )
    list_filter = ("quiz", "is_finished")
    search_fields = ("participant__national_id", "participant__full_name")
    autocomplete_fields = ("participant", "quiz")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"
    list_per_page = 100

    # ✅ تسريع قوي (N+1 fix)
    list_select_related = ("participant", "quiz")

    # دائمًا للعرض فقط
    readonly_fields = ("started_at", "finished_at", "session_key", "started_ip", "user_agent")

    fieldsets = (
        ("الأساسيات", {"fields": ("participant", "quiz", "score", "current_index", "is_finished")}),
        ("الوقت", {"fields": ("started_at", "finished_at")}),
        ("معلومات تقنية", {"fields": ("session_key", "started_ip", "user_agent")}),
    )

    @admin.display(description="السجل", ordering="participant__national_id")
    def participant_nid(self, obj: Attempt) -> str:
        return obj.participant.national_id

    @admin.display(description="الاسم", ordering="participant__full_name")
    def participant_name(self, obj: Attempt) -> str:
        return obj.participant.full_name

    @admin.display(description="الحالة", ordering="is_finished")
    def finish_badge(self, obj: Attempt):
        if obj.is_finished:
            return format_html('<span style="padding:2px 8px;border-radius:999px;background:#dcfce7;color:#166534;font-weight:700;">منتهي</span>')
        return format_html('<span style="padding:2px 8px;border-radius:999px;background:#fef9c3;color:#854d0e;font-weight:700;">جارٍ</span>')

    @admin.display(description="IP")
    def ip_short(self, obj: Attempt) -> str:
        return obj.started_ip or "—"

    # 🔒 منع التعديل بعد الإنهاء (مع السماح بالعرض)
    def has_change_permission(self, request, obj=None):
        perm = super().has_change_permission(request, obj)
        if not perm:
            return False
        if obj and obj.is_finished:
            # اسمح بالدخول لصفحة العرض (GET) لكن امنع POST (التعديل)
            if request.method in ("POST", "PUT", "PATCH"):
                return False
        return True

    # 🔒 منع الحذف بعد الإنهاء
    def has_delete_permission(self, request, obj=None):
        perm = super().has_delete_permission(request, obj)
        if not perm:
            return False
        if obj and obj.is_finished:
            return False
        return True


# ======================================================
# Answer
# ======================================================
@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "attempt_id",
        "quiz_title",
        "participant_nid",
        "question_order",
        "choice_text",
        "is_late",
        "started_at",
        "answered_at",
    )
    list_filter = ("is_late", "question__quiz")
    search_fields = ("question__text", "attempt__participant__national_id", "attempt__participant__full_name")
    autocomplete_fields = ("attempt", "question", "selected_choice")
    ordering = ("-answered_at", "-started_at")
    date_hierarchy = "started_at"
    list_per_page = 100

    # ✅ تسريع قوي (N+1 fix)
    list_select_related = ("attempt", "attempt__participant", "question", "question__quiz", "selected_choice")

    readonly_fields = ("attempt", "question", "selected_choice", "started_at", "answered_at", "is_late")

    @admin.display(description="الاختبار", ordering="question__quiz__title")
    def quiz_title(self, obj: Answer) -> str:
        return obj.question.quiz.title

    @admin.display(description="السجل", ordering="attempt__participant__national_id")
    def participant_nid(self, obj: Answer) -> str:
        return obj.attempt.participant.national_id

    @admin.display(description="رقم السؤال", ordering="question__order")
    def question_order(self, obj: Answer) -> int:
        return obj.question.order

    @admin.display(description="الخيار المختار")
    def choice_text(self, obj: Answer) -> str:
        return obj.selected_choice.text if obj.selected_choice else "—"

    # 🔒 الإجابات لا تُعدل ولا تُحذف نهائيًا
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
