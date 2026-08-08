import calendar
from decimal import Decimal

from flask import Blueprint, render_template, request

from attendance.authentication import login_required
from attendance.calculator import attendance_missing_salary
from attendance.models import AuditLog, AttendanceRecord, PayrollMonth, PayrollResult, SalaryRecord, WeekOffRule

bp = Blueprint("dashboard", __name__)


def money(value):
    return f"{Decimal(value or 0):,.2f}"


def display_month(month):
    if not month:
        return "Not started"
    try:
        year, month_number = (int(part) for part in month.split("-"))
    except ValueError:
        return month
    return f"{calendar.month_name[month_number]} {year}"


def month_snapshot(month):
    salaries = SalaryRecord.query.filter_by(payroll_month=month.month).all()
    results = PayrollResult.query.filter_by(payroll_month=month.month).all()
    calculated = [r for r in results if r.final_salary is not None and r.calculation_status in {"Calculated", "Needs Review"}]
    review_count = len([r for r in results if r.calculation_status != "Calculated"]) + len(attendance_missing_salary(month.month))
    return {
        "month": month,
        "display_month": display_month(month.month),
        "salary_count": len(salaries),
        "attendance_count": AttendanceRecord.query.filter_by(payroll_month=month.month).count(),
        "processed_count": len(calculated),
        "review_count": review_count,
        "total_payable": money(sum((Decimal(r.final_salary) for r in calculated), Decimal("0"))),
    }


