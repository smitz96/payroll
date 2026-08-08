from decimal import Decimal

from attendance.payroll_rules import UnsupportedPayrollResult, resolve_payroll_rule
from attendance.models import SalaryRecord
from attendance.utils import normalize_salary_type


def test_monthly_normalization_resolves_only_monthly():
    for value in ["Monthly", "monthly", "MONTHLY", " Monthly", "Monthly "]:
        assert normalize_salary_type(value) == "MONTHLY"
        assert resolve_payroll_rule(normalize_salary_type(value)) is not None


def test_unsupported_types_do_not_resolve_to_monthly():
    for value in ["Daily", "Hourly", "Contract", "", None, "Unknown"]:
        normalized = normalize_salary_type(value)
        assert normalized != "MONTHLY"
        assert resolve_payroll_rule(normalized) is None


def test_unsupported_result_preserves_salary_without_final_calculation(app):
    salary = SalaryRecord(
        payroll_month="2026-07",
        employee_id="5",
        name="Daily Employee",
        salary_type="Daily",
        normalized_salary_type="DAILY",
        salary=Decimal("25000"),
        adjustment=Decimal("100"),
    )
    result = UnsupportedPayrollResult(salary).to_model()
    assert result.calculation_status == "Payroll Rules Not Configured"
    assert result.final_salary is None
    assert result.manual_adjustment == Decimal("100")
    assert "not configured" in result.message
