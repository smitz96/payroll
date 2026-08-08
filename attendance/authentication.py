from datetime import datetime
from functools import wraps
from uuid import uuid4

from flask import current_app, flash, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from attendance import db
from attendance.models import AuditLog, User

LAST_ACTIVITY_SESSION_KEY = "last_activity_at"
SESSION_TOKEN_KEY = "session_token"
PENDING_LOGIN_USER_KEY = "pending_login_user_id"


def init_admin_user():
    if not User.query.filter_by(username="admin").first():
        db.session.add(User(username="admin", password_hash=generate_password_hash("12345")))
        db.session.add(AuditLog(actor="system", action="Admin Initialized", detail="Initial admin account created"))
        db.session.commit()


def authenticate(username, password):
    user = User.query.filter_by(username=username, is_active=True).first()
    if not user or not check_password_hash(user.password_hash, password):
        return None
    return user


def active_session_is_current(user):
    if not user.active_session_token or not user.active_session_last_seen_at:
        return False
    timeout_seconds = int(current_app.config.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 300))
    inactive_seconds = datetime.utcnow().timestamp() - user.active_session_last_seen_at.timestamp()
    return inactive_seconds <= timeout_seconds


def start_user_session(user, forced=False):
    now = datetime.utcnow()
    token = uuid4().hex
    user.last_login_at = now
    user.active_session_token = token
    user.active_session_started_at = now
    user.active_session_last_seen_at = now
    session.clear()
    session["user_id"] = user.id
    session["username"] = user.username
    session[SESSION_TOKEN_KEY] = token
    mark_session_activity()
    detail = "Successful login"
    if forced:
        detail = "Successful login after logging out previous active session"
    db.session.add(AuditLog(actor=user.username, action="User Login", detail=detail))
    db.session.commit()


def clear_user_session(user, reason="User Logout"):
    if user and session.get(SESSION_TOKEN_KEY) == user.active_session_token:
        user.active_session_token = None
        user.active_session_started_at = None
        user.active_session_last_seen_at = None
        db.session.add(user)
    db.session.add(AuditLog(actor=user.username if user else session.get("username", "admin"), action=reason, detail="Session cleared"))
    db.session.commit()


def mark_session_activity():
    session[LAST_ACTIVITY_SESSION_KEY] = datetime.utcnow().timestamp()
    user_id = session.get("user_id")
    token = session.get(SESSION_TOKEN_KEY)
    if user_id and token:
        user = db.session.get(User, user_id)
        if user and user.active_session_token == token:
            user.active_session_last_seen_at = datetime.utcnow()
            db.session.add(user)
            db.session.commit()


def session_is_expired():
    last_activity_at = session.get(LAST_ACTIVITY_SESSION_KEY)
    if not last_activity_at:
        return False
    timeout_seconds = int(current_app.config.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 300))
    inactive_seconds = datetime.utcnow().timestamp() - float(last_activity_at)
    return inactive_seconds > timeout_seconds


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        user = db.session.get(User, session.get("user_id"))
        if not user or user.active_session_token != session.get(SESSION_TOKEN_KEY):
            username = session.get("username", "admin")
            db.session.add(AuditLog(actor=username, action="User Session Replaced", detail="Logged out because this account was opened in another browser or device"))
            db.session.commit()
            session.clear()
            flash("You were logged out because this account was opened in another browser or device.", "warning")
            return redirect(url_for("auth.login"))
        if session_is_expired():
            username = session.get("username", "admin")
            if user and user.active_session_token == session.get(SESSION_TOKEN_KEY):
                user.active_session_token = None
                user.active_session_started_at = None
                user.active_session_last_seen_at = None
                db.session.add(user)
            db.session.add(AuditLog(actor=username, action="User Auto Logout", detail="Session expired after 5 minutes of inactivity"))
            db.session.commit()
            session.clear()
            flash("Your session expired after 5 minutes of inactivity. Please log in again.", "warning")
            return redirect(url_for("auth.login"))
        mark_session_activity()
        return view(*args, **kwargs)

    return wrapped


def current_username():
    return session.get("username", "admin")


def change_password(user_id, current_password, new_password, confirm_password):
    user = db.session.get(User, user_id)
    if not user or not check_password_hash(user.password_hash, current_password):
        return False, "Current password is incorrect."
    if not new_password or len(new_password) < 8:
        return False, "New password must be at least 8 characters."
    if new_password != confirm_password:
        return False, "New password and confirmation do not match."
    user.password_hash = generate_password_hash(new_password)
    db.session.add(AuditLog(actor=user.username, action="Password Changed", detail="Admin password changed"))
    db.session.commit()
    flash("Password changed successfully.", "success")
    return True, "Password changed successfully."
