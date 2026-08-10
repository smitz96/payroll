import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.master import (
    EMPLOYEE_MASTER_EXPORT_COLUMNS,
    EMPLOYMENT_STATUSES,
    MASTER_IMPORT_REQUIRED_COLUMNS,
    apply_employee_master_import,
    disable_master_employee,
    employee_master_export_rows,
    employee_sort_value,
    enable_master_employee,
    save_master_employee,
)
from attendance.models import Employee

bp = Blueprint("master", __name__, url_prefix="/master")


def parse_csv_upload(file_storage, required_columns):
    if not file_storage or not file_storage.filename:
        raise ValueError("Select a CSV file to import.")
    text = file_storage.read().decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = sorted(required_columns - fieldnames)
    if missing:
        raise ValueError("Import CSV missing column(s): " + ", ".join(missing))
    return list(reader)


def csv_response(filename, fieldnames, rows):
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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
    return render_template("master.html", employees=employees, employment_statuses=EMPLOYMENT_STATUSES)


@bp.route("/export.csv", methods=["GET"])
@login_required
def export_csv():
    return csv_response(
        "employee_master.csv",
        EMPLOYEE_MASTER_EXPORT_COLUMNS,
        employee_master_export_rows(),
    )


@bp.route("/import", methods=["POST"])
@login_required
def import_csv():
    try:
        rows = parse_csv_upload(request.files.get("employee_master_csv"), MASTER_IMPORT_REQUIRED_COLUMNS)
        changed, created = apply_employee_master_import(rows, current_username())
        db.session.commit()
        updated = len(changed) - len(created)
        flash(f"Employee master imported. {len(created)} employee(s) added, {updated} updated.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("master.index"))


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
    return render_template("master_detail.html", employee=employee, employment_statuses=EMPLOYMENT_STATUSES)
