import calendar
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.calculator import attendance_missing_salary, calculate_employee_payroll, calculate_payroll_month, name_mismatches
from attendance.advances import advance_deduction_for_employee, advances_for_payroll_month
from attendance.holidays import holiday_dates_for_records
from attendance.loans import active_loans_for_employee, employee_has_loan, loan_installment_for_employee, loan_skip_for_employee
from attendance.master import employee_active_for_payroll_month, sync_salary_records_from_master
from attendance.models import AuditLog, AttendanceOverride, AttendanceRecord, Employee, LeaveLedger, LoanInstallmentSkip, PayrollMonth, PayrollResult, SalaryRecord, User
from attendance.parser import ensure_month, import_attendance_csv
from attendance.payroll_rules import calculate_monthly_shortage, classify_daily_attendance, classify_monthly_attendance, daily_bonus_explanation
from attendance.reports import attendance_detail_csv, payroll_month_days, payroll_summary_csv, punch_sessions, total_paid_days
from attendance.settings import DAILY_BONUS_RULES, MONTHLY_RULES as CFG
from attendance.utils import decimal_money, display_month, is_valid_payroll_month, minutes_to_duration, money
from attendance.wage_groups import (
    GROUP_LABELS,
    any_group_finalized,
    employee_locked,
    finalize_group,
    group_summary,
    is_group_finalized,
    normalize_group,
    unlock_group,
)

bp = Blueprint("payroll", __name__, url_prefix="/payroll")

ALLOWED = {".csv", ".xlsx"}
OVERRIDE_OPTIONS = [
    "Auto", "Full Day Present", "Half Day Present", "Paid Leave", "Half-Day Paid Leave",
    "Unpaid Leave / LOP", "Half-Day LOP", "Holiday", "Week Off", "Week Off Worked",
    "Worked On-Site", "Work From Home",
    "Punch Error", "Ignore",
]
LOCKED_MESSAGE = "Payroll is finalized and locked. Unlock payroll before making changes."
DELETE_CONFIRMATION_TEXT = "permanently delete"


def employee_id_sort_value(value):
    text = str(value or "").strip()
    if text.isdigit():
        return (0, int(text))
    return (1, text.lower())


def salary_sort_value(salary, sort):
    if sort == "name":
        return (str(salary.name or "").lower(), employee_id_sort_value(salary.employee_id))
    return employee_id_sort_value(salary.employee_id)


def audit_money(value):
    return f"{decimal_money(value):.2f}"


def parse_leave_encashment_days(value):
    text = str(value).strip()
    if not text:
        return Decimal("0.0")
    try:
        days = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid leave encashment days: {value}") from exc
    if days < 0:
        raise ValueError("Leave encashment days cannot be negative.")
    return days.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def verify_admin_password():
    user = db.session.get(User, session.get("user_id"))
    password = request.form.get("admin_password", "")
    return bool(user and check_password_hash(user.password_hash, password))


def previous_calendar_month(today=None):
    today = today or date.today()
    year = today.year
    month = today.month - 1
    if month == 0:
        month = 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def attendance_display_status(raw_status):
    mapping = {
        "Full Day Present": "Full Day",
        "Half Day Present": "Half Day",
        "Paid Leave": "Paid Leave",
        "Half-Day Paid Leave": "Half-Day Paid Leave",
        "Full Day LOP": "Full Day LOP",
        "Half Day LOP": "Half Day LOP",
        "Unpaid Leave / LOP": "LOP",
        "Absent / Attendance Missing": "Absent",
        "Half-Day Paid Leave / Half-Day LOP": "Half Leave + Half LOP",
        "Needs Review": "Needs Review",
        "Punch Error": "Punch Error",
        "Holiday": "Holiday",
        "Week Off": "Week Off",
        "Week Off Worked": "Week Off Worked",
        "Sandwich Leave": "Sandwich Leave",
        "Worked On-Site": "Worked On-Site",
        "Work From Home": "Work From Home",
        "Ignore": "Ignore",
    }
    return mapping.get(raw_status or "", raw_status or "Pending Calculation")


SILENT_EXPLANATION_STATUSES = {"Full Day Present", "Half Day Present", "Week Off", "Holiday", "Week Off Worked", "Worked On-Site", "Work From Home"}


