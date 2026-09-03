import os
import shutil
import subprocess
from datetime import datetime
from tempfile import NamedTemporaryFile
from pathlib import Path

from flask import Blueprint, after_this_request, current_app, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from attendance import db
from attendance.authentication import MODULES, change_password, current_username, login_required, require_permission, validate_new_password
from attendance.backups import build_full_backup_archive, restore_full_backup_archive
from attendance.models import AuditLog, User
from attendance.settings import daily_bonus_rule_rows, leave_rule_rows, monthly_rule_rows
from attendance.statutory import statutory_rule_rows
from attendance.utils import format_ist_datetime

bp = Blueprint("settings", __name__, url_prefix="/settings")
APP_VERSION = "V1.02"
RESET_CONFIRMATION_TEXT = "permanently delete"
RESTORE_CONFIRMATION_TEXT = "restore backup"


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


def current_admin_user():
    return db.session.get(User, session.get("user_id"))


def admin_password_matches(user):
    password = request.form.get("admin_password", "")
    return bool(user and check_password_hash(user.password_hash, password))


def selected_permissions():
    allowed = {key for key, _label, _description in MODULES}
    return sorted({value for value in request.form.getlist("permissions") if value in allowed})


@bp.route("")
@login_required
def index():
    require_permission("settings")
    about = {
        "version": APP_VERSION,
        "release_at": latest_git_release_datetime(Path(current_app.root_path)) or "Not available",
    }
    return render_template(
        "settings.html",
        monthly_rules=monthly_rule_rows(),
        leave_rules=leave_rule_rows(),
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
    require_permission("settings")
    user = current_admin_user()
    if not admin_password_matches(user):
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
    require_permission("settings")
    # This is the most destructive action in the app, so it is gated the same way
    # as finalize/unlock/server-update: typed confirmation *and* the admin password.
    user = current_admin_user()
    if not admin_password_matches(user):
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


@bp.route("/backup/download")
@login_required
def download_backup():
    require_permission("settings")
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    with NamedTemporaryFile(prefix="smartfill-backup-", suffix=".zip", delete=False) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        build_full_backup_archive(temp_path)

        @after_this_request
        def cleanup_backup_file(response):
            try:
                temp_path.unlink()
            except OSError:
                pass
            return response

        return send_file(
            temp_path,
            as_attachment=True,
            download_name=f"smartfill-full-backup-{timestamp}.zip",
            mimetype="application/zip",
            max_age=0,
        )
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


@bp.route("/backup/restore", methods=["POST"])
@login_required
def restore_backup():
    require_permission("settings")
    user = current_admin_user()
    if not admin_password_matches(user):
        flash("Admin password is incorrect. Backup was not restored.", "danger")
        return redirect(url_for("settings.index"))

    confirmation = request.form.get("restore_confirmation", "").strip().lower()
    if confirmation != RESTORE_CONFIRMATION_TEXT:
        flash('Type "restore backup" to replace this server data from a backup.', "danger")
        return redirect(url_for("settings.index"))

    upload = request.files.get("backup_zip")
    if not upload or not upload.filename:
        flash("Select a SMARTfill backup ZIP file to restore.", "danger")
        return redirect(url_for("settings.index"))
    if Path(upload.filename).suffix.lower() != ".zip":
        flash("Only SMARTfill backup ZIP files can be restored.", "danger")
        return redirect(url_for("settings.index"))

    username = user.username
    filename = secure_filename(upload.filename)
    with NamedTemporaryFile(prefix="smartfill-restore-", suffix=".zip", delete=False) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        upload.save(temp_path)
        manifest = restore_full_backup_archive(temp_path)
        db.session.add(AuditLog(
            actor=username,
            action="Backup Restored",
            detail=(
                f"{filename}; created {manifest.get('created_at', 'unknown')}; "
                f"format version {manifest.get('format_version', 'unknown')}"
            )[:2000],
        ))
        db.session.commit()
        flash("Backup restored successfully. Sign in again if your session was from the old server data.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass
    return redirect(url_for("settings.index"))


@bp.route("/users", methods=["GET", "POST"])
@login_required
def users():
    require_permission("settings")
    if request.method == "POST":
        action = request.form.get("action", "create")
        try:
            if action == "create":
                username = request.form.get("username", "").strip()
                password = request.form.get("new_password", "")
                confirm = request.form.get("confirm_password", "")
                if not username:
                    raise ValueError("Username is required.")
                if User.query.filter_by(username=username).first():
                    raise ValueError("Username already exists.")
                if password != confirm:
                    raise ValueError("Password and confirmation do not match.")
                valid, message = validate_new_password(password)
                if not valid:
                    raise ValueError(message)
                user = User(
                    username=username,
                    password_hash=generate_password_hash(password),
                    is_active=request.form.get("is_active") == "on",
                    is_admin=False,
                    permissions_json=selected_permissions(),
                )
                db.session.add(user)
                db.session.add(AuditLog(
                    actor=current_username(),
                    action="User Created",
                    detail=f"{username}; permissions: {', '.join(user.permissions_json or []) or 'none'}",
                ))
                flash("User created successfully.", "success")
            elif action == "update":
                user = db.session.get(User, int(request.form.get("user_id")))
                if not user:
                    raise ValueError("User was not found.")
                if user.is_admin:
                    raise ValueError("Admin permissions cannot be changed here.")
                user.is_active = request.form.get("is_active") == "on"
                user.permissions_json = selected_permissions()
                db.session.add(AuditLog(
                    actor=current_username(),
                    action="User Permissions Updated",
                    detail=f"{user.username}; active: {'Yes' if user.is_active else 'No'}; permissions: {', '.join(user.permissions_json or []) or 'none'}",
                ))
                flash("User permissions updated.", "success")
            elif action == "reset_password":
                user = db.session.get(User, int(request.form.get("user_id")))
                password = request.form.get("new_password", "")
                confirm = request.form.get("confirm_password", "")
                if not user:
                    raise ValueError("User was not found.")
                if user.is_admin and user.id != session.get("user_id"):
                    raise ValueError("Use Change password for the admin account.")
                if password != confirm:
                    raise ValueError("Password and confirmation do not match.")
                valid, message = validate_new_password(password)
                if not valid:
                    raise ValueError(message)
                user.password_hash = generate_password_hash(password)
                user.failed_login_count = 0
                user.locked_until = None
                db.session.add(AuditLog(actor=current_username(), action="User Password Reset", detail=f"{user.username}"))
                flash("User password reset.", "success")
            else:
                raise ValueError("Unknown user management action.")
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("settings.users"))
    user_rows = User.query.order_by(User.is_admin.desc(), User.username).all()
    return render_template("users.html", users=user_rows, modules=MODULES)
