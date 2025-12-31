# quiz/views.py
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .models import Answer, Attempt, Choice, Participant, Question, Quiz

# ======================================================
# Session Keys / Constants
# ======================================================
SESSION_PID = "participant_id"
SESSION_CONFIRMED = "participant_confirmed"
SESSION_ATTEMPT_ID = "attempt_id"

TOTAL_QUESTIONS = 50

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


# ======================================================
# Helpers (Excel + Common)
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


def _domain_label(domain: str) -> str:
    mapping = {
        "deputy": "وكيل",
        "counselor": "موجه طلابي",
        "activity": "رائد نشاط",
    }
    return mapping.get(domain, domain)


def _normalize_domain(v: Any) -> str:
    """
    ✅ يقبل domain بالإنجليزي أو العربي أو مرادفات شائعة ويعيد القياسي:
    deputy/counselor/activity
    """
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


def _finish_attempt(attempt: Attempt, reason: str = "normal") -> None:
    """
    ✅ إنهاء محاولة (مع سبب)
    reason: normal / timeout / forced
    """
    if attempt.is_finished:
        return
    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.finished_reason = reason
    attempt.save(update_fields=["is_finished", "finished_at", "finished_reason"])


def _compute_score(attempt: Attempt) -> int:
    return (
        Answer.objects.filter(attempt=attempt, selected_choice__is_correct=True)
        .select_related("selected_choice")
        .count()
    )


def _get_active_quiz_for_domain(domain: str) -> Quiz | None:
    """
    ✅ اختيار اختبار المجال من الاختبارات النشطة:
    - إذا عنوان الاختبار يحتوي Label المجال (وكيل/موجه/رائد)
    - أو title يساوي domain نفسه
    """
    label = _domain_label(domain)
    qs = Quiz.objects.filter(is_active=True).order_by("-id")

    q1 = qs.filter(title__icontains=label).first()
    if q1:
        return q1

    q2 = qs.filter(title__iexact=domain).first()
    if q2:
        return q2

    return None


def _require_quiz_for_participant(request: HttpRequest, p: Participant) -> Quiz | None:
    quiz = _get_active_quiz_for_domain(p.domain)
    if not quiz:
        messages.error(
            request,
            f"لا يوجد اختبار نشط لمجالك ({_domain_label(p.domain)}). فعّل اختبار هذا المجال من لوحة الإدارة."
        )
        return None

    if not Question.objects.filter(quiz=quiz).exists():
        messages.error(request, f"الاختبار النشط ({quiz.title}) لا يحتوي أسئلة. استورد الأسئلة أولاً.")
        return None

    return quiz


def _quiz_seconds(attempt: Attempt) -> int:
    """
    ✅ وقت السؤال من إعدادات الاختبار
    """
    try:
        sec = int(attempt.quiz.per_question_seconds or 50)
    except Exception:
        sec = 50
    return max(5, min(sec, 3600))  # حماية: على الأقل 5 ثواني وبحد أقصى ساعة


def _auto_advance_if_timeup(attempt: Attempt, questions_count: int) -> int:
    """
    ✅ حماية Server-side:
    إذا انتهى وقت السؤال الحالي ولم يجب، نسجّل إجابة فارغة وننتقل.
    - يعتمد على Quiz.per_question_seconds
    - يزيد timed_out_count
    - قد يلحق أكثر من سؤال إذا ترك الصفحة فترة طويلة

    Returns: عدد مرات التقدم التلقائي التي حصلت بهذه الزيارة.
    """
    advanced = 0
    sec = _quiz_seconds(attempt)

    while (not attempt.is_finished) and (attempt.current_index < questions_count):
        now = timezone.now()

        q = (
            Question.objects.filter(quiz=attempt.quiz)
            .order_by("order", "id")[attempt.current_index: attempt.current_index + 1]
            .first()
        )
        if not q:
            break

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

            advanced += 1
            continue

        break

    return advanced


def _finalize_attempt_if_done(attempt: Attempt, questions_count: int) -> None:
    """
    ✅ إذا خلصت الأسئلة: نحسب النتيجة ونغلق
    - إذا كان فيه timeouts: نختم السبب timeout
    """
    if attempt.is_finished:
        return

    if attempt.current_index >= questions_count:
        attempt.score = _compute_score(attempt)
        attempt.current_index = questions_count
        attempt.is_finished = True
        attempt.finished_at = timezone.now()

        # ✅ لو صار فيه أي تقدم تلقائي أثناء الاختبار
        attempt.finished_reason = "timeout" if (attempt.timed_out_count or 0) > 0 else "normal"

        attempt.save(update_fields=["score", "current_index", "is_finished", "finished_at", "finished_reason"])
        Participant.objects.filter(id=attempt.participant_id).update(has_taken_exam=True)


