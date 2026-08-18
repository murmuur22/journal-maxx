import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
JOURNALMAX_VERSION = os.environ.get("JOURNALMAX_VERSION", (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip())
SECRET_KEY = os.environ.get("DIARY_SECRET_KEY", "development-only-change-me")
DEBUG = os.environ.get("DIARY_DEBUG", "0") == "1"
DEV_PASSWORD_ONLY_USERS = {username.strip() for username in os.environ.get("DIARY_DEV_PASSWORD_ONLY_USERS", "").split(",") if username.strip()} if DEBUG else set()
ALLOWED_HOSTS = [x for x in os.environ.get("DIARY_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x]
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("DIARY_CSRF_TRUSTED_ORIGINS", "").split(",") if x]
CSRF_FAILURE_VIEW = "core.views.csrf_failure"

INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "diary.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "core.context_processors.reviewer_patient",
    ]},
}]
WSGI_APPLICATION = "diary.wsgi.application"
STATE_ROOT = Path(os.environ.get("DIARY_STATE_ROOT", BASE_DIR / ".state"))
STATE_ROOT.mkdir(parents=True, exist_ok=True)
CARD_ROOT = Path(os.environ.get("DIARY_CARD_ROOT", BASE_DIR / ".cards"))
CARD_VOLUME_MARKER = ".journalmax-volume.json"
CARD_VOLUME_REQUIRE_MARKER = os.environ.get("DIARY_CARD_VOLUME_REQUIRE_MARKER", "1" if "DIARY_CARD_ROOT" in os.environ else "0") == "1"
CARD_VOLUME_ID = os.environ.get("DIARY_CARD_VOLUME_ID", "").strip()
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": STATE_ROOT / "diary.sqlite3", "OPTIONS": {"timeout": 20}}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": STATE_ROOT / "cache"}}
AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DIARY_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if os.environ.get("DIARY_BEHIND_HTTPS_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DIARY_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
