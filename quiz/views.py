from __future__ import annotations

import csv
from io import BytesIO

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Participant, Quiz, Attempt, Question, Choice, Answer


SESSION_ATTEMPT_ID = "quiz_attempt_id"


# ======================================================
# الصفحة الرئيسية
# ======================================================
def home(request: HttpRequest) -> HttpResponse:
    attempt_id = request.session.get(SESSION_ATTEMPT_ID)
    if attempt_id:
        return redirect("quiz:question")
    return redirect("quiz:login")


# ======================================================
# Helpers
# ======================================================
def _get_active_quiz() -> Quiz | None:
    return Quiz.objects.filter(is_active=True).first()


def _get_attempt_or_redirect(request: HttpRequest) -> Attempt | None:
    attempt_id = request.session.get(SESSION_ATTEMPT_ID)
    if not attempt_id:
        return None

    attempt = (
        Attempt.objects
        .filter(id=attempt_id)
        .select_related("quiz", "participant")
        .first()
    )

    if not attempt or attempt.is_finished:
        request.session.pop(SESSION_ATTEMPT_ID, None)
        return None

    # ✅ منع تبديل الجلسة: لازم نفس session_key التي بدأت بها المحاولة
    current_skey = request.session.session_key or ""
    if getattr(attempt, "session_key", "") and attempt.session_key != current_skey:
        request.session.pop(SESSION_ATTEMPT_ID, None)
        messages.error(request, "تم اكتشاف دخول من جلسة أخرى. تم إنهاء الجلسة الحالية.")
        return None

    return attempt


def _build_attempts_queryset_for_staff(request: HttpRequest):
    """
    يبني QuerySet للمحاولات بحسب فلاتر GET (للوحة + التصدير)
    GET params:
      q: بحث (سجل/اسم)
      status: all | finished | running
      quiz: quiz_id
      sort: -started_at | started_at | -score | score
    """
    qs = (
        Attempt.objects
        .select_related("participant", "quiz")
        .order_by("-started_at")
    )

    qtxt = (request.GET.get("q") or "").strip()
    if qtxt:
        qs = qs.filter(
            Q(participant__national_id__icontains=qtxt) |
            Q(participant__full_name__icontains=qtxt)
        )

    status = (request.GET.get("status") or "all").strip()
    if status == "finished":
        qs = qs.filter(is_finished=True)
    elif status == "running":
        qs = qs.filter(is_finished=False)

    quiz_id = (request.GET.get("quiz") or "").strip()
    if quiz_id.isdigit():
        qs = qs.filter(quiz_id=int(quiz_id))

    sort = (request.GET.get("sort") or "-started_at").strip()
    allow_sort = {"-started_at", "started_at", "-score", "score"}
    if sort in allow_sort:
        qs = qs.order_by(sort, "-id")

    return qs


# ======================================================
# دخول الموظف (السجل + آخر 4 من الجوال) + منع تعدد الجلسات
# ======================================================
def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        national_id = (request.POST.get("national_id") or "").strip()
        last4 = (request.POST.get("last4") or "").strip()

        if not national_id:
            messages.error(request, "فضلاً أدخل السجل المدني.")
            return redirect("quiz:login")

        if not (last4.isdigit() and len(last4) == 4):
            messages.error(request, "آخر 4 أرقام من الجوال يجب أن تكون 4 أرقام فقط.")
            return redirect("quiz:login")

        p = Participant.objects.filter(national_id=national_id, is_allowed=True).first()
        if not p:
            messages.error(request, "غير مخول بدخول الاختبار.")
            return redirect("quiz:login")

        if (getattr(p, "phone_last4", "") or "").strip() != last4:
            messages.error(request, "بيانات التحقق غير صحيحة.")
            return redirect("quiz:login")

        if p.has_taken_exam:
            messages.error(request, "تم تنفيذ الاختبار مسبقاً لهذا السجل.")
            return redirect("quiz:login")

        quiz = _get_active_quiz()
        if not quiz:
            messages.error(request, "لا يوجد اختبار نشط حالياً.")
            return redirect("quiz:login")

        # ✅ تأكد من وجود session_key
        if not request.session.session_key:
            request.session.create()
        skey = request.session.session_key or ""

        # ✅ منع تعدد الجلسات: إذا فيه محاولة غير منتهية لنفس الموظف
        active_attempt = (
            Attempt.objects
            .filter(participant=p, quiz=quiz, is_finished=False)
            .order_by("-started_at")
            .first()
        )

        if active_attempt:
            # نفس الجلسة: كمّل
            if getattr(active_attempt, "session_key", "") == skey:
                request.session[SESSION_ATTEMPT_ID] = active_attempt.id
                return redirect("quiz:question")

            # جلسة أخرى: امنع
            messages.error(request, "يوجد اختبار جاري لهذا السجل من جهاز/جلسة أخرى. تواصل مع الإدارة لإعادة فتحه.")
            return redirect("quiz:login")

        # إنشاء محاولة جديدة
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


