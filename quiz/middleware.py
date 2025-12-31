# quiz/middleware.py
from __future__ import annotations

from django.shortcuts import redirect
from django.urls import reverse


class ExamSessionGuard:
    """
    حماية صفحات الاختبار من الدخول بدون جلسة صحيحة.
    - يمنع الوصول لـ /question/ إذا ما في attempt_id في السيشن
    - يسمح بالوصول لـ /login/ و /logout/ و /staff/ و /admin/
    """

    ALLOW_PREFIXES = ("/admin/", "/staff/", "/static/")
    ALLOW_PATHS = ("/login/", "/logout/", "/finish/")

    SESSION_ATTEMPT_ID = "attempt_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or "/"

        # allow admin/staff/static
        if path.startswith(self.ALLOW_PREFIXES):
            return self.get_response(request)

        # allow login/logout/finish
        if path in self.ALLOW_PATHS:
            return self.get_response(request)

        # only guard the exam routes (home + question)
        if path in ("/", "/question/"):
            if not request.session.get(self.SESSION_ATTEMPT_ID):
                return redirect(reverse("quiz:login"))

        return self.get_response(request)
