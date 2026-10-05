"""Regression tests for payroll logic bugs found by probing the rules with scenarios."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from attendance import db
from attendance.calculator import calculate_payroll_month
from attendance.models import (AttendanceOverride, AttendanceRecord, Employee, Holiday, PayrollMonth, PayrollResult,
                               SalaryRecord, WeekOffRule)
from attendance.payroll_rules import calculate_monthly_overtime
from attendance.shifts import ShiftTimes
from attendance.wage_groups import finalize_group

MONTH = "2026-07"


def clock(minutes):
    hours, mins = divmod(minutes, 60)
    return f"{(hours % 12) or 12:02d}:{mins:02d} {'AM' if hours < 12 else 'PM'}"


def seed(wage="MONTHLY", salary="31000", minutes=None, overrides=None, holidays=(), opening=None,
         joined_on=None, left_on=None):
    """One employee for July 2026. `minutes` maps a day to minutes worked from 9:30
    (None = no punches); unlisted days are 9 hours, Sundays off."""
    db.session.add(PayrollMonth(month=MONTH))
    db.session.add(Employee(id="5", name="Worker", salary_type=wage.title(), normalized_salary_type=wage,
                            salary=Decimal(salary), joined_on=joined_on, left_on=left_on))
    db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
    db.session.add(SalaryRecord(payroll_month=MONTH, employee_id="5", name="Worker", salary_type=wage.title(),
                                normalized_salary_type=wage, salary=Decimal(salary)))
    for day in holidays:
        db.session.add(Holiday(date=date(2026, 7, day), name="Holiday"))
    if opening is not None:
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY",
                                     calculation_status="Calculated", closing_leave=opening))
    for day in range(1, 32):
        when = date(2026, 7, day)
        worked = (minutes or {}).get(day, 540 if when.weekday() != 6 else None)
        if worked is None:
            db.session.add(AttendanceRecord(payroll_month=MONTH, employee_id="5", date=when, day=when.strftime("%A"),
                                            parse_status="NEEDS_REVIEW", warning="Missing punch and working hours"))
        else:
            punches = [clock(570), clock(570 + worked)]
            db.session.add(AttendanceRecord(payroll_month=MONTH, employee_id="5", date=when, day=when.strftime("%A"),
                                            first_punch=punches[0], last_punch=punches[1], punches_json=punches,
                                            actual_minutes=worked, raw_working_hours="x", parse_status="OK"))
    for day, status in (overrides or {}).items():
        db.session.add(AttendanceOverride(payroll_month=MONTH, employee_id="5", date=date(2026, 7, day), manual_status=status))
    db.session.commit()


def calculate():
    calculate_payroll_month(MONTH)
    result = PayrollResult.query.filter_by(payroll_month=MONTH, employee_id="5").one()
    return result, {row["date"]: row for row in result.detail_json}


# 1. Unresolved review days

def test_unresolved_review_days_are_unpaid_for_monthly_wage_too(app):
    with app.app_context():
        seed()
        record = AttendanceRecord.query.filter_by(employee_id="5", date=date(2026, 7, 6)).one()
        record.parse_status = "NEEDS_REVIEW"
        record.punches_json = [record.first_punch, record.last_punch, "07:00 PM"]
        db.session.commit()
        result, _days = calculate()
        assert result.calculation_status == "Needs Review"
        assert Decimal(result.lop_days) == 1


def test_a_wage_group_with_review_days_cannot_be_finalized(app):
    with app.app_context():
        seed()
        record = AttendanceRecord.query.filter_by(employee_id="5", date=date(2026, 7, 6)).one()
        record.parse_status = "NEEDS_REVIEW"
        record.punches_json = [record.first_punch, record.last_punch, "07:00 PM"]
        db.session.commit()
        calculate()
        with pytest.raises(ValueError, match="cannot be finalized while 1 employee"):
            finalize_group(db.session.get(PayrollMonth, MONTH), "MONTHLY", "admin")


# 2. Overtime only on days worked

@pytest.mark.parametrize("status", ["Paid Leave", "Unpaid Leave / LOP", "Half Day Present", "Week Off"])
def test_no_overtime_on_a_day_set_to_leave_lop_half_day_or_week_off(app, status):
    with app.app_context():
        seed(minutes={6: 660}, overrides={6: status}, opening=Decimal("5"))
        _result, days = calculate()
        assert days["2026-07-06"]["payable_ot"] == 0


def test_overtime_still_paid_on_a_full_day_override(app):
    with app.app_context():
        seed(minutes={6: 660}, overrides={6: "Full Day Present"})
        _result, days = calculate()
        assert days["2026-07-06"]["payable_ot"] == 120


# 3. Daily wage: a week off worked is paid by hours

def test_daily_wage_week_off_worked_is_a_half_or_full_day_by_hours(app):
    pay = {}
    for minutes in (150, 180, 359, 360, 540):
        with app.app_context():
            db.drop_all()
            db.create_all()
            from attendance.shifts import clear_shift_cache, ensure_default_shift
            ensure_default_shift()
            clear_shift_cache()
            seed(wage="DAILY", salary="600", minutes={5: minutes})
            result, _days = calculate()
            pay[minutes] = Decimal(result.final_salary)
    # Under 3h nothing; 3h to 5h59m half a day; 6h a full day less short hours; 9h full.
    assert pay[150] < pay[180] == pay[359] < pay[360] < pay[540]


# 4. Monthly: comp off by hours

def test_week_off_worked_earns_half_or_full_comp_off_by_hours(app):
    with app.app_context():
        seed(minutes={5: 300, 12: 540})
        _result, days = calculate()
        assert days["2026-07-05"]["comp_off_earned"] == "0.5"
        assert days["2026-07-12"]["comp_off_earned"] == "1"


# 5. Days outside the employment dates

def test_days_after_the_last_working_day_are_deducted_not_paid_from_leave(app):
    with app.app_context():
        seed(left_on=date(2026, 7, 15), opening=Decimal("5"), minutes={day: None for day in range(16, 32)})
        result, days = calculate()
        assert Decimal(result.lop_days) == 16
        assert Decimal(result.leave_used) == 0
        assert days["2026-07-20"]["attendance_status"] == "Not Employed"
        # The balance is left whole, to be encashed at exit.
        assert Decimal(result.closing_leave) >= 5


def test_days_before_joining_are_deducted_even_without_attendance_rows(app):
    with app.app_context():
        seed(joined_on=date(2026, 7, 16), minutes={day: None for day in range(1, 16)})
        AttendanceRecord.query.filter(AttendanceRecord.date < date(2026, 7, 10)).delete()
        db.session.commit()
        result, _days = calculate()
        assert Decimal(result.lop_days) == 15
        assert Decimal(result.leave_used) == 0


def test_an_employee_is_not_in_payroll_before_their_joining_month(app):
    from attendance.master import employee_active_for_payroll_month
    employee = Employee(id="9", name="New", employment_status="ACTIVE", joined_on=date(2026, 8, 3))
    assert not employee_active_for_payroll_month(employee, "2026-07")
    assert employee_active_for_payroll_month(employee, "2026-08")


# 6. Holiday worked

def test_monthly_holiday_worked_earns_comp_off(app):
    with app.app_context():
        seed(holidays=[6])
        _result, days = calculate()
        assert days["2026-07-06"]["attendance_status"] == "Holiday"
        assert days["2026-07-06"]["comp_off_earned"] == "1"


def test_daily_wage_holiday_worked_pays_the_day_on_top_of_the_holiday(app):
    with app.app_context():
        seed(wage="DAILY", salary="600", holidays=[6], minutes={6: 540})
        worked, _days = calculate()
        assert Decimal(worked.paid_working_days) + Decimal(worked.holidays) == 27 + 1


# 7. Overtime rounding on shifts that are not whole quarter hours

def test_overtime_minutes_are_whole_intervals_on_an_odd_length_shift():
    shift = ShiftTimes("Odd", 570, 1070)  # 8h20m
    for worked, payable in ((530, 30), (545, 45), (560, 60)):
        _raw, rounded, amount = calculate_monthly_overtime(worked, Decimal("10"), shift=shift)
        assert rounded == payable
        assert amount == Decimal("10") * (payable // 15)


# 8. Sandwich leave through a holiday

def test_sandwich_leave_reaches_through_a_holiday_but_leaves_it_paid(app):
    with app.app_context():
        # Friday 3 and Monday 6 absent; Saturday 4 a holiday not worked; Sunday 5 off.
        seed(minutes={3: None, 4: None, 6: None}, holidays=[4])
        _result, days = calculate()
        assert days["2026-07-04"]["attendance_status"] == "Holiday"
        assert days["2026-07-05"]["sandwich_leave"] is True


def test_a_worked_holiday_breaks_the_sandwich(app):
    with app.app_context():
        seed(minutes={3: None, 6: None}, holidays=[4])
        _result, days = calculate()
        assert not days["2026-07-05"]["sandwich_leave"]


def test_date_of_joining_is_saved_from_the_form_and_the_master_file(client, app):
    from io import BytesIO
    from attendance.master import save_master_employee
    with app.app_context():
        save_master_employee({"employee_id": "7", "name": "Starter", "wage_type": "Monthly", "salary": "20000",
                              "joined_on": "2026-07-16"}, "admin")
        db.session.commit()
        assert db.session.get(Employee, "7").joined_on == date(2026, 7, 16)
    client.post("/login", data={"username": "admin", "password": "12345"})
    export = client.get("/master/export.csv").data.decode()
    assert "Date of Joining" in export.splitlines()[0]
    row = next(line for line in export.splitlines() if line.startswith("7,"))
    assert "16-07-2026" in row
    edited = export.replace(row, row.replace("16-07-2026", "20-07-2026"))
    client.post("/master/import", data={"employee_master_csv": (BytesIO(edited.encode()), "m.csv")},
                content_type="multipart/form-data")
    with app.app_context():
        assert db.session.get(Employee, "7").joined_on == date(2026, 7, 20)


def test_attendance_summary_marks_overtime_days_with_a_tick_and_duration(client, app):
    from io import BytesIO
    from pypdf import PdfReader
    with app.app_context():
        seed(minutes={6: 660, 7: 480})
        calculate()
    client.post("/login", data={"username": "admin", "password": "12345"})
    data = client.get(f"/reports/{MONTH}/employee/5/attendance-summary.pdf").data
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
    assert "2h 00m OT" in text
    assert "1h 00m short" in text
    assert "+120m OT" not in text


def test_employee_calendar_shows_an_overtime_badge(client, app):
    with app.app_context():
        seed(minutes={6: 660})
        calculate()
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get(f"/payroll/{MONTH}/employee/5").data.decode()
    assert 'class="status-badge status-overtime"' in page
    assert "OT: 2h 00m" in page
