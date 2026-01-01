# quiz/admin.py
from __future__ import annotations

from django.contrib import admin, messages
from django.core.exceptions import FieldError
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import Answer, Attempt, Choice, Enrollment, ExamWindow, Participant, Question, Quiz


# ======================================================
# Admin styling (font + sizes)
# ======================================================
class AdminStylingMixin:
    class Media:
        css = {"all": ("admin/custom_admin.css",)}


# ======================================================
# UI helpers
# ======================================================
def _pill(text: str, bg: str, fg: str) -> str:
    return format_html(
        '<span style="display:inline-flex;align-items:center;gap:6px;'
        'padding:3px 10px;border-radius:999px;'
        'background:{};color:{};font-weight:900;font-size:12px;'
        'border:1px solid rgba(2,6,23,.06);white-space:nowrap;">{}</span>',
        bg,
        fg,
        text,
    )


def _domain_label(domain: str) -> str:
    return {"deputy": "وكيل", "counselor": "موجه طلابي", "activity": "رائد نشاط"}.get(domain, domain or "")


def _domain_pill(domain: str) -> str:
    d = (domain or "").strip()
    label = _domain_label(d) or "—"
    if d == "deputy":
        return _pill(label, "#e0f2fe", "#075985")
    if d == "counselor":
        return _pill(label, "#ede9fe", "#5b21b6")
    if d == "activity":
        return _pill(label, "#dcfce7", "#166534")
    return _pill(label, "#f1f5f9", "#0f172a")


# ======================================================
# Filters
# ======================================================
class AttemptScopeFilter(admin.SimpleListFilter):
    title = "التصفية"
    parameter_name = "scope"

    def lookups(self, request, model_admin):
        return (("running", "محاولات جارية"), ("finished", "محاولات منتهية"))

    def queryset(self, request, queryset):
        v = self.value()
        if v == "running":
            return queryset.filter(is_finished=False)
        if v == "finished":
            return queryset.filter(is_finished=True)
        return queryset


class AttemptFinishReasonFilter(admin.SimpleListFilter):
    title = "سبب الإنهاء"
    parameter_name = "finish_reason"

    def lookups(self, request, model_admin):
        return (("timeout", "تلقائي/وقت"), ("forced", "إغلاق إداري"), ("normal", "طبيعي"))

    def queryset(self, request, queryset):
        v = self.value()
        if v in {"timeout", "forced", "normal"}:
            return queryset.filter(is_finished=True, finished_reason=v)
        return queryset


class ExamWindowNowFilter(admin.SimpleListFilter):
    title = "حالة النافذة"
    parameter_name = "when"

    def lookups(self, request, model_admin):
        return (
            ("now", "نشطة الآن"),
            ("future", "قادمة"),
            ("past", "منتهية"),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        v = self.value()
        if v == "now":
            return queryset.filter(is_active=True, starts_at__lte=now, ends_at__gte=now)
        if v == "future":
            return queryset.filter(is_active=True, starts_at__gt=now)
        if v == "past":
            return queryset.filter(ends_at__lt=now)
        return queryset


# ======================================================
# Admins
# ======================================================
@admin.register(Quiz)
class QuizAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "title", "is_active", "per_question_seconds", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title",)
    ordering = ("-id",)
    list_editable = ("is_active", "per_question_seconds")


@admin.register(Question)
class QuestionAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "quiz", "order", "short_text")
    list_filter = ("quiz",)
    search_fields = ("text", "quiz__title")
    ordering = ("quiz_id", "order", "id")
    list_select_related = ("quiz",)

    @admin.display(description="نص السؤال")
    def short_text(self, obj: Question) -> str:
        t = (obj.text or "").strip()
        return (t[:90] + "…") if len(t) > 90 else t


@admin.register(Choice)
class ChoiceAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "question", "is_correct", "text")
    list_filter = ("is_correct", "question__quiz")
    search_fields = ("text", "question__text", "question__quiz__title")
    ordering = ("-id",)
    list_select_related = ("question", "question__quiz")


