from datetime import date, datetime
from decimal import Decimal

from attendance import db
from attendance.models import AuditLog, Employee, LeaveLedger, WeekOffRule


def default_sunday_weekoff_rule(employee_id):
    """Sunday off, awaiting confirmation.

    Deliberately not stamped as confirmed: the app asks for week offs to be checked
    before the first payroll, and a rule the system wrote is exactly the one that
    needs checking. Stamping it here answered that question on the reviewer's behalf,
    so nobody was ever asked and an employee on another day was never noticed.
    """
    return WeekOffRule(employee_id=employee_id, sunday="WEEK_OFF_ALL")


def ensure_employee_defaults(employee_id):
    created = []
    if not WeekOffRule.query.filter_by(employee_id=employee_id).first():
        db.session.add(default_sunday_weekoff_rule(employee_id))
        created.append("Sunday week off rule")
    if not LeaveLedger.query.filter_by(employee_id=employee_id).first():
        db.session.add(LeaveLedger(
            employee_id=employee_id,
            date=date.today(),
            payroll_month="OPENING",
            transaction_type="OPENING",
            amount=Decimal("0.0"),
            description="Auto-created opening leave balance for new employee.",
        ))
        created.append("opening leave balance")
    return created


def backfill_default_weekoffs(actor="system"):
    created_for = []
    for employee in Employee.query.order_by(Employee.id).all():
        if WeekOffRule.query.filter_by(employee_id=employee.id).first():
            continue
        db.session.add(default_sunday_weekoff_rule(employee.id))
        created_for.append(f"{employee.id} - {employee.name}")
    if created_for:
        detail = "Assigned Sunday as default week off for employees without week-off rules: " + "; ".join(created_for)
        db.session.add(AuditLog(actor=actor, action="Default Week Off Backfilled", detail=detail))
    return created_for