# ======================================================
# Public
# ======================================================
def home(request: HttpRequest) -> HttpResponse:
    return redirect("quiz:login")


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """
    ✅ بعد النجاح: لا ننشئ Attempt هنا
    -> نخزن participant_id في session
    -> نذهب لصفحة confirm (بيانات + إقرار)
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

        p = Participant.objects.filter(national_id=national_id, is_allowed=True).first()
        if not p:
            messages.error(request, "غير مسموح لك بدخول الاختبار (غير موجود أو غير مخول).")
            return redirect("quiz:login")

        if (p.phone_last4 or "").strip() != last4:
            messages.error(request, "بيانات التحقق غير صحيحة.")
            return redirect("quiz:login")

        # ✅ يمنع دخول أي اختبار ثاني إذا أدى اختبار واحد سابقاً
        if p.has_taken_exam:
            messages.error(request, "تم أداء الاختبار مسبقاً لهذا السجل.")
            return redirect("quiz:login")

        quiz = _require_quiz_for_participant(request, p)
        if not quiz:
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
    ✅ صفحة البيانات + الإقرار.
    POST: عند الموافقة ننشئ Attempt ثم نذهب للسؤال الأول.
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
        messages.error(request, "تم أداء الاختبار مسبقاً لهذا السجل.")
        request.session.flush()
        return redirect("quiz:login")

    quiz = _require_quiz_for_participant(request, p)
    if not quiz:
        return redirect("quiz:login")

    if request.method == "POST":
        agree = request.POST.get("agree") == "1"
        if not agree:
            messages.error(request, "لا يمكنك المتابعة بدون الموافقة على الإقرار.")
            return redirect("quiz:confirm")

        skey = _ensure_session_key(request)

        # ✅ لو فيه محاولة جارية لنفس المرشح/نفس الاختبار
        active_attempt = (
            Attempt.objects.filter(participant=p, quiz=quiz, is_finished=False)
            .order_by("-started_at")
            .first()
        )
        if active_attempt:
            if (active_attempt.session_key or "") == skey:
                request.session[SESSION_ATTEMPT_ID] = active_attempt.id
                request.session[SESSION_CONFIRMED] = True
                return redirect("quiz:question")
            messages.error(request, "يوجد اختبار جارٍ لهذا السجل من جهاز/جلسة أخرى. تواصل مع الإدارة لإعادة فتحه.")
            return redirect("quiz:login")

        ip = request.META.get("REMOTE_ADDR") or ""
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]

        attempt = Attempt.objects.create(
            participant=p,
            quiz=quiz,
            session_key=skey,
            started_ip=ip or None,
            user_agent=ua or None,
        )

        request.session[SESSION_ATTEMPT_ID] = attempt.id
        request.session[SESSION_CONFIRMED] = True
        return redirect("quiz:question")

    return render(request, "quiz/confirm.html", {"p": p, "quiz": quiz})


@require_http_methods(["GET", "POST"])
def question_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_from_session(request)
    if not attempt:
        messages.error(request, "الرجاء تسجيل الدخول أولاً.")
        return redirect("quiz:login")

    if not request.session.get(SESSION_CONFIRMED):
        return redirect("quiz:confirm")

    # ✅ حماية: نفس attempt لازم يكون من نفس session_key
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

    # ✅ ترقية تلقائية لو انتهى وقت السؤال الحالي (تعتمد على per_question_seconds)
    _auto_advance_if_timeup(attempt, len(questions))
    _finalize_attempt_if_done(attempt, len(questions))
    if attempt.is_finished:
        return redirect("quiz:finish")

    q = questions[attempt.current_index]
    choices = list(Choice.objects.filter(question=q).order_by("id"))

    ans, created = Answer.objects.get_or_create(
        attempt=attempt,
        question=q,
        defaults={"started_at": timezone.now()},
    )
    # لو قديم ولا بدأ وقت السؤال (حماية)
    if ans.started_at is None:
        ans.started_at = timezone.now()
        ans.save(update_fields=["started_at"])

    # حساب المتبقي (للواجهة)
    now = timezone.now()
    sec = _quiz_seconds(attempt)
    deadline = (ans.started_at or now) + timedelta(seconds=sec)
    remaining = int(max(0, (deadline - now).total_seconds()))

    if request.method == "POST":
        # ✅ لو انتهى الوقت أثناء الإرسال نعتبرها فارغ وننتقل + زيادة timed_out_count
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
            "question_seconds": sec,          # ✅ من Quiz
            "remaining_seconds": remaining,   # ✅ من Quiz
        },
    )


def finish_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_from_session(request)
    return render(request, "quiz/finish.html", {"attempt": attempt})


# ======================================================
# Staff - Auth
# ======================================================
@staff_member_required
def staff_logout_view(request: HttpRequest) -> HttpResponse:
    from django.contrib.auth import logout as auth_logout

    auth_logout(request)
    return redirect("quiz:staff_manage")


# ======================================================
# Staff - Dashboard
# ======================================================
@staff_member_required
def staff_manage_view(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "all").strip()
    quiz_id = (request.GET.get("quiz") or "").strip()
    sort = (request.GET.get("sort") or "-started_at").strip()

    qs = Attempt.objects.select_related("participant", "quiz")

    if q:
        qs = qs.filter(Q(participant__national_id__icontains=q) | Q(participant__full_name__icontains=q))

    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)

    if quiz_id:
        qs = qs.filter(quiz_id=quiz_id)

    if sort not in {"-started_at", "started_at", "-score", "score"}:
        sort = "-started_at"
    qs = qs.order_by(sort)

    # KPIs
    kpi_total = qs.count()
    kpi_finished = qs.filter(is_finished=True).count()
    kpi_running = qs.filter(is_finished=False).count()
    kpi_avg = qs.filter(is_finished=True).aggregate(a=Avg("score"))["a"] or 0
    kpi_avg = round(float(kpi_avg), 2)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    quizzes = list(Quiz.objects.order_by("-id"))

    return render(
        request,
        "quiz/staff_manage.html",
        {
            "attempts": page_obj,
            "quizzes": quizzes,
            "filters": {"q": q, "status": status, "quiz": quiz_id, "sort": sort},
            "kpi": {"total": kpi_total, "finished": kpi_finished, "running": kpi_running, "avg_score": kpi_avg},
        },
    )


# ======================================================
# Staff - Import Participants (with domain)
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
        ix_allowed = headers.index("is_allowed") if "is_allowed" in headers else None
        ix_taken = headers.index("has_taken_exam") if "has_taken_exam" in headers else None

        if replace:
            Participant.objects.all().update(is_allowed=False)

        created = updated = skipped = 0
        valid_domains = {c[0] for c in Participant._meta.get_field("domain").choices}

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
            if domain not in valid_domains:
                skipped += 1
                continue

            is_allowed = True if replace else _to_bool(r[ix_allowed] if ix_allowed is not None else None, True)
            has_taken_exam = False if reset_taken else _to_bool(r[ix_taken] if ix_taken is not None else None, False)

            _, was_created = Participant.objects.update_or_create(
                national_id=national_id,
                defaults={
                    "full_name": full_name,
                    "phone_last4": phone_last4,
                    "domain": domain,
                    "is_allowed": is_allowed,
                    "has_taken_exam": has_taken_exam,
                },
            )
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

        messages.success(request, f"✅ تم استيراد المتقدمين: (جديد {created}) (تحديث {updated}) (تجاهل {skipped})")
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_participants.html")


# ======================================================
# Staff - Import Questions (3 quizzes × 50) + Preview
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
    title = _domain_label(domain)
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

            for q in qs:
                if q.correct not in {"A", "B", "C", "D"}:
                    messages.error(request, f"الشيت '{s}': correct لازم يكون A/B/C/D فقط (سؤال {q.order}).")
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

            for q in all_data[domain]:
                qq = Question.objects.create(quiz=quiz, order=q.order, text=q.text)
                Choice.objects.create(question=qq, text=q.a, is_correct=(q.correct == "A"))
                Choice.objects.create(question=qq, text=q.b, is_correct=(q.correct == "B"))
                Choice.objects.create(question=qq, text=q.c, is_correct=(q.correct == "C"))
                Choice.objects.create(question=qq, text=q.d, is_correct=(q.correct == "D"))

        messages.success(request, "✅ تم استيراد الأسئلة بنجاح (3 اختبارات × 50 سؤال).")
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_questions.html", ctx)


# ======================================================
# Staff - Export
# ======================================================
@staff_member_required
def staff_export_csv_view(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "all").strip()
    quiz_id = (request.GET.get("quiz") or "").strip()

    qs = Attempt.objects.select_related("participant", "quiz")

    if q:
        qs = qs.filter(Q(participant__national_id__icontains=q) | Q(participant__full_name__icontains=q))
    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)
    if quiz_id:
        qs = qs.filter(quiz_id=quiz_id)

    buff = io.StringIO()
    w = csv.writer(buff)
    w.writerow(["national_id", "full_name", "domain", "quiz", "score", "is_finished", "finished_reason",
                "timed_out_count", "started_at", "finished_at", "ip"])

    for a in qs.order_by("-started_at"):
        w.writerow(
            [
                a.participant.national_id,
                a.participant.full_name,
                a.participant.domain,
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
    # تبسيط: نفس CSV. إذا تبي XLSX حقيقي أبنيه لك فوراً.
    return staff_export_csv_view(request)


# ======================================================
# Staff - Attempt tools
# ======================================================
@staff_member_required
def staff_attempt_detail_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
    answers = list(
        Answer.objects.filter(attempt=attempt)
        .select_related("question", "selected_choice")
        .order_by("id")
    )
    return render(request, "quiz/staff_attempt_detail.html", {"attempt": attempt, "answers": answers})


@staff_member_required
@require_POST
def staff_force_finish_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt, id=attempt_id)
    _finish_attempt(attempt, reason="forced")
    Participant.objects.filter(id=attempt.participant_id).update(has_taken_exam=True)
    messages.success(request, "✅ تم إنهاء المحاولة (إغلاق إداري).")
    return redirect("quiz:staff_manage")


@staff_member_required
@require_POST
def staff_reset_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt, id=attempt_id)
    pid = attempt.participant_id
    Answer.objects.filter(attempt=attempt).delete()
    attempt.delete()
    Participant.objects.filter(id=pid).update(has_taken_exam=False)
    messages.success(request, "✅ تم إعادة فتح الاختبار (حذف المحاولة).")
    return redirect("quiz:staff_manage")
