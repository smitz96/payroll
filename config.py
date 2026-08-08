import os
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
