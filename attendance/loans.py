from datetime import date
from decimal import Decimal, InvalidOperation

from attendance.models import Loan, LoanInstallmentSkip


def parse_money(value, field_name):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value}") from exc
    if amount < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return amount.quantize(Decimal("0.01"))


def parse_tenure(value):
    try:
        tenure = int(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("Tenure must be a whole number of months.") from exc
    if tenure <= 0:
        raise ValueError("Tenure must be greater than zero.")
    return tenure


def add_months(month_start, count):
    year = month_start.year + ((month_start.month - 1 + count) // 12)
    month = ((month_start.month - 1 + count) % 12) + 1
    return date(year, month, 1)


def month_start_from_ym(month):
    year, month_number = (int(part) for part in month.split("-"))
    return date(year, month_number, 1)


def loan_is_active_for_month(loan, month):
    payroll_month = month_start_from_ym(month)
    deduction_month = add_months(date(loan.start_date.year, loan.start_date.month, 1), 1)
    return bool(loan.is_active and deduction_month <= payroll_month and loan_remaining_before_month(loan, month) > 0)


def active_loans_for_employee(employee_id, month):
    loans = Loan.query.filter_by(employee_id=employee_id, is_active=True).order_by(Loan.start_date.asc(), Loan.id.asc()).all()
    return [loan for loan in loans if loan_is_active_for_month(loan, month)]


def loan_skip_for_employee(employee_id, month):
    return LoanInstallmentSkip.query.filter_by(employee_id=employee_id, payroll_month=month).first()


def month_ym(month_start):
    return f"{month_start.year:04d}-{month_start.month:02d}"


def completed_installment_months_before(loan, month):
    target_month = month_start_from_ym(month)
    cursor = add_months(date(loan.start_date.year, loan.start_date.month, 1), 1)
    count = 0
    while cursor < target_month:
        ym = month_ym(cursor)
        skip = loan_skip_for_employee(loan.employee_id, ym)
        if not skip or not skip.skip:
            count += 1
        cursor = add_months(cursor, 1)
    return count


def loan_paid_before_month(loan, month):
    paid = Decimal(loan.monthly_deduction or 0) * Decimal(completed_installment_months_before(loan, month))
    return min(Decimal(loan.amount or 0), paid).quantize(Decimal("0.01"))


def loan_remaining_before_month(loan, month):
    return (Decimal(loan.amount or 0) - loan_paid_before_month(loan, month)).quantize(Decimal("0.01"))


def loan_installment_for_loan(loan, month):
    skip = loan_skip_for_employee(loan.employee_id, month)
    if skip and skip.skip:
        return Decimal("0.00")
    remaining = loan_remaining_before_month(loan, month)
    if remaining <= 0:
        return Decimal("0.00")
    return min(remaining, Decimal(loan.monthly_deduction or 0)).quantize(Decimal("0.01"))


def loan_installment_for_employee(employee_id, month):
    return sum((loan_installment_for_loan(loan, month) for loan in active_loans_for_employee(employee_id, month)), Decimal("0.00")).quantize(Decimal("0.01"))


def loan_pending_after_month(loan, month):
    remaining = loan_remaining_before_month(loan, month)
    if loan_is_active_for_month(loan, month):
        remaining -= loan_installment_for_loan(loan, month)
    return max(Decimal("0.00"), remaining).quantize(Decimal("0.01"))


def loan_pending_after_month_for_employee(employee_id, month):
    pending = Decimal("0.00")
    for loan in Loan.query.filter_by(employee_id=employee_id, is_active=True).order_by(Loan.start_date.asc(), Loan.id.asc()).all():
        remaining = loan_pending_after_month(loan, month)
        if remaining > 0:
            pending += remaining
    return pending.quantize(Decimal("0.01"))


def employee_has_loan(employee_id):
    return Loan.query.filter_by(employee_id=employee_id).count() > 0


def loan_repayment_schedule(loan, as_of_month):
    as_of = month_start_from_ym(as_of_month)
    cursor = add_months(date(loan.start_date.year, loan.start_date.month, 1), 1)
    remaining = Decimal(loan.amount or 0).quantize(Decimal("0.01"))
    rows = []
    guard = 0
    while remaining > 0 and guard < 240:
        ym = month_ym(cursor)
        skip = loan_skip_for_employee(loan.employee_id, ym)
        installment = Decimal("0.00") if skip and skip.skip else min(remaining, Decimal(loan.monthly_deduction or 0)).quantize(Decimal("0.01"))
        if skip and skip.skip:
            status = "Skipped"
        elif cursor < as_of:
            status = "Paid"
        else:
            status = "Pending"
        rows.append({
            "month": ym,
            "date": cursor,
            "installment": installment,
            "status": status,
            "notes": skip.notes if skip and skip.skip else "",
            "remaining_after": (remaining - installment).quantize(Decimal("0.01")),
        })
        remaining = (remaining - installment).quantize(Decimal("0.01"))
        cursor = add_months(cursor, 1)
        guard += 1
    return rows
