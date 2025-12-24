from __future__ import annotations

from django.db import models
from django.utils import timezone


# ======================================================
# المشاركون (الموظفون/المتقدمون)
# ======================================================
class Participant(models.Model):
    national_id = models.CharField("السجل المدني", max_length=20, unique=True, db_index=True)
    full_name = models.CharField("الاسم", max_length=200, blank=True, default="")
    phone_last4 = models.CharField("آخر 4 أرقام من الجوال", max_length=4, blank=True, default="")

    is_allowed = models.BooleanField("مسموح له بالدخول", default=True)
    has_taken_exam = models.BooleanField("نفّذ الاختبار سابقاً", default=False)

    class Meta:
        verbose_name = "مشارك"
        verbose_name_plural = "المشاركون"
        ordering = ["full_name", "national_id"]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.national_id})" if self.full_name else self.national_id


# ======================================================
# الاختبار
# ======================================================
class Quiz(models.Model):
    title = models.CharField("عنوان الاختبار", max_length=200)
    time_per_question_seconds = models.PositiveIntegerField("مدة السؤال (ثانية)", default=30)
    is_active = models.BooleanField("نشط", default=True)

    class Meta:
        verbose_name = "اختبار"
        verbose_name_plural = "الاختبارات"
        ordering = ["-id"]

    def __str__(self) -> str:
        return self.title


# ======================================================
# الأسئلة
# ======================================================
class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions", verbose_name="الاختبار")
    text = models.TextField("نص السؤال")
    order = models.PositiveIntegerField("الترتيب", default=0)

    class Meta:
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.text[:60]


# ======================================================
# الخيارات
# ======================================================
class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices", verbose_name="السؤال")
    text = models.CharField("الخيار", max_length=300)
    is_correct = models.BooleanField("إجابة صحيحة", default=False)

    class Meta:
        verbose_name = "خيار"
        verbose_name_plural = "الخيارات"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.text


# ======================================================
# المحاولة
# ======================================================
class Attempt(models.Model):
    participant = models.ForeignKey(
        Participant,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name="المشارك",
    )
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name="الاختبار",
    )

    started_at = models.DateTimeField("بدأ في", default=timezone.now)
    finished_at = models.DateTimeField("انتهى في", null=True, blank=True)

    # ✅ لمنع تعدد الجلسات + توثيق
    session_key = models.CharField("مفتاح الجلسة", max_length=64, blank=True, default="", db_index=True)
    started_ip = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent = models.CharField("User-Agent", max_length=255, blank=True, default="")

    current_index = models.PositiveIntegerField("مؤشر السؤال الحالي", default=0)
    score = models.PositiveIntegerField("الدرجة", default=0)
    is_finished = models.BooleanField("منتهي", default=False)

    class Meta:
        verbose_name = "محاولة"
        verbose_name_plural = "المحاولات"
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["quiz", "is_finished"]),
            models.Index(fields=["participant", "is_finished"]),
        ]

    def __str__(self) -> str:
        return f"{self.participant} - {self.quiz}"


# ======================================================
# الإجابة
# ======================================================
class Answer(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="answers", verbose_name="المحاولة")
    question = models.ForeignKey(Question, on_delete=models.PROTECT, verbose_name="السؤال")
    selected_choice = models.ForeignKey(
        Choice,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="الإجابة المختارة",
    )

    started_at = models.DateTimeField("بداية السؤال", default=timezone.now)
    answered_at = models.DateTimeField("وقت الإجابة", null=True, blank=True)

    is_late = models.BooleanField("متأخر", default=False)

    class Meta:
        verbose_name = "إجابة"
        verbose_name_plural = "الإجابات"
        constraints = [
            models.UniqueConstraint(fields=["attempt", "question"], name="uniq_attempt_question")
        ]
        ordering = ["id"]

    def __str__(self) -> str:
        return f"إجابة ({self.attempt_id}) - سؤال ({self.question_id})"
