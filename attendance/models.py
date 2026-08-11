from datetime import datetime
from decimal import Decimal

from attendance import db


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)
    active_session_token = db.Column(db.String(64))
    active_session_started_at = db.Column(db.DateTime)
    active_session_last_seen_at = db.Column(db.DateTime)
    # Brute-force protection state, persisted so a restart cannot clear a lockout.
    failed_login_count = db.Column(db.Integer, default=0, nullable=False)
    last_failed_login_at = db.Column(db.DateTime)
    locked_until = db.Column(db.DateTime)


class Employee(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    department = db.Column(db.String(160))
    designation = db.Column(db.String(160))
    salary_type = db.Column(db.String(80))
    normalized_salary_type = db.Column(db.String(80), index=True)
    # `salary` stays the figure payroll is calculated from. For monthly wage employees
    # it can optionally be broken up into the components below, which must add up to it.
    salary = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    basic_salary = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    hra = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    allowance = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    # Monthly wage only. Entered by hand, not derived: the amount to deduct as
    # tax at source each month.
    tds = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    pf_enabled = db.Column(db.Boolean, default=False, nullable=False)
    esic_enabled = db.Column(db.Boolean, default=False, nullable=False)
    # Both flags read the same way as the UI: ticked means "skip this rule for this
    # employee". `ot_ignored` replaced an inverted `ot_enabled` column.
    ot_ignored = db.Column(db.Boolean, default=False, nullable=False)
    less_hours_ignored = db.Column(db.Boolean, default=False, nullable=False)
    # Daily wage only: excludes the employee from the monthly attendance bonus.
    bonus_ignored = db.Column(db.Boolean, default=False, nullable=False)
    employment_status = db.Column(db.String(32), default="ACTIVE", nullable=False)
    inactive_at = db.Column(db.DateTime)
    inactive_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PayrollMonth(db.Model):
    month = db.Column(db.String(7), primary_key=True)
    # `status` stays the overall roll-up (FINALIZED only once every wage group with
    # employees is finalized); the per-group timestamps below are the source of truth.
    status = db.Column(db.String(32), default="DRAFT", nullable=False)
    encash_all_leaves = db.Column(db.Boolean, default=False, nullable=False)
    attendance_submitted = db.Column(db.Boolean, default=False, nullable=False)
    finalized_at = db.Column(db.DateTime)
    monthly_finalized_at = db.Column(db.DateTime)
    daily_finalized_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AttendanceRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month = db.Column(db.String(7), db.ForeignKey("payroll_month.month"), nullable=False, index=True)
    employee_id = db.Column(db.String(64), db.ForeignKey("employee.id"), nullable=False, index=True)
    employee_name = db.Column(db.String(160))
    department = db.Column(db.String(160))
    designation = db.Column(db.String(160))
    date = db.Column(db.Date, nullable=False)
    day = db.Column(db.String(32))
    shift = db.Column(db.String(80))
    shift_from = db.Column(db.String(32))
    shift_to = db.Column(db.String(32))
    first_punch = db.Column(db.String(32))
    last_punch = db.Column(db.String(32))
    punches_json = db.Column(db.JSON, default=list)
    raw_working_hours = db.Column(db.String(32))
    actual_minutes = db.Column(db.Integer)
    parse_status = db.Column(db.String(32), default="OK")
    warning = db.Column(db.Text)
    __table_args__ = (db.UniqueConstraint("payroll_month", "employee_id", "date", name="uq_attendance_month_employee_date"),)


class SalaryRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month = db.Column(db.String(7), db.ForeignKey("payroll_month.month"), nullable=False, index=True)
    employee_id = db.Column(db.String(64), db.ForeignKey("employee.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    salary_type = db.Column(db.String(80))
    normalized_salary_type = db.Column(db.String(80), index=True)
    salary = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    adjustment = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    loan = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    leave_encashment_enabled = db.Column(db.Boolean, default=False, nullable=False)
    leave_encashment_disabled = db.Column(db.Boolean, default=False, nullable=False)
    leave_encashment_days = db.Column(db.Numeric(8, 2), nullable=False, default=Decimal("0"))
    leave_encashment_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0"))
    warning = db.Column(db.Text)
    __table_args__ = (db.UniqueConstraint("payroll_month", "employee_id", name="uq_salary_month_employee"),)


class AttendanceOverride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month = db.Column(db.String(7), nullable=False, index=True)
    employee_id = db.Column(db.String(64), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    manual_status = db.Column(db.String(80), nullable=False)
    notes = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("payroll_month", "employee_id", "date", name="uq_override_month_employee_date"),)


class Holiday(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    holiday_type = db.Column(db.String(24), default="VARIABLE", nullable=False)
    notes = db.Column(db.Text)


class WeekOffRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(64), db.ForeignKey("employee.id"), unique=True, nullable=False, index=True)
    monday = db.Column(db.String(24), default="WORKING", nullable=False)
    tuesday = db.Column(db.String(24), default="WORKING", nullable=False)
    wednesday = db.Column(db.String(24), default="WORKING", nullable=False)
    thursday = db.Column(db.String(24), default="WORKING", nullable=False)
    friday = db.Column(db.String(24), default="WORKING", nullable=False)
    saturday = db.Column(db.String(24), default="WORKING", nullable=False)
    sunday = db.Column(db.String(24), default="WEEK_OFF_ALL", nullable=False)
    confirmed_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class LeaveLedger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(64), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    payroll_month = db.Column(db.String(7), nullable=False, index=True)
    transaction_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Numeric(8, 2), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(64), db.ForeignKey("employee.id"), nullable=False, index=True)
    start_date = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    monthly_deduction = db.Column(db.Numeric(12, 2), nullable=False)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class LoanInstallmentSkip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month = db.Column(db.String(7), nullable=False, index=True)
    employee_id = db.Column(db.String(64), nullable=False, index=True)
    skip = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("payroll_month", "employee_id", name="uq_loan_skip_month_employee"),)


class AdvanceSalary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(64), db.ForeignKey("employee.id"), nullable=False, index=True)
    advance_date = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PayrollResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_month = db.Column(db.String(7), nullable=False, index=True)
    employee_id = db.Column(db.String(64), nullable=False, index=True)
    payroll_rule_type = db.Column(db.String(80))
    calculation_status = db.Column(db.String(80), nullable=False)
    message = db.Column(db.Text)
    full_days = db.Column(db.Numeric(8, 2), default=0)
    half_days = db.Column(db.Numeric(8, 2), default=0)
    paid_working_days = db.Column(db.Numeric(8, 2), default=0)
    week_offs = db.Column(db.Integer, default=0)
    holidays = db.Column(db.Integer, default=0)
    paid_leaves = db.Column(db.Numeric(8, 2), default=0)
    lop_days = db.Column(db.Numeric(8, 2), default=0)
    opening_leave = db.Column(db.Numeric(8, 2), default=0)
    leave_earned = db.Column(db.Numeric(8, 2), default=0)
    leave_used = db.Column(db.Numeric(8, 2), default=0)
    closing_leave = db.Column(db.Numeric(8, 2), default=0)
    actual_working_minutes = db.Column(db.Integer, default=0)
    less_hours_minutes = db.Column(db.Integer, default=0)
    less_hours_deduction = db.Column(db.Numeric(12, 2), default=0)
    ot_minutes = db.Column(db.Integer, default=0)
    payable_ot_minutes = db.Column(db.Integer, default=0)
    ot_amount = db.Column(db.Numeric(12, 2), default=0)
    # Daily wage attendance bonus. Absence is the month's total shortfall against a
    # full working day, including late reporting and early leaving.
    absence_minutes = db.Column(db.Integer, default=0)
    attendance_bonus_percent = db.Column(db.Numeric(5, 2), default=0)
    attendance_bonus_amount = db.Column(db.Numeric(12, 2), default=0)
    # Statutory contributions. The employee shares reduce take-home pay; the employer
    # shares are a company cost and never touch the payable salary.
    pf_wage = db.Column(db.Numeric(12, 2), default=0)
    pf_employee = db.Column(db.Numeric(12, 2), default=0)
    pf_employer = db.Column(db.Numeric(12, 2), default=0)
    pf_pension = db.Column(db.Numeric(12, 2), default=0)
    pf_edli = db.Column(db.Numeric(12, 2), default=0)
    pf_admin = db.Column(db.Numeric(12, 2), default=0)
    esi_wage = db.Column(db.Numeric(12, 2), default=0)
    esi_employee = db.Column(db.Numeric(12, 2), default=0)
    esi_employer = db.Column(db.Numeric(12, 2), default=0)
    professional_tax = db.Column(db.Numeric(12, 2), default=0)
    tds = db.Column(db.Numeric(12, 2), default=0)
    lop_deduction = db.Column(db.Numeric(12, 2), default=0)
    manual_adjustment = db.Column(db.Numeric(12, 2), default=0)
    leave_encashment_days = db.Column(db.Numeric(8, 2), default=0)
    leave_encashment_amount = db.Column(db.Numeric(12, 2), default=0)
    loan_deduction = db.Column(db.Numeric(12, 2), default=0)
    loan_pending_amount = db.Column(db.Numeric(12, 2), default=0)
    advance_deduction = db.Column(db.Numeric(12, 2), default=0)
    total_deduction = db.Column(db.Numeric(12, 2), default=0)
    total_addition = db.Column(db.Numeric(12, 2), default=0)
    final_salary = db.Column(db.Numeric(12, 2))
    detail_json = db.Column(db.JSON, default=list)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("payroll_month", "employee_id", name="uq_result_month_employee"),)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(80))
    action = db.Column(db.String(120), nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
