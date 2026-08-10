import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-this-smartfill-secret")
    DATABASE_PATH = os.getenv("DATABASE_PATH", "data/attendance.db")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + str((BASE_DIR / DATABASE_PATH).resolve())
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    UPLOAD_FOLDER = str((BASE_DIR / "uploads").resolve())
    APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT = int(os.getenv("APP_PORT", "3000"))
    SESSION_INACTIVITY_TIMEOUT_SECONDS = int(os.getenv("SESSION_INACTIVITY_TIMEOUT_SECONDS", "300"))
    WTF_CSRF_ENABLED = os.getenv("WTF_CSRF_ENABLED", "true").lower() != "false"

    # Session cookie hardening. SESSION_COOKIE_SECURE defaults to off because the
    # Pi serves plain HTTP on the LAN; set SESSION_COOKIE_SECURE=true once the app
    # is behind HTTPS so the cookie is never sent over an unencrypted connection.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "smartfill_session")
    PERMANENT_SESSION_LIFETIME = timedelta(seconds=SESSION_INACTIVITY_TIMEOUT_SECONDS)

    # Brute-force protection: lock the account after this many consecutive failures.
    LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))
    # A pending "already signed in elsewhere" takeover must be confirmed promptly;
    # otherwise the password check that authorised it goes stale in the session.
    LOGIN_TAKEOVER_WINDOW_SECONDS = int(os.getenv("LOGIN_TAKEOVER_WINDOW_SECONDS", "120"))
    MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "10"))
