from datetime import date
from decimal import Decimal

from attendance import db
from attendance.models import (
    AttendanceOverride,
    AttendanceRecord,
    AuditLog,
    Employee,
    LeaveLedger,
    PayrollMonth,
    PayrollResult,
    SalaryRecord,
    WeekOffRule,
)
from attendance.holidays import holiday_dates_for_records
from attendance.master import employee_active_for_payroll_month
from attendance.payroll_rules import UnsupportedPayrollResult, resolve_payroll_rule
from attendance.wage_groups import GROUP_LABELS, finalized_groups, normalize_group


def opening_leave_for(employee_id, month):
    from attendance.leave_balances import format_leave, manual_adjustments_after

    previous = (
        PayrollResult.query.filter(
            PayrollResult.employee_id == employee_id,
            PayrollResult.payroll_month < month,
            PayrollResult.payroll_rule_type == "MONTHLY",
            PayrollResult.calculation_status.in_(["Calculated", "Needs Review"]),
        )
        .order_by(PayrollResult.payroll_month.desc())
        .first()
    )
    opening = Decimal(previous.closing_leave) if previous else Decimal("0")
    for adjustment in manual_adjustments_after(employee_id, previous):
        opening += Decimal(adjustment.amount)
    return format_leave(opening)


def calculate_payroll_month(month, actor="admin", wage_group=None):
    """Recalculate the month.

    Finalized wage groups are never touched, so recalculating Daily cannot disturb a
    Monthly payroll that has already been signed off. Pass `wage_group` to restrict
    the run to a single group.
    """
    unconfirmed = first_payroll_employees_without_confirmed_weekoff(month)
    if unconfirmed:
        raise ValueError("Week off must be selected before first payroll for: " + ", ".join(unconfirmed))
    payroll_month = db.session.get(PayrollMonth, month)
    requested = normalize_group(wage_group)
    skipped_groups = set(finalized_groups(payroll_month))

    def in_scope(salary):
        group = normalize_group(salary.normalized_salary_type)
        if group and group in skipped_groups:
            return False
        if requested and group != requested:
            return False
        return True

    protected_ids = {
        salary.employee_id
        for salary in SalaryRecord.query.filter_by(payroll_month=month).all()
        if not in_scope(salary)
    }
    stale_results = PayrollResult.query.filter_by(payroll_month=month)
    stale_ledger = LeaveLedger.query.filter_by(payroll_month=month)
    if protected_ids:
        stale_results = stale_results.filter(PayrollResult.employee_id.notin_(protected_ids))
        stale_ledger = stale_ledger.filter(LeaveLedger.employee_id.notin_(protected_ids))
    stale_results.delete(synchronize_session=False)
    stale_ledger.delete(synchronize_session=False)
    db.session.flush()
    db.session.expunge_all()
    salary_records = [
        salary for salary in SalaryRecord.query.filter_by(payroll_month=month).order_by(SalaryRecord.employee_id).all()
        if in_scope(salary) and employee_active_for_payroll_month(db.session.get(Employee, salary.employee_id), month)
    ]
    attendance_by_employee = {}
    for record in AttendanceRecord.query.filter_by(payroll_month=month).all():
        attendance_by_employee.setdefault(record.employee_id, []).append(record)
    holidays = holiday_dates_for_records([record for records in attendance_by_employee.values() for record in records])
    overrides_by_employee = {}
    for override in AttendanceOverride.query.filter_by(payroll_month=month).all():
        overrides_by_employee.setdefault(override.employee_id, {})[override.date] = override
    results = []
    for salary in salary_records:
        result = calculate_salary_record_result(
            salary,
            attendance_by_employee.get(salary.employee_id, []),
            holidays,
            overrides_by_employee.get(salary.employee_id, {}),
        )
        db.session.add(result)
        results.append(result)
    db.session.flush()
    for result in results:
        write_leave_ledger_for_result(result)
    scope = GROUP_LABELS.get(requested, "All wage types") if requested else "All open wage types"
    db.session.add(AuditLog(actor=actor, action="Payroll Calculated", detail=f"{month}: {len(results)} employees; scope {scope}"))
    db.session.commit()
    return results


def calculate_salary_record_result(salary, attendance_records, holidays, overrides):
    rule = resolve_payroll_rule(salary.normalized_salary_type)
    if not rule:
        result = UnsupportedPayrollResult(salary).to_model()
    else:
        result = rule.calculate_employee_month(
            salary,
            attendance_records,
            opening_leave_for(salary.employee_id, salary.payroll_month),
            holidays,
            overrides,
        )
        if not attendance_records:
            result.calculation_status = "Needs Review"
            result.message = "No attendance data found."
    return result


