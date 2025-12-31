# quiz/views.py
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from openpyxl import load_workbook, Workbook

from .models import Answer, Attempt, Choice, Participant, Question, Quiz


# ======================================================
# Constants / Session Keys
# ======================================================
SESSION_ATTEMPT_ID = "quiz_attempt_id"


# ======================================================
# Domain helpers (Participant.domain values)
# ======================================================
DOMAIN_LABEL = {
    "deputy": "وكيل",
    "counselor": "موجه طلابي",
    "activity": "رائد نشاط",
}
DOMAIN_SHEETS_DEFAULT = {
    "deputy": "deputy",
    "counselor": "guidance",  # ✅ في ملفك اسم الشيت guidance
    "activity": "activity",
}


def _norm(s: Any) -> str:
    return (str(s).strip() if s is not None else "")


def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _to_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    s = _norm(v).lower()
    if s in ("1", "true", "yes", "y", "نعم", "صح"):
        return True
    if s in ("0", "false", "no", "n", "لا", "خطأ"):
        return False
    return default


def _domain_from_any(v: Any) -> str:
    """
    يقبل:
    - deputy/counselor/activity
    - عربي: وكيل / موجه طلابي / رائد نشاط
    - اختصارات: deputy, guidance, counselor, activity
    """
    s = _norm(v).lower()
    if not s:
        return ""

    # english
    if s in ("deputy",):
        return "deputy"
    if s in ("counselor", "guidance"):
        return "counselor"
    if s in ("activity",):
        return "activity"

    # arabic
    if "وكيل" in s:
        return "deputy"
    if "موجه" in s or "توجيه" in s or "طلاب" in s:
        return "counselor"
    if "رائد" in s or "نشاط" in s:
        return "activity"

    return ""


def _active_quiz_for_domain(domain: str) -> Quiz | None:
    """
    اختيار الاختبار النشط حسب المجال.
    الاستراتيجية:
    - إذا يوجد اختبار نشط واحد فقط: نستخدمه
    - إذا يوجد أكثر من نشط: نختار الذي عنوانه يحتوي على اسم المجال (عربي أو قيمة domain)
    """
    qs = Quiz.objects.filter(is_active=True).order_by("id")
    if not qs.exists():
        return None
    if qs.count() == 1:
        return qs.first()

    label = DOMAIN_LABEL.get(domain, "")
    # match by arabic label first
    if label:
        z = qs.filter(title__icontains=label).first()
        if z:
            return z

    # match by domain keyword
    z = qs.filter(title__icontains=domain).first()
    if z:
        return z

    # fallback
    return qs.first()


def _get_attempt_from_session(request: HttpRequest) -> Attempt | None:
    aid = request.session.get(SESSION_ATTEMPT_ID)
    if not aid:
        return None
    return Attempt.objects.filter(id=aid).select_related("participant", "quiz").first()


