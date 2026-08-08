import calendar
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.calculator import calculate_payroll_month
from attendance.models import AttendanceRecord, AuditLog, PayrollMonth, PayrollResult, SalaryRecord
from attendance.parser import import_attendance_csv, parse_punch_times, working_minutes_from_punches
from attendance.utils import minutes_to_duration

bp = Blueprint("attendance_manager", __name__, url_prefix="/attendance")
ALLOWED_UPLOADS = {".csv", ".xlsx"}


def display_month(month):
    try:
        year, month_number = (int(part) for part in month.split("-"))
    except (AttributeError, ValueError):
        return month
    return f"{calendar.month_name[month_number]} {year}"


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
    dates = []
    date_seen = set()
    rows_by_employee = {}
    for record in records:
        if record.date not in date_seen:
            dates.append(record.date)
            date_seen.add(record.date)
        employee = rows_by_employee.setdefault(record.employee_id, {
            "employee_id": record.employee_id,
            "name": record.employee_name or record.employee_id,
            "role": record.designation or "",
            "department": record.department or "",
            "cells": {},
        })
        punches = punch_list(record)
        employee["cells"][record.date] = {
            "record": record,
            "punches": punches,
            "punch_text": "\n".join(punches),
            "odd": len(punches) % 2 == 1,
            "warning": record.warning or "",
        }
    rows = list(rows_by_employee.values())
    for row in rows:
        row["has_odd"] = any(cell["odd"] for cell in row["cells"].values())
    return dates, rows


@bp.route("/")
@login_required
def index():
    latest = PayrollMonth.query.order_by(PayrollMonth.month.desc()).first()
    if latest:
        return redirect(url_for("attendance_manager.month", month=latest.month))
    return redirect(url_for("payroll.new"))


@bp.route("/<month>", methods=["GET", "POST"])
@login_required
def month(month):
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
            except Exception as exc:
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
    dates, employee_rows = attendance_grid(month)
    employee_rows = sorted(employee_rows, key=lambda row: employee_sort_value(row, sort), reverse=order == "desc")
    odd_count = sum(1 for row in employee_rows for cell in row["cells"].values() if cell["odd"])
    return render_template(
        "attendance_manager.html",
        month=month,
        month_label=display_month(month),
        payroll_month=payroll_month,
        dates=dates,
        employee_rows=employee_rows,
        odd_count=odd_count,
        is_finalized=is_payroll_finalized(month),
        sort=sort,
        order=order,
    )