def write_leave_ledger_for_result(result):
    if result.payroll_rule_type != "MONTHLY":
        return
    ledger_date = date.fromisoformat(result.payroll_month + "-01")
    db.session.add(LeaveLedger(employee_id=result.employee_id, date=ledger_date, payroll_month=result.payroll_month, transaction_type="OPENING", amount=result.opening_leave, description="Opening leave balance"))
    db.session.add(LeaveLedger(employee_id=result.employee_id, date=ledger_date, payroll_month=result.payroll_month, transaction_type="EARNED", amount=result.leave_earned, description="Leave earned this month"))
    if Decimal(result.leave_used or 0):
        db.session.add(LeaveLedger(employee_id=result.employee_id, date=ledger_date, payroll_month=result.payroll_month, transaction_type="USED", amount=result.leave_used, description="Leave used this month"))
    if Decimal(getattr(result, "leave_encashment_days", 0) or 0):
        db.session.add(LeaveLedger(employee_id=result.employee_id, date=ledger_date, payroll_month=result.payroll_month, transaction_type="ENCASHED", amount=result.leave_encashment_days, description="Leave encashed this month"))
    db.session.add(LeaveLedger(employee_id=result.employee_id, date=ledger_date, payroll_month=result.payroll_month, transaction_type="CARRY_FORWARD", amount=result.closing_leave, description="Closing leave carried forward"))


def calculate_employee_payroll(month, employee_id, actor="admin"):
    unconfirmed = first_payroll_employees_without_confirmed_weekoff(month, employee_id)
    if unconfirmed:
        raise ValueError("Week off must be selected before first payroll for: " + ", ".join(unconfirmed))
    PayrollResult.query.filter_by(payroll_month=month, employee_id=employee_id).delete()
    LeaveLedger.query.filter_by(payroll_month=month, employee_id=employee_id).delete()
    db.session.flush()
    db.session.expunge_all()
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    if not salary:
        raise ValueError(f"Salary data not present for Employee ID {employee_id}.")
    employee = db.session.get(Employee, employee_id)
    if not employee_active_for_payroll_month(employee, month):
        raise ValueError(f"Employee ID {employee_id} is inactive for payroll month {month}.")
    attendance_records = AttendanceRecord.query.filter_by(payroll_month=month, employee_id=employee_id).order_by(AttendanceRecord.date).all()
    holidays = holiday_dates_for_records(attendance_records)
    overrides = {o.date: o for o in AttendanceOverride.query.filter_by(payroll_month=month, employee_id=employee_id).all()}
    result = calculate_salary_record_result(salary, attendance_records, holidays, overrides)
    db.session.add(result)
    db.session.flush()
    write_leave_ledger_for_result(result)
    db.session.add(AuditLog(actor=actor, action="Employee Payroll Recalculated", detail=f"{month}: Employee ID {employee_id}"))
    db.session.commit()
    return result


def first_payroll_employees_without_confirmed_weekoff(month, employee_id=None):
    query = SalaryRecord.query.filter_by(payroll_month=month)
    if employee_id is not None:
        query = query.filter_by(employee_id=employee_id)
    missing = []
    for salary in query.order_by(SalaryRecord.employee_id).all():
        if not employee_active_for_payroll_month(db.session.get(Employee, salary.employee_id), month):
            continue
        prior_result = PayrollResult.query.filter(
            PayrollResult.employee_id == salary.employee_id,
            PayrollResult.payroll_month < month,
        ).first()
        current_result = PayrollResult.query.filter_by(payroll_month=month, employee_id=salary.employee_id).first()
        if prior_result or current_result:
            continue
        rule = WeekOffRule.query.filter_by(employee_id=salary.employee_id).first()
        if not rule or not rule.confirmed_at:
            missing.append(f"{salary.employee_id} - {salary.name}")
    return missing


def attendance_missing_salary(month):
    salary_ids = {s.employee_id for s in SalaryRecord.query.filter_by(payroll_month=month).all()}
    records = AttendanceRecord.query.filter_by(payroll_month=month).all()
    missing = {}
    for rec in records:
        employee = db.session.get(Employee, rec.employee_id)
        if employee and not employee_active_for_payroll_month(employee, month):
            continue
        if rec.employee_id not in salary_ids:
            missing[rec.employee_id] = rec.employee_name
    return missing


def name_mismatches(month):
    salary_by_id = {s.employee_id: s for s in SalaryRecord.query.filter_by(payroll_month=month).all()}
    mismatches = []
    seen = set()
    for rec in AttendanceRecord.query.filter_by(payroll_month=month).all():
        employee = db.session.get(Employee, rec.employee_id)
        if employee and not employee_active_for_payroll_month(employee, month):
            continue
        salary = salary_by_id.get(rec.employee_id)
        key = (rec.employee_id, rec.employee_name, salary.name if salary else "")
        if salary and rec.employee_name and salary.name and rec.employee_name.strip().lower() != salary.name.strip().lower() and key not in seen:
            seen.add(key)
            mismatches.append((rec.employee_id, rec.employee_name, salary.name))
    return mismatches