# ======================================================
# صفحة السؤال (30 ثانية + تحكم من السيرفر)
# ======================================================
@transaction.atomic
def question_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_or_redirect(request)
    if not attempt:
        return redirect("quiz:login")

    quiz = attempt.quiz
    questions = list(Question.objects.filter(quiz=quiz).order_by("order", "id"))
    total = len(questions)

    if total == 0:
        messages.error(request, "لا توجد أسئلة لهذا الاختبار بعد.")
        return redirect("quiz:finish")

    if attempt.current_index >= total:
        return redirect("quiz:finish")

    q = questions[attempt.current_index]

    ans, _created = Answer.objects.get_or_create(
        attempt=attempt,
        question=q,
        defaults={"started_at": timezone.now()},
    )

    if request.method == "GET":
        return render(
            request,
            "quiz/question.html",
            {
                "quiz": quiz,
                "question": q,
                "choices": q.choices.all(),
                "index": attempt.current_index + 1,
                "total": total,
                "seconds": quiz.time_per_question_seconds,
            },
        )

    # POST
    if ans.answered_at is not None:
        attempt.current_index += 1
        attempt.save(update_fields=["current_index"])
        return redirect("quiz:question")

    now = timezone.now()
    delta = (now - ans.started_at).total_seconds()
    is_late = delta > quiz.time_per_question_seconds

    choice_id = request.POST.get("choice")
    selected = None

    if (not is_late) and choice_id:
        selected = Choice.objects.filter(id=choice_id, question=q).first()

    ans.selected_choice = selected
    ans.answered_at = now
    ans.is_late = is_late
    ans.save()

    if selected and selected.is_correct and (not is_late):
        attempt.score += 1

    attempt.current_index += 1
    attempt.save(update_fields=["score", "current_index"])

    return redirect("quiz:question")


# ======================================================
# إنهاء الاختبار
# ======================================================
def finish_view(request: HttpRequest) -> HttpResponse:
    attempt = _get_attempt_or_redirect(request)
    if not attempt:
        return redirect("quiz:login")

    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["is_finished", "finished_at"])

    p = attempt.participant
    p.has_taken_exam = True
    p.save(update_fields=["has_taken_exam"])

    request.session.pop(SESSION_ATTEMPT_ID, None)
    return render(request, "quiz/finish.html")


# ======================================================
# ✅ لوحة إدارة VIP (Staff only)
# ======================================================
@staff_member_required
def staff_manage_view(request: HttpRequest) -> HttpResponse:
    quizzes = Quiz.objects.all().order_by("-id")
    qs = _build_attempts_queryset_for_staff(request)

    agg = qs.aggregate(
        total=Count("id"),
        finished=Count("id", filter=Q(is_finished=True)),
        running=Count("id", filter=Q(is_finished=False)),
        avg_score=Avg("score"),
    )
    total = int(agg["total"] or 0)
    finished = int(agg["finished"] or 0)
    running = int(agg["running"] or 0)
    avg_score = float(agg["avg_score"] or 0.0)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    ctx = {
        "quizzes": quizzes,
        "attempts": page_obj,
        "kpi": {
            "total": total,
            "finished": finished,
            "running": running,
            "avg_score": round(avg_score, 2),
        },
        "filters": {
            "q": (request.GET.get("q") or "").strip(),
            "status": (request.GET.get("status") or "all").strip(),
            "quiz": (request.GET.get("quiz") or "").strip(),
            "sort": (request.GET.get("sort") or "-started_at").strip(),
        },
    }
    return render(request, "quiz/staff_manage.html", ctx)


