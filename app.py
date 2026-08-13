from pathlib import Path

from flask import Flask, flash, redirect, render_template, url_for
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

from attendance import db
from attendance.authentication import init_admin_user
from attendance.employee_defaults import backfill_default_weekoffs
from attendance.utils import format_ist_datetime, format_percent, money_text
from config import Config

csrf = CSRFProtect()


def ensure_schema_columns():
    inspector = db.inspect(db.engine)
    tables = inspector.get_table_names()
    stale_employee_columns = []
    if "user" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("user")}
        if "active_session_token" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_token VARCHAR(64)"))
        if "active_session_started_at" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_started_at DATETIME"))
        if "active_session_last_seen_at" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_last_seen_at DATETIME"))
        if "failed_login_count" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0"))
        if "last_failed_login_at" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_failed_login_at DATETIME"))
        if "locked_until" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN locked_until DATETIME"))
    if "salary_record" in tables:
        salary_columns = {column["name"] for column in inspector.get_columns("salary_record")}
        if "loan" not in salary_columns:
            db.session.execute(db.text("ALTER TABLE salary_record ADD COLUMN loan NUMERIC(12, 2) NOT NULL DEFAULT 0"))
        if "leave_encashment_enabled" not in salary_columns:
            db.session.execute(db.text("ALTER TABLE salary_record ADD COLUMN leave_encashment_enabled BOOLEAN NOT NULL DEFAULT 0"))
        if "leave_encashment_disabled" not in salary_columns:
            db.session.execute(db.text("ALTER TABLE salary_record ADD COLUMN leave_encashment_disabled BOOLEAN NOT NULL DEFAULT 0"))
        if "leave_encashment_days" not in salary_columns:
            db.session.execute(db.text("ALTER TABLE salary_record ADD COLUMN leave_encashment_days NUMERIC(8, 1) NOT NULL DEFAULT 0"))
        if "leave_encashment_amount" not in salary_columns:
            db.session.execute(db.text("ALTER TABLE salary_record ADD COLUMN leave_encashment_amount NUMERIC(12, 2) NOT NULL DEFAULT 0"))
    if "employee" in tables:
        employee_columns = {column["name"] for column in inspector.get_columns("employee")}
        if "salary_type" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN salary_type VARCHAR(80)"))
        if "normalized_salary_type" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN normalized_salary_type VARCHAR(80)"))
        if "salary" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN salary NUMERIC(12, 2) NOT NULL DEFAULT 0"))
        # Left blank for anyone already marked Left or Terminated: nobody recorded a
        # last working day at the time, so payroll keeps leaving them out until one is
        # entered, and the payroll month page says who is being left out.
        if "left_on" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN left_on DATE"))
        # `ot_enabled` was inverted into `ot_ignored`, and `less_hours_exempt` renamed
        # to `less_hours_ignored`, so both columns now read the same way as the form.
        if "ot_ignored" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN ot_ignored BOOLEAN NOT NULL DEFAULT 0"))
            if "ot_enabled" in employee_columns:
                db.session.execute(db.text(
                    "UPDATE employee SET ot_ignored = CASE WHEN ot_enabled = 0 THEN 1 ELSE 0 END"
                ))
        if "less_hours_ignored" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN less_hours_ignored BOOLEAN NOT NULL DEFAULT 0"))
            if "less_hours_exempt" in employee_columns:
                db.session.execute(db.text("UPDATE employee SET less_hours_ignored = less_hours_exempt"))
        # Conveyance Allowance was the same thing as Allowance, so fold any captured
        # value into it before dropping the column; otherwise a stored breakup would
        # stop adding up to the salary.
        if "conveyance_allowance" in employee_columns:
            db.session.execute(db.text(
                "UPDATE employee SET allowance = COALESCE(allowance, 0) + COALESCE(conveyance_allowance, 0)"
            ))
        stale_employee_columns = [
            name for name in ("ot_enabled", "less_hours_exempt", "conveyance_allowance")
            if name in employee_columns
        ]
        if "employment_status" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN employment_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE'"))
        if "inactive_at" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN inactive_at DATETIME"))
        if "inactive_reason" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN inactive_reason TEXT"))
        for column, definition in (
            ("basic_salary", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("hra", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("allowance", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
            ("pf_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("esic_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            # Daily wage attendance bonus opt-out. Defaulting to 0 keeps every existing
            # daily employee in the bonus, which is how it worked before the flag.
            ("bonus_ignored", "BOOLEAN NOT NULL DEFAULT 0"),
            ("tds", "NUMERIC(12, 2) NOT NULL DEFAULT 0"),
        ):
            if column not in employee_columns:
                db.session.execute(db.text(f"ALTER TABLE employee ADD COLUMN {column} {definition}"))
    if "payroll_result" in tables:
        payroll_columns = {column["name"] for column in inspector.get_columns("payroll_result")}
        if "loan_deduction" not in payroll_columns:
            db.session.execute(db.text("ALTER TABLE payroll_result ADD COLUMN loan_deduction NUMERIC(12, 2) DEFAULT 0"))
        if "loan_pending_amount" not in payroll_columns:
            db.session.execute(db.text("ALTER TABLE payroll_result ADD COLUMN loan_pending_amount NUMERIC(12, 2) DEFAULT 0"))
        if "leave_encashment_amount" not in payroll_columns:
            db.session.execute(db.text("ALTER TABLE payroll_result ADD COLUMN leave_encashment_amount NUMERIC(12, 2) DEFAULT 0"))
        if "leave_encashment_days" not in payroll_columns:
            db.session.execute(db.text("ALTER TABLE payroll_result ADD COLUMN leave_encashment_days NUMERIC(8, 1) DEFAULT 0"))
        if "advance_deduction" not in payroll_columns:
            db.session.execute(db.text("ALTER TABLE payroll_result ADD COLUMN advance_deduction NUMERIC(12, 2) DEFAULT 0"))
        # Daily wage attendance bonus. Existing rows keep 0, which is correct: months
        # calculated before the bonus existed did not pay one.
        # Statutory PF and ESI. Existing rows keep 0, which is correct: months
        # calculated before these existed did not deduct them.
        for column, definition in (
            ("absence_minutes", "INTEGER DEFAULT 0"),
            ("attendance_bonus_percent", "NUMERIC(5, 2) DEFAULT 0"),
            ("attendance_bonus_amount", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_wage", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_employee", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_employer", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_pension", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_edli", "NUMERIC(12, 2) DEFAULT 0"),
            ("pf_admin", "NUMERIC(12, 2) DEFAULT 0"),
            ("esi_wage", "NUMERIC(12, 2) DEFAULT 0"),
            ("esi_employee", "NUMERIC(12, 2) DEFAULT 0"),
            ("esi_employer", "NUMERIC(12, 2) DEFAULT 0"),
            ("professional_tax", "NUMERIC(12, 2) DEFAULT 0"),
            ("tds", "NUMERIC(12, 2) DEFAULT 0"),
        ):
            if column not in payroll_columns:
                db.session.execute(db.text(f"ALTER TABLE payroll_result ADD COLUMN {column} {definition}"))
    if "payroll_month" in tables:
        payroll_month_columns = {column["name"] for column in inspector.get_columns("payroll_month")}
        if "encash_all_leaves" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN encash_all_leaves BOOLEAN NOT NULL DEFAULT 0"))
        if "attendance_submitted" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN attendance_submitted BOOLEAN NOT NULL DEFAULT 0"))
        if "monthly_finalized_at" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN monthly_finalized_at DATETIME"))
            # Months finalized before per-wage-type locking existed were finalized as a
            # whole, so backfill both groups from the old single timestamp.
            db.session.execute(db.text(
                "UPDATE payroll_month SET monthly_finalized_at = finalized_at WHERE status = 'FINALIZED'"
            ))
        if "daily_finalized_at" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN daily_finalized_at DATETIME"))
            db.session.execute(db.text(
                "UPDATE payroll_month SET daily_finalized_at = finalized_at WHERE status = 'FINALIZED'"
            ))
    if "attendance_record" in tables:
        attendance_columns = {column["name"] for column in inspector.get_columns("attendance_record")}
        if "punches_json" not in attendance_columns:
            db.session.execute(db.text("ALTER TABLE attendance_record ADD COLUMN punches_json JSON"))
    if "week_off_rule" in tables:
        weekoff_columns = {column["name"] for column in inspector.get_columns("week_off_rule")}
        if "confirmed_at" not in weekoff_columns:
            db.session.execute(db.text("ALTER TABLE week_off_rule ADD COLUMN confirmed_at DATETIME"))
    if "holiday" in tables:
        holiday_columns = {column["name"] for column in inspector.get_columns("holiday")}
        if "holiday_type" not in holiday_columns:
            db.session.execute(db.text("ALTER TABLE holiday ADD COLUMN holiday_type VARCHAR(24) NOT NULL DEFAULT 'VARIABLE'"))
    db.session.commit()

    # Superseded columns are dropped only after the data has been migrated and
    # committed, each in its own transaction: DROP COLUMN needs SQLite 3.35+, and a
    # failure here must not roll back the migration above.
    for stale in stale_employee_columns:
        try:
            db.session.execute(db.text(f"ALTER TABLE employee DROP COLUMN {stale}"))
            db.session.commit()
        except Exception:
            db.session.rollback()


ERROR_PAGES = {
    403: ("Not allowed", "You do not have access to this page."),
    404: ("Page not found", "The page you asked for does not exist, or the payroll month is not a valid YYYY-MM value."),
    405: ("Not allowed here", "That action is not available on this page."),
    413: ("Upload too large", "The file is larger than the 16 MB upload limit."),
    500: ("Something went wrong", "The action could not be completed. The error has been logged; try again, and check Activity Logs if it keeps happening."),
}


def register_error_pages(app):
    """Serve the branded error page instead of the default Werkzeug text."""
    def render_error(code):
        title, message = ERROR_PAGES[code]
        return render_template("error.html", code=code, title=title, message=message), code

    for code in ERROR_PAGES:
        app.register_error_handler(code, lambda error, code=code: render_error(code))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        # A stale form after a session timeout is the common cause, so send the
        # user somewhere they can act rather than showing a raw 400.
        flash("Your form expired. Please sign in and try again.", "warning")
        return redirect(url_for("auth.login"))


def register_static_versioning(app):
    """Stamp static URLs with the file's mtime.

    Flask serves static files with a long max-age, so a CSS or JS change would
    otherwise keep showing the browser's cached copy after a server update.
    """
    def versioned_url_for(endpoint, **values):
        if endpoint == "static" and "filename" in values and "v" not in values:
            file_path = Path(app.static_folder) / values["filename"]
            try:
                values["v"] = int(file_path.stat().st_mtime)
            except OSError:
                pass
        return url_for(endpoint, **values)

    app.jinja_env.globals["url_for"] = versioned_url_for


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    app.jinja_env.filters["ist_datetime"] = format_ist_datetime
    app.jinja_env.filters["percent"] = format_percent
    app.jinja_env.filters["money"] = money_text
    register_static_versioning(app)
    register_error_pages(app)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    db.init_app(app)
    csrf.init_app(app)
    with app.app_context():
        from attendance import models  # noqa: F401
        db.create_all()
        ensure_schema_columns()
        init_admin_user()
        backfill_default_weekoffs()
        db.session.commit()
    from routes.auth import bp as auth_bp
    from routes.advances import bp as advances_bp
    from routes.attendance_manager import bp as attendance_manager_bp
    from routes.dashboard import bp as dashboard_bp
    from routes.payroll import bp as payroll_bp
    from routes.holidays import bp as holidays_bp
    from routes.leave_balances import bp as leave_balances_bp
    from routes.loans import bp as loans_bp
    from routes.master import bp as master_bp
    from routes.settings import bp as settings_bp
    from routes.reports import bp as reports_bp
    from routes.salary_slips import bp as salary_slips_bp
    from routes.weekoffs import bp as weekoffs_bp
    from routes.logs import bp as logs_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(advances_bp)
    app.register_blueprint(attendance_manager_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(holidays_bp)
    app.register_blueprint(leave_balances_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(master_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(salary_slips_bp)
    app.register_blueprint(weekoffs_bp)
    app.register_blueprint(logs_bp)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)