# ======================================================
# Public views
# ======================================================
def home(request: HttpRequest) -> HttpResponse:
    return redirect("quiz:login")


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        national_id = _digits(_norm(request.POST.get("national_id")))
        last4 = _digits(_norm(request.POST.get("last4")))

        if not national_id:
            messages.error(request, "فضلاً أدخل رقم الهوية.")
            return redirect("quiz:login")

        if not (last4.isdigit() and len(last4) == 4):
            messages.error(request, "آخر 4 أرقام من الجوال يجب أن تكون 4 أرقام فقط.")
            return redirect("quiz:login")

        p = Participant.objects.filter(national_id=national_id, is_allowed=True).first()
        if not p:
            messages.error(request, "غير مخول بدخول الاختبار.")
            return redirect("quiz:login")

        if _digits(p.phone_last4 or "") != last4:
            messages.error(request, "بيانات التحقق غير صحيحة.")
            return redirect("quiz:login")

        if p.has_taken_exam:
            messages.error(request, "تم تنفيذ الاختبار مسبقاً لهذا السجل.")
            return redirect("quiz:login")

        if not p.domain:
            messages.error(request, "لم يتم تحديد مجال هذا المترشح. راجع الاستيراد (domain).")
            return redirect("quiz:login")

        quiz = _active_quiz_for_domain(p.domain)
        if not quiz:
            messages.error(request, "لا يوجد اختبار نشط حالياً.")
            return redirect("quiz:login")

        # ensure session key
        if not request.session.session_key:
            request.session.create()
        skey = request.session.session_key or ""

        # prevent multi-session running attempts
        active_attempt = (
            Attempt.objects.filter(participant=p, quiz=quiz, is_finished=False)
            .order_by("-started_at")
            .first()
        )
        if active_attempt:
            if (active_attempt.session_key or "") == skey:
                request.session[SESSION_ATTEMPT_ID] = active_attempt.id
                return redirect("quiz:question")
            messages.error(request, "يوجد اختبار جارٍ لهذا السجل من جهاز/جلسة أخرى. تواصل مع الإدارة لإعادة فتحه.")
            return redirect("quiz:login")

        ip = request.META.get("REMOTE_ADDR")
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]

        attempt = Attempt.objects.create(
            participant=p,
            quiz=quiz,
            session_key=skey,
            started_ip=ip if ip else None,
            user_agent=ua,
        )
        request.session[SESSION_ATTEMPT_ID] = attempt.id
        return redirect("quiz:question")

    return render(request, "quiz/login.html")


def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.pop(SESSION_ATTEMPT_ID, None)
    messages.success(request, "تم تسجيل الخروج.")
    return redirect("quiz:login")


@require_http_methods(["GET", "POST"])
def question_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_from_session(request)
    if not attempt:
        return redirect("quiz:login")

    if attempt.is_finished:
        return redirect("quiz:finish")

    # load questions
    questions = list(
        Question.objects.filter(quiz=attempt.quiz).order_by("order").prefetch_related("choice_set")
    )
    if not questions:
        messages.error(request, "لا توجد أسئلة لهذا الاختبار. راجع استيراد الأسئلة.")
        return redirect("quiz:login")

    total = len(questions)
    idx = max(0, min(attempt.current_index, total - 1))
    q = questions[idx]
    choices = list(q.choice_set.all())

    # create/get Answer row for timing
    ans, _created = Answer.objects.get_or_create(
        attempt=attempt,
        question=q,
        defaults={"started_at": timezone.now()},
    )

    per_q = int(attempt.quiz.time_per_question_seconds or 60)
    deadline = ans.started_at + timezone.timedelta(seconds=per_q)
    now = timezone.now()
    remaining = max(0, int((deadline - now).total_seconds()))

    if request.method == "POST":
        selected_id = _digits(_norm(request.POST.get("choice_id")))
        selected = None
        if selected_id:
            selected = Choice.objects.filter(id=int(selected_id), question=q).first()

        answered_at = timezone.now()
        is_late = answered_at > deadline

        ans.selected_choice = selected
        ans.answered_at = answered_at
        ans.is_late = is_late
        ans.save(update_fields=["selected_choice", "answered_at", "is_late"])

        # advance
        attempt.current_index = idx + 1

        # if last question -> finish
        if attempt.current_index >= total:
            return _finish_attempt(request, attempt)

        attempt.save(update_fields=["current_index"])
        return redirect("quiz:question")

    return render(
        request,
        "quiz/question.html",
        {
            "attempt": attempt,
            "question": q,
            "choices": choices,
            "index": idx + 1,
            "total": total,
            "remaining_seconds": remaining,
            "time_per_question_seconds": per_q,
        },
    )


def _finish_attempt(request: HttpRequest, attempt: Attempt) -> HttpResponse:
    # compute score
    # معيار: الإجابة صحيحة إذا selected_choice.is_correct True
    correct = (
        Answer.objects.filter(attempt=attempt, selected_choice__is_correct=True)
        .values("id")
        .count()
    )
    total = Question.objects.filter(quiz=attempt.quiz).count()

    attempt.score = int(correct)
    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["score", "is_finished", "finished_at"])

    Participant.objects.filter(id=attempt.participant_id).update(has_taken_exam=True)

    request.session.pop(SESSION_ATTEMPT_ID, None)
    return redirect("quiz:finish")


