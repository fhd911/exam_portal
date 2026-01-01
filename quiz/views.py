# quiz/views.py
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Q, Count
from django.db.models.functions import TruncHour
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .models import (
    Answer,
    Attempt,
    Choice,
    Enrollment,
    ExamWindow,
    Participant,
    Question,
    Quiz,
    domain_label,
)

# ======================================================
# Session Keys / Constants
# ======================================================
SESSION_PID = "participant_id"
SESSION_CONFIRMED = "participant_confirmed"
SESSION_ATTEMPT_ID = "attempt_id"

TOTAL_QUESTIONS = 50
VALID_DOMAINS = {"deputy", "counselor", "activity"}

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


# ======================================================
# Helpers: strings / digits
# ======================================================
def _cell_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v).strip()
    return str(v).strip()


def _digits_only(s: str) -> str:
    s = (s or "").translate(_AR_DIGITS)
    return "".join(ch for ch in s if ch.isdigit())


def _extract_last4(v: Any) -> str:
    digits = _digits_only(_cell_to_str(v))
    return digits[-4:] if digits else ""


def _to_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    s = _cell_to_str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "نعم", "صح"):
        return True
    if s in ("0", "false", "no", "n", "لا", "خطأ"):
        return False
    return default


def _normalize_domain(v: Any) -> str:
    """Accept English/Arabic/variants -> deputy/counselor/activity."""
    s = _cell_to_str(v).strip().lower()
    s = (
        s.replace("ـ", "")
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
    )
    if s in {"deputy", "counselor", "activity"}:
        return s
    if s == "guidance":
        return "counselor"
    if "وكيل" in s:
        return "deputy"
    if "موجه" in s or "توجيه" in s:
        return "counselor"
    if "رائد" in s or "نشاط" in s:
        return "activity"
    return s


# ======================================================
# Helpers: session + attempt
# ======================================================
def _ensure_session_key(request: HttpRequest) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key or ""


def _get_participant_from_session(request: HttpRequest) -> Participant | None:
    pid = request.session.get(SESSION_PID)
    if not pid:
        return None
    return Participant.objects.filter(id=pid).first()


def _get_attempt_from_session(request: HttpRequest) -> Attempt | None:
    aid = request.session.get(SESSION_ATTEMPT_ID)
    if not aid:
        return None
    return Attempt.objects.filter(id=aid).select_related("participant", "quiz").first()


def _compute_score(attempt: Attempt) -> int:
    return Answer.objects.filter(attempt=attempt, selected_choice__is_correct=True).count()


def _finish_attempt(attempt: Attempt, reason: str = "normal") -> None:
    if attempt.is_finished:
        return
    attempt.score = _compute_score(attempt)
    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.finished_reason = reason
    attempt.save(update_fields=["score", "is_finished", "finished_at", "finished_reason"])


# ======================================================
# Helpers: Quiz selection per domain
# ======================================================
def _get_active_quiz_for_domain(domain: str) -> Quiz | None:
    label = domain_label(domain)
    qs = Quiz.objects.filter(is_active=True).order_by("-id")

    q1 = qs.filter(title__icontains=label).first()
    if q1:
        return q1

    q2 = qs.filter(title__iexact=domain).first()
    if q2:
        return q2

    return None


def _require_quiz_for_domain(request: HttpRequest, domain: str) -> Quiz | None:
    quiz = _get_active_quiz_for_domain(domain)
    if not quiz:
        messages.error(
            request,
            f"لا يوجد اختبار نشط للمجال ({domain_label(domain)}). فعّل اختبار هذا المجال من لوحة الإدارة.",
        )
        return None
    if not Question.objects.filter(quiz=quiz).exists():
        messages.error(request, f"الاختبار النشط ({quiz.title}) لا يحتوي أسئلة. استورد الأسئلة أولاً.")
        return None
    return quiz


def _peek_quiz_for_domain(domain: str) -> Quiz | None:
    """مثل require لكن بدون messages (لا نزعج المستخدم في GET)."""
    quiz = _get_active_quiz_for_domain(domain)
    if not quiz:
        return None
    if not Question.objects.filter(quiz=quiz).exists():
        return None
    return quiz


# ======================================================
# Helpers: timing
# ======================================================
def _quiz_seconds(attempt: Attempt) -> int:
    try:
        sec = int(attempt.quiz.per_question_seconds or 50)
    except Exception:
        sec = 50
    return max(5, min(sec, 3600))