@bp.route("/")
@login_required
def index():
    months = PayrollMonth.query.order_by(PayrollMonth.month.desc()).all()
    month = months[0] if months else None
    requested_month = request.args.get("month", "").strip()
    if requested_month:
        month = db_month = PayrollMonth.query.filter_by(month=requested_month).first()
        if not db_month:
            month = months[0] if months else None
    selected = month.month if month else None
    salaries = SalaryRecord.query.filter_by(payroll_month=selected).all() if selected else []
    results = PayrollResult.query.filter_by(payroll_month=selected).all() if selected else []
    attendance_count = AttendanceRecord.query.filter_by(payroll_month=selected).count() if selected else 0
    monthly = [s for s in salaries if s.normalized_salary_type == "MONTHLY"]
    unsupported = [s for s in salaries if s.normalized_salary_type != "MONTHLY"]
    calculated = [r for r in results if r.final_salary is not None and r.calculation_status in {"Calculated", "Needs Review"}]
    missing_salary = attendance_missing_salary(selected) if selected else {}
    expected_employee_count = max(len(salaries) + len(missing_salary), len(salaries), len(results))
    unconfirmed_weekoff = []
    for salary in salaries:
        rule = WeekOffRule.query.filter_by(employee_id=salary.employee_id).first()
        if not rule or not rule.confirmed_at:
            unconfirmed_weekoff.append(salary)
    review_results = [r for r in results if r.calculation_status != "Calculated"]
    review_items = []
    for employee_id, name in list(missing_salary.items())[:5]:
        review_items.append({"tone": "warning", "label": f"{employee_id} - {name}", "detail": "Attendance exists but salary data is missing."})
    for salary in unconfirmed_weekoff[:5]:
        review_items.append({"tone": "warning", "label": f"{salary.employee_id} - {salary.name}", "detail": "Week off must be confirmed before first payroll."})
    for result in review_results[:5]:
        review_items.append({"tone": "danger", "label": f"{result.employee_id}", "detail": result.message or result.calculation_status})
    payroll_health = {
        "selected": selected or "Not started",
        "selected_label": display_month(selected) if selected else "Not started",
        "status": month.status if month else "DRAFT",
        "is_finalized": bool(month and month.status == "FINALIZED"),
        "completion": int(round((len(calculated) / expected_employee_count) * 100)) if expected_employee_count else 0,
        "review_count": len(review_results) + len(missing_salary) + len(unconfirmed_weekoff),
        "expected_employee_count": expected_employee_count,
    }
    status_counts = {}
    for result in results:
        status_counts[result.calculation_status] = status_counts.get(result.calculation_status, 0) + 1
    if salaries and not results:
        status_counts["Not Calculated"] = len(salaries)
    salary_by_employee = {salary.employee_id: salary for salary in salaries}
    top_payables = []
    for result in sorted(calculated, key=lambda item: Decimal(item.final_salary or 0), reverse=True)[:5]:
        salary = salary_by_employee.get(result.employee_id)
        top_payables.append({
            "employee_id": result.employee_id,
            "name": salary.name if salary else result.employee_id,
            "final_salary": money(result.final_salary),
            "paid_days": result.paid_working_days,
            "deduction": money(result.total_deduction),
        })
    payroll_steps = [
        {"label": "Attendance Uploaded", "done": attendance_count > 0, "detail": f"{attendance_count} rows", "target": "payroll"},
        {"label": "Salary Uploaded", "done": len(salaries) > 0, "detail": f"{len(salaries)} employees", "target": "payroll"},
        {"label": "Week Off Confirmed", "done": not unconfirmed_weekoff and len(salaries) > 0, "detail": f"{len(unconfirmed_weekoff)} pending", "target": "weekoffs"},
        {"label": "Payroll Calculated", "done": expected_employee_count > 0 and len(calculated) == expected_employee_count, "detail": f"{len(calculated)} of {expected_employee_count} processed", "target": "payroll"},
        {"label": "Review Cleared", "done": payroll_health["review_count"] == 0 and len(salaries) > 0, "detail": f"{payroll_health['review_count']} item(s)", "target": "errors"},
        {"label": "Payroll Finalized", "done": payroll_health["is_finalized"], "detail": payroll_health["status"], "target": "payroll"},
    ]
    cards = [
        {"label": "Salary Employees", "value": len(salaries), "detail": f"{len(monthly)} monthly, {len(unsupported)} unsupported"},
        {"label": "Attendance Rows", "value": attendance_count, "detail": "Imported attendance records"},
        {"label": "Employees Processed", "value": len(calculated), "detail": f"{payroll_health['completion']}% complete"},
        {"label": "Total Base Salary", "value": money(sum((Decimal(s.salary) for s in monthly), Decimal("0"))), "detail": "Monthly employees only"},
        {"label": "Total Payable Salary", "value": money(sum((Decimal(r.final_salary) for r in calculated), Decimal("0"))), "detail": "Calculated payable amount"},
        {"label": "Total Deductions", "value": money(sum((Decimal(r.total_deduction) for r in calculated), Decimal("0"))), "detail": "LOP, less hours, loan"},
        {"label": "Over Time Amount", "value": money(sum((Decimal(r.ot_amount) for r in calculated), Decimal("0"))), "detail": "Payable overtime"},
        {"label": "Review Items", "value": payroll_health["review_count"], "detail": "Missing salary, unconfirmed week off, errors"},
        {"label": "Less Hours", "value": sum((int(r.less_hours_minutes or 0) for r in calculated), 0), "detail": "Total less-hours minutes"},
        {"label": "Average Payable", "value": money((sum((Decimal(r.final_salary) for r in calculated), Decimal("0")) / len(calculated)) if calculated else Decimal("0")), "detail": "Per processed employee"},
    ]
    month_rows = [month_snapshot(item) for item in months[:6]]
    month_options = [{"value": item.month, "label": display_month(item.month)} for item in months]
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(8).all()
    return render_template(
        "dashboard.html",
        cards=cards,
        month=selected,
        months=months,
        month_options=month_options,
        payroll_health=payroll_health,
        payroll_steps=payroll_steps,
        review_items=review_items[:8],
        status_counts=status_counts,
        top_payables=top_payables,
        month_rows=month_rows,
        recent_logs=recent_logs,
    )