def finish_view(request: HttpRequest) -> HttpResponse:
    # نعرض آخر محاولة للمستخدم (من نفس session_key إن أمكن) — أو نعطي صفحة عامة
    attempt = _get_attempt_from_session(request)
    if attempt:
        # لو لسه موجودة بالجلسة، حاول تكمّل المنطق
        if not attempt.is_finished:
            return redirect("quiz:question")

    return render(request, "quiz/finish.html")


# ======================================================
# Staff views
# ======================================================
@staff_member_required
def staff_manage_view(request: HttpRequest) -> HttpResponse:
    q = _norm(request.GET.get("q"))
    status = _norm(request.GET.get("status") or "all")
    quiz_id = _norm(request.GET.get("quiz"))
    sort = _norm(request.GET.get("sort") or "-started_at")

    qs = Attempt.objects.select_related("participant", "quiz").all()

    if q:
        qs = qs.filter(
            Q(participant__national_id__icontains=q)
            | Q(participant__full_name__icontains=q)
        )

    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)

    if quiz_id:
        try:
            qs = qs.filter(quiz_id=int(quiz_id))
        except ValueError:
            pass

    allowed_sorts = {"-started_at", "started_at", "-score", "score"}
    if sort not in allowed_sorts:
        sort = "-started_at"
    qs = qs.order_by(sort)

    # KPIs (on filtered set)
    agg = qs.aggregate(
        total=Count("id"),
        finished=Count("id", filter=Q(is_finished=True)),
        running=Count("id", filter=Q(is_finished=False)),
        avg_score=Avg("score"),
    )
    kpi = {
        "total": agg["total"] or 0,
        "finished": agg["finished"] or 0,
        "running": agg["running"] or 0,
        "avg_score": round(float(agg["avg_score"] or 0), 2),
    }

    paginator = Paginator(qs, 25)
    page = request.GET.get("page") or "1"
    attempts = paginator.get_page(page)

    quizzes = Quiz.objects.order_by("id").all()

    return render(
        request,
        "quiz/staff_manage.html",
        {
            "attempts": attempts,
            "quizzes": quizzes,
            "kpi": kpi,
            "filters": {"q": q, "status": status, "quiz": quiz_id, "sort": sort},
        },
    )


