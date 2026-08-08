import os
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from attendance import db
from attendance.authentication import change_password, login_required
from attendance.models import AuditLog, User
from attendance.settings import MONTHLY_RULES

bp = Blueprint("settings", __name__, url_prefix="/settings")
UPDATE_TIMEOUT_SECONDS = 120


def command_output(result):
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    return output[-4000:] if output else ""


def run_command(command, app_root):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(command, cwd=app_root, text=True, capture_output=True, timeout=UPDATE_TIMEOUT_SECONDS, env=env)


def run_git_update(app_root):
    app_root = Path(app_root)
    if not (app_root / ".git").exists():
        return False, "Git repository was not found on this server. Clone the app from GitHub before using Git Pull Update."
    try:
        status = run_command(["git", "status", "--porcelain"], app_root)
        if status.returncode != 0:
            return False, "Git status failed.\n" + command_output(status)
        if status.stdout.strip():
            return False, "Local server changes exist. Commit or stash them before pulling updates from GitHub."

        pull = run_command(["git", "pull", "--ff-only"], app_root)
        if pull.returncode != 0:
            return False, "Git pull failed. For a private repository, configure SSH deploy key or GitHub token on this server.\n" + command_output(pull)

        init_db = run_command([sys.executable, "scripts/init_db.py"], app_root)
        if init_db.returncode != 0:
            return False, "Git pull completed, but database initialization failed.\n" + command_output(init_db)

        output = command_output(pull) or "Already up to date."
        return True, "Git pull completed. Restart the service if Python or template files changed.\n" + output
    except subprocess.TimeoutExpired:
        return False, "Git update timed out. Check GitHub authentication and network access on the server."


@bp.route("")
@login_required
def index():
    return render_template("settings.html", monthly_rules=MONTHLY_RULES)


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
        flash("Admin password is incorrect. Git Pull Update was not started.", "danger")
        return redirect(url_for("settings.index"))

    ok, message = run_git_update(Path(current_app.root_path))
    db.session.add(AuditLog(actor=user.username, action="Git Pull Update" if ok else "Git Pull Update Failed", detail=message[:2000]))
    db.session.commit()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("settings.index"))
