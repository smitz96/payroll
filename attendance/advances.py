from datetime import date
from decimal import Decimal

from attendance.models import AdvanceSalary


def add_months(month_start, count):
    year = month_start.year + ((month_start.month - 1 + count) // 12)
    month = ((month_start.month - 1 + count) % 12) + 1
    return date(year, month, 1)


def month_start_from_ym(month):
    year, month_number = (int(part) for part in month.split("-"))
    return date(year, month_number, 1)


def deduction_month_for_advance(advance):
    advance_month = date(advance.advance_date.year, advance.advance_date.month, 1)
    return add_months(advance_month, 1).strftime("%Y-%m")


def advances_for_payroll_month(employee_id, month):
    advances = AdvanceSalary.query.filter_by(employee_id=employee_id).order_by(AdvanceSalary.advance_date.asc(), AdvanceSalary.id.asc()).all()
    return [advance for advance in advances if deduction_month_for_advance(advance) == month]


def advance_deduction_for_employee(employee_id, month):
    return sum((Decimal(advance.amount or 0) for advance in advances_for_payroll_month(employee_id, month)), Decimal("0.00")).quantize(Decimal("0.01"))
