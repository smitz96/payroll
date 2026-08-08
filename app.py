from pathlib import Path

from flask import Flask
from flask_wtf import CSRFProtect

from attendance import db
from attendance.authentication import init_admin_user
from attendance.utils import format_ist_datetime
from config import Config

csrf = CSRFProtect()


def ensure_schema_columns():
    inspector = db.inspect(db.engine)
    tables = inspector.get_table_names()
    if "user" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("user")}
        if "active_session_token" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_token VARCHAR(64)"))
        if "active_session_started_at" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_started_at DATETIME"))
        if "active_session_last_seen_at" not in user_columns:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN active_session_last_seen_at DATETIME"))
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
        if "ot_enabled" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN ot_enabled BOOLEAN NOT NULL DEFAULT 1"))
        if "less_hours_exempt" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN less_hours_exempt BOOLEAN NOT NULL DEFAULT 0"))
        if "employment_status" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN employment_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE'"))
        if "inactive_at" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN inactive_at DATETIME"))
        if "inactive_reason" not in employee_columns:
            db.session.execute(db.text("ALTER TABLE employee ADD COLUMN inactive_reason TEXT"))
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
    if "payroll_month" in tables:
        payroll_month_columns = {column["name"] for column in inspector.get_columns("payroll_month")}
        if "encash_all_leaves" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN encash_all_leaves BOOLEAN NOT NULL DEFAULT 0"))
        if "attendance_submitted" not in payroll_month_columns:
            db.session.execute(db.text("ALTER TABLE payroll_month ADD COLUMN attendance_submitted BOOLEAN NOT NULL DEFAULT 0"))
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


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    app.jinja_env.filters["ist_datetime"] = format_ist_datetime
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    db.init_app(app)
    csrf.init_app(app)
    with app.app_context():
        from attendance import models  # noqa: F401
        db.create_all()
        ensure_schema_columns()
        init_admin_user()
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
    app.register_blueprint(weekoffs_bp)
    app.register_blueprint(logs_bp)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)
