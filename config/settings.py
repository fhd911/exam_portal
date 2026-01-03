# config/settings.py  (استبدله بالكامل) ✅ جاهز لـ Render (PostgreSQL) + حماية CSRF + WhiteNoise
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# =========================
# Helpers
# =========================
def env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_str(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None else v


# =========================
# .env (اختياري)
# =========================
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass


# =========================
# Security
# =========================
DEBUG = env_bool("DJANGO_DEBUG", True)

SECRET_KEY = env_str("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-change-me-in-prod"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY is required when DEBUG=0")

_hosts = env_str("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
ALLOWED_HOSTS = [h.strip() for h in _hosts.split(",") if h.strip()]


# =========================
# Apps
# =========================
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # ✅ تعريب اسم التطبيق داخل لوحة الإدارة
    "quiz.apps.QuizConfig",
]


# =========================
# Middleware
# =========================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    # ✅ مهم لعرض static على Render/Production
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",

    # ✅ مهم للتعريب/RTL
    "django.middleware.locale.LocaleMiddleware",

    "django.middleware.common.CommonMiddleware",

    # ✅ حارس جلسة الاختبار
    "quiz.middleware.ExamSessionGuard",

    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "config.urls"


# =========================
# Templates
# =========================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],  # ✅ مهم لـ templates/admin/base_site.html
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


WSGI_APPLICATION = "config.wsgi.application"


# =========================
# Database (Render: PostgreSQL / Local: SQLite)
# =========================
DATABASE_URL = env_str("DATABASE_URL", "").strip()

if DATABASE_URL:
    # ✅ على Render لازم PostgreSQL يشتغل فعليًا — بدون سكوت
    try:
        import dj_database_url  # pip install dj-database-url
    except Exception as e:
        raise RuntimeError(
            "DATABASE_URL is set but dj-database-url is not installed. "
            "Add 'dj-database-url' to requirements.txt"
        ) from e

    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=int(env_str("DJANGO_DB_CONN_MAX_AGE", "600")),
            ssl_require=True,
        )
    }
else:
    # تطوير محلي فقط
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# =========================
# Password validators
# =========================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# =========================
# Locale / Time
# =========================
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]


# =========================
# Static
# =========================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ✅ إذا عندك مجلد static في جذر المشروع (BASE_DIR/static) فعّل هذا:
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

# ✅ WhiteNoise storage (مهم في الإنتاج)
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# =========================
# Production hardening
# =========================
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SECURE_HSTS_SECONDS = int(env_str("DJANGO_HSTS_SECONDS", "60"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", True)

    # ✅ CSRF trusted origins (مهم جدًا على Render)
    _csrf = env_str("DJANGO_CSRF_TRUSTED_ORIGINS", "").strip()
    if _csrf:
        CSRF_TRUSTED_ORIGINS = [x.strip() for x in _csrf.split(",") if x.strip()]
    else:
        CSRF_TRUSTED_ORIGINS = [
            f"https://{h}"
            for h in ALLOWED_HOSTS
            if h not in {"localhost", "127.0.0.1"} and "*" not in h
        ]
