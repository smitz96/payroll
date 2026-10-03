from decimal import Decimal

from attendance.payroll_rules import (
    calculate_monthly_leave_earned,
    days_in_payroll_month,
    calculate_monthly_overtime,
    calculate_day_overtime,
    calculate_less_hours,
    calculate_monthly_shortage,
    calculate_shift_overtime,
    classify_monthly_attendance,
    punch_clock_minutes,
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


def punched_day(first, last, actual=None):
    """A full working day punched `first` to `last`, with matching working minutes."""
    if actual is None:
        start, end = punch_clock_minutes(first), punch_clock_minutes(last)
        actual = (end - start) % (24 * 60)
    return AttendanceRecord(date=__import__("datetime").date(2026, 7, 1), first_punch=first,
                            last_punch=last, punches_json=[first, last], actual_minutes=actual, parse_status="OK")


def test_late_check_in_has_ten_minute_grace_then_rounds_up_from_shift_start():
    expected = {
        "09:30 AM": 0,
        "09:35 AM": 0,
        "09:40 AM": 0,
        "09:41 AM": 15,
        "09:45 AM": 15,
        "09:46 AM": 30,
        "09:50 AM": 30,
        "10:00 AM": 30,
        "10:01 AM": 45,
    }
    for first, late in expected.items():
        assert calculate_less_hours(punched_day(first, "06:30 PM")) == (late, 0, late), first


def test_early_check_out_rounds_up_to_shift_end_with_no_grace():
    expected = {
        "06:45 PM": 0,
        "06:30 PM": 0,
        "06:29 PM": 15,
        "06:15 PM": 15,
        "06:14 PM": 30,
        "06:00 PM": 30,
        "05:59 PM": 45,
    }
    for last, early in expected.items():
        assert calculate_less_hours(punched_day("09:30 AM", last)) == (0, early, early), last


def test_late_in_and_early_out_are_added():
    assert calculate_less_hours(punched_day("09:50 AM", "06:10 PM")) == (30, 30, 60)
    # The 8h50m grace no longer waives less hours: 9:45 to 6:40 is still 15 late.
    assert calculate_less_hours(punched_day("09:45 AM", "06:40 PM")) == (15, 0, 15)


def test_punch_times_are_read_in_24_hour_form_too():
    assert calculate_less_hours(punched_day("09:41", "18:29")) == (15, 15, 30)


def test_half_days_and_short_days_carry_no_less_hours():
    assert calculate_less_hours(punched_day("01:00 PM", "06:30 PM")) == (0, 0, 0)
    assert calculate_less_hours(punched_day("09:30 AM", "11:00 AM")) == (0, 0, 0)


def test_less_hours_without_punch_times_falls_back_to_total_hours():
    record = AttendanceRecord(date=__import__("datetime").date(2026, 7, 1), actual_minutes=parse_duration("8h 12m"), parse_status="OK")
    assert calculate_less_hours(record) == (0, 0, 60)


def test_full_day_overtime_counts_from_shift_end_after_thirty_minutes():
    expected = {
        "06:30 PM": 0,
        "06:50 PM": 0,
        "06:59 PM": 0,
        "07:00 PM": 30,
        "07:10 PM": 30,
        "07:40 PM": 60,
        "08:00 PM": 90,
    }
    for last, payable in expected.items():
        assert calculate_shift_overtime(punched_day("09:30 AM", last), Decimal("10"))[1] == payable, last


def test_late_arrival_still_earns_overtime_after_shift_end():
    day = punched_day("09:50 AM", "07:40 PM")
    assert calculate_less_hours(day) == (30, 0, 30)
    raw, payable, amount = calculate_day_overtime(day, "Full Day Present", Decimal("10"))
    assert (raw, payable, amount) == (70, 60, Decimal("40"))


def test_arriving_early_earns_no_overtime():
    assert calculate_day_overtime(punched_day("08:00 AM", "06:30 PM"), "Full Day Present", Decimal("10"))[1] == 0


def test_week_off_worked_keeps_overtime_on_total_hours():
    day = punched_day("08:00 AM", "06:00 PM")
    assert calculate_day_overtime(day, "Week Off Worked", Decimal("10"))[1] == 60
