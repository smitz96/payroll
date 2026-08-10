from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from attendance import db
from attendance.authentication import (
    PENDING_LOGIN_GRANTED_AT_KEY,
    PENDING_LOGIN_USER_KEY,
    SESSION_TOKEN_KEY,
    active_session_is_current,
    authenticate,
    clear_user_session,
    login_required,
    start_user_session,
)
from attendance.models import User

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("action") == "force_login":
            # The takeover is authorised by the password check that set this key, so
            # it must not stay valid indefinitely in an abandoned session.
            granted_at = session.get(PENDING_LOGIN_GRANTED_AT_KEY)
            window = int(current_app.config.get("LOGIN_TAKEOVER_WINDOW_SECONDS", 120))
            expired = not granted_at or (datetime.utcnow().timestamp() - float(granted_at)) > window
            user = db.session.get(User, session.get(PENDING_LOGIN_USER_KEY))
            if not user or expired:
                session.pop(PENDING_LOGIN_USER_KEY, None)
                session.pop(PENDING_LOGIN_GRANTED_AT_KEY, None)
                flash("Login confirmation expired. Please log in again.", "warning")
                return redirect(url_for("auth.login"))
            start_user_session(user, forced=True)
            return redirect(url_for("dashboard.index"))

        if request.form.get("action") == "cancel_force_login":
            session.pop(PENDING_LOGIN_USER_KEY, None)
            session.pop(PENDING_LOGIN_GRANTED_AT_KEY, None)
            flash("Login cancelled. Existing session remains active.", "info")
            return redirect(url_for("auth.login"))

        user, error = authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if user:
            if session.get("user_id") == user.id and session.get(SESSION_TOKEN_KEY) == user.active_session_token:
                return redirect(url_for("dashboard.index"))
            if active_session_is_current(user):
                session[PENDING_LOGIN_USER_KEY] = user.id
                session[PENDING_LOGIN_GRANTED_AT_KEY] = datetime.utcnow().timestamp()
                return render_template("login.html", active_session_user=user)
            start_user_session(user)
            return redirect(url_for("dashboard.index"))
        flash(error or "Invalid username or password.", "danger")
    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    user = db.session.get(User, session.get("user_id"))
    clear_user_session(user)
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
