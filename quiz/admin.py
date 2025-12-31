# quiz/admin.py
from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Count, Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Answer, Attempt, Choice, Participant, Question, Quiz


# ======================================================
# UI helpers
# ======================================================
def _pill(text: str, bg: str, fg: str) -> str:
    # ✅ format_html لازم يستقبل args/kwargs (عشان ما يطلع TypeError)
    return format_html(
        '<span style="display:inline-flex;align-items:center;gap:6px;'
        'padding:2px 10px;border-radius:999px;'
        'background:{};color:{};font-weight:900;font-size:12px;'
        'border:1px solid rgba(0,0,0,.06);white-space:nowrap;">{}</span>',
        bg,
        fg,
        text,
    )


def _domain_label(domain: str) -> str:
    return {"deputy": "وكيل", "counselor": "موجه طلابي", "activity": "رائد نشاط"}.get(domain, domain or "")


# ======================================================
# Filters
# ======================================================
class AttemptOverdueFilter(admin.SimpleListFilter):
    title = "التأخير"
    parameter_name = "overdue"

    def lookups(self, request, model_admin):
        return (
            ("overdue", "متأخرين (تقريبًا)"),
            ("ok", "غير متأخر"),
        )

    def queryset(self, request, queryset):
        """
        ⚠️ فلتر تقريبي:
        - يعتبر "متأخر" = غير منتهٍ
        الدقة 100% تحتاج join للسؤال الحالي + Answer.started_at + quiz.per_question_seconds
        """
        v = self.value()
        if v == "overdue":
            return queryset.filter(is_finished=False)
        if v == "ok":
            return queryset
        return queryset


class AttemptAutoFinishFilter(admin.SimpleListFilter):
    title = "سبب الإنهاء"
    parameter_name = "finish_reason"

    def lookups(self, request, model_admin):
        return (
            ("timeout", "منتهين تلقائيًا"),
            ("forced", "مغلق إداريًا"),
            ("normal", "منتهين طبيعيًا"),
        )

    def queryset(self, request, queryset):
        v = self.value()
        if v in {"timeout", "forced", "normal"}:
            return queryset.filter(is_finished=True, finished_reason=v)
        return queryset


# ======================================================
# Admins
# ======================================================
@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "is_active", "per_question_seconds", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title",)
    ordering = ("-id",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "quiz", "order", "short_text")
    list_filter = ("quiz",)
    search_fields = ("text", "quiz__title")
    ordering = ("quiz_id", "order", "id")

    @admin.display(description="نص السؤال")
    def short_text(self, obj: Question) -> str:
        t = (obj.text or "").strip()
        return (t[:90] + "…") if len(t) > 90 else t


