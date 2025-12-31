# quiz/models.py
from __future__ import annotations

from django.db import models
from django.utils import timezone


class Domain(models.TextChoices):
    DEPUTY = "deputy", "وكيل"
    COUNSELOR = "counselor", "موجه طلابي"
    ACTIVITY = "activity", "رائد نشاط"


class Quiz(models.Model):
    title = models.CharField("عنوان الاختبار", max_length=220)
    is_active = models.BooleanField("مفعل", default=False)
    time_per_question_seconds = models.PositiveIntegerField("زمن السؤال (ثانية)", default=60)

    class Meta:
        verbose_name = "اختبار"
        verbose_name_plural = "الاختبارات"

    def __str__(self) -> str:
        return self.title


class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, verbose_name="الاختبار")
    text = models.TextField("نص السؤال")
    order = models.IntegerField("الترتيب", default=0)

    class Meta:
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"
        ordering = ["quiz_id", "order"]

    def __str__(self) -> str:
        return f"{self.quiz} / سؤال {self.order}"


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, verbose_name="السؤال")
    text = models.CharField("نص الخيار", max_length=300)
    is_correct = models.BooleanField("إجابة صحيحة", default=False)

    class Meta:
        verbose_name = "خيار"
        verbose_name_plural = "الخيارات"

    def __str__(self) -> str:
        return self.text


class Participant(models.Model):
    national_id = models.CharField("رقم الهوية", max_length=20)
    full_name = models.CharField("الاسم الكامل", max_length=220)
    phone_last4 = models.CharField("آخر 4 أرقام من الجوال", max_length=10)

    # ✅ المجال (نخليه مؤقتًا يقبل null لتفادي سؤال makemigrations)
    domain = models.CharField(
        "المجال",
        max_length=20,
        choices=Domain.choices,
        null=True,
        blank=True,
    )

    is_allowed = models.BooleanField("مسموح له بالدخول", default=True)
    has_taken_exam = models.BooleanField("أدى الاختبار", default=False)

    class Meta:
        verbose_name = "مترشح"
        verbose_name_plural = "المترشحون"
        indexes = [
            models.Index(fields=["national_id"]),
            models.Index(fields=["domain"]),
            models.Index(fields=["national_id", "domain"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.national_id}"


class Attempt(models.Model):
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, verbose_name="المترشح")
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, verbose_name="الاختبار")

    score = models.IntegerField("الدرجة", default=0)
    current_index = models.IntegerField("مؤشر السؤال الحالي", default=0)
    is_finished = models.BooleanField("منتهٍ", default=False)

    started_at = models.DateTimeField("وقت البدء", default=timezone.now)
    finished_at = models.DateTimeField("وقت الانتهاء", null=True, blank=True)

    session_key = models.CharField("مفتاح الجلسة", max_length=200)
    started_ip = models.GenericIPAddressField("عنوان IP عند البدء", null=True, blank=True)
    user_agent = models.CharField("وكيل المستخدم (User Agent)", max_length=255)

    class Meta:
        verbose_name = "محاولة"
        verbose_name_plural = "المحاولات"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["is_finished", "started_at"]),
            models.Index(fields=["participant", "quiz", "is_finished"]),
        ]

    def __str__(self) -> str:
        return f"{self.participant} / {self.quiz}"


class Answer(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, verbose_name="المحاولة")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, verbose_name="السؤال")
    selected_choice = models.ForeignKey(
        Choice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="الخيار المختار",
    )

    started_at = models.DateTimeField("وقت بدء السؤال", default=timezone.now)
    answered_at = models.DateTimeField("وقت الإجابة", null=True, blank=True)

    is_late = models.BooleanField("متأخر", default=False)

    class Meta:
        verbose_name = "إجابة"
        verbose_name_plural = "الإجابات"
        indexes = [
            models.Index(fields=["attempt", "question"]),
            models.Index(fields=["answered_at"]),
        ]

    def __str__(self) -> str:
        return f"إجابة / محاولة {self.attempt_id} / سؤال {self.question_id}"
