import csv
from io import StringIO
from pathlib import Path

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.calculator import calculate_payroll_month
from attendance.models import AttendanceRecord, AuditLog, PayrollMonth, PayrollResult, SalaryRecord
from attendance.parser import UnknownEmployeesError, implausible_session_minutes, import_attendance_csv, parse_punch_times, working_minutes_from_punches
from attendance.register import MISSING_PUNCH_SCOPE, REGISTER_REQUIRED_COLUMNS, apply_register_import, register_csv, register_statuses
from attendance.utils import display_month, is_valid_payroll_month, minutes_to_duration
from attendance.weekoffs import is_week_off_for_date

bp = Blueprint("attendance_manager", __name__, url_prefix="/attendance")
ALLOWED_UPLOADS = {".csv", ".xlsx"}


def is_payroll_finalized(month):
    payroll_month = db.session.get(PayrollMonth, month)
    return bool(payroll_month and payroll_month.status == "FINALIZED")


def save_attendance_upload(file, month):
    if not file or not file.filename:
        raise ValueError("Select an attendance CSV or XLSX file.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_UPLOADS:
        raise ValueError("Only CSV or XLSX uploads are allowed.")
    filename = secure_filename(f"{month}-attendance-{file.filename}")
    path = Path("uploads") / filename
    file.save(path)
    return str(path)


def punch_list(record):
    if record.punches_json:
        return list(record.punches_json)
    return [punch for punch in [record.first_punch, record.last_punch] if punch]


def apply_punches(record, punches):
    total_minutes = working_minutes_from_punches(punches)
    record.punches_json = punches
    record.first_punch = punches[0] if punches else ""
    record.last_punch = punches[-1] if punches else ""
    record.actual_minutes = total_minutes
    record.raw_working_hours = minutes_to_duration(total_minutes) if total_minutes is not None else ""
    warnings = []
    if len(punches) % 2 == 1:
        warnings.append("Odd punch count")
    if not punches:
        warnings.append("Missing punch and working hours")
    long_session = implausible_session_minutes(punches)
    if long_session:
        warnings.append(f"Punch out before punch in ({minutes_to_duration(long_session)} session)")
    record.parse_status = "NEEDS_REVIEW" if warnings else "OK"
    record.warning = "; ".join(warnings)


def employee_sort_value(row, sort):
    if sort == "name":
        return (row["name"] or "").lower(), str(row["employee_id"])
    try:
        return 0, int(row["employee_id"])
    except (TypeError, ValueError):
        return 1, str(row["employee_id"])


def attendance_grid(month):
    records = (
        AttendanceRecord.query.filter_by(payroll_month=month)
        .order_by(AttendanceRecord.employee_id, AttendanceRecord.date)
        .all()
    )
    week_off_dates = {}
    date_seen = set()
    rows_by_employee = {}
    for record in records:
        date_seen.add(record.date)
        employee = rows_by_employee.setdefault(record.employee_id, {
            "employee_id": record.employee_id,
            "name": record.employee_name or record.employee_id,
            "role": record.designation or "",
            "department": record.department or "",
            "cells": {},
        })
        punches = punch_list(record)
        week_off = week_off_dates.get((record.employee_id, record.date))
        if week_off is None:
            week_off = is_week_off_for_date(record.employee_id, record.date)
            week_off_dates[(record.employee_id, record.date)] = week_off
        employee["cells"][record.date] = {
            "record": record,
            "punches": punches,
            "punch_text": "\n".join(punches),
            "odd": len(punches) % 2 == 1,
            # A working day with no punches is an unexplained absence: it is neither
            # paid nor deducted until someone sets an override, so it must be visible.
            "missing": not punches and not week_off,
            "week_off": week_off,
            "warning": record.warning or "",
        }
    # Sorted explicitly: first-seen order is only correct when every employee has
    # every date, which is not true for row-per-day CSV imports with gaps.
    dates = sorted(date_seen)
    rows = list(rows_by_employee.values())
    for row in rows:
        cells = row["cells"].values()
        row["odd_count"] = sum(1 for cell in cells if cell["odd"])
        row["missing_count"] = sum(1 for cell in cells if cell["missing"])
        row["has_odd"] = row["odd_count"] > 0
        row["has_missing"] = row["missing_count"] > 0
        row["needs_review"] = row["has_odd"] or row["has_missing"]
    return dates, rows


@bp.route("/")
@login_required
def index():
    latest = PayrollMonth.query.order_by(PayrollMonth.month.desc()).first()
    if latest:
        return redirect(url_for("attendance_manager.month", month=latest.month))
    return redirect(url_for("payroll.new"))


def parse_register_upload(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Select a register CSV to import.")
    reader = csv.DictReader(StringIO(file_storage.read().decode("utf-8-sig")))
    missing = sorted(REGISTER_REQUIRED_COLUMNS - set(reader.fieldnames or []))
    if missing:
        raise ValueError("Register CSV missing column(s): " + ", ".join(missing))
    return list(reader)


@bp.route("/<month>/register.csv")
@login_required
def register_export(month):
    """The month as a register sheet. `scope=missing` narrows it to no-punch days."""
    if not is_valid_payroll_month(month):
        abort(404)
    scope = MISSING_PUNCH_SCOPE if request.args.get("scope") == MISSING_PUNCH_SCOPE else None
    suffix = "-missing" if scope else ""
    return Response(
        register_csv(month, scope),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=smartfill-attendance-register-{month}{suffix}.csv"},
    )


@bp.route("/<month>", methods=["GET", "POST"])
@login_required
def month(month):
    if not is_valid_payroll_month(month):
        abort(404)
    payroll_month = db.session.get(PayrollMonth, month)
    if not payroll_month:
        flash("Payroll month not found.", "danger")
        return redirect(url_for("payroll.new"))
    if request.method == "POST":
        if is_payroll_finalized(month):
            flash("Payroll is finalized and locked. Unlock payroll before editing attendance.", "danger")
            return redirect(url_for("attendance_manager.month", month=month))
        action = request.form.get("action", "save")
        if action == "import_attendance":
            try:
                had_existing = AttendanceRecord.query.filter_by(payroll_month=month).count() > 0
                count, warnings = import_attendance_csv(save_attendance_upload(request.files.get("attendance_csv"), month), month, current_username())
                flash(f"{'Re-imported' if had_existing else 'Imported'} attendance: {count} rows. Review and submit before calculating payroll.", "success")
                for warning in warnings:
                    flash(warning, "warning")
            except UnknownEmployeesError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
                # Carry the missing list through the redirect so the page can offer a
                # direct route to add them instead of just naming them in a flash.
                session["unknown_attendance_employees"] = exc.missing
            except Exception as exc:
                flash(str(exc), "danger")
            return redirect(url_for("attendance_manager.month", month=month))
        if action == "import_register":
            # The register is the handwritten record of who was actually working, so
            # it lands as the same day-status overrides the employee page writes.
            try:
                rows = parse_register_upload(request.files.get("register_csv"))
                applied, cleared, read = apply_register_import(rows, month, current_username())
                flash(f"Attendance register imported. {applied} day status(es) applied, "
                      f"{cleared} cleared, {read} row(s) read. Submit attendance to recalculate payroll.", "success")
            except Exception as exc:
                db.session.rollback()
                flash(str(exc), "danger")
            return redirect(url_for("attendance_manager.month", month=month))
        records = AttendanceRecord.query.filter_by(payroll_month=month).all()
        changed = 0
        for record in records:
            field_name = f"punches_{record.id}"
            if field_name not in request.form:
                continue
            old_punches = punch_list(record)
            new_punches = parse_punch_times(request.form.get(field_name, ""))
            if new_punches != old_punches:
                changed += 1
            apply_punches(record, new_punches)
            db.session.add(record)
        if action == "submit":
            payroll_month.attendance_submitted = True
            db.session.add(AuditLog(actor=current_username(), action="Bulk Attendance Submitted", detail=f"{month}: {changed} day(s) changed"))
            db.session.commit()
            try:
                if SalaryRecord.query.filter_by(payroll_month=month).count():
                    results = calculate_payroll_month(month, current_username())
                    flash(f"Attendance submitted and payroll calculated for {len(results)} employee(s).", "success")
                else:
                    flash("Attendance submitted. Load wage from master before calculating payroll.", "warning")
            except Exception as exc:
                flash(f"Attendance submitted, but payroll calculation needs review: {exc}", "warning")
            return redirect(url_for("payroll.month", month=month))
        payroll_month.attendance_submitted = False
        PayrollResult.query.filter_by(payroll_month=month).delete()
        db.session.add(AuditLog(actor=current_username(), action="Bulk Attendance Saved", detail=f"{month}: {changed} day(s) changed; payroll marked pending"))
        db.session.commit()
        flash("Attendance saved as draft. Submit attendance before calculating payroll.", "success")
        return redirect(url_for("attendance_manager.month", month=month))

    sort = request.args.get("sort", "id")
    if sort not in {"id", "name"}:
        sort = "id"
    order = request.args.get("order", "asc")
    if order not in {"asc", "desc"}:
        order = "asc"
    unknown_employees = session.pop("unknown_attendance_employees", None) or {}
    dates, employee_rows = attendance_grid(month)
    employee_rows = sorted(employee_rows, key=lambda row: employee_sort_value(row, sort), reverse=order == "desc")
    odd_count = sum(row["odd_count"] for row in employee_rows)
    missing_count = sum(row["missing_count"] for row in employee_rows)
    return render_template(
        "attendance_manager.html",
        month=month,
        month_label=display_month(month),
        payroll_month=payroll_month,
        register_statuses=register_statuses(),
        dates=dates,
        employee_rows=employee_rows,
        odd_count=odd_count,
        missing_count=missing_count,
        review_employee_count=sum(1 for row in employee_rows if row["needs_review"]),
        unknown_employees=unknown_employees,
        is_finalized=is_payroll_finalized(month),
        sort=sort,
        order=order,
    )