def attendance_note(record, raw_status, is_shortage, explanation=""):
    """Explanatory text for a day, or empty when the day is unremarkable."""
    if raw_status in SILENT_EXPLANATION_STATUSES and not is_shortage:
        return ""
    # Once a day has been resolved into leave or LOP, the raw import warning that
    # produced it ("Missing punch and working hours") is no longer the story.
    resolved = {"Paid Leave", "Half-Day Paid Leave", "Sandwich Leave",
                "Half-Day Paid Leave / Half-Day LOP", "Absent / Attendance Missing",
                "Full Day LOP", "Half Day LOP", "Unpaid Leave / LOP"}
    if raw_status in resolved:
        return ""
    # A day flagged for review can parse cleanly and so carry no import warning; the
    # rule's own explanation is then the only thing that says why it was flagged.
    return record.warning or explanation


def is_attendance_error(record, raw_status):
    if raw_status in {"Full Day Present", "Half Day Present", "Paid Leave", "Half-Day Paid Leave", "Holiday", "Week Off", "Week Off Worked", "Sandwich Leave", "Worked On-Site", "Work From Home", "Ignore"}:
        return False
    if record.parse_status != "OK":
        return True
    return raw_status in {"Needs Review", "Punch Error", "Absent / Attendance Missing"}


def attendance_status_tone(display_status, is_error, is_shortage):
    if is_error:
        return "error"
    if is_shortage:
        return "shortage"
    if display_status in {"Worked On-Site", "Work From Home"}:
        return "offsite"
    if display_status in {"Full Day", "Half Day", "Week Off Worked"}:
        return "ok"
    if display_status in {"Paid Leave", "Half-Day Paid Leave", "Sandwich Leave"}:
        return "leave"
    return "other"


WEEKDAY_HEADINGS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def attendance_calendar(month, rows):
    """Lay the month's attendance out as calendar weeks, Sunday first.

    Every day of the month gets a cell whether or not attendance was imported for
    it, so a gap in the sheet is visible rather than silently absent.
    """
    year, month_number = (int(part) for part in month.split("-"))
    days_in_month = calendar.monthrange(year, month_number)[1]
    by_date = {row["record"].date: row for row in rows}
    first = date(year, month_number, 1)
    # date.weekday() is Monday=0; shift so Sunday leads the week.
    cells = [None] * ((first.weekday() + 1) % 7)
    for day in range(1, days_in_month + 1):
        current = date(year, month_number, day)
        cells.append({"date": current, "day": day, "row": by_date.get(current)})
    while len(cells) % 7:
        cells.append(None)
    return [cells[index:index + 7] for index in range(0, len(cells), 7)]


def employee_attendance_rows(records, result=None, salary=None, overrides=None):
    overrides = overrides or {}
    detail_by_date = {}
    if result and result.detail_json:
        detail_by_date = {row.get("date"): row for row in result.detail_json}
    holidays = holiday_dates_for_records(records)
    rows = []
    for record in records:
        detail = detail_by_date.get(record.date.isoformat())
        if detail:
            raw_status = detail.get("attendance_status")
            explanation = detail.get("explanation") or ""
            shortage_minutes = int(detail.get("shortage_minutes") or 0)
        elif salary and salary.normalized_salary_type == "MONTHLY":
            classified = classify_monthly_attendance(record, holidays, overrides.get(record.date), record.employee_id)
            raw_status = classified["status"]
            explanation = classified["explanation"]
            shortage_minutes = calculate_monthly_shortage(record.actual_minutes)
        elif salary and salary.normalized_salary_type == "DAILY":
            classified = classify_daily_attendance(record, holidays, overrides.get(record.date), record.employee_id)
            raw_status = classified["status"]
            explanation = classified["explanation"]
            shortage_minutes = calculate_monthly_shortage(record.actual_minutes)
        elif salary and salary.normalized_salary_type not in {"MONTHLY", "DAILY"}:
            raw_status = "Payroll Rules Not Configured"
            explanation = "Salary type rules not configured."
            shortage_minutes = 0
        else:
            raw_status = "Pending Calculation" if record.parse_status == "OK" else "Needs Review"
            explanation = record.warning or ""
            shortage_minutes = 0
        error = is_attendance_error(record, raw_status)
        display_status = attendance_display_status(raw_status)
        is_shortage = raw_status == "Full Day Present" and shortage_minutes > 0
        rows.append({
            "record": record,
            "raw_status": raw_status,
            "display_status": display_status,
            "explanation": explanation,
            "note": attendance_note(record, raw_status, is_shortage, explanation),
            "sessions": punch_sessions(record.punches_json, record.first_punch or "", record.last_punch or ""),
            "is_error": error,
            "is_shortage": is_shortage,
            "shortage_minutes": shortage_minutes,
            "status_tone": attendance_status_tone(display_status, error, is_shortage),
            "sort_key": (0 if error else 1, record.date),
        })
    return sorted(rows, key=lambda row: row["sort_key"])


