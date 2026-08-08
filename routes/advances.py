from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.advances import deduction_month_for_advance
from attendance.loans import parse_money
from attendance.models import AdvanceSalary, AuditLog, Employee
from attendance.utils import parse_csv_date

bp = Blueprint("advances", __name__, url_prefix="/advances")
DELETE_CONFIRMATION_TEXT = "permanently delete"


def employee_sort_value(employee):
    return int(employee.id) if str(employee.id).isdigit() else str(employee.id).lower()


@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        try:
            action = request.form.get("action", "create")
            if action == "delete":
                confirmation = request.form.get("delete_confirmation", "").strip().lower()
                if confirmation != DELETE_CONFIRMATION_TEXT:
                    flash('Type "permanently delete" to delete this advance salary record.', "danger")
                    return redirect(url_for("advances.index"))
                advance = db.session.get(AdvanceSalary, int(request.form.get("advance_id")))
                if not advance:
                    raise ValueError("Advance salary record was not found.")
                deduction_month = deduction_month_for_advance(advance)
                detail = f"Advance #{advance.id}; Employee ID {advance.employee_id}; Amount {advance.amount}; Advance Date {advance.advance_date}; Deduction Month {deduction_month}"
                db.session.delete(advance)
                db.session.add(AuditLog(actor=current_username(), action="Advance Salary Deleted", detail=detail))
                db.session.commit()
                flash("Advance salary permanently deleted.", "success")
            else:
                employee_id = request.form.get("employee_id", "").strip()
                employee = db.session.get(Employee, employee_id)
                if not employee:
                    raise ValueError("Employee is required.")
                advance_date = parse_csv_date(request.form.get("advance_date", ""))
                amount = parse_money(request.form.get("amount"), "Advance salary amount")
                notes = request.form.get("notes", "").strip()
                advance = AdvanceSalary(employee_id=employee.id, advance_date=advance_date, amount=amount, notes=notes)
                db.session.add(advance)
                db.session.flush()
                deduction_month = deduction_month_for_advance(advance)
                db.session.add(AuditLog(
                    actor=current_username(),
                    action="Advance Salary Created",
                    detail=f"Advance #{advance.id}; Employee ID {employee.id}; Amount {amount}; Advance Date {advance_date}; Deduction Month {deduction_month}",
                ))
                db.session.commit()
                flash(f"Advance salary saved. It will deduct in payroll month {deduction_month}.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("advances.index"))

    employees = sorted(Employee.query.all(), key=employee_sort_value)
    employee_map = {employee.id: employee for employee in employees}
    advances = AdvanceSalary.query.order_by(AdvanceSalary.advance_date.desc(), AdvanceSalary.id.desc()).all()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    advance_rows = []
    for advance in advances:
        deduction_month = deduction_month_for_advance(advance)
        advance_rows.append({
            "advance": advance,
            "deduction_month": deduction_month,
            "is_due_this_month": deduction_month == month,
        })
    due_rows = [row for row in advance_rows if row["is_due_this_month"]]
    return render_template(
        "advances.html",
        employees=employees,
        employee_map=employee_map,
        advance_rows=advance_rows,
        due_rows=due_rows,
        month=month,
    )
