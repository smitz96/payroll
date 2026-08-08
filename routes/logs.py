from datetime import datetime

from flask import Blueprint, render_template, request

from attendance.authentication import login_required
from attendance.models import AuditLog
from attendance.utils import ist_day_to_utc_bounds

bp = Blueprint("logs", __name__, url_prefix="/logs")


def parse_filter_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


@bp.route("")
@login_required
def index():
    per_page = 50
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)
    start_date = parse_filter_date(request.args.get("start_date"))
    end_date = parse_filter_date(request.args.get("end_date"))
    action_filter = (request.args.get("action") or "").strip()
    actions = [row[0] for row in AuditLog.query.with_entities(AuditLog.action).distinct().order_by(AuditLog.action.asc()).all()]
    query = AuditLog.query
    if start_date:
        start_at, _ = ist_day_to_utc_bounds(start_date)
        query = query.filter(AuditLog.created_at >= start_at)
    if end_date:
        _, end_at = ist_day_to_utc_bounds(end_date)
        query = query.filter(AuditLog.created_at <= end_at)
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    total = query.count()
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    logs = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    filters = {
        "start_date": start_date.isoformat() if start_date else "",
        "end_date": end_date.isoformat() if end_date else "",
        "action": action_filter,
    }
    return render_template(
        "logs.html",
        logs=logs,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
        actions=actions,
        filters=filters,
    )