def save_employee_detail_changes(month, employee_id):
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    change_details = []
    if salary:
        old_adjustment = salary.adjustment
        old_loan = salary.loan
        old_leave_enabled = bool(getattr(salary, "leave_encashment_enabled", False))
        old_leave_disabled = bool(getattr(salary, "leave_encashment_disabled", False))
        old_leave_days = getattr(salary, "leave_encashment_days", 0)
        old_leave_amount = getattr(salary, "leave_encashment_amount", 0)
        payroll_month = db.session.get(PayrollMonth, month)
        global_leave_encashment = bool(payroll_month and payroll_month.encash_all_leaves)
        existing_skip = loan_skip_for_employee(employee_id, month)
        old_skip = bool(existing_skip and existing_skip.skip)
        salary.adjustment = decimal_money(request.form.get("adjustment", "0"))
        loan = decimal_money(request.form.get("loan", "0"))
        if loan < 0:
            raise ValueError("Loan deduction cannot be negative.")
        salary.loan = loan
        monthly_wage = salary.normalized_salary_type == "MONTHLY"
        salary.leave_encashment_enabled = monthly_wage and request.form.get("leave_encashment_enabled") == "on"
        salary.leave_encashment_disabled = bool(monthly_wage and global_leave_encashment and not salary.leave_encashment_enabled)
        leave_encashment_days = parse_leave_encashment_days(request.form.get("leave_encashment_days", "0")) if monthly_wage else Decimal("0")
        result = PayrollResult.query.filter_by(payroll_month=month, employee_id=employee_id).first()
        if salary.leave_encashment_enabled and result and not global_leave_encashment:
            available_leave = Decimal(result.closing_leave or 0) + Decimal(getattr(result, "leave_encashment_days", 0) or 0)
            if leave_encashment_days > available_leave:
                if available_leave <= 0:
                    raise ValueError("No leaves available for encashment.")
                raise ValueError(f"Only {available_leave} leave(s) available for encashment.")
        salary.leave_encashment_days = leave_encashment_days if salary.leave_encashment_enabled and not global_leave_encashment else 0
        salary.leave_encashment_amount = money((Decimal(salary.salary or 0) / Decimal(CFG["SALARY_CALCULATION_DAYS"])) * salary.leave_encashment_days) if salary.leave_encashment_enabled and not global_leave_encashment else 0
        if salary.adjustment != old_adjustment:
            change_details.append(f"Adjustment: {audit_money(old_adjustment)} -> {audit_money(salary.adjustment)}")
        if salary.loan != old_loan:
            change_details.append(f"Loan: {audit_money(old_loan)} -> {audit_money(salary.loan)}")
        if salary.leave_encashment_enabled != old_leave_enabled or salary.leave_encashment_disabled != old_leave_disabled or salary.leave_encashment_days != old_leave_days or salary.leave_encashment_amount != old_leave_amount:
            change_details.append(
                f"Leave Encashment: {'Enabled' if old_leave_enabled else 'Disabled'} {Decimal(old_leave_days or 0)} day(s) / {audit_money(old_leave_amount)} -> "
                f"{'Enabled' if salary.leave_encashment_enabled else 'Disabled'} {salary.leave_encashment_days} day(s) / {audit_money(salary.leave_encashment_amount)}"
            )
        skip_loan = request.form.get("loan_installment_skip") == "on"
        skip_notes = request.form.get("loan_skip_notes", "").strip()
        if existing_skip or skip_loan:
            if not existing_skip:
                existing_skip = LoanInstallmentSkip(payroll_month=month, employee_id=employee_id)
            existing_skip.skip = skip_loan
            existing_skip.notes = skip_notes
            db.session.add(existing_skip)
        if skip_loan != old_skip:
            change_details.append(f"Loan Installment Skip: {'Yes' if old_skip else 'No'} -> {'Yes' if skip_loan else 'No'}")
        db.session.add(salary)
    records = AttendanceRecord.query.filter_by(payroll_month=month, employee_id=employee_id).all()
    saved_overrides = 0
    for record in records:
        status = request.form.get(f"manual_status_{record.id}", "Auto")
        notes = request.form.get(f"notes_{record.id}", "")
        existing = AttendanceOverride.query.filter_by(payroll_month=month, employee_id=employee_id, date=record.date).first()
        if status == "Auto":
            if existing:
                change_details.append(f"{record.date}: override {existing.manual_status} -> Auto")
                db.session.delete(existing)
                saved_overrides += 1
        else:
            if not existing:
                existing = AttendanceOverride(payroll_month=month, employee_id=employee_id, date=record.date, manual_status=status)
                change_details.append(f"{record.date}: override Auto -> {status}")
            elif existing.manual_status != status or (existing.notes or "") != notes:
                change_details.append(f"{record.date}: override {existing.manual_status} -> {status}; notes updated")
            existing.manual_status = status
            existing.notes = notes
            db.session.add(existing)
            saved_overrides += 1
    if change_details:
        db.session.add(AuditLog(
            actor=current_username(),
            action="Employee Payroll Data Changed",
            detail=f"{month}: Employee ID {employee_id}; " + " | ".join(change_details),
        ))
    db.session.flush()
    return saved_overrides


