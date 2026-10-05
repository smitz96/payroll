"""Rules from the payroll office's corrected August 2026 salary sheet."""
from datetime import date, datetime
from decimal import Decimal

from attendance import db
from attendance.models import Employee, PayrollMonth, PayrollResult, SalaryRecord
from attendance.payroll_rules import round_net_pay
from attendance.statutory import esi_coverage_wage
from tests.test_logic_fixes import MONTH, calculate, seed


# 1. Net pay in whole rupees

def test_net_pay_is_rounded_half_up_to_the_rupee():
    assert round_net_pay(Decimal("67243.75")) == (Decimal("67244"), Decimal("0.25"))
    assert round_net_pay(Decimal("52722.78")) == (Decimal("52723"), Decimal("0.22"))
    assert round_net_pay(Decimal("13283.40")) == (Decimal("13283"), Decimal("-0.40"))
    assert round_net_pay(Decimal("100.50")) == (Decimal("101"), Decimal("0.50"))


def test_calculated_net_pay_is_whole_rupees_with_the_rounding_recorded(app):
    with app.app_context():
        seed(salary="31000", minutes={6: 500})  # a short day leaves paise in the net
        result, _days = calculate()
        assert Decimal(result.final_salary) == Decimal(result.final_salary).to_integral_value()
        assert Decimal(result.round_off) != 0
        assert abs(Decimal(result.round_off)) <= Decimal("0.50")


def test_salary_slip_shows_the_round_off_so_it_adds_up(app):
    from io import BytesIO
    from pypdf import PdfReader
    from attendance.reports import build_employee_pdf
    with app.app_context():
        seed(salary="31000", minutes={6: 500})
        calculate()
        text = " ".join(PdfReader(BytesIO(build_employee_pdf(MONTH, "5"))).pages[0].extract_text().split())
    assert "Round Off (+/-)" in text


# 2. SHORT LEAVE no longer repeats loss of pay

def test_salary_sheet_short_leave_does_not_repeat_loss_of_pay(app):
    from attendance.reports import SALARY_REGISTER_HEADERS, salary_register_rows
    with app.app_context():
        seed(minutes={6: None, 7: None})  # absences beyond the leave earned
        result, _days = calculate()
        assert Decimal(result.lop_deduction) > 0 or Decimal(result.lop_days) >= 0
        row = salary_register_rows(MONTH)[0]
        assert row[SALARY_REGISTER_HEADERS.index("SHORT LEAVE")] == Decimal("0.00")
        assert row[SALARY_REGISTER_HEADERS.index("NET SALARY")] == Decimal(result.final_salary)


# 5a. ESI coverage on the wage excluding HRA

def test_esi_coverage_wage_leaves_out_hra_up_to_half_of_pay():
    jayesh = Employee(hra=Decimal("7805"))
    assert esi_coverage_wage(jayesh, Decimal("22300")) == Decimal("14495")
    # HRA beyond half of pay counts as wage again.
    heavy_hra = Employee(hra=Decimal("20000"))
    assert esi_coverage_wage(heavy_hra, Decimal("30000")) == Decimal("15000")
    assert esi_coverage_wage(None, Decimal("18000")) == Decimal("18000")


def test_employee_above_21000_gross_is_covered_when_wages_without_hra_are_not(app):
    with app.app_context():
        seed(salary="22300")
        employee = db.session.get(Employee, "5")
        employee.basic_salary, employee.hra, employee.esic_enabled = Decimal("14495"), Decimal("7805"), True
        db.session.commit()
        result, _days = calculate()
        # Contribution is 0.75% of the gross wage, rounded up: 22,300 -> 168.
        assert Decimal(result.esi_employee) == Decimal("168")
        assert Decimal(result.esi_wage) == Decimal("22300.00")