@staff_member_required
@transaction.atomic
def staff_import_participants_view(request: HttpRequest) -> HttpResponse:
    """
    Excel columns (required):
      national_id, full_name, phone_last4, domain
    Optional:
      is_allowed, has_taken_exam
    """
    if request.method == "POST":
        sheet_name = _norm(request.POST.get("sheet_name") or "participants")
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
        headers = [_norm(h).lower() for h in headers_raw]

        need = ["national_id", "full_name", "phone_last4", "domain"]
        missing = [c for c in need if c not in headers]
        if missing:
            messages.error(request, f"أعمدة ناقصة: {missing} | الموجود: {headers_raw}")
            return redirect("quiz:staff_import_participants")

        idx = {h: headers.index(h) for h in need}
        ix_allowed = headers.index("is_allowed") if "is_allowed" in headers else None
        ix_taken = headers.index("has_taken_exam") if "has_taken_exam" in headers else None

        # validate duplicates-with-different-domain inside the file
        seen: dict[str, str] = {}
        bad_dupes: list[str] = []

        parsed = []
        skipped = 0

        for r in rows[1:]:
            national_id = _digits(_norm(r[idx["national_id"]]))
            full_name = _norm(r[idx["full_name"]])
            phone_last4 = _digits(_norm(r[idx["phone_last4"]]))[-4:]
            domain = _domain_from_any(r[idx["domain"]])

            if not national_id:
                skipped += 1
                continue

            if phone_last4 and (not phone_last4.isdigit() or len(phone_last4) != 4):
                skipped += 1
                continue

            if domain not in ("deputy", "counselor", "activity"):
                skipped += 1
                continue

            prev = seen.get(national_id)
            if prev and prev != domain:
                bad_dupes.append(national_id)
                continue
            seen[national_id] = domain

            is_allowed = True if replace else _to_bool(r[ix_allowed] if ix_allowed is not None else None, True)
            has_taken_exam = False if reset_taken else _to_bool(r[ix_taken] if ix_taken is not None else None, False)

            parsed.append(
                {
                    "national_id": national_id,
                    "full_name": full_name,
                    "phone_last4": phone_last4,
                    "domain": domain,
                    "is_allowed": is_allowed,
                    "has_taken_exam": has_taken_exam,
                }
            )

        if bad_dupes:
            messages.error(
                request,
                "يوجد تكرار لهوية واحدة بأكثر من مجال داخل ملف الاستيراد. "
                f"مثال: {bad_dupes[:10]} (إجمالي: {len(bad_dupes)})"
            )
            return redirect("quiz:staff_import_participants")

        if replace:
            Participant.objects.all().update(is_allowed=False)

        created = updated = 0
        for item in parsed:
            obj, was_created = Participant.objects.update_or_create(
                national_id=item["national_id"],
                defaults=item,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        extra = []
        if replace:
            extra.append("تم جعل الجميع غير مسموحين ثم تفعيل المستوردين")
        if reset_taken:
            extra.append("تم تصفير حالة (أدى الاختبار) للمستورَدين")

        msg = f"✅ تم استيراد المتقدمين: (جدد {created}) (تحديث {updated}) (تجاهل {skipped})"
        if extra:
            msg += " | " + " — ".join(extra)
        messages.success(request, msg)
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_participants.html")


@dataclass
class ParsedQuestion:
    order: int
    text: str
    A: str
    B: str
    C: str
    D: str
    correct: str  # "A"/"B"/"C"/"D"


def _parse_questions_sheet(wb: Any, sheet_name: str) -> tuple[list[ParsedQuestion], list[str]]:
    """
    Sheet headers expected:
      order, question, A, B, C, D, correct
    """
    errors: list[str] = []
    if sheet_name not in wb.sheetnames:
        return [], [f"الشيت غير موجود: {sheet_name}"]

    ws = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], [f"الشيت فارغ: {sheet_name}"]

    headers_raw = [h for h in rows[0]]
    headers = [_norm(h).lower() for h in headers_raw]
    need = ["order", "question", "a", "b", "c", "d", "correct"]
    missing = [c for c in need if c not in headers]
    if missing:
        return [], [f"أعمدة ناقصة في '{sheet_name}': {missing} | الموجود: {headers_raw}"]

    ix = {h: headers.index(h) for h in need}
    parsed: list[ParsedQuestion] = []

    for r in rows[1:]:
        order_raw = r[ix["order"]]
        qtext = _norm(r[ix["question"]])
        A = _norm(r[ix["a"]])
        B = _norm(r[ix["b"]])
        C = _norm(r[ix["c"]])
        D = _norm(r[ix["d"]])
        correct = _norm(r[ix["correct"]]).upper()

        if not qtext:
            continue

        try:
            order = int(float(order_raw)) if order_raw is not None else 0
        except Exception:
            order = 0

        if correct not in ("A", "B", "C", "D"):
            errors.append(f"قيمة correct غير صحيحة في '{sheet_name}' عند order={order or '?'} (لازم A/B/C/D)")
            continue

        # minimal validation for choices
        if not (A and B and C and D):
            errors.append(f"خيارات ناقصة في '{sheet_name}' عند order={order or '?'}")
            continue

        parsed.append(ParsedQuestion(order=order, text=qtext, A=A, B=B, C=C, D=D, correct=correct))

    # enforce 50 exactly
    if len(parsed) != 50:
        errors.append(f"عدد الأسئلة في '{sheet_name}' = {len(parsed)} (لازم 50 بالضبط)")

    # enforce unique order 1..50 if present
    orders = [p.order for p in parsed if p.order]
    if orders:
        if len(set(orders)) != len(orders):
            errors.append(f"يوجد تكرار في order داخل '{sheet_name}'")
        if sorted(orders) != list(range(1, 51)):
            errors.append(f"ترقيم order داخل '{sheet_name}' يفضّل يكون 1..50 (حاليًا: أول/آخر = {min(orders)}..{max(orders)})")

    return parsed, errors


