import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from attendance import db
from attendance.authentication import change_password, login_required
from attendance.models import AuditLog, User
from attendance.settings import daily_bonus_rule_rows, monthly_rule_rows
from attendance.statutory import statutory_rule_rows
from attendance.utils import format_ist_datetime

bp = Blueprint("settings", __name__, url_prefix="/settings")
APP_VERSION = "V0.04"
RESET_CONFIRMATION_TEXT = "permanently delete"


def latest_git_release_datetime(app_root):
    app_root = Path(app_root)
    if not (app_root / ".git").exists():
        return ""
    git_path = shutil.which("git") or "/usr/bin/git"
    if not Path(git_path).exists():
        return ""
    try:
        result = subprocess.run(
            [git_path, "log", "-1", "--format=%cI"],
            cwd=app_root,
            text=True,
            capture_output=True,
            timeout=5,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if not value:
        return ""
    try:
        return format_ist_datetime(datetime.fromisoformat(value))
    except ValueError:
        return ""


def run_app_update(app_root):
    app_root = Path(app_root)
    update_script = app_root / "update.sh"
    if not update_script.exists():
        return False, "update.sh was not found on this server. Pull the latest code once from terminal, then try Update App again."
    sudo_path = shutil.which("sudo") or "/usr/bin/sudo"
    if not Path(sudo_path).exists():
        return False, "sudo was not found on this server. Install sudo or run updates manually from terminal."
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.Popen(
            [sudo_path, str(update_script)],
            cwd=app_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        return True, "App update started. The service may restart in a few moments. Check output/server-update.log on the server if needed."
    except OSError as exc:
        return False, f"App update could not be started: {exc}"


def reset_application_data():
    deleted = {}
    for table in reversed(db.metadata.sorted_tables):
        if table.name == User.__tablename__:
            continue
        result = db.session.execute(table.delete())
        deleted[table.name] = result.rowcount or 0
    db.session.commit()
    return deleted


@bp.route("")
@login_required
def index():
    about = {
        "version": APP_VERSION,
        "release_at": latest_git_release_datetime(Path(current_app.root_path)) or "Not available",
    }
    return render_template(
        "settings.html",
        monthly_rules=monthly_rule_rows(),
        daily_bonus_rules=daily_bonus_rule_rows(),
        statutory_rules=statutory_rule_rows(),
        about=about,
    )


@bp.route("/security", methods=["GET", "POST"])
@login_required
def security():
    if request.method == "POST":
        ok, message = change_password(session["user_id"], request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("confirm_password", ""))
        if not ok:
            flash(message, "danger")
        return redirect(url_for("settings.security"))
    return render_template("security.html")


@bp.route("/git-pull", methods=["POST"])
@login_required
def git_pull():
    user = db.session.get(User, session["user_id"])
    password = request.form.get("admin_password", "")
    if not user or not check_password_hash(user.password_hash, password):
        flash("Admin password is incorrect. Update App was not started.", "danger")
        return redirect(url_for("settings.index"))

    ok, message = run_app_update(Path(current_app.root_path))
    db.session.add(AuditLog(actor=user.username, action="App Update" if ok else "App Update Failed", detail=message[:2000]))
    db.session.commit()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("settings.index"))


@bp.route("/reset-data", methods=["POST"])
@login_required
def reset_data():
    # This is the most destructive action in the app, so it is gated the same way
    # as finalize/unlock/server-update: typed confirmation *and* the admin password.
    user = db.session.get(User, session.get("user_id"))
    password = request.form.get("admin_password", "")
    if not user or not check_password_hash(user.password_hash, password):
        flash("Admin password is incorrect. No data was reset.", "danger")
        return redirect(url_for("settings.index"))

    confirmation = request.form.get("reset_confirmation", "").strip().lower()
    if confirmation != RESET_CONFIRMATION_TEXT:
        flash('Type "permanently delete" to reset all app data.', "danger")
        return redirect(url_for("settings.index"))

    deleted = reset_application_data()
    deleted_rows = sum(deleted.values())
    # The audit log is one of the tables just cleared, so record the reset itself
    # afterwards; otherwise there is no trace of who wiped the database.
    db.session.add(AuditLog(
        actor=user.username,
        action="All Data Reset",
        detail=f"{deleted_rows} row(s) deleted across {len(deleted)} table(s). Admin login kept.",
    ))
    db.session.commit()
    flash(f"All app data has been reset. {deleted_rows} row(s) deleted. Admin login was kept.", "success")
    return redirect(url_for("settings.index"))
