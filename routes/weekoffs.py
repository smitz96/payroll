from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.models import AuditLog, Employee, Shift
from attendance.shifts import (
    all_shifts,
    clear_shift_cache,
    format_input_clock,
    format_threshold,
    shift_for_rule_field,
    shift_times,
    shift_usage,
    stored_shift_id,
    validate_shift,
    weekday_shift_field,
)
from attendance.utils import display_month
from attendance.weekoffs import WEEKDAY_DISPLAY_FIELDS, WEEKDAY_FIELDS, WEEK_OFF_OPTIONS, get_or_create_weekoff_rule, months_needing_recalculation, normalize_weekoff_codes, selected_weekoff_codes

bp = Blueprint("weekoffs", __name__, url_prefix="/weekoffs")


def employee_sort_value(employee, sort):
    if sort == "name":
        return (employee.name or "").lower()
    return int(employee.id) if str(employee.id).isdigit() else str(employee.id).lower()


@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    sort = request.args.get("sort", "id")
    if sort not in {"id", "name"}:
        sort = "id"
    order = request.args.get("order", "asc")
    if order not in {"asc", "desc"}:
        order = "asc"
    employees = Employee.query.all()
    employees = sorted(employees, key=lambda employee: employee_sort_value(employee, sort), reverse=order == "desc")
    if request.method == "POST":
        changed = 0
        confirmed = 0
        details = []
        for employee in employees:
            # An employee the form did not carry is left exactly as they are. Reading
            # a missing checkbox as "working" would quietly turn their week offs into
            # unpaid absences, which is thousands of rupees a month, and the
            # attendance save already works this way.
            if f"{employee.id}_present" not in request.form:
                continue
            rule = get_or_create_weekoff_rule(employee.id)
            employee_changes = []
            for field, label in WEEKDAY_FIELDS:
                value = normalize_weekoff_codes(request.form.getlist(f"{employee.id}_{field}") or ["WORKING"])
                old = getattr(rule, field)
                if old != value:
                    setattr(rule, field, value)
                    employee_changes.append(f"{label}: {old} -> {value}")
                shift_key = f"{employee.id}_{field}_shift"
                if shift_key in request.form:
                    try:
                        new_shift_id = stored_shift_id(request.form.get(shift_key))
                    except ValueError:
                        # A shift deleted while this page was open. Leave the day as
                        # it is rather than guessing which shift was meant.
                        continue
                    shift_field = weekday_shift_field(field)
                    if getattr(rule, shift_field) != new_shift_id:
                        before = shift_for_rule_field(rule, field).name
                        setattr(rule, shift_field, new_shift_id)
                        employee_changes.append(f"{label} shift: {before} -> {shift_for_rule_field(rule, field).name}")
            if not rule.confirmed_at:
                rule.confirmed_at = datetime.utcnow()
                confirmed += 1
                if not employee_changes:
                    details.append(f"{employee.id} {employee.name}: week off confirmed")
            if employee_changes:
                changed += 1
                details.append(f"{employee.id} {employee.name}: " + "; ".join(employee_changes))
        if changed or confirmed:
            db.session.add(AuditLog(
                actor=current_username(),
                action="Week Off Rules Changed",
                detail=" | ".join(details),
            ))
        db.session.commit()
        flash(f"Week off settings saved. {changed} employee rule(s) changed. {confirmed} employee rule(s) confirmed.", "success")
        return redirect(url_for("weekoffs.index"))

    rows = []
    for employee in employees:
        rule = get_or_create_weekoff_rule(employee.id)
        rows.append({
            "employee": employee,
            "rule": rule,
            "shift_ids": {field: shift_for_rule_field(rule, field).id for field, _label in WEEKDAY_FIELDS},
        })
    db.session.commit()
    shifts = all_shifts()
    shift_rows = []
    for shift in shifts:
        times = shift_times(shift)
        shift_rows.append({
            "shift": shift,
            "start": format_input_clock(shift.start_minutes),
            "end": format_input_clock(shift.end_minutes),
            "length": format_threshold(times.length),
            "full_day": format_threshold(times.full_day_minimum),
            "half_day": format_threshold(times.half_day_minimum),
            "used_by": len(shift_usage(shift.id)) if not shift.is_default else None,
        })
    return render_template(
        "weekoffs.html", rows=rows, weekdays=WEEKDAY_DISPLAY_FIELDS, options=WEEK_OFF_OPTIONS,
        selected_weekoff_codes=selected_weekoff_codes, sort=sort, order=order,
        shifts=[shift_times(shift) for shift in shifts], shift_rows=shift_rows,
        default_shift_id=next((shift.id for shift in shifts if shift.is_default), None),
        stale_months=[(month, display_month(month)) for month in months_needing_recalculation()],
        open_shift_panel=request.args.get("shifts") == "open",
    )


@bp.route("/shifts", methods=["POST"])
@login_required
def save_shifts():
    """Edit every listed shift, and add a new one if its row was filled in."""
    details = []
    try:
        pending = []
        for shift in Shift.query.all():
            if f"shift_{shift.id}_name" not in request.form:
                continue
            name, start, end = validate_shift(
                request.form.get(f"shift_{shift.id}_name"),
                request.form.get(f"shift_{shift.id}_start"),
                request.form.get(f"shift_{shift.id}_end"),
                shift.id,
            )
            pending.append((shift, name, start, end))
        new_values = [request.form.get(key, "").strip() for key in ("new_name", "new_start", "new_end")]
        new_shift = None
        if any(new_values):
            name, start, end = validate_shift(*new_values)
            taken = {pending_name.lower() for _shift, pending_name, _start, _end in pending}
            if name.lower() in taken:
                raise ValueError(f'A shift named "{name}" already exists.')
            new_shift = (name, start, end)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("weekoffs.index", shifts="open"))
    for shift, name, start, end in pending:
        before = shift_times(shift)
        if (shift.name, shift.start_minutes, shift.end_minutes) != (name, start, end):
            shift.name, shift.start_minutes, shift.end_minutes = name, start, end
            details.append(f"{before.label} -> {shift_times(shift).label}")
    if new_shift:
        name, start, end = new_shift
        shift = Shift(name=name, start_minutes=start, end_minutes=end, is_default=False)
        db.session.add(shift)
        db.session.flush()
        details.append(f"Added {shift_times(shift).label}")
    if details:
        db.session.add(AuditLog(actor=current_username(), action="Shifts Changed", detail=" | ".join(details)))
        db.session.commit()
        clear_shift_cache()
        flash("Shifts saved.", "success")
    else:
        flash("No shift changes to save.", "info")
    return redirect(url_for("weekoffs.index", shifts="open"))


@bp.route("/shifts/<int:shift_id>/delete", methods=["POST"])
@login_required
def delete_shift(shift_id):
    shift = db.session.get(Shift, shift_id)
    if not shift:
        flash("That shift no longer exists.", "warning")
        return redirect(url_for("weekoffs.index"))
    if shift.is_default:
        flash(f"{shift.name} is the default shift and cannot be deleted. Its times can be edited.", "danger")
        return redirect(url_for("weekoffs.index"))
    used_by = shift_usage(shift.id)
    if used_by:
        flash(f"{shift.name} is still assigned to {len(used_by)} employee(s): {', '.join(used_by)}. "
              f"Move them to another shift first.", "danger")
        return redirect(url_for("weekoffs.index"))
    label = shift_times(shift).label
    db.session.delete(shift)
    db.session.add(AuditLog(actor=current_username(), action="Shift Deleted", detail=label))
    db.session.commit()
    flash(f"Deleted {label}.", "success")
    return redirect(url_for("weekoffs.index"))
