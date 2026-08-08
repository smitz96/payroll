import os
import subprocess
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from attendance import db
from attendance.authentication import change_password, login_required
from attendance.models import AuditLog, User
from attendance.settings import MONTHLY_RULES

bp = Blueprint("settings", __name__, url_prefix="/settings")


def run_app_update(app_root):
    app_root = Path(app_root)
    update_script = app_root / "update.sh"
    if not update_script.exists():
        return False, "update.sh was not found on this server. Pull the latest code once from terminal, then try Update App again."
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.Popen(
            ["sudo", str(update_script)],
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
        flash("Admin password is incorrect. Update App was not started.", "danger")
        return redirect(url_for("settings.index"))

    ok, message = run_app_update(Path(current_app.root_path))
    db.session.add(AuditLog(actor=user.username, action="App Update" if ok else "App Update Failed", detail=message[:2000]))
    db.session.commit()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("settings.index"))