def is_payroll_finalized(month, employee_id=None):
    """Locked state. With an employee, follows that employee's wage group."""
    if employee_id is not None:
        return employee_locked(month, employee_id)
    payroll_month = db.session.get(PayrollMonth, month)
    return bool(payroll_month and payroll_month.status == "FINALIZED")


def payroll_workflow_steps(month, payroll_month, attendance_count, salary_count, results, locked=False):
    """The five stages of a payroll month, in order, with the first unfinished one marked current.

    Each step carries everything needed to act on it, so the stepper is the only
    place these actions live. A step may offer a link (`href`) and/or a POST action
    (`action`); actions stay available on completed steps so wages can be reloaded
    after Employee Master changes.
    """
    calculated_count = len([r for r in results.values() if r.final_salary is not None])
    steps = [
        {
            "key": "attendance",
            "label": "Import attendance",
            "done": attendance_count > 0,
            "detail": f"{attendance_count} row(s) imported" if attendance_count else "Upload the punch CSV or XLSX",
            "href": url_for("attendance_manager.month", month=month),
            "cta": "Review attendance" if attendance_count else "Import attendance",
        },
        {
            "key": "wages",
            "label": "Load wages",
            "done": salary_count > 0,
            "detail": f"{salary_count} wage record(s)" if salary_count else "Pull wage type and salary from Employee Master",
            # No link: Employees is one click away in the sidebar, and a second button
            # here made this the only step with two, throwing the row out of line.
            "href": None,
            "cta": None,
            "action": None if locked else "salary",
            # Kept short so it stays on one line and lines up with the other steps;
            # the step title and detail already say where the wages come from.
            "action_label": "Reload wages" if salary_count else "Load wages",
            "action_hint": "Wage type and salary come from Employee Master. Employees with a zero salary or an inactive status are skipped.",
        },
        {
            "key": "submit",
            "label": "Review & submit",
            "done": bool(payroll_month.attendance_submitted),
            "detail": "Attendance submitted" if payroll_month.attendance_submitted else "Fix punch errors, then submit",
            "href": url_for("attendance_manager.month", month=month),
            # Short enough to stay on one line, so every step button is the same height.
            "cta": "Review punches",
        },
        {
            "key": "calculate",
            "label": "Calculate payroll",
            "done": calculated_count > 0,
            "detail": f"{calculated_count} employee(s) calculated" if calculated_count else "Use Recalculate at the top of the page",
            # Recalculate and Reset & Recalculate both live in the page header now,
            # so this step reports progress rather than carrying a duplicate button.
            "href": None,
            "cta": None,
        },
        {
            "key": "finalize",
            "label": "Finalize",
            "done": payroll_month.status == "FINALIZED",
            "detail": "Locked" if payroll_month.status == "FINALIZED" else "Lock each wage type when it is signed off",
            "href": "#wage-locks",
            "cta": "Finalize",
        },
    ]
    # A step is actionable only once everything before it is done. Steps that are
    # already done stay actionable so wages can be reloaded or attendance revisited.
    prerequisites_met = True
    for step in steps:
        step["enabled"] = step["done"] or prerequisites_met
        step["blocked_reason"] = "" if step["enabled"] else "Complete the earlier steps first"
        prerequisites_met = prerequisites_met and step["done"]
    current_marked = False
    for step in steps:
        if not step["done"] and not current_marked:
            step["state"] = "current"
            current_marked = True
        elif step["done"]:
            step["state"] = "done"
        else:
            step["state"] = "todo"
    return steps


