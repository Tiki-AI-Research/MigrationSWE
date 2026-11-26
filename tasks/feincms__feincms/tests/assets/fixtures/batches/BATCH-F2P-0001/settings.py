"""Minimal Django settings used by the BATCH-F2P-0001 probes.

The settings never touch a persistent database (in-memory SQLite), never write
to the filesystem and never contact the network, so the probes can run to
completion inside the harness timeout.
"""

SECRET_KEY = "batch-f2p-0001-probe"
USE_TZ = True
TIME_ZONE = "Europe/Zurich"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.admin",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.sites",
    "feincms",
    "mptt",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