def _auto_advance_if_timeup(attempt: Attempt, questions: list[Question]) -> None:
    """
    Server-side safety net.
    If current question time is over and not answered -> mark as timed out and move next.
    """
    if attempt.is_finished:
        return

    total = len(questions)
    if total <= 0:
        return

    if attempt.current_index < 0:
        attempt.current_index = 0
        attempt.save(update_fields=["current_index"])
        return
    if attempt.current_index >= total:
        return

    sec = _quiz_seconds(attempt)
    now = timezone.now()
    q = questions[attempt.current_index]

    ans = Answer.objects.filter(attempt=attempt, question=q).first()
    if not ans:
        ans = Answer.objects.create(attempt=attempt, question=q, started_at=now)

    started_at = ans.started_at or now
    deadline = started_at + timedelta(seconds=sec)

    if now > deadline and ans.answered_at is None:
        ans.selected_choice = None
        ans.answered_at = now
        ans.save(update_fields=["selected_choice", "answered_at"])

        attempt.current_index += 1
        attempt.timed_out_count = (attempt.timed_out_count or 0) + 1
        attempt.save(update_fields=["current_index", "timed_out_count"])


def _finalize_attempt_if_done(attempt: Attempt, total_questions: int) -> None:
    if attempt.is_finished:
        return
    if attempt.current_index >= total_questions:
        attempt.score = _compute_score(attempt)
        attempt.current_index = total_questions
        attempt.is_finished = True
        attempt.finished_at = timezone.now()
        attempt.finished_reason = "timeout" if (attempt.timed_out_count or 0) > 0 else "normal"
        attempt.save(update_fields=["score", "current_index", "is_finished", "finished_at", "finished_reason"])


# ======================================================
# Window + Enrollment logic (Core)
# ======================================================
def _now() -> datetime:
    return timezone.localtime(timezone.now())


def _get_active_window_now() -> ExamWindow | None:
    now = _now()
    return (
        ExamWindow.objects.filter(is_active=True, starts_at__lte=now, ends_at__gt=now)
        .order_by("starts_at", "id")
        .first()
    )


def _is_enrolled_for_domain(p: Participant, domain: str) -> bool:
    return Enrollment.objects.filter(participant=p, domain=domain, is_allowed=True).exists()


def _get_next_windows_for_participant(p: Participant, limit: int = 3) -> list[ExamWindow]:
    now = _now()
    domains = list(p.enrollments.filter(is_allowed=True).values_list("domain", flat=True))
    if not domains:
        return []
    return list(
        ExamWindow.objects.filter(is_active=True, starts_at__gt=now, domain__in=domains)
        .order_by("starts_at", "id")[:limit]
    )


def _window_gate_message(p: Participant, active: ExamWindow | None, next_list: list[ExamWindow]) -> str:
    if active is None:
        if not next_list:
            return "لا يوجد اختبار متاح الآن، ولا يوجد لديك أي تسجيلات فعّالة. تواصل مع الإدارة."
        nxt = next_list[0]
        return (
            "لا يوجد اختبار متاح الآن. "
            f"أقرب اختبار لك: {nxt.domain_label} — يبدأ {timezone.localtime(nxt.starts_at).strftime('%H:%M')} "
            f"وينتهي {timezone.localtime(nxt.ends_at).strftime('%H:%M')}."
        )

    if not _is_enrolled_for_domain(p, active.domain):
        if not next_list:
            return f"الاختبار الحالي الآن هو ({active.domain_label}) لكن سجلك غير مسجل في هذا المجال."
        nxt = next_list[0]
        return (
            f"الاختبار المتاح الآن هو ({active.domain_label}) لكن سجلك غير مسجل في هذا المجال. "
            f"أقرب اختبار مسجل لك: {nxt.domain_label} يبدأ {timezone.localtime(nxt.starts_at).strftime('%H:%M')}."
        )

    return ""


def _locked_message(p: Participant) -> str:
    if not p.locked_domain:
        return "تم بدء اختبار سابقاً لهذا السجل. لا يمكنك دخول اختبار آخر."
    when = timezone.localtime(p.locked_at).strftime("%H:%M") if p.locked_at else ""
    extra = f" اليوم الساعة {when}" if when else ""
    return (
        f"لا يمكنك دخول اختبار آخر لأنك بدأت اختبار ({p.locked_domain_label}){extra}. "
        f"النظام يسمح باختبار واحد فقط."
    )


