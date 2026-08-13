"""Per-wage-type finalization.

Monthly and Daily payroll are reviewed and signed off independently, so each wage
group carries its own finalized timestamp on PayrollMonth. Everything that needs to
know "is this locked?" goes through here rather than reading PayrollMonth.status,
which is only the roll-up of the groups.
"""
from datetime import datetime

from attendance import db
from attendance.models import AuditLog, Employee, PayrollMonth, SalaryRecord

MONTHLY = "MONTHLY"
DAILY = "DAILY"
WAGE_GROUPS = (MONTHLY, DAILY)
GROUP_LABELS = {MONTHLY: "Monthly", DAILY: "Daily"}
_FIELDS = {MONTHLY: "monthly_finalized_at", DAILY: "daily_finalized_at"}


def normalize_group(value):
    """Accept 'monthly'/'MONTHLY'/'Monthly' and return the canonical key, else None."""
    text = str(value or "").strip().upper()
    return text if text in WAGE_GROUPS else None


def group_finalized_at(payroll_month, group):
    group = normalize_group(group)
    if not payroll_month or not group:
        return None
    return getattr(payroll_month, _FIELDS[group], None)


def is_group_finalized(payroll_month, group):
    return group_finalized_at(payroll_month, group) is not None


def groups_with_employees(month):
    """Wage groups that actually have wage records for the month."""
    rows = db.session.query(SalaryRecord.normalized_salary_type).filter_by(payroll_month=month).distinct().all()
    present = {normalize_group(row[0]) for row in rows}
    return [group for group in WAGE_GROUPS if group in present]


def refresh_month_status(payroll_month):
    """Roll the per-group state up into PayrollMonth.status.

    The month counts as finalized only when every wage group that has employees is
    finalized. A month with no wage records yet can never be finalized.
    """
    if not payroll_month:
        return
    present = groups_with_employees(payroll_month.month)
    finalized = [group for group in present if is_group_finalized(payroll_month, group)]
    if present and len(finalized) == len(present):
        payroll_month.status = "FINALIZED"
        payroll_month.finalized_at = max(group_finalized_at(payroll_month, group) for group in finalized)
    else:
        payroll_month.status = "DRAFT"
        payroll_month.finalized_at = None
    db.session.add(payroll_month)


def finalize_group(payroll_month, group, actor, detail=""):
    group = normalize_group(group)
    if not group:
        raise ValueError("Select a wage type to finalize.")
    if is_group_finalized(payroll_month, group):
        raise ValueError(f"{GROUP_LABELS[group]} payroll is already finalized.")
    if group not in groups_with_employees(payroll_month.month):
        raise ValueError(f"No {GROUP_LABELS[group].lower()} wage employees to finalize for this month.")
    setattr(payroll_month, _FIELDS[group], datetime.utcnow())
    refresh_month_status(payroll_month)
    db.session.add(AuditLog(
        actor=actor,
        action="Payroll Finalized",
        detail=f"{payroll_month.month}: {GROUP_LABELS[group]} wage payroll locked{('; ' + detail) if detail else ''}",
    ))
    return group


def unlock_group(payroll_month, group, actor):
    group = normalize_group(group)
    if not group:
        raise ValueError("Select a wage type to unlock.")
    if not is_group_finalized(payroll_month, group):
        raise ValueError(f"{GROUP_LABELS[group]} payroll is not finalized.")
    setattr(payroll_month, _FIELDS[group], None)
    refresh_month_status(payroll_month)
    db.session.add(AuditLog(
        actor=actor,
        action="Payroll Unlocked",
        detail=f"{payroll_month.month}: {GROUP_LABELS[group]} wage payroll reopened for changes",
    ))
    return group


def finalized_groups(payroll_month):
    return [group for group in WAGE_GROUPS if is_group_finalized(payroll_month, group)]


def open_groups(month, payroll_month):
    """Groups that still accept calculation for this month."""
    return [group for group in groups_with_employees(month) if not is_group_finalized(payroll_month, group)]


def any_group_finalized(payroll_month):
    return bool(finalized_groups(payroll_month))


def employee_group(employee_id):
    """The wage group an employee belongs to, or None when their type has no rule."""
    employee = db.session.get(Employee, employee_id)
    return normalize_group(employee.normalized_salary_type) if employee else None


def employee_locked(month, employee_id):
    """True when this employee's wage group is finalized for the month."""
    payroll_month = db.session.get(PayrollMonth, month)
    if not payroll_month:
        return False
    group = employee_group(employee_id)
    if not group:
        # Wage types without a payroll rule follow the overall month state.
        return payroll_month.status == "FINALIZED"
    return is_group_finalized(payroll_month, group)


def group_summary(month, payroll_month):
    """Per-group state for the payroll month page."""
    summary = []
    present = groups_with_employees(month)
    for group in WAGE_GROUPS:
        summary.append({
            "key": group,
            "slug": group.lower(),
            "label": GROUP_LABELS[group],
            "has_employees": group in present,
            "finalized": is_group_finalized(payroll_month, group),
            "finalized_at": group_finalized_at(payroll_month, group),
        })
    return summary


def months_open_before(month):
    """Earlier payroll months that have not been finalized, oldest first.

    A month's opening leave balance is the previous month's closing figure, so
    starting a new month while an earlier one is still being edited means the new
    month is built on a number that can still move.
    """
    return [
        item.month for item in
        PayrollMonth.query.filter(PayrollMonth.month < month).order_by(PayrollMonth.month).all()
        if item.status != "FINALIZED"
    ]