@admin.register(Enrollment)
class EnrollmentAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "participant_link", "domain_badge", "is_allowed", "created_at")
    list_filter = ("domain", "is_allowed")
    search_fields = ("participant__national_id", "participant__full_name")
    ordering = ("-id",)
    list_select_related = ("participant",)
    list_editable = ("is_allowed",)

    @admin.display(description="المشارك")
    def participant_link(self, obj: Enrollment) -> str:
        url = reverse("admin:quiz_participant_change", args=[obj.participant_id])
        label = f"{obj.participant.full_name} ({obj.participant.national_id})"
        return format_html('<a href="{}" style="font-weight:900;text-decoration:none;">{}</a>', url, label)

    @admin.display(description="المجال")
    def domain_badge(self, obj: Enrollment) -> str:
        return _domain_pill(obj.domain)


@admin.register(ExamWindow)
class ExamWindowAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "name_or_domain", "domain_badge", "starts_at", "ends_at", "is_active")
    list_filter = ("domain", "is_active", ExamWindowNowFilter)
    search_fields = ("name",)
    ordering = ("-starts_at", "-id")
    date_hierarchy = "starts_at"
    list_editable = ("is_active",)

    @admin.display(description="النافذة")
    def name_or_domain(self, obj: ExamWindow) -> str:
        t = (obj.name or "").strip()
        return t or _domain_label(obj.domain)

    @admin.display(description="المجال")
    def domain_badge(self, obj: ExamWindow) -> str:
        return _domain_pill(obj.domain)


@admin.register(Participant)
class ParticipantAdmin(AdminStylingMixin, admin.ModelAdmin):
    """
    ✅ Participant يمثل الشخص
    ✅ المجالات المسجل بها عبر Enrollment
    """
    list_display = (
        "id",
        "national_id",
        "full_name",
        "enrolled_domains_badge",
        "locked_domain_badge",
        "is_allowed",
        "taken_badge",
        "created_at",
    )
    list_filter = ("is_allowed", "has_taken_exam", "locked_domain", "enrollments__domain")
    search_fields = ("national_id", "full_name")
    ordering = ("-id",)
    list_editable = ("is_allowed",)

    def get_queryset(self, request: HttpRequest):
        qs = super().get_queryset(request)
        return qs.prefetch_related("enrollments")

    @admin.display(description="المجالات المسجل بها")
    def enrolled_domains_badge(self, obj: Participant) -> str:
        domains = list(obj.enrollments.values_list("domain", flat=True))
        if not domains:
            return _pill("—", "#f1f5f9", "#334155")

        pills = [_domain_pill(d) for d in sorted(set(domains))]
        inner = format_html_join(" ", "{}", ((p,) for p in pills))
        return format_html('<div style="display:flex;gap:6px;flex-wrap:wrap;">{}</div>', inner)

    @admin.display(description="المجال المقفول")
    def locked_domain_badge(self, obj: Participant) -> str:
        d = (obj.locked_domain or "").strip()
        return _domain_pill(d) if d else _pill("—", "#f1f5f9", "#334155")

    @admin.display(description="أدى؟")
    def taken_badge(self, obj: Participant) -> str:
        return _pill("نعم", "#dcfce7", "#166534") if obj.has_taken_exam else _pill("لا", "#fee2e2", "#991b1b")