@staff_member_required
@transaction.atomic
def staff_import_questions_view(request: HttpRequest) -> HttpResponse:
    """
    يدعم ملفك: questions_template_3domains_50.xlsx
    sheets:
      deputy, guidance, activity
    """
    if request.method == "POST":
        file = request.FILES.get("file")
        replace = request.POST.get("replace") == "1"
        do_preview = request.POST.get("preview") == "1"

        # sheet names (allow override)
        deputy_sheet = _norm(request.POST.get("deputy_sheet") or DOMAIN_SHEETS_DEFAULT["deputy"])
        counselor_sheet = _norm(request.POST.get("counselor_sheet") or DOMAIN_SHEETS_DEFAULT["counselor"])
        activity_sheet = _norm(request.POST.get("activity_sheet") or DOMAIN_SHEETS_DEFAULT["activity"])

        if not file or not file.name.lower().endswith(".xlsx"):
            messages.error(request, "ارفع ملف Excel (.xlsx) للأسئلة.")
            return redirect("quiz:staff_import")  # اسم المسار عندك

        wb = load_workbook(file)

        parsed_all: dict[str, list[ParsedQuestion]] = {}
        errors_all: list[str] = []

        mapping = {
            "deputy": deputy_sheet,
            "counselor": counselor_sheet,
            "activity": activity_sheet,
        }

        for domain, sheet in mapping.items():
            parsed, errs = _parse_questions_sheet(wb, sheet)
            parsed_all[domain] = parsed
            errors_all.extend(errs)

        if errors_all:
            for e in errors_all[:8]:
                messages.error(request, e)
            if len(errors_all) > 8:
                messages.error(request, f"... وإجمالي أخطاء: {len(errors_all)}")
            return render(
                request,
                "quiz/staff_import_questions.html",
                {
                    "preview": True,
                    "counts": {d: len(parsed_all[d]) for d in parsed_all},
                    "mapping": mapping,
                    "sample": {
                        d: parsed_all[d][:5] for d in parsed_all
                    },
                },
            )

        # preview only
        if do_preview:
            messages.success(request, "✅ Preview جاهز — الأعداد صحيحة (50 لكل مجال).")
            return render(
                request,
                "quiz/staff_import_questions.html",
                {
                    "preview": True,
                    "counts": {d: len(parsed_all[d]) for d in parsed_all},
                    "mapping": mapping,
                    "sample": {d: parsed_all[d][:5] for d in parsed_all},
                },
            )

        # import (write to DB)
        created_quiz = 0
        total_questions = 0
        total_choices = 0

        for domain, items in parsed_all.items():
            label = DOMAIN_LABEL[domain]

            # get or create quiz (title contains label)
            quiz = Quiz.objects.filter(title__icontains=label).order_by("id").first()
            if not quiz:
                quiz = Quiz.objects.create(title=f"اختبار {label}", is_active=False, time_per_question_seconds=60)
                created_quiz += 1

            if replace:
                # remove old questions for this quiz
                Question.objects.filter(quiz=quiz).delete()

            # ensure order by "order" if valid else preserve list
            items_sorted = sorted(items, key=lambda x: x.order or 10**9)

            for pq in items_sorted:
                q = Question.objects.create(quiz=quiz, text=pq.text, order=int(pq.order or 0))
                total_questions += 1

                # create four choices
                choices_map = {
                    "A": pq.A,
                    "B": pq.B,
                    "C": pq.C,
                    "D": pq.D,
                }
                for key, txt in choices_map.items():
                    Choice.objects.create(
                        question=q,
                        text=txt,
                        is_correct=(key == pq.correct),
                    )
                    total_choices += 1

        messages.success(
            request,
            f"✅ تم استيراد الأسئلة بنجاح | اختبارات جديدة: {created_quiz} | أسئلة: {total_questions} | خيارات: {total_choices}"
            + (" | (تم الاستبدال بالحذف) " if replace else "")
        )
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_questions.html")