def save_finalized_csv_exports(month):
    output_dir = Path("output") / "csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"smartfill-payroll-summary-{month}.csv"
    attendance_path = output_dir / f"smartfill-attendance-detail-{month}.csv"
    summary_path.write_text(payroll_summary_csv(month), encoding="utf-8")
    attendance_path.write_text(attendance_detail_csv(month), encoding="utf-8")
    return summary_path, attendance_path


def delete_payroll_month(month):
    counts = {
        "attendance": AttendanceRecord.query.filter_by(payroll_month=month).delete(),
        "salary": SalaryRecord.query.filter_by(payroll_month=month).delete(),
        "results": PayrollResult.query.filter_by(payroll_month=month).delete(),
        "overrides": AttendanceOverride.query.filter_by(payroll_month=month).delete(),
        "leave_ledger": LeaveLedger.query.filter_by(payroll_month=month).delete(),
        "loan_skips": LoanInstallmentSkip.query.filter_by(payroll_month=month).delete(),
    }
    PayrollMonth.query.filter_by(month=month).delete()
    return counts


def clear_manual_payroll_modifications(month, wage_group=None):
    """Wipe manual edits for the month, optionally only for one wage group."""
    group = normalize_group(wage_group)
    scoped_ids = None
    if group:
        scoped_ids = {
            salary.employee_id
            for salary in SalaryRecord.query.filter_by(payroll_month=month, normalized_salary_type=group).all()
        }
    override_query = AttendanceOverride.query.filter_by(payroll_month=month)
    skip_query = LoanInstallmentSkip.query.filter_by(payroll_month=month)
    if scoped_ids is not None:
        override_query = override_query.filter(AttendanceOverride.employee_id.in_(scoped_ids or {""}))
        skip_query = skip_query.filter(LoanInstallmentSkip.employee_id.in_(scoped_ids or {""}))
    override_count = override_query.delete(synchronize_session=False)
    loan_skip_count = skip_query.delete(synchronize_session=False)
    salary_reset_count = 0
    salary_query = SalaryRecord.query.filter_by(payroll_month=month)
    if group:
        salary_query = salary_query.filter_by(normalized_salary_type=group)
    for salary in salary_query.all():
        if decimal_money(salary.adjustment) != 0 or decimal_money(salary.loan) != 0 or getattr(salary, "leave_encashment_enabled", False) or getattr(salary, "leave_encashment_disabled", False) or Decimal(getattr(salary, "leave_encashment_days", 0) or 0) != 0 or decimal_money(getattr(salary, "leave_encashment_amount", 0)) != 0:
            salary_reset_count += 1
        salary.adjustment = 0
        salary.loan = 0
        salary.leave_encashment_enabled = False
        salary.leave_encashment_disabled = False
        salary.leave_encashment_days = 0
        salary.leave_encashment_amount = 0
        db.session.add(salary)
    db.session.add(AuditLog(
        actor=current_username(),
        action="Manual Payroll Modifications Cleared",
        detail=(
            f"{month}: scope {GROUP_LABELS.get(group, 'All wage types')}; {override_count} override row(s) deleted; "
            f"{loan_skip_count} loan skip row(s) deleted; {salary_reset_count} salary adjustment/loan/leave encashment row(s) reset"
        ),
    ))
    db.session.flush()
    return override_count, salary_reset_count, loan_skip_count


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        month = request.form.get("month")
        if not month:
            flash("Select a payroll month.", "danger")
            return redirect(url_for("payroll.new"))
        if not is_valid_payroll_month(month):
            flash("Select a valid payroll month in YYYY-MM format.", "danger")
            return redirect(url_for("payroll.new"))
        ensure_month(month)
        db.session.add(AuditLog(actor=current_username(), action="Payroll Month Created", detail=month))
        db.session.commit()
        return redirect(url_for("payroll.month", month=month))
    return render_template("payroll_new.html", default_month=previous_calendar_month())


