from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.models import AuditLog, Employee
from attendance.weekoffs import WEEKDAY_DISPLAY_FIELDS, WEEKDAY_FIELDS, WEEK_OFF_OPTIONS, get_or_create_weekoff_rule, normalize_weekoff_codes, selected_weekoff_codes

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
            rule = get_or_create_weekoff_rule(employee.id)
            employee_changes = []
            for field, label in WEEKDAY_FIELDS:
                value = normalize_weekoff_codes(request.form.getlist(f"{employee.id}_{field}") or ["WORKING"])
                old = getattr(rule, field)
                if old != value:
                    setattr(rule, field, value)
                    employee_changes.append(f"{label}: {old} -> {value}")
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
        rows.append({"employee": employee, "rule": get_or_create_weekoff_rule(employee.id)})
    db.session.commit()
    return render_template("weekoffs.html", rows=rows, weekdays=WEEKDAY_DISPLAY_FIELDS, options=WEEK_OFF_OPTIONS, selected_weekoff_codes=selected_weekoff_codes, sort=sort, order=order)
