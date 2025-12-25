from __future__ import annotations
from django.db import models


class Quiz(models.Model):
    title = models.CharField(max_length=200, verbose_name="عنوان الاختبار")
    is_active = models.BooleanField(default=False, verbose_name="نشط")
    time_per_question_seconds = models.PositiveIntegerField(default=30, verbose_name="زمن السؤال (ث)")

    def __str__(self) -> str:
        return self.title


class Participant(models.Model):
    national_id = models.CharField(max_length=20, unique=True, verbose_name="السجل")
    full_name = models.CharField(max_length=255, blank=True, default="", verbose_name="الاسم")
    phone_last4 = models.CharField(max_length=4, blank=True, default="", verbose_name="آخر 4 من الجوال")
    is_allowed = models.BooleanField(default=True, verbose_name="مسموح")
    has_taken_exam = models.BooleanField(default=False, verbose_name="نفّذ الاختبار")

    def __str__(self) -> str:
        return f"{self.national_id} - {self.full_name or ''}".strip()


class Attempt(models.Model):
    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="attempts")
    quiz = models.ForeignKey(Quiz, on_delete=models.PROTECT, related_name="attempts")

    score = models.IntegerField(default=0)
    current_index = models.IntegerField(default=0)

    is_finished = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    session_key = models.CharField(max_length=64, blank=True, default="")
    started_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.participant.national_id} / {self.quiz.title} / {self.score}"


class Question(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return f"Q{self.order}: {self.text[:40]}"


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.text[:40]


class Answer(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")

    selected_choice = models.ForeignKey(Choice, on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField()
    answered_at = models.DateTimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)

    class Meta:
        unique_together = ("attempt", "question")