# ======================================================
# ✅ تفاصيل محاولة (Staff)
# ======================================================
@staff_member_required
def staff_attempt_detail_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = get_object_or_404(
        Attempt.objects.select_related("participant", "quiz"),
        id=attempt_id
    )

    answers_qs = (
        Answer.objects
        .filter(attempt=attempt)
        .select_related("question", "selected_choice")
        .prefetch_related("question__choices")
        .order_by("question__order", "question_id")
    )

    rows = []
    for ans in answers_qs:
        q = ans.question
        correct = None
        for c in q.choices.all():
            if c.is_correct:
                correct = c
                break

        is_correct = bool(
            ans.selected_choice and correct and ans.selected_choice_id == correct.id and (not ans.is_late)
        )

        spent_seconds = None
        if ans.answered_at and ans.started_at:
            spent_seconds = int((ans.answered_at - ans.started_at).total_seconds())

        rows.append({
            "order": q.order,
            "question": q.text,
            "selected": ans.selected_choice.text if ans.selected_choice else "—",
            "correct": correct.text if correct else "—",
            "is_late": ans.is_late,
            "is_correct": is_correct,
            "spent": spent_seconds,
            "answered_at": ans.answered_at,
        })

    return render(request, "quiz/staff_attempt_detail.html", {
        "attempt": attempt,
        "rows": rows,
    })


# ======================================================
# ✅ إجراء: إعادة فتح محاولة (Reset) (Staff POST)
# ======================================================
@staff_member_required
@require_POST
@transaction.atomic
def staff_reset_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = Attempt.objects.select_related("participant").filter(id=attempt_id).first()
    if not attempt:
        messages.error(request, "المحاولة غير موجودة.")
        return redirect("quiz:staff_manage")

    participant = attempt.participant

    Answer.objects.filter(attempt=attempt).delete()
    attempt.delete()

    participant.has_taken_exam = False
    participant.save(update_fields=["has_taken_exam"])

    messages.success(request, f"✅ تم إعادة فتح الاختبار للموظف: {participant.full_name or participant.national_id}")
    return redirect("quiz:staff_manage")


# ======================================================
# ✅ إجراء: إنهاء محاولة جارية (Force Finish) (Staff POST)
# ======================================================
@staff_member_required
@require_POST
@transaction.atomic
def staff_force_finish_attempt_view(request: HttpRequest, attempt_id: int) -> HttpResponse:
    attempt = Attempt.objects.select_related("participant").filter(id=attempt_id).first()
    if not attempt:
        messages.error(request, "المحاولة غير موجودة.")
        return redirect("quiz:staff_manage")

    if attempt.is_finished:
        messages.info(request, "هذه المحاولة منتهية بالفعل.")
        return redirect("quiz:staff_manage")

    attempt.is_finished = True
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["is_finished", "finished_at"])

    participant = attempt.participant
    participant.has_taken_exam = True
    participant.save(update_fields=["has_taken_exam"])

    messages.success(request, "✅ تم إنهاء المحاولة الجارية بنجاح.")
    return redirect("quiz:staff_manage")


# ======================================================
# ✅ التصدير: CSV (حسب الفلاتر)
# ======================================================
@staff_member_required
def staff_export_results_csv(request: HttpRequest) -> HttpResponse:
    qs = _build_attempts_queryset_for_staff(request)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="results.csv"'

    writer = csv.writer(response)
    writer.writerow(["national_id", "full_name", "quiz", "score", "status", "started_at", "finished_at", "ip", "user_agent"])

    for a in qs:
        writer.writerow([
            a.participant.national_id,
            a.participant.full_name,
            a.quiz.title,
            a.score,
            "finished" if a.is_finished else "running",
            a.started_at,
            a.finished_at,
            getattr(a, "started_ip", None),
            getattr(a, "user_agent", ""),
        ])

    return response


