"""Salary slip history for one employee.

Payroll runs a month at a time, but an employee filing a return needs the whole
financial year in one file. This module answers that: pick the employee, pick the
year, get every slip that was actually issued. Only finalized months appear —
a draft month has no slip yet, so there is nothing here to hand over.
"""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import login_required
from attendance.models import Employee, SalaryRecord
from attendance.reports import build_employee_slip_history_pdf, finalized_slip_months, pending_slip_months, slip_months_by_employee
from attendance.utils import display_month, financial_year_label, financial_year_start

bp = Blueprint("salary_slips", __name__, url_prefix="/salary-slips")
ALL_YEARS = "all"


def employee_sort_key(value):
    return (0, int(value), "") if str(value).isdigit() else (1, 0, str(value).lower())


def financial_years(months):
    """Slip months grouped into April-March years, newest year first."""
    years = {}
    for month in months:
        years.setdefault(financial_year_start(month), []).append(month)
    return [
        {
            "start": start,
            "value": str(start),
            "label": financial_year_label(start),
            "months": sorted(values),
            "count": len(values),
        }
        for start, values in sorted(years.items(), reverse=True)
        if start is not None
    ]


def slip_rows():
    """One row per employee who has at least one finalized salary slip."""
    employees = {employee.id: employee for employee in Employee.query.all()}
    names = {salary.employee_id: salary.name for salary in SalaryRecord.query.all()}
    rows = []
    for employee_id, months in slip_months_by_employee().items():
        employee = employees.get(employee_id)
        rows.append({
            "employee_id": employee_id,
            "name": (employee.name if employee else None) or names.get(employee_id) or employee_id,
            "department": (employee.department if employee else "") or "Not Assigned",
            "status": employee.employment_status if employee else "",
            "count": len(months),
            "first_label": display_month(months[0]),
            "last_label": display_month(months[-1]),
            "years": financial_years(months),
        })
    return sorted(rows, key=lambda row: employee_sort_key(row["employee_id"]))


@bp.route("")
@login_required
def index():
    # The table is filtered in the page, the same as Employees, so picking someone
    # is one keystroke rather than a form submit.
    rows = slip_rows()
    return render_template("salary_slips.html", rows=rows, total_slips=sum(row["count"] for row in rows))


@bp.route("/<employee_id>")
@login_required
def employee(employee_id):
    months = finalized_slip_months(employee_id)
    record = db.session.get(Employee, employee_id)
    if not record and not months:
        abort(404)
    name = record.name if record else next(
        (salary.name for salary in SalaryRecord.query.filter_by(employee_id=employee_id).all()), employee_id)
    return render_template(
        "salary_slips_employee.html",
        employee=record,
        employee_id=employee_id,
        employee_name=name,
        months=months,
        month_labels={month: display_month(month) for month in months},
        years=financial_years(months),
        pending=[display_month(month) for month in pending_slip_months(employee_id)],
    )


@bp.route("/<employee_id>.pdf")
@login_required
def download(employee_id):
    """Every finalized slip for the employee, or just one financial year of them."""
    months = finalized_slip_months(employee_id)
    requested = request.args.get("fy", ALL_YEARS).strip() or ALL_YEARS
    period = "All Months"
    if requested != ALL_YEARS:
        if not requested.isdigit():
            abort(404)
        start = int(requested)
        months = [month for month in months if financial_year_start(month) == start]
        period = financial_year_label(start)
    if not months:
        flash("No finalized salary slips are available for this employee yet. "
              "Slips are released once the month's monthly wage payroll is finalized.", "warning")
        return redirect(url_for("salary_slips.employee", employee_id=employee_id))
    salary = SalaryRecord.query.filter_by(employee_id=employee_id).first()
    name = salary.name if salary else employee_id
    slug = period.replace(" ", "-").lower()
    return Response(
        build_employee_slip_history_pdf(employee_id, months, title=f"{name} Salary Slips - {period}"),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=smartfill-salary-slips-{employee_id}-{slug}.pdf"},
    )
