"""Bulk day-status editing from the handwritten attendance register.

Field staff are on customer sites, so the punch machine shows gaps that the
handwritten register fills in. Correcting those one day at a time does not scale —
a single month can carry a couple of hundred no-punch working days — so this module
exports the month as a register sheet and reads back a whole month of day statuses
in one pass, writing them as the same attendance overrides the employee page sets.
"""
import csv
from collections import Counter
from datetime import datetime
from io import StringIO

from attendance import db
from attendance.models import AttendanceOverride, AttendanceRecord, AuditLog
from attendance.utils import clean, is_valid_payroll_month, minutes_to_duration
from attendance.weekoffs import is_week_off_for_date

REGISTER_COLUMNS = [
    "Employee ID", "Employee Name", "Date", "Day", "Punch In", "Punch Out",
    "Hours", "Issue", "Issue Count", "System Status", "Register Status", "Notes",
]
# Blank means "leave this day as the punches read it"; AUTO_STATUS clears a status
# set earlier. Everything else must be one of the statuses the employee page offers.
BLANK_STATUS = ""
AUTO_STATUS = "Auto"
REGISTER_REQUIRED_COLUMNS = {"Employee ID", "Date", "Register Status"}
# Only the working days a register is actually consulted for, so the file stays
# readable: no punches, and not a week off.
MISSING_PUNCH_SCOPE = "missing"
ISSUE_PRIORITY = {
    "Odd punch count": 0,
    "Missing punch and working hours": 1,
}


def register_statuses():
    """Statuses accepted in the Register Status column, matching the employee page."""
    from routes.payroll import OVERRIDE_OPTIONS

    return [option for option in OVERRIDE_OPTIONS if option != AUTO_STATUS]


def system_status_for(record):
    """What the punch data alone says about the day, in plain words."""
    if record.actual_minutes:
        return "Punched"
    if record.parse_status == "OK":
        return "No punches"
    return record.warning or record.parse_status or "Needs review"


def issue_for_status(status):
    status = str(status or "")
    for issue in ISSUE_PRIORITY:
        if issue in status:
            return issue
    return status or "Needs review"


def issue_priority(issue):
    priority = min(
        (ISSUE_PRIORITY[item] for item in ISSUE_PRIORITY if item in issue),
        default=len(ISSUE_PRIORITY),
    )
    return priority, issue.lower()


def issue_sort_key(row):
    return (*issue_priority(row["Issue"]), employee_sort_key(row["Employee ID"]), row["Date"])


def register_rows(month, scope=None):
    """The month as register lines, oldest day first within each employee."""
    overrides = {
        (item.employee_id, item.date): item
        for item in AttendanceOverride.query.filter_by(payroll_month=month).all()
    }
    records = AttendanceRecord.query.filter_by(payroll_month=month).all()
    records.sort(key=lambda item: (employee_sort_key(item.employee_id), item.date))
    rows = []
    for record in records:
        if scope == MISSING_PUNCH_SCOPE:
            # Only working days the punch machine could not see. A week off with no
            # punches is not a gap in the record, and including every Sunday would
            # bury the days that actually need the register.
            if record.actual_minutes or is_week_off_for_date(record.employee_id, record.date):
                continue
        override = overrides.get((record.employee_id, record.date))
        system_status = system_status_for(record)
        rows.append({
            "Employee ID": record.employee_id,
            "Employee Name": record.employee_name or "",
            "Date": record.date.strftime("%d-%m-%Y"),
            "Day": record.day or record.date.strftime("%A"),
            "Punch In": record.first_punch or "",
            "Punch Out": record.last_punch or "",
            "Hours": minutes_to_duration(record.actual_minutes or 0),
            "Issue": issue_for_status(system_status) if scope == MISSING_PUNCH_SCOPE else "",
            "System Status": system_status,
            "Register Status": override.manual_status if override else BLANK_STATUS,
            "Notes": (override.notes if override else "") or "",
        })
    if scope == MISSING_PUNCH_SCOPE:
        rows.sort(key=issue_sort_key)
        issue_counts = Counter(row["Issue"] for row in rows)
        for row in rows:
            row["Issue Count"] = issue_counts[row["Issue"]]
    else:
        for row in rows:
            row["Issue Count"] = ""
    return rows


def employee_sort_key(value):
    return (0, int(value), "") if str(value).isdigit() else (1, 0, str(value).lower())


def register_csv(month, scope=None):
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REGISTER_COLUMNS)
    writer.writeheader()
    writer.writerows(register_rows(month, scope))
    return buffer.getvalue()


def parse_register_date(value):
    text = clean(value)
    if not text:
        raise ValueError("Date is required")
    for date_format in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError(f'Date "{text}" is not in DD-MM-YYYY format')


def apply_register_import(rows, month, actor):
    """Apply a register sheet to the month's attendance overrides.

    Every row is checked before anything is written, and the whole file is rejected
    if any row is wrong: a half-applied register would leave the month in a state
    nobody can reason about. The caller gets every bad line at once rather than one
    error per attempt.
    """
    if not is_valid_payroll_month(month):
        raise ValueError(f"{month} is not a valid payroll month.")
    allowed = set(register_statuses())
    records = {
        (item.employee_id, item.date): item
        for item in AttendanceRecord.query.filter_by(payroll_month=month).all()
    }
    errors = []
    planned = []
    for row_number, row in enumerate(rows, start=2):
        employee_id = clean(row.get("Employee ID"))
        status = clean(row.get("Register Status"))
        if not employee_id:
            errors.append(f"Row {row_number}: Employee ID is required.")
            continue
        try:
            day = parse_register_date(row.get("Date"))
        except ValueError as exc:
            errors.append(f"Row {row_number}: {exc}.")
            continue
        record = records.get((employee_id, day))
        if not record:
            errors.append(
                f"Row {row_number}: No attendance row for employee {employee_id} on "
                f"{day.strftime('%d-%m-%Y')} in this month."
            )
            continue
        if status and status != AUTO_STATUS and status not in allowed:
            errors.append(f'Row {row_number}: "{status}" is not a valid register status.')
            continue
        planned.append((row_number, employee_id, day, status, clean(row.get("Notes"))))
    if errors:
        raise ValueError(
            f"{len(errors)} row(s) could not be applied, so nothing was changed:\n"
            + "\n".join(errors[:25])
            + (f"\n...and {len(errors) - 25} more." if len(errors) > 25 else "")
        )

    applied = 0
    cleared = 0
    for _row_number, employee_id, day, status, notes in planned:
        existing = AttendanceOverride.query.filter_by(
            payroll_month=month, employee_id=employee_id, date=day).first()
        if not status:
            # Blank leaves the day exactly as it was, override or not.
            continue
        if status == AUTO_STATUS:
            if existing:
                db.session.delete(existing)
                cleared += 1
            continue
        if existing and existing.manual_status == status and (existing.notes or "") == notes:
            continue
        if not existing:
            existing = AttendanceOverride(payroll_month=month, employee_id=employee_id, date=day,
                                          manual_status=status)
        existing.manual_status = status
        existing.notes = notes
        db.session.add(existing)
        applied += 1
    if applied or cleared:
        db.session.add(AuditLog(
            actor=actor,
            action="Attendance Register Imported",
            detail=f"{month}: {applied} day status(es) applied, {cleared} cleared from {len(rows)} row(s).",
        ))
    db.session.commit()
    return applied, cleared, len(rows)