@staff_member_required
def staff_attempt_detail_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant", "quiz"), id=attempt_id)
    answers = (
        Answer.objects.filter(attempt=attempt)
        .select_related("question", "selected_choice")
        .order_by("question__order", "id")
    )
    return render(request, "quiz/staff_attempt_detail.html", {"attempt": attempt, "answers": answers})


@staff_member_required
@require_POST
@transaction.atomic
def staff_force_finish_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt, id=attempt_id)
    if attempt.is_finished:
        messages.info(request, "المحاولة منتهية بالفعل.")
        return redirect("quiz:staff_manage")

    # finish now with current score state (recompute)
    correct = Answer.objects.filter(attempt=attempt, selected_choice__is_correct=True).count()
    attempt.score = int(correct)
    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["score", "is_finished", "finished_at"])
    Participant.objects.filter(id=attempt.participant_id).update(has_taken_exam=True)

    messages.success(request, "✅ تم إنهاء المحاولة.")
    return redirect("quiz:staff_manage")


@staff_member_required
@require_POST
@transaction.atomic
def staff_reset_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(Attempt.objects.select_related("participant"), id=attempt_id)

    # delete answers + attempt, reset taken flag
    pid = attempt.participant_id
    Answer.objects.filter(attempt=attempt).delete()
    attempt.delete()
    Participant.objects.filter(id=pid).update(has_taken_exam=False)

    messages.success(request, "✅ تم إعادة فتح الاختبار (حذف المحاولة وإتاحة الدخول من جديد).")
    return redirect("quiz:staff_manage")


# ======================================================
# Exports
# ======================================================
@staff_member_required
def staff_export_csv_view(request: HttpRequest) -> HttpResponse:
    # reuse same filters like staff_manage
    q = _norm(request.GET.get("q"))
    status = _norm(request.GET.get("status") or "all")
    quiz_id = _norm(request.GET.get("quiz"))
    sort = _norm(request.GET.get("sort") or "-started_at")

    qs = Attempt.objects.select_related("participant", "quiz").all()
    if q:
        qs = qs.filter(Q(participant__national_id__icontains=q) | Q(participant__full_name__icontains=q))
    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)
    if quiz_id:
        try:
            qs = qs.filter(quiz_id=int(quiz_id))
        except ValueError:
            pass
    if sort in {"-started_at", "started_at", "-score", "score"}:
        qs = qs.order_by(sort)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["national_id", "full_name", "domain", "quiz", "score", "is_finished", "started_at", "finished_at"])
    for a in qs:
        w.writerow([
            a.participant.national_id,
            a.participant.full_name,
            a.participant.domain,
            a.quiz.title,
            a.score,
            int(a.is_finished),
            a.started_at.isoformat() if a.started_at else "",
            a.finished_at.isoformat() if a.finished_at else "",
        ])

    resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="attempts.csv"'
    return resp


@staff_member_required
def staff_export_xlsx_view(request: HttpRequest) -> HttpResponse:
    q = _norm(request.GET.get("q"))
    status = _norm(request.GET.get("status") or "all")
    quiz_id = _norm(request.GET.get("quiz"))
    sort = _norm(request.GET.get("sort") or "-started_at")

    qs = Attempt.objects.select_related("participant", "quiz").all()
    if q:
        qs = qs.filter(Q(participant__national_id__icontains=q) | Q(participant__full_name__icontains=q))
    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)
    if quiz_id:
        try:
            qs = qs.filter(quiz_id=int(quiz_id))
        except ValueError:
            pass
    if sort in {"-started_at", "started_at", "-score", "score"}:
        qs = qs.order_by(sort)

    wb = Workbook()
    ws = wb.active
    ws.title = "attempts"
    ws.append(["national_id", "full_name", "domain", "quiz", "score", "is_finished", "started_at", "finished_at"])

    for a in qs:
        ws.append([
            a.participant.national_id,
            a.participant.full_name,
            a.participant.domain,
            a.quiz.title,
            a.score,
            int(a.is_finished),
            a.started_at.strftime("%Y-%m-%d %H:%M:%S") if a.started_at else "",
            a.finished_at.strftime("%Y-%m-%d %H:%M:%S") if a.finished_at else "",
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="attempts.xlsx"'
    return resp
