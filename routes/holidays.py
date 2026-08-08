from flask import Blueprint, flash, redirect, render_template, request, url_for

from attendance import db
from attendance.authentication import current_username, login_required
from attendance.holidays import HOLIDAY_TYPES, normalize_holiday_type
from attendance.models import AuditLog, Holiday
from attendance.utils import parse_csv_date

bp = Blueprint("holidays", __name__, url_prefix="/holidays")


@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        holiday_id = request.form.get("id")
        if request.form.get("action") == "delete":
            holiday = db.session.get(Holiday, holiday_id)
            detail = f"{holiday.date}: {holiday.name}" if holiday else f"Holiday ID {holiday_id}"
            Holiday.query.filter_by(id=holiday_id).delete()
            db.session.add(AuditLog(actor=current_username(), action="Holiday Deleted", detail=detail))
        else:
            date_value = parse_csv_date(request.form.get("date"))
            holiday = db.session.get(Holiday, holiday_id) if holiday_id else Holiday()
            action = "Holiday Updated" if holiday_id else "Holiday Created"
            holiday.date = date_value
            holiday.name = request.form.get("name", "").strip()
            holiday.holiday_type = normalize_holiday_type(request.form.get("holiday_type"))
            holiday.notes = request.form.get("notes", "").strip()
            db.session.add(holiday)
            db.session.add(AuditLog(actor=current_username(), action=action, detail=f"{holiday.date}: {holiday.name}; {HOLIDAY_TYPES[holiday.holiday_type]}"))
        db.session.commit()
        flash("Holiday calendar updated.", "success")
        return redirect(url_for("holidays.index"))
    return render_template("holidays.html", holidays=Holiday.query.order_by(Holiday.date.desc()).all(), holiday_types=HOLIDAY_TYPES)
