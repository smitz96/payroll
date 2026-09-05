from decimal import Decimal

from attendance.payroll_rules import (
    calculate_monthly_leave_earned,
    days_in_payroll_month,
    calculate_monthly_overtime,
    calculate_monthly_shortage,
    classify_monthly_attendance,
)
from attendance.models import AttendanceRecord
from attendance.utils import floor_to_interval, parse_csv_date, parse_duration


def test_full_day_grace_shortage_boundary():
    assert calculate_monthly_shortage(parse_duration("9h 00m")) == 0
    assert calculate_monthly_shortage(parse_duration("8h 59m")) == 0
    assert calculate_monthly_shortage(parse_duration("8h 50m")) == 0
    assert calculate_monthly_shortage(parse_duration("8h 49m")) == 15


def test_monthly_rounding_floors_to_previous_15_minutes():
    cases = {
        "8h 49m": "8h 45m",
        "8h 03m": "8h 00m",
        "8h 00m": "8h 00m",
        "8h 14m": "8h 00m",
        "8h 15m": "8h 15m",
        "8h 59m": "8h 45m",
        "9h 00m": "9h 00m",
    }
    for raw, rounded in cases.items():
        assert floor_to_interval(parse_duration(raw), 15) == parse_duration(rounded)


def test_half_day_and_lop_classification():
    for raw in ["5h 59m", "4h 30m", "4h 00m", "3h 00m"]:
        rec = AttendanceRecord(date=__import__("datetime").date(2026, 7, 1), actual_minutes=parse_duration(raw), parse_status="OK")
        assert classify_monthly_attendance(rec)["status"] == "Half Day Present"
    for raw in ["2h 59m", "0h 00m"]:
        rec = AttendanceRecord(date=__import__("datetime").date(2026, 7, 1), actual_minutes=parse_duration(raw), parse_status="OK")
        assert classify_monthly_attendance(rec)["status"] == "Full Day LOP"


def test_overtime_threshold_and_flooring():
    expected = {
        "9h 00m": 0,
        "9h 10m": 0,
        "9h 14m": 0,
        "9h 15m": 0,
        "9h 16m": 0,
        "9h 29m": 0,
        "9h 30m": 30,
        "9h 31m": 30,
        "9h 42m": 30,
        "9h 45m": 45,
        "10h 00m": 60,
        "10h 14m": 60,
        "10h 17m": 75,
    }
    for raw, payable in expected.items():
        assert calculate_monthly_overtime(parse_duration(raw), Decimal("10"))[1] == payable


def test_leave_earning_uses_month_days_and_truncates_two_decimals():
    expected = {
        (0, 31): Decimal("0.00"),
        (15, 30): Decimal("1.00"),
        (28, 31): Decimal("1.80"),
        (28, 28): Decimal("2.00"),
        # A single decimal truncated this to 1.4, losing most of a tenth of a day.
        (23, 31): Decimal("1.48"),
    }
    for (eligible_days, days_in_month), earned in expected.items():
        assert calculate_monthly_leave_earned(Decimal(eligible_days), days_in_month) == earned


def test_days_in_payroll_month_handles_calendar_month_length():
    assert days_in_payroll_month("2026-02") == 28
    assert days_in_payroll_month("2026-07") == 31


def test_parse_date_accepts_manual_and_picker_formats():
    assert parse_csv_date("15-08-2026").isoformat() == "2026-08-15"
    assert parse_csv_date("2026-08-15").isoformat() == "2026-08-15"
    assert parse_csv_date("15/08/2026").isoformat() == "2026-08-15"
    assert parse_csv_date("15/08/26").isoformat() == "2026-08-15"
