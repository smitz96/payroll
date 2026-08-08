from decimal import Decimal
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from attendance import db
from attendance.authentication import login_required
from attendance.leave_balances import apply_leave_balance_updates, leave_balance_rows, leave_history
from attendance.models import Employee, LeaveLedger, User

bp = Blueprint("leave_balances", __name__, url_prefix="/leave-balances")


def employee_sort_value(row, sort):
    employee = row["employee"]
    if sort == "name":
        return (employee.name or "").lower()
    return int(employee.id) if str(employee.id).isdigit() else str(employee.id).lower()


@bp.route("", methods=["GET"])
@login_required
def index():
    rows = leave_balance_rows()
    search_id = request.args.get("employee_id", "").strip().lower()
    search_name = request.args.get("employee_name", "").strip().lower()
    department = request.args.get("department", "").strip()
    wage_type = (request.args.get("wage_type") or request.args.get("salary_type", "")).strip()
    sort = request.args.get("sort", "id")
    if sort not in {"id", "name"}:
        sort = "id"
    order = request.args.get("order", "asc")
    if order not in {"asc", "desc"}:
        order = "asc"
    departments = sorted({row["employee"].department for row in rows if row["employee"].department})
    wage_types = sorted({row["salary_type"] for row in rows if row["salary_type"]})
    if search_id:
        rows = [row for row in rows if search_id in row["employee"].id.lower()]
    if search_name:
        rows = [row for row in rows if search_name in row["employee"].name.lower()]
    if department:
        rows = [row for row in rows if row["employee"].department == department]
    if wage_type:
        rows = [row for row in rows if row["salary_type"] == wage_type]
    rows = sorted(rows, key=lambda row: employee_sort_value(row, sort), reverse=order == "desc")
    total_leave = sum((Decimal(row["current_balance"]) for row in rows), Decimal("0"))
    edited_today = LeaveLedger.query.filter_by(transaction_type="MANUAL_ADJUSTMENT", date=date.today()).count()
    summary = {
        "Employees": len(rows),
        "Total Stored Leave": total_leave.quantize(Decimal("0.1")),
        "Employees With Zero Balance": len([row for row in rows if Decimal(row["current_balance"]) == 0]),
        "Manual Edits": edited_today or 0,
    }
    leave_logs = (
        LeaveLedger.query.filter_by(transaction_type="MANUAL_ADJUSTMENT")
        .order_by(LeaveLedger.created_at.desc(), LeaveLedger.id.desc())
        .limit(50)
        .all()
    )
    employee_map = {employee.id: employee for employee in Employee.query.all()}
    return render_template(
        "leave_balances.html",
        rows=rows,
        leave_logs=leave_logs,
        employee_map=employee_map,
        summary=summary,
        departments=departments,
        wage_types=wage_types,
        filters={"employee_id": search_id, "employee_name": search_name, "department": department, "wage_type": wage_type, "sort": sort, "order": order},
    )


@bp.route("/update", methods=["POST"])
@login_required
def update():
    user = db.session.get(User, session["user_id"])
    password = request.form.get("password", "")
    if not user or not check_password_hash(user.password_hash, password):
        flash("Incorrect password. Leave balances were not changed.", "danger")
        return redirect(url_for("leave_balances.index"))
    employee_ids = request.form.getlist("employee_id")
    changes = []
    for employee_id in employee_ids:
        current = request.form.get(f"current_balance_{employee_id}", "").strip()
        new = request.form.get(f"new_balance_{employee_id}", "").strip()
        reason = request.form.get(f"reason_{employee_id}", "").strip()
        if new != current:
            changes.append({"employee_id": employee_id, "new_balance": new, "reason": reason})
    if not changes:
        flash("No leave balance changes to save.", "info")
        return redirect(url_for("leave_balances.index"))
    try:
        changed = apply_leave_balance_updates(changes, user.username)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("leave_balances.index"))
    except Exception:
        flash("Leave balance update failed. No changes were saved.", "danger")
        return redirect(url_for("leave_balances.index"))
    flash(f"Leave balances updated successfully. {len(changed)} employee balance(s) changed.", "success")
    return redirect(url_for("leave_balances.index"))


@bp.route("/<employee_id>/history", methods=["GET"])
@login_required
def history(employee_id):
    employee = db.session.get(Employee, employee_id)
    return render_template("leave_balance_history.html", employee=employee, employee_id=employee_id, history=leave_history(employee_id))