# ======================================================
# Public
# ======================================================
def home(request: HttpRequest) -> HttpResponse:
    return redirect("quiz:login")


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    تسجيل الدخول (تحقق هوية فقط) — لا يتم إنشاء Attempt هنا.
    """
    if request.method == "POST":
        national_id = _digits_only((request.POST.get("national_id") or "").strip())
        last4 = _digits_only((request.POST.get("last4") or "").strip())

        if not national_id:
            messages.error(request, "فضلاً أدخل رقم الهوية/السجل المدني.")
            return redirect("quiz:login")

        if not (last4.isdigit() and len(last4) == 4):
            messages.error(request, "آخر 4 أرقام من الجوال يجب أن تكون 4 أرقام فقط.")
            return redirect("quiz:login")

        p = Participant.objects.filter(national_id=national_id).first()
        if not p or not p.is_allowed:
            messages.error(request, "غير مسموح لك بدخول الاختبار (غير موجود أو غير مخول).")
            return redirect("quiz:login")

        if (p.phone_last4 or "").strip() != last4:
            messages.error(request, "بيانات التحقق غير صحيحة.")
            return redirect("quiz:login")

        if p.has_taken_exam:
            messages.error(request, _locked_message(p))
            return redirect("quiz:login")

        request.session[SESSION_PID] = p.id
        request.session[SESSION_CONFIRMED] = False
        request.session.pop(SESSION_ATTEMPT_ID, None)

        return redirect("quiz:confirm")

    return render(request, "quiz/login.html")


def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    return redirect("quiz:login")


@require_http_methods(["GET", "POST"])
def confirm_view(request: HttpRequest) -> HttpResponse:
    """
    صفحة البيانات + الإقرار.
    POST = محاولة بدء الاختبار ضمن النافذة الحالية فقط.
    """
    p = _get_participant_from_session(request)
    if not p:
        messages.error(request, "انتهت الجلسة. الرجاء تسجيل الدخول من جديد.")
        return redirect("quiz:login")

    if not p.is_allowed:
        messages.error(request, "تم إيقاف صلاحيتك. تواصل مع الإدارة.")
        request.session.flush()
        return redirect("quiz:login")

    if p.has_taken_exam:
        messages.error(request, _locked_message(p))
        request.session.flush()
        return redirect("quiz:login")

    active_window = _get_active_window_now()
    next_windows = _get_next_windows_for_participant(p, limit=3)
    current_domain_label = active_window.domain_label if active_window else ""

    if active_window and (not _is_enrolled_for_domain(p, active_window.domain)):
        messages.error(request, _window_gate_message(p, active_window, next_windows))
        return render(
            request,
            "quiz/confirm.html",
            {
                "p": p,
                "quiz": None,
                "window": active_window,
                "next_windows": next_windows,
                "domain_label": current_domain_label,
            },
        )

    if not active_window:
        messages.info(request, _window_gate_message(p, None, next_windows))

    if request.method == "POST":
        if request.POST.get("agree") != "1":
            messages.error(request, "لا يمكنك المتابعة بدون الموافقة على الإقرار.")
            return redirect("quiz:confirm")

        active_window2 = _get_active_window_now()
        next_windows2 = _get_next_windows_for_participant(p, limit=3)

        if not active_window2:
            messages.error(request, _window_gate_message(p, None, next_windows2))
            return redirect("quiz:confirm")

        if not _is_enrolled_for_domain(p, active_window2.domain):
            messages.error(request, _window_gate_message(p, active_window2, next_windows2))
            return redirect("quiz:confirm")

        quiz = _require_quiz_for_domain(request, active_window2.domain)
        if not quiz:
            return redirect("quiz:confirm")

        skey = _ensure_session_key(request)
        ip = request.META.get("REMOTE_ADDR") or ""
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]

        with transaction.atomic():
            p_locked = Participant.objects.select_for_update().get(id=p.id)

            if (not p_locked.is_allowed) or p_locked.has_taken_exam:
                messages.error(
                    request,
                    _locked_message(p_locked) if p_locked.has_taken_exam else "تم إيقاف صلاحيتك.",
                )
                request.session.flush()
                return redirect("quiz:login")

            active_attempt = (
                Attempt.objects.select_for_update()
                .filter(participant=p_locked, is_finished=False)
                .order_by("-started_at", "-id")
                .first()
            )
            if active_attempt:
                if (active_attempt.session_key or "") == skey:
                    request.session[SESSION_ATTEMPT_ID] = active_attempt.id
                    request.session[SESSION_CONFIRMED] = True
                    return redirect("quiz:question")

                messages.error(request, "يوجد اختبار جارٍ لهذا السجل من جهاز/جلسة أخرى. تواصل مع الإدارة لإعادة فتحه.")
                request.session.flush()
                return redirect("quiz:login")

            attempt = Attempt.objects.create(
                participant=p_locked,
                quiz=quiz,
                domain=active_window2.domain,
                session_key=skey,
                started_ip=ip or None,
                user_agent=ua or None,
                started_at=timezone.now(),
            )

            Participant.objects.filter(id=p_locked.id).update(
                has_taken_exam=True,
                locked_domain=active_window2.domain,
                locked_at=timezone.now(),
            )

        request.session[SESSION_ATTEMPT_ID] = attempt.id
        request.session[SESSION_CONFIRMED] = True
        return redirect("quiz:question")

    quiz_for_display = _peek_quiz_for_domain(active_window.domain) if active_window else None
    return render(
        request,
        "quiz/confirm.html",
        {
            "p": p,
            "quiz": quiz_for_display,
            "window": active_window,
            "next_windows": next_windows,
            "domain_label": current_domain_label,
        },
    )


@require_http_methods(["GET", "POST"])
def question_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_from_session(request)
    if not attempt:
        messages.error(request, "الرجاء تسجيل الدخول أولاً.")
        return redirect("quiz:login")

    if not request.session.get(SESSION_CONFIRMED):
        return redirect("quiz:confirm")

    skey = _ensure_session_key(request)
    if (attempt.session_key or "") and (attempt.session_key != skey):
        messages.error(request, "هذه الجلسة لا تطابق جلسة بدء الاختبار. سجّل الدخول من جديد.")
        request.session.flush()
        return redirect("quiz:login")

    if attempt.is_finished:
        return redirect("quiz:finish")

    questions = list(Question.objects.filter(quiz=attempt.quiz).order_by("order", "id"))
    if not questions:
        messages.error(request, "هذا الاختبار لا يحتوي أسئلة. تواصل مع الإدارة.")
        _finish_attempt(attempt, reason="forced")
        return redirect("quiz:finish")

    _auto_advance_if_timeup(attempt, questions)
    _finalize_attempt_if_done(attempt, len(questions))
    if attempt.is_finished:
        return redirect("quiz:finish")

    if attempt.current_index < 0:
        attempt.current_index = 0
        attempt.save(update_fields=["current_index"])
    if attempt.current_index >= len(questions):
        _finalize_attempt_if_done(attempt, len(questions))
        return redirect("quiz:finish")

    q = questions[attempt.current_index]
    choices = list(Choice.objects.filter(question=q).order_by("id"))

    ans, _ = Answer.objects.get_or_create(
        attempt=attempt,
        question=q,
        defaults={"started_at": timezone.now()},
    )
    if ans.started_at is None:
        ans.started_at = timezone.now()
        ans.save(update_fields=["started_at"])

    now = timezone.now()
    sec = _quiz_seconds(attempt)
    deadline = (ans.started_at or now) + timedelta(seconds=sec)
    remaining = int(max(0, (deadline - now).total_seconds()))

    if request.method == "POST":
        now2 = timezone.now()
        deadline2 = (ans.started_at or now2) + timedelta(seconds=sec)
        remaining2 = int(max(0, (deadline2 - now2).total_seconds()))

        if remaining2 <= 0 and ans.answered_at is None:
            ans.selected_choice = None
            ans.answered_at = now2
            ans.save(update_fields=["selected_choice", "answered_at"])

            attempt.current_index += 1
            attempt.timed_out_count = (attempt.timed_out_count or 0) + 1
            attempt.save(update_fields=["current_index", "timed_out_count"])

            _finalize_attempt_if_done(attempt, len(questions))
            return redirect("quiz:question")

        cid = request.POST.get("choice_id")
        selected = Choice.objects.filter(id=cid, question=q).first() if cid else None

        ans.selected_choice = selected
        ans.answered_at = now2
        ans.save(update_fields=["selected_choice", "answered_at"])

        attempt.current_index += 1
        attempt.save(update_fields=["current_index"])

        _finalize_attempt_if_done(attempt, len(questions))
        return redirect("quiz:question")

    return render(
        request,
        "quiz/question.html",
        {
            "attempt": attempt,
            "q": q,
            "choices": choices,
            "index": attempt.current_index + 1,
            "total": len(questions),
            "question_seconds": sec,
            "remaining_seconds": remaining,
        },
    )


def finish_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_from_session(request)
    if attempt and not attempt.is_finished:
        questions_count = Question.objects.filter(quiz=attempt.quiz).count()
        _finalize_attempt_if_done(attempt, questions_count)
        if not attempt.is_finished:
            _finish_attempt(attempt, reason="forced")
    return render(request, "quiz/finish.html", {"attempt": attempt})


# ======================================================
# Staff - Auth
# ======================================================
@staff_member_required
def staff_logout_view(request: HttpRequest) -> HttpResponse:
    from django.contrib.auth import logout as auth_logout

    auth_logout(request)
    return redirect("quiz:login")


# ======================================================
# Staff - Filters
# ======================================================
def _parse_date_yyyy_mm_dd(value: str) -> datetime | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except Exception:
        return None


def _apply_attempt_filters(request: HttpRequest, qs):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "all").strip()
    quiz_id = (request.GET.get("quiz") or "").strip()
    sort = (request.GET.get("sort") or "-started_at").strip()

    fr = (request.GET.get("fr") or "").strip()
    dom = (request.GET.get("domain") or "").strip()

    min_score_raw = (request.GET.get("min_score") or "").strip()
    from_raw = (request.GET.get("from") or "").strip()
    to_raw = (request.GET.get("to") or "").strip()

    if q:
        qs = qs.filter(Q(participant__national_id__icontains=q) | Q(participant__full_name__icontains=q))

    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)

    if quiz_id:
        qs = qs.filter(quiz_id=quiz_id)

    if fr in {"normal", "timeout", "forced"}:
        qs = qs.filter(is_finished=True, finished_reason=fr)

    if dom in VALID_DOMAINS:
        qs = qs.filter(domain=dom)

    if min_score_raw:
        try:
            ms = int(min_score_raw)
            qs = qs.filter(score__gte=ms)
        except Exception:
            pass

    d_from = _parse_date_yyyy_mm_dd(from_raw)
    d_to = _parse_date_yyyy_mm_dd(to_raw)

    tz = timezone.get_current_timezone()
    if d_from:
        start = timezone.make_aware(datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0), tz)
        qs = qs.filter(started_at__gte=start)

    if d_to:
        end = timezone.make_aware(datetime(d_to.year, d_to.month, d_to.day, 23, 59, 59), tz)
        qs = qs.filter(started_at__lte=end)

    if sort not in {"-started_at", "started_at", "-score", "score"}:
        sort = "-started_at"

    qs = qs.order_by(sort)

    filters = {
        "q": q,
        "status": status,
        "quiz": quiz_id,
        "sort": sort,
        "fr": fr,
        "domain": dom,
        "min_score": min_score_raw,
        "from": from_raw,
        "to": to_raw,
    }
    return qs, filters


# ======================================================
# Staff - Dashboard (Full)
# ======================================================
@staff_member_required
def staff_manage_view(request: HttpRequest) -> HttpResponse:
    base = Attempt.objects.select_related("participant", "quiz")
    qs, filters = _apply_attempt_filters(request, base)

    # KPIs
    kpi_total = qs.count()
    kpi_finished = qs.filter(is_finished=True).count()
    kpi_running = qs.filter(is_finished=False).count()
    kpi_avg = qs.filter(is_finished=True).aggregate(a=Avg("score"))["a"] or 0
    kpi_avg = round(float(kpi_avg), 2)

    now = timezone.now()
    kpi_last_60m = qs.filter(started_at__gte=now - timedelta(minutes=60)).count()
    kpi_last_24h = qs.filter(started_at__gte=now - timedelta(hours=24)).count()

    # Breakdown by domain
    by_domain = list(
        qs.values("domain")
        .annotate(
            total=Count("id"),
            finished=Count("id", filter=Q(is_finished=True)),
            running=Count("id", filter=Q(is_finished=False)),
            avg_score=Avg("score", filter=Q(is_finished=True)),
            timeouts=Count("id", filter=Q(timed_out_count__gt=0)),
        )
        .order_by("-total")
    )
    for d in by_domain:
        d["label"] = domain_label(d["domain"])
        d["avg_score"] = round(float(d["avg_score"] or 0), 2)

    scored_domains = [d for d in by_domain if (d.get("finished") or 0) > 0]
    best_domain = max(scored_domains, key=lambda x: (x.get("avg_score") or 0), default=None)
    worst_domain = min(scored_domains, key=lambda x: (x.get("avg_score") or 0), default=None)

    # Timeout alert
    timeout_threshold = 15  # %
    timeout_count = qs.filter(is_finished=True, timed_out_count__gt=0).count()
    timeout_rate = (timeout_count / kpi_finished * 100) if kpi_finished else 0
    timeout_alert = {
        "enabled": (kpi_finished > 0 and timeout_rate >= timeout_threshold),
        "rate": round(timeout_rate, 1),
        "count": int(timeout_count),
        "threshold": int(timeout_threshold),
    }

    # Breakdown by finish reason
    by_reason = list(
        qs.filter(is_finished=True)
        .values("finished_reason")
        .annotate(total=Count("id"))
        .order_by("-total")
    )

    # Hourly trend (last 12 hours)
    tz = timezone.get_current_timezone()
    start = now - timedelta(hours=12)
    hourly = list(
        qs.filter(started_at__gte=start)
        .annotate(h=TruncHour("started_at", tzinfo=tz))
        .values("h")
        .annotate(
            total=Count("id"),
            finished=Count("id", filter=Q(is_finished=True)),
            running=Count("id", filter=Q(is_finished=False)),
        )
        .order_by("h")
    )

    trend_labels = [timezone.localtime(x["h"]).strftime("%H:%M") for x in hourly]
    trend_total = [int(x["total"] or 0) for x in hourly]
    trend_finished = [int(x["finished"] or 0) for x in hourly]
    trend_running = [int(x["running"] or 0) for x in hourly]

    # Top best/worst
    top_best = list(qs.filter(is_finished=True).order_by("-score", "-finished_at")[:5])
    top_worst = list(qs.filter(is_finished=True).order_by("score", "-finished_at")[:5])

    # Paging
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    quizzes = list(Quiz.objects.order_by("-id"))

    return render(
        request,
        "quiz/staff_manage.html",
        {
            "attempts": page_obj,
            "quizzes": quizzes,
            "filters": filters,
            "kpi": {
                "total": kpi_total,
                "finished": kpi_finished,
                "running": kpi_running,
                "avg_score": kpi_avg,
                "last_60m": kpi_last_60m,
                "last_24h": kpi_last_24h,
            },
            "by_domain": by_domain,
            "best_domain": best_domain,
            "worst_domain": worst_domain,
            "timeout_alert": timeout_alert,
            "by_reason": by_reason,
            "trend": {
                "labels": trend_labels,
                "total": trend_total,
                "finished": trend_finished,
                "running": trend_running,
            },
            "top_best": top_best,
            "top_worst": top_worst,
        },
    )


# ======================================================
# Staff - Import Participants
# ======================================================
@staff_member_required
@transaction.atomic
@require_http_methods(["GET", "POST"])
def staff_import_participants_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        sheet_name = (request.POST.get("sheet_name") or "participants").strip()
        replace = request.POST.get("replace") == "1"
        reset_taken = request.POST.get("reset_taken") == "1"
        file = request.FILES.get("file")

        if not file or not file.name.lower().endswith(".xlsx"):
            messages.error(request, "ارفع ملف Excel (.xlsx).")
            return redirect("quiz:staff_import_participants")

        wb = load_workbook(file)
        if sheet_name not in wb.sheetnames:
            messages.error(request, f"الشيت '{sheet_name}' غير موجود. المتاح: {wb.sheetnames}")
            return redirect("quiz:staff_import_participants")

        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, "الشيت فارغ.")
            return redirect("quiz:staff_import_participants")

        headers_raw = [h for h in rows[0]]
        headers = [str(h).strip().lower() if h is not None else "" for h in headers_raw]

        need = ["national_id", "full_name", "phone_last4", "domain"]
        missing = [c for c in need if c not in headers]
        if missing:
            messages.error(request, f"أعمدة ناقصة: {missing} | الموجود: {headers_raw}")
            return redirect("quiz:staff_import_participants")

        idx = {h: headers.index(h) for h in need}
        ix_person_allowed = headers.index("person_allowed") if "person_allowed" in headers else None
        ix_enroll_allowed = headers.index("is_allowed") if "is_allowed" in headers else None

        if replace:
            Enrollment.objects.all().update(is_allowed=False)

        created_people = updated_people = created_enroll = updated_enroll = skipped = 0
        touched_participant_ids: set[int] = set()

        for r in rows[1:]:
            national_id = _digits_only(_cell_to_str(r[idx["national_id"]]))
            full_name = _cell_to_str(r[idx["full_name"]])
            phone_last4 = _extract_last4(r[idx["phone_last4"]])
            domain = _normalize_domain(r[idx["domain"]])

            if not national_id:
                skipped += 1
                continue
            if phone_last4 and (not phone_last4.isdigit() or len(phone_last4) != 4):
                skipped += 1
                continue
            if domain not in VALID_DOMAINS:
                skipped += 1
                continue

            person_allowed = _to_bool(r[ix_person_allowed] if ix_person_allowed is not None else None, True)
            enroll_allowed = True if replace else _to_bool(
                r[ix_enroll_allowed] if ix_enroll_allowed is not None else None, True
            )

            p, p_created = Participant.objects.update_or_create(
                national_id=national_id,
                defaults={"full_name": full_name, "phone_last4": phone_last4, "is_allowed": person_allowed},
            )
            created_people += 1 if p_created else 0
            updated_people += 0 if p_created else 1

            e, e_created = Enrollment.objects.update_or_create(
                participant=p,
                domain=domain,
                defaults={"is_allowed": enroll_allowed},
            )
            created_enroll += 1 if e_created else 0
            updated_enroll += 0 if e_created else 1

            touched_participant_ids.add(p.id)

        if reset_taken and touched_participant_ids:
            Participant.objects.filter(id__in=list(touched_participant_ids)).update(
                has_taken_exam=False,
                locked_domain="",
                locked_at=None,
            )

        messages.success(
            request,
            "✅ تم الاستيراد بنجاح: "
            f"(أشخاص جديد {created_people}) (تحديث أشخاص {updated_people}) "
            f"(تسجيلات جديدة {created_enroll}) (تحديث تسجيلات {updated_enroll}) "
            f"(تجاهل {skipped})"
        )
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_participants.html")


# ======================================================
# Staff - Import Questions (3 sheets × 50)
# ======================================================
@dataclass
class ImportedQuestion:
    order: int
    text: str
    a: str
    b: str
    c: str
    d: str
    correct: str


def _read_questions_sheet(ws: Worksheet) -> list[ImportedQuestion]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers_raw = [h for h in rows[0]]
    headers = [str(h).strip().lower() if h is not None else "" for h in headers_raw]

    need = ["order", "text", "a", "b", "c", "d", "correct"]
    missing = [c for c in need if c not in headers]
    if missing:
        raise ValueError(f"أعمدة ناقصة: {missing} | الموجود: {headers_raw}")

    idx = {h: headers.index(h) for h in need}
    out: list[ImportedQuestion] = []

    for r in rows[1:]:
        text = _cell_to_str(r[idx["text"]])
        if not text:
            continue

        order_s = _cell_to_str(r[idx["order"]])
        try:
            order = int(float(order_s)) if order_s else 0
        except Exception:
            order = 0

        out.append(
            ImportedQuestion(
                order=order,
                text=text,
                a=_cell_to_str(r[idx["a"]]),
                b=_cell_to_str(r[idx["b"]]),
                c=_cell_to_str(r[idx["c"]]),
                d=_cell_to_str(r[idx["d"]]),
                correct=_cell_to_str(r[idx["correct"]]).strip().upper(),
            )
        )

    out.sort(key=lambda x: (x.order or 10**9))
    for i, q in enumerate(out, start=1):
        if not q.order:
            q.order = i
    return out


def _get_or_create_domain_quiz(domain: str) -> Quiz:
    title = domain_label(domain)
    quiz, _ = Quiz.objects.get_or_create(title=title, defaults={"is_active": True})
    if not quiz.is_active:
        quiz.is_active = True
        quiz.save(update_fields=["is_active"])
    return quiz


@staff_member_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def staff_import_questions_view(request: HttpRequest) -> HttpResponse:
    ctx: dict[str, Any] = {"preview": None, "counts": None, "sheetnames": []}

    if request.method == "POST":
        file = request.FILES.get("file")
        dry_run = request.POST.get("dry_run") == "1"

        if not file or not file.name.lower().endswith(".xlsx"):
            messages.error(request, "ارفع ملف Excel (.xlsx).")
            return redirect("quiz:staff_import_questions")

        wb = load_workbook(file)
        ctx["sheetnames"] = wb.sheetnames

        required = ["deputy", "counselor", "activity"]
        missing = [s for s in required if s not in wb.sheetnames]
        if missing:
            messages.error(request, f"ملف الأسئلة لازم يحتوي شيتات: {required}. الناقص: {missing}")
            return redirect("quiz:staff_import_questions")

        all_data: dict[str, list[ImportedQuestion]] = {}
        for s in required:
            qs = _read_questions_sheet(wb[s])

            if len(qs) != TOTAL_QUESTIONS:
                messages.error(request, f"الشيت '{s}' لازم يكون {TOTAL_QUESTIONS} سؤال بالضبط. الحالي: {len(qs)}")
                return redirect("quiz:staff_import_questions")

            for qx in qs:
                if qx.correct not in {"A", "B", "C", "D"}:
                    messages.error(request, f"الشيت '{s}': correct لازم يكون A/B/C/D فقط (سؤال {qx.order}).")
                    return redirect("quiz:staff_import_questions")

            all_data[s] = qs

        ctx["counts"] = {k: len(v) for k, v in all_data.items()}
        ctx["preview"] = {k: v[:5] for k, v in all_data.items()}

        if dry_run:
            messages.info(request, "✅ Preview فقط — لم يتم الحفظ. اضغط (استيراد فعلي) للحفظ.")
            return render(request, "quiz/staff_import_questions.html", ctx)

        for domain in required:
            quiz = _get_or_create_domain_quiz(domain)
            Question.objects.filter(quiz=quiz).delete()

            for qx in all_data[domain]:
                qq = Question.objects.create(quiz=quiz, order=qx.order, text=qx.text)
                Choice.objects.create(question=qq, text=qx.a, is_correct=(qx.correct == "A"))
                Choice.objects.create(question=qq, text=qx.b, is_correct=(qx.correct == "B"))
                Choice.objects.create(question=qq, text=qx.c, is_correct=(qx.correct == "C"))
                Choice.objects.create(question=qq, text=qx.d, is_correct=(qx.correct == "D"))

        messages.success(request, "✅ تم استيراد الأسئلة بنجاح (3 اختبارات × 50 سؤال).")
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_questions.html", ctx)


# ======================================================
# Staff - Export
# ======================================================
@staff_member_required
def staff_export_csv_view(request: HttpRequest) -> HttpResponse:
    base = Attempt.objects.select_related("participant", "quiz")
    qs, _filters = _apply_attempt_filters(request, base)

    buff = io.StringIO()
    w = csv.writer(buff)
    w.writerow(
        [
            "national_id",
            "full_name",
            "domain",
            "quiz",
            "score",
            "is_finished",
            "finished_reason",
            "timed_out_count",
            "started_at",
            "finished_at",
            "ip",
        ]
    )

    for a in qs.order_by("-started_at"):
        w.writerow(
            [
                a.participant.national_id,
                a.participant.full_name,
                a.domain,
                a.quiz.title,
                a.score,
                "1" if a.is_finished else "0",
                a.finished_reason,
                a.timed_out_count,
                a.started_at.isoformat() if a.started_at else "",
                a.finished_at.isoformat() if a.finished_at else "",
                a.started_ip or "",
            ]
        )

    resp = HttpResponse(buff.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="attempts.csv"'
    return resp


@staff_member_required
def staff_export_xlsx_view(request: HttpRequest) -> HttpResponse:
    """
    XLSX فعلي (openpyxl) مع RTL + Freeze + AutoFilter + تنسيق.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    base = Attempt.objects.select_related("participant", "quiz")
    qs, _filters = _apply_attempt_filters(request, base)

    wb = Workbook()
    ws = wb.active
    ws.title = "Attempts"
    ws.sheet_view.rightToLeft = True

    headers = [
        "رقم الهوية/السجل",
        "الاسم",
        "المجال",
        "الاختبار",
        "الدرجة",
        "منتهٍ؟",
        "سبب الإنهاء",
        "عدد انتهاء الوقت",
        "وقت البدء",
        "وقت الانتهاء",
        "IP",
    ]
    ws.append(headers)

    header_font = Font(bold=True, size=12, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0EA5E9")
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    head_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_align = Alignment(horizontal="right", vertical="center", wrap_text=True)

    for col in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = head_align
        c.border = border

    def _reason_label(r: str) -> str:
        return {"normal": "طبيعي", "timeout": "تلقائي", "forced": "إداري"}.get(r or "", r or "")

    for a in qs.order_by("-started_at"):
        ws.append(
            [
                a.participant.national_id,
                a.participant.full_name,
                domain_label(a.domain),
                a.quiz.title,
                int(a.score or 0),
                "نعم" if a.is_finished else "لا",
                _reason_label(a.finished_reason),
                int(a.timed_out_count or 0),
                timezone.localtime(a.started_at).strftime("%Y-%m-%d %H:%M:%S") if a.started_at else "",
                timezone.localtime(a.finished_at).strftime("%Y-%m-%d %H:%M:%S") if a.finished_at else "",
                a.started_ip or "",
            ]
        )

    max_row = ws.max_row
    max_col = ws.max_column
    for r in range(2, max_row + 1):
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            cell.alignment = cell_align
            cell.border = border

    widths = {1: 18, 2: 30, 3: 14, 4: 18, 5: 10, 6: 10, 7: 14, 8: 14, 9: 20, 10: 20, 11: 16}
    for i in range(1, max_col + 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(i, 16)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(max_col)}{max_row}"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    resp = HttpResponse(
        out.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="attempts.xlsx"'
    return resp


# ======================================================
# Staff - Confirm pages (GET)
# ======================================================
@staff_member_required
@require_http_methods(["GET"])
def staff_attempt_finish_confirm_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
    return render(
        request,
        "quiz/staff_attempt_confirm.html",
        {
            "attempt": attempt,
            "title": "تأكيد إنهاء المحاولة",
            "message": "هل أنت متأكد أنك تريد إنهاء هذه المحاولة؟ سيتم اعتبارها منتهية (إغلاق إداري).",
            "action_url_name": "quiz:staff_attempt_force_finish",
            "action_btn": "نعم، إنهاء المحاولة",
        },
    )


@staff_member_required
@require_http_methods(["GET"])
def staff_attempt_reset_confirm_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
    return render(
        request,
        "quiz/staff_attempt_confirm.html",
        {
            "attempt": attempt,
            "title": "تأكيد إعادة فتح الاختبار",
            "message": "هل أنت متأكد؟ سيتم حذف المحاولة وإزالة القفل عن المشارك ليتمكن من دخول اختبار جديد.",
            "action_url_name": "quiz:staff_attempt_reset",
            "action_btn": "نعم، إعادة فتح الاختبار",
        },
    )


# ======================================================
# Staff - Attempt tools
# ======================================================
@staff_member_required
def staff_attempt_detail_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
    answers = list(Answer.objects.filter(attempt=attempt).select_related("question", "selected_choice").order_by("id"))
    ctx = {"attempt": attempt, "answers": answers}

    if request.GET.get("partial") == "1":
        return render(request, "quiz/partials/attempt_detail_panel.html", ctx)

    return render(request, "quiz/staff_attempt_detail.html", ctx)


@staff_member_required
@require_POST
def staff_force_finish_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt, id=attempt_id)
    _finish_attempt(attempt, reason="forced")
    messages.success(request, "✅ تم إنهاء المحاولة (إغلاق إداري).")
    return redirect("quiz:staff_manage")


@staff_member_required
@require_POST
def staff_reset_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    """
    إعادة فتح: حذف المحاولة + إزالة القفل عن المشارك.
    """
    attempt = get_object_or_404(Attempt, id=attempt_id)
    pid = attempt.participant_id

    Answer.objects.filter(attempt=attempt).delete()
    attempt.delete()

    Participant.objects.filter(id=pid).update(
        has_taken_exam=False,
        locked_domain="",
        locked_at=None,
    )
    messages.success(request, "✅ تم إعادة فتح الاختبار (حذف المحاولة + إزالة القفل).")
    return redirect("quiz:staff_manage")
