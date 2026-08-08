from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.loans import (
    loan_installment_for_loan,
    loan_paid_before_month,
    loan_pending_after_month,
    loan_remaining_before_month,
    loan_repayment_schedule,
    parse_money,
    parse_tenure,
)
from attendance.models import AuditLog, Employee, Loan
from attendance.utils import parse_csv_date

bp = Blueprint("loans", __name__, url_prefix="/loans")
DELETE_CONFIRMATION_TEXT = "permanently delete"


def employee_sort_value(employee):
    return int(employee.id) if str(employee.id).isdigit() else str(employee.id).lower()


@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        action = request.form.get("action", "create")
        try:
            if action == "create":
                employee_id = request.form.get("employee_id", "").strip()
                employee = db.session.get(Employee, employee_id)
                if not employee:
                    raise ValueError("Employee is required.")
                start_date = parse_csv_date(request.form.get("start_date", ""))
                amount = parse_money(request.form.get("amount"), "Loan amount")
                tenure = parse_tenure(request.form.get("tenure_months"))
                monthly = parse_money(request.form.get("monthly_deduction"), "Monthly deduction")
                if monthly <= 0:
                    raise ValueError("Monthly deduction must be greater than zero.")
                notes = request.form.get("notes", "").strip()
                loan = Loan(employee_id=employee.id, start_date=start_date, amount=amount, tenure_months=tenure, monthly_deduction=monthly, notes=notes)
                db.session.add(loan)
                db.session.flush()
                db.session.add(AuditLog(
                    actor=current_username(),
                    action="Loan Created",
                    detail=f"Loan #{loan.id}; Employee ID {employee.id}; Amount {amount}; Tenure {tenure}; Monthly Deduction {monthly}; Start {start_date}",
                ))
                flash("Loan created successfully.", "success")
            elif action == "deactivate":
                loan = db.session.get(Loan, int(request.form.get("loan_id")))
                if not loan:
                    raise ValueError("Loan was not found.")
                loan.is_active = False
                loan.updated_at = datetime.utcnow()
                db.session.add(AuditLog(actor=current_username(), action="Loan Deactivated", detail=f"Loan #{loan.id}; Employee ID {loan.employee_id}"))
                flash("Loan deactivated.", "success")
            elif action == "delete":
                confirmation = request.form.get("delete_confirmation", "").strip().lower()
                if confirmation != DELETE_CONFIRMATION_TEXT:
                    flash('Type "permanently delete" to delete this loan.', "danger")
                    return redirect(url_for("loans.index"))
                loan = db.session.get(Loan, int(request.form.get("loan_id")))
                if not loan:
                    raise ValueError("Loan was not found.")
                detail = f"Loan #{loan.id}; Employee ID {loan.employee_id}; Amount {loan.amount}; Start {loan.start_date}"
                db.session.delete(loan)
                db.session.add(AuditLog(actor=current_username(), action="Loan Deleted", detail=detail))
                flash("Loan permanently deleted.", "success")
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("loans.index"))

    employees = sorted(Employee.query.all(), key=employee_sort_value)
    employee_map = {employee.id: employee for employee in employees}
    loans = Loan.query.order_by(Loan.is_active.desc(), Loan.start_date.desc(), Loan.id.desc()).all()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    loan_rows = []
    for loan in loans:
        pending = loan_pending_after_month(loan, month)
        loan_rows.append({
            "loan": loan,
            "paid": loan_paid_before_month(loan, month),
            "remaining": loan_remaining_before_month(loan, month),
            "pending": pending,
            "installment": loan_installment_for_loan(loan, month),
            "is_in_progress": bool(loan.is_active and pending > 0),
        })
    in_progress_rows = [row for row in loan_rows if row["is_in_progress"]]
    return render_template("loans.html", employees=employees, employee_map=employee_map, loan_rows=loan_rows, in_progress_rows=in_progress_rows, month=month)


@bp.route("/<int:loan_id>")
@login_required
def detail(loan_id):
    loan = db.session.get(Loan, loan_id)
    if not loan:
        flash("Loan was not found.", "danger")
        return redirect(url_for("loans.index"))
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    employee = db.session.get(Employee, loan.employee_id)
    schedule = loan_repayment_schedule(loan, month)
    return render_template(
        "loan_detail.html",
        employee=employee,
        loan=loan,
        month=month,
        paid=loan_paid_before_month(loan, month),
        remaining=loan_remaining_before_month(loan, month),
        pending=loan_pending_after_month(loan, month),
        installment=loan_installment_for_loan(loan, month),
        schedule=schedule,
        end_date=schedule[-1]["date"] if schedule else None,
    )
