# quiz/models.py
from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.utils import timezone


class Quiz(models.Model):
    title = models.CharField("عنوان الاختبار", max_length=200, unique=True)
    is_active = models.BooleanField("نشط؟", default=False)

    # ✅ لكل سؤال كم ثانية (بدل ما تكون ثابتة في views)
    per_question_seconds = models.PositiveIntegerField("ثواني لكل سؤال", default=50)

    created_at = models.DateTimeField("تاريخ الإنشاء", default=timezone.now)

    class Meta:
        ordering = ["-id"]
        verbose_name = "اختبار"
        verbose_name_plural = "الاختبارات"

    def __str__(self) -> str:
        return self.title


class Participant(models.Model):
    DOMAIN_CHOICES = (
        ("deputy", "وكيل"),
        ("counselor", "موجه طلابي"),
        ("activity", "رائد نشاط"),
    )

    national_id = models.CharField("رقم الهوية/السجل", max_length=20, unique=True, db_index=True)
    full_name = models.CharField("الاسم", max_length=220)
    phone_last4 = models.CharField("آخر 4 أرقام من الجوال", max_length=4, blank=True, default="")

    # ✅ default يحل مشكلة السجلات القديمة NULL وقت makemigrations
    domain = models.CharField(
        "المجال",
        max_length=30,
        choices=DOMAIN_CHOICES,
        default="deputy",
        db_index=True,
    )

    is_allowed = models.BooleanField("مسموح بالدخول؟", default=True)
    has_taken_exam = models.BooleanField("أدى الاختبار؟", default=False)

    created_at = models.DateTimeField("تاريخ الإضافة", default=timezone.now)

    class Meta:
        ordering = ["-id"]
        verbose_name = "مشارك"
        verbose_name_plural = "المشاركون"

    def __str__(self) -> str:
        return f"{self.full_name} ({self.national_id})"


class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, verbose_name="الاختبار")
    order = models.PositiveIntegerField("ترتيب السؤال", default=1)
    text = models.TextField("نص السؤال")

    class Meta:
        ordering = ["quiz_id", "order", "id"]
        unique_together = (("quiz", "order"),)
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"

    def __str__(self) -> str:
        return f"{self.quiz.title} - Q{self.order}"


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, verbose_name="السؤال")
    text = models.TextField("نص الخيار")
    is_correct = models.BooleanField("إجابة صحيحة؟", default=False)

    class Meta:
        ordering = ["id"]
        verbose_name = "خيار"
        verbose_name_plural = "الخيارات"

    def __str__(self) -> str:
        return f"Choice({self.id})"


class Attempt(models.Model):
    FINISH_REASON_CHOICES = (
        ("normal", "طبيعي"),
        ("timeout", "تلقائي/انتهاء وقت"),
        ("forced", "إغلاق إداري"),
    )

    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, verbose_name="المشارك")
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, verbose_name="الاختبار")

    session_key = models.CharField("مفتاح الجلسة", max_length=64, blank=True, default="", db_index=True)
    started_ip = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=255, null=True, blank=True)

    started_at = models.DateTimeField("وقت البدء", default=timezone.now, db_index=True)
    finished_at = models.DateTimeField("وقت الانتهاء", null=True, blank=True)

    current_index = models.PositiveIntegerField("مؤشر السؤال الحالي", default=0)
    score = models.PositiveIntegerField("النتيجة", default=0)

    is_finished = models.BooleanField("منتهية؟", default=False, db_index=True)

    # ✅ تمييز سبب الإنهاء
    finished_reason = models.CharField(
        "سبب الإنهاء",
        max_length=20,
        choices=FINISH_REASON_CHOICES,
        default="normal",
        db_index=True,
    )

    # ✅ كم مرة تم تجاوز الوقت
    timed_out_count = models.PositiveIntegerField("عدد مرات انتهاء الوقت", default=0)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "محاولة"
        verbose_name_plural = "المحاولات"

    def __str__(self) -> str:
        return f"Attempt({self.id})"

    # ---------------------------
    # Admin helpers / properties
    # ---------------------------
    def answered_count(self) -> int:
        return self.answers.filter(answered_at__isnull=False).count()

    def questions_total(self) -> int:
        return self.quiz.question_set.count()

    def current_question(self):
        if self.is_finished:
            return None
        return (
            self.quiz.question_set.order_by("order", "id")
            .all()[self.current_index : self.current_index + 1]
            .first()
        )

    def current_answer(self):
        q = self.current_question()
        if not q:
            return None
        return self.answers.filter(question=q).first()

    def current_deadline(self):
        ans = self.current_answer()
        if not ans:
            return None
        started = ans.started_at or timezone.now()
        sec = int(self.quiz.per_question_seconds or 50)
        return started + timedelta(seconds=sec)

    def remaining_seconds(self) -> int | None:
        if self.is_finished:
            return None
        ans = self.current_answer()
        if not ans:
            return None
        deadline = self.current_deadline()
        if not deadline:
            return None
        return int(max(0, (deadline - timezone.now()).total_seconds()))

    def is_overdue(self) -> bool:
        rem = self.remaining_seconds()
        return (not self.is_finished) and (rem is not None) and (rem <= 0)


class Answer(models.Model):
    attempt = models.ForeignKey(
        Attempt, on_delete=models.CASCADE, related_name="answers", verbose_name="المحاولة"
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE, verbose_name="السؤال")
    selected_choice = models.ForeignKey(
        Choice, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="الخيار المختار"
    )

    started_at = models.DateTimeField("وقت بدء السؤال", default=timezone.now)
    answered_at = models.DateTimeField("وقت الإجابة", null=True, blank=True)

    class Meta:
        ordering = ["id"]
        unique_together = (("attempt", "question"),)
        verbose_name = "إجابة"
        verbose_name_plural = "الإجابات"

    def __str__(self) -> str:
        return f"Answer({self.id})"
