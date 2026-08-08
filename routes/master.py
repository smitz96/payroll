from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.master import disable_master_employee, employee_sort_value, enable_master_employee, save_master_employee
from attendance.models import Employee

bp = Blueprint("master", __name__, url_prefix="/master")


@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        action = request.form.get("action", "save")
        try:
            if action == "disable":
                disable_master_employee(
                    request.form.get("employee_id", "").strip(),
                    request.form.get("employment_status", "").strip(),
                    request.form.get("inactive_reason", "").strip(),
                    request.form.get("disable_confirmation", "").strip(),
                    current_username(),
                )
                flash("Employee marked as inactive.", "success")
            elif action == "enable":
                enable_master_employee(
                    request.form.get("employee_id", "").strip(),
                    request.form.get("enable_confirmation", "").strip(),
                    current_username(),
                )
                flash("Employee enabled successfully.", "success")
            else:
                save_master_employee(request.form, current_username())
                flash("Employee master saved.", "success")
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("master.index"))

    employees = sorted(Employee.query.all(), key=employee_sort_value)
    return render_template("master.html", employees=employees)


@bp.route("/<employee_id>", methods=["GET", "POST"])
@login_required
def detail(employee_id):
    employee = db.session.get(Employee, employee_id)
    if not employee:
        flash("Employee was not found.", "danger")
        return redirect(url_for("master.index"))
    if request.method == "POST":
        try:
            save_master_employee(request.form, current_username())
            db.session.commit()
            flash("Employee master updated.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("master.detail", employee_id=employee_id))
    return render_template("master_detail.html", employee=employee)