@admin.register(Choice)
class ChoiceAdmin(admin.ModelAdmin):
    list_display = ("id", "question", "is_correct", "text")
    list_filter = ("is_correct", "question__quiz")
    search_fields = ("text", "question__text")
    ordering = ("-id",)


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "national_id", "full_name", "domain_badge", "is_allowed", "has_taken_exam_badge", "created_at")
    list_filter = ("domain", "is_allowed", "has_taken_exam")
    search_fields = ("national_id", "full_name")
    ordering = ("-id",)

    @admin.display(description="المجال")
    def domain_badge(self, obj: Participant) -> str:
        d = (obj.domain or "").strip()
        label = _domain_label(d) or "-"
        if d == "deputy":
            return _pill(label, "#e0f2fe", "#075985")
        if d == "counselor":
            return _pill(label, "#ede9fe", "#5b21b6")
        if d == "activity":
            return _pill(label, "#dcfce7", "#166534")
        return _pill(label, "#f1f5f9", "#0f172a")

    @admin.display(description="أدى الاختبار؟")
    def has_taken_exam_badge(self, obj: Participant) -> str:
        return _pill("نعم", "#dcfce7", "#166534") if obj.has_taken_exam else _pill("لا", "#fee2e2", "#991b1b")


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "participant_link",
        "domain_badge",
        "quiz",
        "finish_badge",
        "reason_badge",
        "score_badge",
        "answers_count_badge",
        "remaining_badge",
        "current_index",
        "started_at",
        "finished_at",
        "tools",
    )
    list_filter = ("is_finished", "quiz", "participant__domain", AttemptAutoFinishFilter, AttemptOverdueFilter)
    search_fields = ("participant__national_id", "participant__full_name", "quiz__title")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"
    actions = ("action_force_finish",)

    def get_queryset(self, request):
        """
        ✅ تحسين أداء + أرقام جاهزة للـ list_display
        """
        qs = super().get_queryset(request).select_related("participant", "quiz")
        qs = qs.annotate(
            answered_done=Count("answers", filter=Q(answers__answered_at__isnull=False), distinct=True),
            total_questions=Count("quiz__question", distinct=True),
        )
        return qs

    # -------------------------
    # Badges / Columns
    # -------------------------
    @admin.display(description="المرشح")
    def participant_link(self, obj: Attempt) -> str:
        url = reverse("admin:quiz_participant_change", args=[obj.participant_id])
        label = f"{obj.participant.full_name} ({obj.participant.national_id})"
        return format_html('<a href="{}" style="font-weight:900;">{}</a>', url, label)

    @admin.display(description="المجال")
    def domain_badge(self, obj: Attempt) -> str:
        d = (obj.participant.domain or "").strip()
        label = _domain_label(d) or "-"
        if d == "deputy":
            return _pill(label, "#e0f2fe", "#075985")
        if d == "counselor":
            return _pill(label, "#ede9fe", "#5b21b6")
        if d == "activity":
            return _pill(label, "#dcfce7", "#166534")
        return _pill(label, "#f1f5f9", "#0f172a")

    @admin.display(description="الحالة", ordering="is_finished")
    def finish_badge(self, obj: Attempt) -> str:
        if obj.is_finished:
            return _pill("منتهٍ", "#dcfce7", "#166534")
        # server-side check (قد يسبب استعلامات إضافية لكنه أدق من الفلتر)
        if obj.is_overdue():
            return _pill("متأخر", "#fee2e2", "#991b1b")
        return _pill("جارٍ", "#fef9c3", "#854d0e")

    @admin.display(description="سبب الإنهاء", ordering="finished_reason")
    def reason_badge(self, obj: Attempt) -> str:
        if not obj.is_finished:
            return _pill("—", "#f1f5f9", "#334155")
        if obj.finished_reason == "timeout":
            return _pill("تلقائي", "#ffe4e6", "#9f1239")
        if obj.finished_reason == "forced":
            return _pill("إداري", "#e0e7ff", "#3730a3")
        return _pill("طبيعي", "#e0f2fe", "#075985")

    @admin.display(description="النتيجة", ordering="score")
    def score_badge(self, obj: Attempt) -> str:
        if not obj.is_finished:
            return _pill("—", "#f1f5f9", "#334155")
        s = int(obj.score or 0)
        if s >= 40:
            return _pill(str(s), "#dcfce7", "#166534")
        if s >= 25:
            return _pill(str(s), "#e0f2fe", "#075985")
        return _pill(str(s), "#fee2e2", "#991b1b")

    @admin.display(description="عدد الإجابات")
    def answers_count_badge(self, obj: Attempt) -> str:
        done = getattr(obj, "answered_done", None)
        total = getattr(obj, "total_questions", None)

        if done is None:
            done = obj.answered_count()
        if total is None:
            total = obj.questions_total()

        text = f"{int(done)}/{int(total)}"
        if obj.is_finished:
            return _pill(text, "#dcfce7", "#166534")
        return _pill(text, "#e0f2fe", "#075985")

    @admin.display(description="الوقت المتبقي")
    def remaining_badge(self, obj: Attempt) -> str:
        rem = obj.remaining_seconds()
        if rem is None:
            return _pill("—", "#f1f5f9", "#334155")
        if rem <= 0:
            return _pill("0s", "#fee2e2", "#991b1b")
        if rem <= 10:
            return _pill(f"{rem}s", "#fef9c3", "#854d0e")
        return _pill(f"{rem}s", "#dcfce7", "#166534")

    @admin.display(description="أدوات")
    def tools(self, obj: Attempt) -> str:
        finish_url = reverse("admin:quiz_attempt_force_finish", args=[obj.id])
        reset_url = reverse("admin:quiz_attempt_reset", args=[obj.id])

        return format_html(
            '<a href="{}" style="padding:3px 10px;border-radius:10px;background:#0ea5e9;'
            'color:#fff;font-weight:900;text-decoration:none;margin-inline-end:6px;display:inline-block;">إغلاق</a>'
            '<a href="{}" style="padding:3px 10px;border-radius:10px;background:#ef4444;'
            'color:#fff;font-weight:900;text-decoration:none;display:inline-block;">إعادة فتح</a>',
            finish_url,
            reset_url,
        )

    # -------------------------
    # Custom admin URLs
    # -------------------------
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:attempt_id>/force-finish/",
                self.admin_site.admin_view(self.force_finish_view),
                name="quiz_attempt_force_finish",
            ),
            path(
                "<int:attempt_id>/reset/",
                self.admin_site.admin_view(self.reset_view),
                name="quiz_attempt_reset",
            ),
        ]
        return custom + urls

    def force_finish_view(self, request: HttpRequest, attempt_id: int):
        a = get_object_or_404(Attempt, id=attempt_id)
        if a.is_finished:
            messages.info(request, "المحاولة منتهية مسبقاً.")
            return redirect("admin:quiz_attempt_changelist")

        a.is_finished = True
        a.finished_at = timezone.now()
        a.finished_reason = "forced"
        a.save(update_fields=["is_finished", "finished_at", "finished_reason"])
        Participant.objects.filter(id=a.participant_id).update(has_taken_exam=True)

        messages.success(request, "✅ تم إغلاق المحاولة إداريًا.")
        return redirect("admin:quiz_attempt_changelist")

    def reset_view(self, request: HttpRequest, attempt_id: int):
        a = get_object_or_404(Attempt, id=attempt_id)
        pid = a.participant_id
        Answer.objects.filter(attempt=a).delete()
        a.delete()
        Participant.objects.filter(id=pid).update(has_taken_exam=False)
        messages.success(request, "✅ تم إعادة فتح الاختبار (حذف المحاولة).")
        return redirect("admin:quiz_attempt_changelist")

    # -------------------------
    # Actions
    # -------------------------
    @admin.action(description="إغلاق إداري للمحاولات المحددة")
    def action_force_finish(self, request, queryset):
        now = timezone.now()
        updated = 0
        for a in queryset.select_related("participant"):
            if not a.is_finished:
                a.is_finished = True
                a.finished_at = now
                a.finished_reason = "forced"
                a.save(update_fields=["is_finished", "finished_at", "finished_reason"])
                Participant.objects.filter(id=a.participant_id).update(has_taken_exam=True)
                updated += 1

        self.message_user(request, f"✅ تم إغلاق {updated} محاولة إداريًا.", level=messages.SUCCESS)


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("id", "attempt", "question", "selected_choice", "started_at", "answered_at")
    list_filter = ("attempt__quiz",)
    search_fields = ("attempt__participant__national_id", "question__text", "selected_choice__text")
    ordering = ("-id",)
