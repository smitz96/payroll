from decimal import Decimal

from attendance.payroll_rules import (
    calculate_monthly_leave_earned,
    days_in_payroll_month,
    calculate_monthly_overtime,
    calculate_day_overtime,
    calculate_less_hours,
    calculate_monthly_shortage,
    classify_monthly_attendance,
    punch_clock_minutes,
)
from attendance.models import AttendanceRecord
from attendance.utils import floor_to_interval, parse_csv_date, parse_duration


def test_shortage_is_charged_below_the_shift_length_when_there_is_no_grace():
    # Normal Shift with no grace: required hours are the full 9 hours.
    assert calculate_monthly_shortage(parse_duration("9h 00m")) == 0
    assert calculate_monthly_shortage(parse_duration("8h 59m")) == 15
    assert calculate_monthly_shortage(parse_duration("8h 45m")) == 15
    assert calculate_monthly_shortage(parse_duration("8h 44m")) == 30


def test_grace_lowers_the_bar_but_the_shortfall_counts_from_the_shift_length():
    from attendance.shifts import ShiftTimes
    shift = ShiftTimes("Normal Shift", 570, 1110, late_in_grace=10)
    assert shift.required_minutes == 530
    assert calculate_monthly_shortage(parse_duration("8h 52m"), shift) == 0
    assert calculate_monthly_shortage(parse_duration("8h 50m"), shift) == 0
    # 20 minutes short of 9 hours, rounded up.
    assert calculate_monthly_shortage(parse_duration("8h 40m"), shift) == 30
    assert calculate_monthly_shortage(parse_duration("7h 30m"), shift) == 90


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


TEN_MINUTE_IN_GRACE = __import__("attendance.shifts", fromlist=["ShiftTimes"]).ShiftTimes("Normal Shift", 570, 1110, late_in_grace=10)


def test_enough_hours_carry_no_penalty_however_late_the_check_in():
    # 9:42 to 6:34 is 8h52m, past the 8h50m bar: late, but the hours are done.
    assert calculate_less_hours(punched_day("09:42 AM", "06:34 PM"), TEN_MINUTE_IN_GRACE) == (12, 0, 0)


def test_short_hours_charge_the_shortfall_from_the_shift_length():
    # 9:50 to 6:30 is 8h40m: 20 minutes short of 9 hours, charged as 30.
    assert calculate_less_hours(punched_day("09:50 AM", "06:30 PM"), TEN_MINUTE_IN_GRACE) == (20, 0, 30)
    # Late and early together: 9:50 to 6:10 is 8h20m, 40 short, charged as 45.
    assert calculate_less_hours(punched_day("09:50 AM", "06:10 PM"), TEN_MINUTE_IN_GRACE) == (20, 20, 45)


def test_a_long_break_is_charged_through_the_hours_even_with_punctual_punches():
    day = punched_day("09:30 AM", "06:30 PM", actual=parse_duration("7h 30m"))
    assert calculate_less_hours(day, TEN_MINUTE_IN_GRACE) == (0, 0, 90)


def test_late_in_and_early_out_are_reported_in_clock_minutes_past_each_grace():
    from attendance.shifts import ShiftTimes
    shift = ShiftTimes("Normal Shift", 570, 1110, late_in_grace=10, early_out_grace=5)
    day = punched_day("09:38 AM", "06:20 PM")
    # Inside the check-in grace, so no late minutes; 10 minutes early is past it.
    late_in, early_out, _charged = calculate_less_hours(day, shift)
    assert (late_in, early_out) == (0, 10)


def test_punch_times_are_read_in_24_hour_form_too():
    assert calculate_less_hours(punched_day("09:41", "18:29")) == (11, 1, 15)


def test_half_days_and_short_days_carry_no_less_hours():
    assert calculate_less_hours(punched_day("01:00 PM", "06:30 PM")) == (0, 0, 0)
    assert calculate_less_hours(punched_day("09:30 AM", "11:00 AM")) == (0, 0, 0)


def test_less_hours_without_punch_times_still_uses_working_hours():
    record = AttendanceRecord(date=__import__("datetime").date(2026, 7, 1), actual_minutes=parse_duration("8h 12m"), parse_status="OK")
    assert calculate_less_hours(record) == (0, 0, 60)


def test_overtime_is_paid_on_hours_beyond_the_shift_once_past_the_ot_grace():
    expected = {
        "9h 00m": 0,
        "9h 29m": 0,
        "9h 30m": 30,
        "9h 50m": 45,
        "10h 00m": 60,
        "10h 14m": 60,
    }
    for worked, payable in expected.items():
        assert calculate_monthly_overtime(parse_duration(worked), Decimal("10"))[1] == payable, worked


def test_overtime_grace_is_set_per_shift():
    from attendance.shifts import ShiftTimes
    no_grace = ShiftTimes("Strict", 570, 1110, overtime_grace=0)
    hour_grace = ShiftTimes("Relaxed", 570, 1110, overtime_grace=60)
    assert calculate_monthly_overtime(parse_duration("9h 15m"), Decimal("10"), shift=no_grace)[1] == 15
    assert calculate_monthly_overtime(parse_duration("9h 50m"), Decimal("10"), shift=hour_grace)[1] == 0
    assert calculate_monthly_overtime(parse_duration("10h 00m"), Decimal("10"), shift=hour_grace)[1] == 60


def test_late_arrival_who_stays_late_is_judged_on_hours():
    # 9:50 to 7:40 is 9h50m: no less hours, 45 minutes of overtime.
    day = punched_day("09:50 AM", "07:40 PM")
    assert calculate_less_hours(day, TEN_MINUTE_IN_GRACE)[2] == 0
    raw, payable, amount = calculate_day_overtime(day, "Full Day Present", Decimal("10"), TEN_MINUTE_IN_GRACE)
    assert (raw, payable, amount) == (50, 45, Decimal("30"))


def test_arriving_early_counts_towards_overtime():
    # 8:00 to 6:30 is 10h30m of work, so 1h30m beyond the 9-hour shift.
    assert calculate_day_overtime(punched_day("08:00 AM", "06:30 PM"), "Full Day Present", Decimal("10"))[1] == 90


def test_week_off_worked_uses_the_same_overtime_rule():
    day = punched_day("08:00 AM", "06:00 PM")
    assert calculate_day_overtime(day, "Week Off Worked", Decimal("10"))[1] == 60
