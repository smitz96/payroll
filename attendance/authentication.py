from datetime import datetime, timedelta
from functools import wraps
from uuid import uuid4

from flask import abort, current_app, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from attendance import db
from attendance.models import AuditLog, User

LAST_ACTIVITY_SESSION_KEY = "last_activity_at"
SESSION_TOKEN_KEY = "session_token"
PENDING_LOGIN_USER_KEY = "pending_login_user_id"
PENDING_LOGIN_GRANTED_AT_KEY = "pending_login_granted_at"

MODULES = (
    ("dashboard", "Dashboard", "View the main overview after sign-in."),
    ("attendance", "Attendance", "Import, review, edit, and submit attendance."),
    ("employees", "Employees", "Maintain employee master data and imports."),
    ("weekoffs", "Week Offs", "Confirm and maintain weekly off patterns."),
    ("leave_balances", "Leave Balance", "View, import, and adjust leave balances."),
    ("holidays", "Holidays", "Maintain the holiday calendar."),
    ("payroll", "Payroll", "Open months, load wages, calculate payroll, and edit employee payroll details."),
    ("finalization", "Finalization", "Finalize or unlock payroll wage groups."),
    ("reports", "Reports", "View and download reports and salary slips."),
    ("money", "Loans & Advances", "Maintain loans and salary advances."),
    ("logs", "Activity Logs", "View system activity logs."),
    ("settings", "Settings & Users", "Manage users, backup/restore, app updates, and reset data."),
)
MODULE_LABELS = {key: label for key, label, _description in MODULES}
ALL_MODULE_KEYS = {key for key, _label, _description in MODULES}

ENDPOINT_MODULES = {
    "dashboard": "dashboard",
    "attendance_manager": "attendance",
    "master": "employees",
    "weekoffs": "weekoffs",
    "leave_balances": "leave_balances",
    "holidays": "holidays",
    "payroll": "payroll",
    "reports": "reports",
    "salary_slips": "reports",
    "loans": "money",
    "advances": "money",
    "logs": "logs",
    "settings": "settings",
}
ANY_AUTHENTICATED_ENDPOINTS = {"auth.logout", "settings.security"}


def init_admin_user():
    user = User.query.filter_by(username="admin").first()
    if not user:
        db.session.add(User(username="admin", password_hash=generate_password_hash("12345"), is_admin=True))
        db.session.add(AuditLog(actor="system", action="Admin Initialized", detail="Initial admin account created"))
        db.session.commit()
        return
    if not user.is_admin:
        user.is_admin = True
        db.session.add(user)
        db.session.commit()


# A real hash to compare against when the username does not exist, so a missing
# account costs the same time as a wrong password and cannot be probed for.
_DUMMY_HASH = generate_password_hash("smartfill-dummy-password")


def lockout_remaining_seconds(user):
    if not user or not user.locked_until:
        return 0
    remaining = user.locked_until.timestamp() - datetime.utcnow().timestamp()
    return int(remaining) if remaining > 0 else 0


def _register_failed_attempt(user):
    """Count a failed attempt and lock the account once the limit is reached."""
    max_attempts = int(current_app.config.get("LOGIN_MAX_ATTEMPTS", 5))
    lockout_minutes = int(current_app.config.get("LOGIN_LOCKOUT_MINUTES", 15))
    user.failed_login_count = (user.failed_login_count or 0) + 1
    user.last_failed_login_at = datetime.utcnow()
    detail = f"Failed login attempt {user.failed_login_count} of {max_attempts}"
    if user.failed_login_count >= max_attempts:
        user.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
        user.failed_login_count = 0
        detail = f"Account locked for {lockout_minutes} minute(s) after {max_attempts} failed attempts"
        db.session.add(AuditLog(actor=user.username, action="User Login Locked", detail=detail))
    else:
        db.session.add(AuditLog(actor=user.username, action="User Login Failed", detail=detail))
    db.session.add(user)
    db.session.commit()