# ======================================================
# ✅ التصدير: Excel (.xlsx) (حسب الفلاتر)
# ======================================================
@staff_member_required
def staff_export_results_xlsx(request: HttpRequest) -> HttpResponse:
    qs = _build_attempts_queryset_for_staff(request)

    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError:
        messages.error(request, "مكتبة openpyxl غير مثبتة. نفّذ: pip install openpyxl")
        return redirect("quiz:staff_manage")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"

    headers = ["السجل", "الاسم", "الاختبار", "الدرجة", "الحالة", "وقت البدء", "وقت الانتهاء", "IP", "User-Agent"]
    ws.append(headers)

    for a in qs:
        ws.append([
            a.participant.national_id,
            a.participant.full_name,
            a.quiz.title,
            a.score,
            "منتهي" if a.is_finished else "جارٍ",
            a.started_at.strftime("%Y-%m-%d %H:%M") if a.started_at else "",
            a.finished_at.strftime("%Y-%m-%d %H:%M") if a.finished_at else "",
            str(getattr(a, "started_ip", "") or ""),
            str(getattr(a, "user_agent", "") or ""),
        ])

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    resp = HttpResponse(
        bio.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="results.xlsx"'
    return resp


# ======================================================
# ✅ التصدير: PDF (حسب الفلاتر)
# ======================================================
@staff_member_required
def staff_export_results_pdf(request: HttpRequest) -> HttpResponse:
    qs = _build_attempts_queryset_for_staff(request)[:500]

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        messages.error(request, "مكتبة reportlab غير مثبتة. نفّذ: pip install reportlab")
        return redirect("quiz:staff_manage")

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Exam Results (Admin)")
    y -= 20

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Exported at: {timezone.now().strftime('%Y-%m-%d %H:%M')}")
    y -= 25

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "National ID")
    c.drawString(150, y, "Name")
    c.drawString(330, y, "Score")
    c.drawString(380, y, "Status")
    y -= 14
    c.line(40, y, width - 40, y)
    y -= 14

    c.setFont("Helvetica", 9)
    for a in qs:
        if y < 60:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 9)

        c.drawString(40, y, str(a.participant.national_id))
        c.drawString(150, y, (a.participant.full_name or "")[:26])
        c.drawString(330, y, str(a.score))
        c.drawString(380, y, "Finished" if a.is_finished else "Running")
        y -= 14

    c.save()
    buffer.seek(0)

    resp = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    resp["Content-Disposition"] = 'attachment; filename="results.pdf"'
    return resp


# ======================================================
# ✅ استيراد الأسئلة من Excel (Staff)
# ======================================================
@staff_member_required
@transaction.atomic
def staff_import_questions_view(request: HttpRequest) -> HttpResponse:
    quizzes = Quiz.objects.all().order_by("-id")

    if request.method == "POST":
        quiz_id = request.POST.get("quiz_id")
        sheet_name = (request.POST.get("sheet_name") or "questions").strip()
        replace = request.POST.get("replace") == "1"
        file = request.FILES.get("file")

        if not quiz_id:
            messages.error(request, "اختر اختبار (Quiz) أولاً.")
            return redirect("quiz:staff_import_questions")

        quiz = Quiz.objects.filter(id=quiz_id).first()
        if not quiz:
            messages.error(request, "الاختبار غير موجود.")
            return redirect("quiz:staff_import_questions")

        if not file:
            messages.error(request, "ارفع ملف Excel (.xlsx).")
            return redirect("quiz:staff_import_questions")

        if not file.name.lower().endswith(".xlsx"):
            messages.error(request, "الملف يجب أن يكون بصيغة .xlsx")
            return redirect("quiz:staff_import_questions")

        try:
            import openpyxl
        except ImportError:
            messages.error(request, "مكتبة openpyxl غير مثبتة. نفّذ: pip install openpyxl")
            return redirect("quiz:staff_import_questions")

        wb = openpyxl.load_workbook(file)
        if sheet_name not in wb.sheetnames:
            messages.error(request, f"الشيت '{sheet_name}' غير موجود. المتاح: {wb.sheetnames}")
            return redirect("quiz:staff_import_questions")

        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, "الشيت فارغ.")
            return redirect("quiz:staff_import_questions")

        headers_raw = [h for h in rows[0]]
        headers = [str(h).strip().lower() if h is not None else "" for h in headers_raw]

        need = ["order", "question", "a", "b", "c", "d", "correct"]
        missing = [c for c in need if c not in headers]
        if missing:
            messages.error(request, f"أعمدة ناقصة: {missing} | الموجود: {headers_raw}")
            return redirect("quiz:staff_import_questions")

        idx = {h: headers.index(h) for h in need}

        if replace:
            Question.objects.filter(quiz=quiz).delete()

        created = 0
        skipped = 0

        for _row_num, r in enumerate(rows[1:], start=2):
            order_val = r[idx["order"]]
            qtext = r[idx["question"]]
            a = r[idx["a"]]
            b = r[idx["b"]]
            c = r[idx["c"]]
            d = r[idx["d"]]
            correct = str(r[idx["correct"]] or "").strip().upper()

            if not qtext or not a or not b or not c or not d or correct not in ("A", "B", "C", "D"):
                skipped += 1
                continue

            try:
                order_int = int(order_val) if order_val is not None and str(order_val).strip() != "" else 0
            except Exception:
                order_int = 0

            q = Question.objects.create(quiz=quiz, text=str(qtext).strip(), order=order_int)

            Choice.objects.create(question=q, text=str(a).strip(), is_correct=(correct == "A"))
            Choice.objects.create(question=q, text=str(b).strip(), is_correct=(correct == "B"))
            Choice.objects.create(question=q, text=str(c).strip(), is_correct=(correct == "C"))
            Choice.objects.create(question=q, text=str(d).strip(), is_correct=(correct == "D"))

            created += 1

        messages.success(request, f"✅ تم استيراد {created} سؤال. (تم تجاهل {skipped} صف غير مكتمل)")
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import.html", {"quizzes": quizzes})