def save_upload(file, month, label):
    if not file or not file.filename:
        raise ValueError(f"Select a {label} file.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("Only CSV or XLSX uploads are allowed.")
    filename = secure_filename(f"{month}-{label}-{file.filename}")
    path = Path("uploads") / filename
    file.save(path)
    return str(path)


@bp.route("/<month>", methods=["GET", "POST"])
@login_required
def month(month):
    # Without this, a malformed month reaches calendar.month_name and 500s, and any
    # GET would create a junk PayrollMonth row keyed on the bad string.
    if not is_valid_payroll_month(month):
        abort(404)
    payroll_month = ensure_month(month)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "finalize":
                if not verify_admin_password():
                    flash("Admin password is required to finalize payroll.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                group = normalize_group(request.form.get("wage_group"))
                if not group:
                    flash("Select the wage type to finalize.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                summary_path, attendance_path = save_finalized_csv_exports(month)
                finalize_group(payroll_month, group, current_username(), f"saved {summary_path.name}, {attendance_path.name}")
                db.session.commit()
                flash(f"{GROUP_LABELS[group]} wage payroll finalized and locked.", "success")
            elif action == "unlock":
                if not verify_admin_password():
                    flash("Admin password is required to unlock payroll.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                group = normalize_group(request.form.get("wage_group"))
                if not group:
                    flash("Select the wage type to unlock.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                unlock_group(payroll_month, group, current_username())
                db.session.commit()
                flash(f"{GROUP_LABELS[group]} wage payroll unlocked. Changes are allowed again.", "success")
            elif action == "delete":
                confirmation = request.form.get("delete_confirmation", "").strip().lower()
                if confirmation != DELETE_CONFIRMATION_TEXT:
                    flash('Type "permanently delete" to delete this payroll month.', "danger")
                else:
                    counts = delete_payroll_month(month)
                    detail = (
                        f"{month} deleted; attendance rows: {counts['attendance']}; salary rows: {counts['salary']}; "
                        f"results: {counts['results']}; overrides: {counts['overrides']}; leave ledger rows: {counts['leave_ledger']}"
                    )
                    db.session.add(AuditLog(actor=current_username(), action="Payroll Deleted", detail=detail))
                    db.session.commit()
                    flash(f"Payroll {month} permanently deleted.", "success")
                    return redirect(url_for("payroll.new"))
            elif action in {"leave_encashment", "attendance", "salary"} and any_group_finalized(payroll_month):
                flash(LOCKED_MESSAGE, "danger")
            elif action == "leave_encashment":
                enabled = request.form.get("encash_all_leaves") == "on"
                old_enabled = bool(payroll_month.encash_all_leaves)
                payroll_month.encash_all_leaves = enabled
                reset_count = 0
                if enabled:
                    for salary in SalaryRecord.query.filter_by(payroll_month=month).all():
                        if getattr(salary, "leave_encashment_disabled", False):
                            reset_count += 1
                        salary.leave_encashment_disabled = False
                        db.session.add(salary)
                if enabled != old_enabled or reset_count:
                    db.session.add(AuditLog(
                        actor=current_username(),
                        action="Global Leave Encashment Changed",
                        detail=f"{month}: {'Enabled' if old_enabled else 'Disabled'} -> {'Enabled' if enabled else 'Disabled'}; {reset_count} employee disable override(s) cleared",
                    ))
                db.session.commit()
                flash("Global leave encashment setting saved.", "success")
            elif action == "attendance":
                count, warnings = import_attendance_csv(save_upload(request.files.get("attendance_csv"), month, "attendance"), month, current_username())
                flash(f"Attendance imported: {count} rows.", "success")
                for warning in warnings:
                    flash(warning, "warning")
                flash("Review and submit attendance before calculating payroll.", "warning")
                return redirect(url_for("attendance_manager.month", month=month))
            elif action == "salary":
                created, updated, skipped = sync_salary_records_from_master(month, current_username())
                db.session.commit()
                flash(f"Wage data loaded from master: {created} created, {updated} updated, {len(skipped)} skipped.", "success")
                # Name the skipped employees; a bare count hides who is missing from payroll.
                for reason in skipped:
                    flash(f"Skipped {reason}", "warning")
            elif action == "calculate":
                if attendance_count := AttendanceRecord.query.filter_by(payroll_month=month).count():
                    if not payroll_month.attendance_submitted:
                        flash(f"{attendance_count} attendance row(s) are pending review. Submit attendance from Attendance Manager before calculating payroll.", "danger")
                        return redirect(url_for("attendance_manager.month", month=month))
                group = normalize_group(request.form.get("wage_group"))
                if group and is_group_finalized(payroll_month, group):
                    flash(f"{GROUP_LABELS[group]} wage payroll is finalized. Unlock it before recalculating.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                override_count, salary_reset_count, loan_skip_count = clear_manual_payroll_modifications(month, group)
                results = calculate_payroll_month(month, current_username(), wage_group=group)
                scope = f"{GROUP_LABELS[group]} wage" if group else "all open wage types"
                flash(f"Reset and recalculated {scope} for {len(results)} wage record(s). Cleared {override_count} override row(s), {loan_skip_count} loan skip row(s), and reset {salary_reset_count} adjustment/loan/leave encashment row(s).", "success")
            elif action == "recheck_holidays":
                if attendance_count := AttendanceRecord.query.filter_by(payroll_month=month).count():
                    if not payroll_month.attendance_submitted:
                        flash(f"{attendance_count} attendance row(s) are pending review. Submit attendance from Attendance Manager before rechecking holidays.", "danger")
                        return redirect(url_for("attendance_manager.month", month=month))
                group = normalize_group(request.form.get("wage_group"))
                if group and is_group_finalized(payroll_month, group):
                    flash(f"{GROUP_LABELS[group]} wage payroll is finalized. Unlock it before recalculating.", "danger")
                    return redirect(url_for("payroll.month", month=month))
                results = calculate_payroll_month(month, current_username(), wage_group=group)
                db.session.add(AuditLog(actor=current_username(), action="Holiday Recheck", detail=f"{month}: payroll rerun against current holiday calendar for {len(results)} employee(s)"))
                db.session.commit()
                scope = f"{GROUP_LABELS[group]} wage" if group else "all open wage types"
                flash(f"Recalculated {scope} for {len(results)} wage record(s). Manual employee changes were kept.", "success")
        except Exception as exc:
            flash(str(exc), "danger")
        return redirect(url_for("payroll.month", month=month))
    sort = request.args.get("sort", "id")
    if sort not in {"id", "name"}:
        sort = "id"
    order = request.args.get("order", "asc")
    if order not in {"asc", "desc"}:
        order = "asc"
    salaries = [
        salary for salary in SalaryRecord.query.filter_by(payroll_month=month).all()
        if employee_active_for_payroll_month(db.session.get(Employee, salary.employee_id), month)
    ]
    salaries = sorted(salaries, key=lambda salary: salary_sort_value(salary, sort), reverse=order == "desc")
    monthly_salaries = [salary for salary in salaries if salary.normalized_salary_type == "MONTHLY"]
    daily_salaries = [salary for salary in salaries if salary.normalized_salary_type == "DAILY"]
    other_salaries = [salary for salary in salaries if salary.normalized_salary_type not in {"MONTHLY", "DAILY"}]
    results = {
        r.employee_id: r
        for r in PayrollResult.query.filter_by(payroll_month=month).all()
        if employee_active_for_payroll_month(db.session.get(Employee, r.employee_id), month)
    }
    attendance_count = AttendanceRecord.query.filter_by(payroll_month=month).count()
    missing_salary = attendance_missing_salary(month)
    mismatches = name_mismatches(month)
    wage_types = sorted({s.normalized_salary_type or "MISSING" for s in salaries})
    wage_filter = normalize_group(request.args.get("wage")) if request.args.get("wage") else None
    groups = group_summary(month, payroll_month)
    steps = payroll_workflow_steps(month, payroll_month, attendance_count, len(salaries), results, locked=any_group_finalized(payroll_month))
    total_payable = sum((Decimal(r.final_salary) for r in results.values() if r.final_salary is not None), Decimal("0"))
    return render_template(
        "payroll_month.html",
        month=month,
        month_label=display_month(month),
        payroll_month=payroll_month,
        is_finalized=payroll_month.status == "FINALIZED",
        salaries=salaries,
        monthly_salaries=monthly_salaries,
        daily_salaries=daily_salaries,
        other_salaries=other_salaries,
        results=results,
        attendance_count=attendance_count,
        missing_salary=missing_salary,
        mismatches=mismatches,
        wage_types=wage_types,
        steps=steps,
        groups=groups,
        wage_filter=wage_filter,
        any_finalized=any_group_finalized(payroll_month),
        bonus_allowance_duration=minutes_to_duration(DAILY_BONUS_RULES["PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES"]),
        bonus_excluded_ids={employee.id for employee in Employee.query.filter_by(bonus_ignored=True).all()},
        total_payable=f"{total_payable:,.2f}",
        review_count=len([r for r in results.values() if r.calculation_status != "Calculated"]) + len(missing_salary),
        sort=sort,
        order=order,
    )


@bp.route("/<month>/employee/<employee_id>", methods=["GET", "POST"])
@login_required
def employee(month, employee_id):
    if not is_valid_payroll_month(month):
        abort(404)
    if request.method == "POST":
        if is_payroll_finalized(month, employee_id):
            flash(LOCKED_MESSAGE, "danger")
            return redirect(url_for("payroll.employee", month=month, employee_id=employee_id))
        action = request.form.get("action", "save")
        try:
            saved_overrides = save_employee_detail_changes(month, employee_id)
            if action == "recalculate":
                calculate_employee_payroll(month, employee_id, current_username())
                flash("Employee changes saved and payroll recalculated.", "success")
            else:
                db.session.commit()
                flash(f"Employee changes saved. {saved_overrides} override row(s) updated. Recalculate payroll to update results.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        return redirect(url_for("payroll.employee", month=month, employee_id=employee_id))
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    employee = db.session.get(Employee, employee_id)
    if employee and not employee_active_for_payroll_month(employee, month):
        flash(f"Employee ID {employee_id} is inactive for payroll month {month}.", "warning")
        return redirect(url_for("payroll.month", month=month))
    result = PayrollResult.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    records = AttendanceRecord.query.filter_by(payroll_month=month, employee_id=employee_id).order_by(AttendanceRecord.date).all()
    overrides = {o.date: o for o in AttendanceOverride.query.filter_by(payroll_month=month, employee_id=employee_id).all()}
    active_loans = active_loans_for_employee(employee_id, month)
    loan_skip = loan_skip_for_employee(employee_id, month)
    active_loan_installment = loan_installment_for_employee(employee_id, month)
    payroll_advances = advances_for_payroll_month(employee_id, month)
    advance_deduction = advance_deduction_for_employee(employee_id, month)
    payroll_month = db.session.get(PayrollMonth, month)
    global_leave_encashment = bool(payroll_month and payroll_month.encash_all_leaves)
    attendance_rows = employee_attendance_rows(records, result, salary, overrides)
    return render_template(
        "employee_detail.html",
        calendar_weeks=attendance_calendar(month, attendance_rows),
        weekday_headings=WEEKDAY_HEADINGS,
        month=month,
        employee_id=employee_id,
        employee=employee,
        salary=salary,
        result=result,
        month_days=payroll_month_days(month),
        total_paid_days=total_paid_days(result),
        active_loans=active_loans,
        has_loan=employee_has_loan(employee_id),
        active_loan_installment=active_loan_installment,
        loan_skip=loan_skip,
        payroll_advances=payroll_advances,
        advance_deduction=advance_deduction,
        global_leave_encashment=global_leave_encashment,
        bonus_absence_duration=minutes_to_duration(int(getattr(result, "absence_minutes", 0) or 0)) if result else "",
        bonus_explanation=daily_bonus_explanation(
            int(getattr(result, "absence_minutes", 0) or 0),
            bool(employee and employee.bonus_ignored),
        ) if result else "",
        bonus_ignored=bool(employee and employee.bonus_ignored),
        bonus_allowance_duration=minutes_to_duration(DAILY_BONUS_RULES["PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES"]),
        attendance_rows=attendance_rows,
        overrides=overrides,
        override_options=OVERRIDE_OPTIONS,
        is_finalized=is_payroll_finalized(month, employee_id),
    )