@admin.register(Attempt)
class AttemptAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "participant_link",
        "domain_badge",
        "quiz",
        "status_badge",
        "reason_badge",
        "score_badge",
        "progress_badge",
        "started_at",
        "finished_at",
        "tools",
    )

    list_filter = ("quiz", "domain", "is_finished", AttemptFinishReasonFilter, AttemptScopeFilter)
    search_fields = ("participant__national_id", "participant__full_name", "quiz__title")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"
    list_select_related = ("participant", "quiz")
    actions = ("action_force_finish",)

    readonly_fields = (
        "participant",
        "quiz",
        "domain",
        "session_key",
        "started_ip",
        "user_agent",
        "started_at",
        "finished_at",
        "current_index",
        "score",
        "is_finished",
        "finished_reason",
        "timed_out_count",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def get_queryset(self, request: HttpRequest):
        qs = super().get_queryset(request).select_related("participant", "quiz")

        qs = qs.annotate(
            answered_done=Count(
                "answers",
                filter=Q(answers__answered_at__isnull=False),
                distinct=True,
            )
        )

        # total_questions: يعتمد على related_name / related_query_name لعلاقة Question->Quiz
        try:
            return qs.annotate(total_questions=Count("quiz__questions", distinct=True))
        except FieldError:
            return qs.annotate(total_questions=Count("quiz__question", distinct=True))

    @admin.display(description="المرشح")
    def participant_link(self, obj: Attempt) -> str:
        url = reverse("admin:quiz_participant_change", args=[obj.participant_id])
        label = f"{obj.participant.full_name} ({obj.participant.national_id})"
        return format_html('<a href="{}" style="font-weight:900;text-decoration:none;">{}</a>', url, label)

    @admin.display(description="المجال")
    def domain_badge(self, obj: Attempt) -> str:
        return _domain_pill(obj.domain)

    @admin.display(description="الحالة", ordering="is_finished")
    def status_badge(self, obj: Attempt) -> str:
        return _pill("منتهٍ", "#dcfce7", "#166534") if obj.is_finished else _pill("جارٍ", "#dbeafe", "#1d4ed8")

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

    @admin.display(description="التقدم")
    def progress_badge(self, obj: Attempt) -> str:
        done = int(getattr(obj, "answered_done", 0) or 0)
        total = int(getattr(obj, "total_questions", 0) or 0)
        if total <= 0:
            return _pill(f"{done}/—", "#f1f5f9", "#334155")
        text = f"{done}/{total}"
        return _pill(text, "#dcfce7", "#166534") if obj.is_finished else _pill(text, "#e0f2fe", "#075985")

    @admin.display(description="أدوات")
    def tools(self, obj: Attempt) -> str:
        confirm_finish_url = reverse("admin:quiz_attempt_confirm_finish", args=[obj.id])
        confirm_reset_url = reverse("admin:quiz_attempt_confirm_reset", args=[obj.id])

        finish_btn = (
            format_html(
                '<a href="{}" style="display:inline-flex;align-items:center;justify-content:center;'
                'padding:6px 10px;border-radius:12px;background:#0ea5e9;color:#fff;'
                'font-weight:900;text-decoration:none;box-shadow:0 6px 14px rgba(2,6,23,.10);">إغلاق</a>',
                confirm_finish_url,
            )
            if not obj.is_finished
            else _pill("منتهٍ", "#f1f5f9", "#334155")
        )

        reset_btn = format_html(
            '<a href="{}" style="display:inline-flex;align-items:center;justify-content:center;'
            'padding:6px 10px;border-radius:12px;background:#ef4444;color:#fff;'
            'font-weight:900;text-decoration:none;box-shadow:0 6px 14px rgba(2,6,23,.10);">إعادة فتح</a>',
            confirm_reset_url,
        )

        return format_html('<div style="display:flex;gap:6px;flex-wrap:wrap;">{} {}</div>', finish_btn, reset_btn)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:attempt_id>/confirm-finish/",
                self.admin_site.admin_view(self.confirm_finish_view),
                name="quiz_attempt_confirm_finish",
            ),
            path(
                "<int:attempt_id>/confirm-reset/",
                self.admin_site.admin_view(self.confirm_reset_view),
                name="quiz_attempt_confirm_reset",
            ),
            path(
                "<int:attempt_id>/do-finish/",
                self.admin_site.admin_view(self.do_finish_view),
                name="quiz_attempt_do_finish",
            ),
            path(
                "<int:attempt_id>/do-reset/",
                self.admin_site.admin_view(self.do_reset_view),
                name="quiz_attempt_do_reset",
            ),
        ]
        return custom + urls

    def _confirm_ctx(
        self,
        request: HttpRequest,
        attempt: Attempt,
        *,
        title: str,
        warning: str,
        action_label: str,
        action_color: str,
        post_url: str,
    ):
        return dict(
            self.admin_site.each_context(request),
            title=title,
            attempt=attempt,
            warning=warning,
            action_label=action_label,
            action_color=action_color,
            post_url=post_url,
            back_url=reverse("admin:quiz_attempt_changelist"),
        )

    def confirm_finish_view(self, request: HttpRequest, attempt_id: int) -> HttpResponse:
        a = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
        if a.is_finished:
            messages.info(request, "المحاولة منتهية مسبقاً.")
            return redirect("admin:quiz_attempt_changelist")

        ctx = self._confirm_ctx(
            request,
            a,
            title="تأكيد إغلاق المحاولة",
            warning="سيتم إنهاء المحاولة فوراً (إغلاق إداري).",
            action_label="تأكيد الإغلاق",
            action_color="#0ea5e9",
            post_url=reverse("admin:quiz_attempt_do_finish", args=[a.id]),
        )
        return render(request, "admin/quiz/attempt_confirm_action.html", ctx)

    def confirm_reset_view(self, request: HttpRequest, attempt_id: int) -> HttpResponse:
        a = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
        ctx = self._confirm_ctx(
            request,
            a,
            title="تأكيد إعادة فتح الاختبار",
            warning="سيتم حذف جميع إجابات المحاولة وحذف المحاولة نفسها، ثم إزالة قفل المشارك.",
            action_label="تأكيد إعادة الفتح",
            action_color="#ef4444",
            post_url=reverse("admin:quiz_attempt_do_reset", args=[a.id]),
        )
        return render(request, "admin/quiz/attempt_confirm_action.html", ctx)

    def do_finish_view(self, request: HttpRequest, attempt_id: int) -> HttpResponse:
        if request.method != "POST":
            messages.error(request, "طريقة غير مسموحة.")
            return redirect("admin:quiz_attempt_changelist")

        a = get_object_or_404(Attempt, id=attempt_id)
        if a.is_finished:
            messages.info(request, "المحاولة منتهية مسبقاً.")
            return redirect("admin:quiz_attempt_changelist")

        a.is_finished = True
        a.finished_at = timezone.now()
        a.finished_reason = "forced"
        a.save(update_fields=["is_finished", "finished_at", "finished_reason"])

        messages.success(request, "✅ تم إغلاق المحاولة إداريًا.")
        return redirect("admin:quiz_attempt_changelist")

    def do_reset_view(self, request: HttpRequest, attempt_id: int) -> HttpResponse:
        if request.method != "POST":
            messages.error(request, "طريقة غير مسموحة.")
            return redirect("admin:quiz_attempt_changelist")

        a = get_object_or_404(Attempt, id=attempt_id)
        pid = a.participant_id

        Answer.objects.filter(attempt=a).delete()
        a.delete()

        Participant.objects.filter(id=pid).update(
            has_taken_exam=False,
            locked_domain="",
            locked_at=None,
        )

        messages.success(request, "✅ تم إعادة فتح الاختبار (حذف المحاولة + إزالة القفل).")
        return redirect("admin:quiz_attempt_changelist")

    @admin.action(description="إغلاق إداري للمحاولات المحددة")
    def action_force_finish(self, request: HttpRequest, queryset):
        now = timezone.now()
        open_qs = queryset.filter(is_finished=False)
        updated = open_qs.count()

        open_qs.update(is_finished=True, finished_at=now, finished_reason="forced")

        self.message_user(request, f"✅ تم إغلاق {updated} محاولة إداريًا.", level=messages.SUCCESS)


@admin.register(Answer)
class AnswerAdmin(AdminStylingMixin, admin.ModelAdmin):
    list_display = ("id", "attempt", "question", "selected_choice", "started_at", "answered_at")
    list_filter = ("attempt__quiz", "attempt__is_finished")
    search_fields = (
        "attempt__participant__national_id",
        "attempt__participant__full_name",
        "question__text",
        "selected_choice__text",
    )
    ordering = ("-id",)
    list_select_related = ("attempt", "attempt__participant", "question", "selected_choice")
