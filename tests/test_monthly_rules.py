from decimal import Decimal

from attendance.payroll_rules import (
    calculate_monthly_leave_earned,
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
        "9h 30m": 15,
        "9h 31m": 15,
        "9h 44m": 15,
        "9h 45m": 30,
        "10h 00m": 45,
        "10h 17m": 60,
    }
    for raw, payable in expected.items():
        assert calculate_monthly_overtime(parse_duration(raw), Decimal("10"))[1] == payable


def test_leave_earning_truncates_one_decimal():
    expected = {
        0: Decimal("0.0"),
        6: Decimal("0.5"),
        12: Decimal("1.0"),
        15: Decimal("1.2"),
        18: Decimal("1.5"),
        24: Decimal("2.0"),
    }
    for paid_days, earned in expected.items():
        assert calculate_monthly_leave_earned(Decimal(paid_days)) == earned


def test_parse_date_accepts_manual_and_picker_formats():
    assert parse_csv_date("15-08-2026").isoformat() == "2026-08-15"
    assert parse_csv_date("2026-08-15").isoformat() == "2026-08-15"