# ======================================================
# ✅ استيراد المتقدمين (Participants) من Excel (Staff)
# ======================================================
@staff_member_required
@transaction.atomic
def staff_import_participants_view(request: HttpRequest) -> HttpResponse:
    """
    استيراد بيانات المتقدمين من ملف Excel (.xlsx)
    الشيت الافتراضي: participants

    الأعمدة المطلوبة (غير حساسة لحالة الأحرف):
      national_id, full_name, phone_last4

    أعمدة اختيارية:
      is_allowed (0/1 أو TRUE/FALSE)
      has_taken_exam (0/1 أو TRUE/FALSE)

    الخيارات:
      replace=1  => حذف جميع المتقدمين ثم استيرادهم من جديد
      upsert     => (الافتراضي) تحديث/إضافة حسب national_id
    """
    if request.method == "POST":
        sheet_name = (request.POST.get("sheet_name") or "participants").strip()
        replace = request.POST.get("replace") == "1"
        file = request.FILES.get("file")

        if not file:
            messages.error(request, "ارفع ملف Excel (.xlsx).")
            return redirect("quiz:staff_import_participants")

        if not file.name.lower().endswith(".xlsx"):
            messages.error(request, "الملف يجب أن يكون بصيغة .xlsx")
            return redirect("quiz:staff_import_participants")

        try:
            import openpyxl
        except ImportError:
            messages.error(request, "مكتبة openpyxl غير مثبتة. نفّذ: pip install openpyxl")
            return redirect("quiz:staff_import_participants")

        wb = openpyxl.load_workbook(file)
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

        need = ["national_id", "full_name", "phone_last4"]
        missing = [c for c in need if c not in headers]
        if missing:
            messages.error(request, f"أعمدة ناقصة: {missing} | الموجود: {headers_raw}")
            return redirect("quiz:staff_import_participants")

        idx = {h: headers.index(h) for h in need}

        # optional
        def _opt_index(col: str) -> int | None:
            return headers.index(col) if col in headers else None

        ix_allowed = _opt_index("is_allowed")
        ix_taken = _opt_index("has_taken_exam")

        def _to_bool(v, default: bool) -> bool:
            if v is None:
                return default
            s = str(v).strip().lower()
            if s in ("1", "true", "yes", "y", "نعم", "صح"):
                return True
            if s in ("0", "false", "no", "n", "لا", "خطأ"):
                return False
            return default

        if replace:
            Participant.objects.all().delete()

        created = 0
        updated = 0
        skipped = 0

        for _row_num, r in enumerate(rows[1:], start=2):
            national_id = str(r[idx["national_id"]] or "").strip()
            full_name = str(r[idx["full_name"]] or "").strip()
            phone_last4 = str(r[idx["phone_last4"]] or "").strip()

            if not national_id:
                skipped += 1
                continue

            # last4: لو فاضي نخليه فاضي، لو موجود لازم 4 أرقام
            if phone_last4:
                phone_last4 = phone_last4.replace(" ", "")
                if (not phone_last4.isdigit()) or len(phone_last4) != 4:
                    skipped += 1
                    continue

            is_allowed = _to_bool(r[ix_allowed] if ix_allowed is not None else None, True)
            has_taken_exam = _to_bool(r[ix_taken] if ix_taken is not None else None, False)

            obj, was_created = Participant.objects.update_or_create(
                national_id=national_id,
                defaults={
                    "full_name": full_name,
                    "phone_last4": phone_last4,
                    "is_allowed": is_allowed,
                    "has_taken_exam": has_taken_exam,
                }
            )
            if was_created:
                created += 1
            else:
                updated += 1

        messages.success(
            request,
            f"✅ تم استيراد المتقدمين بنجاح: (جدد {created}) (تم تحديث {updated}) (تم تجاهل {skipped})"
        )
        return redirect("quiz:staff_manage")

    return render(request, "quiz/staff_import_participants.html")
