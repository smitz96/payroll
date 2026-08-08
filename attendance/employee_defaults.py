from datetime import date
from decimal import Decimal

from attendance import db
from attendance.models import LeaveLedger, WeekOffRule


def ensure_employee_defaults(employee_id):
    created = []
    if not WeekOffRule.query.filter_by(employee_id=employee_id).first():
        db.session.add(WeekOffRule(employee_id=employee_id))
        created.append("week off rule")
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