def authenticate(username, password):
    """Return (user, error). `error` is a message to show when user is None."""
    user = User.query.filter_by(username=username, is_active=True).first()
    if not user:
        # Still hash, so a wrong username is indistinguishable from a wrong password.
        check_password_hash(_DUMMY_HASH, password or "")
        db.session.add(AuditLog(actor=username or "unknown", action="User Login Failed", detail="Unknown or inactive username"))
        db.session.commit()
        return None, "Invalid username or password."

    remaining = lockout_remaining_seconds(user)
    if remaining:
        minutes = max(1, (remaining + 59) // 60)
        return None, f"Too many failed attempts. Try again in {minutes} minute(s)."

    if not check_password_hash(user.password_hash, password or ""):
        _register_failed_attempt(user)
        remaining = lockout_remaining_seconds(user)
        if remaining:
            minutes = max(1, (remaining + 59) // 60)
            return None, f"Too many failed attempts. Try again in {minutes} minute(s)."
        return None, "Invalid username or password."

    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
        db.session.add(user)
        db.session.commit()
    return user, ""


def active_session_is_current(user):
    if not user.active_session_token or not user.active_session_last_seen_at:
        return False
    inactive_seconds = datetime.utcnow().timestamp() - user.active_session_last_seen_at.timestamp()
    return inactive_seconds <= inactivity_timeout_seconds()


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


def inactivity_timeout_seconds():
    return int(current_app.config.get("SESSION_INACTIVITY_TIMEOUT_SECONDS", 900))


def inactivity_timeout_label():
    """The timeout as it should read to a user, derived from the setting.

    The wording used to hardcode "5 minutes", so changing the timeout would have left
    the message telling people something untrue.
    """
    seconds = inactivity_timeout_seconds()
    minutes, remainder = divmod(seconds, 60)
    if remainder or minutes == 0:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def session_is_expired():
    last_activity_at = session.get(LAST_ACTIVITY_SESSION_KEY)
    if not last_activity_at:
        return False
    inactive_seconds = datetime.utcnow().timestamp() - float(last_activity_at)
    return inactive_seconds > inactivity_timeout_seconds()


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
            db.session.add(AuditLog(actor=username, action="User Auto Logout", detail=f"Session expired after {inactivity_timeout_label()} of inactivity"))
            db.session.commit()
            session.clear()
            flash(f"Your session expired after {inactivity_timeout_label()} of inactivity. Please log in again.", "warning")
            return redirect(url_for("auth.login"))
        mark_session_activity()
        return view(*args, **kwargs)

    return wrapped


def current_username():
    return session.get("username", "admin")


def current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def normalized_permissions(user):
    values = getattr(user, "permissions_json", None) or []
    return {str(value) for value in values if str(value) in ALL_MODULE_KEYS}


def user_has_permission(user, permission):
    if not user:
        return False
    if user.is_admin:
        return True
    if permission == "dashboard":
        return True
    return permission in normalized_permissions(user)


def has_permission(permission):
    return user_has_permission(current_user(), permission)


def require_permission(permission):
    if not has_permission(permission):
        abort(403)


def register_permission_hooks(app):
    @app.before_request
    def enforce_module_permissions():
        endpoint = request.endpoint or ""
        if not endpoint or endpoint == "static" or endpoint.startswith("auth.login"):
            return None
        if endpoint in ANY_AUTHENTICATED_ENDPOINTS:
            return None
        if not session.get("user_id"):
            return None
        module = ENDPOINT_MODULES.get(endpoint.split(".", 1)[0])
        if module == "payroll" and (has_permission("payroll") or has_permission("finalization")):
            return None
        if module and not has_permission(module):
            abort(403)
        return None

    @app.context_processor
    def inject_permission_helpers():
        user = current_user()
        return {
            "current_user": user,
            "available_modules": MODULES,
            "has_permission": has_permission,
            "is_admin": bool(user and user.is_admin),
        }


def change_password(user_id, current_password, new_password, confirm_password):
    user = db.session.get(User, user_id)
    if not user or not check_password_hash(user.password_hash, current_password):
        return False, "Current password is incorrect."
    if new_password != confirm_password:
        return False, "New password and confirmation do not match."
    valid, message = validate_new_password(new_password, current_password)
    if not valid:
        return False, message
    user.password_hash = generate_password_hash(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    db.session.add(AuditLog(actor=user.username, action="Password Changed", detail="Admin password changed"))
    db.session.commit()
    flash("Password changed successfully.", "success")
    return True, "Password changed successfully."


def validate_new_password(new_password, current_password=None):
    minimum = int(current_app.config.get("MIN_PASSWORD_LENGTH", 10))
    if not new_password or len(new_password) < minimum:
        return False, f"New password must be at least {minimum} characters."
    if not any(character.isalpha() for character in new_password) or not any(character.isdigit() for character in new_password):
        return False, "New password must contain at least one letter and one number."
    if new_password.strip().lower() in {"12345", "password", "admin", "smartfill"}:
        return False, "Choose a less predictable password."
    if current_password is not None and new_password == current_password:
        return False, "New password must be different from the current password."
    return True, ""
