from datetime import date, datetime
from decimal import Decimal
from io import BytesIO

from attendance import db
from attendance.calculator import calculate_payroll_month
from attendance.models import AttendanceRecord, AuditLog, Employee, PayrollMonth, PayrollResult, SalaryRecord, Shift, WeekOffRule
from attendance.payroll_rules import (
    calculate_day_overtime,
    calculate_less_hours,
    classify_monthly_attendance,
    punch_clock_minutes,
)
from attendance.shifts import (
    DEFAULT_SHIFT,
    ShiftTimes,
    parse_shift_pattern,
    shift_for_date,
    shift_pattern_text,
)


def login(client):
    client.post("/login", data={"username": "admin", "password": "12345"})


def clock(text):
    return punch_clock_minutes(text)


def day_record(first, last, when=date(2026, 7, 6), employee_id="5"):
    actual = clock(last) - clock(first)
    return AttendanceRecord(date=when, employee_id=employee_id, first_punch=first, last_punch=last,
                            punches_json=[first, last], actual_minutes=actual, parse_status="OK")


TEN_TO_SIX_THIRTY = ShiftTimes("Late Shift", clock("10:00 AM"), clock("06:30 PM"))
NINE_THIRTY_TO_FIVE_THIRTY = ShiftTimes("Short Shift", clock("09:30 AM"), clock("05:30 PM"))


# --- Thresholds scale with the shift ---

def test_normal_shift_keeps_six_and_three_hours():
    assert DEFAULT_SHIFT.length == 540
    assert DEFAULT_SHIFT.full_day_minimum == 360
    assert DEFAULT_SHIFT.half_day_minimum == 180
    assert DEFAULT_SHIFT.full_day_grace == 530
    assert DEFAULT_SHIFT.total_hours_overtime_start == 570


def test_shorter_shift_scales_full_and_half_day_exactly():
    eight_hours = NINE_THIRTY_TO_FIVE_THIRTY
    assert eight_hours.length == 480
    # 2/3 and 1/3 of 8 hours: 5h20m and 2h40m.
    assert eight_hours.full_day_minimum == 320
    assert eight_hours.half_day_minimum == 160
    assert eight_hours.is_full_day(320) and not eight_hours.is_full_day(319)
    assert eight_hours.is_half_day(160) and not eight_hours.is_half_day(159)
    assert TEN_TO_SIX_THIRTY.full_day_minimum == 340


def test_classification_uses_the_shift_thresholds():
    when = date(2026, 7, 6)

    def status(minutes):
        record = AttendanceRecord(date=when, employee_id="5", actual_minutes=minutes, parse_status="OK")
        return classify_monthly_attendance(record, shift=NINE_THIRTY_TO_FIVE_THIRTY)["status"]

    assert status(320) == "Full Day Present"
    assert status(319) == "Half Day Present"
    assert status(160) == "Half Day Present"
    assert status(159) == "Full Day LOP"


# --- Less hours and overtime measured from the shift's own clock ---

def test_late_check_in_grace_runs_from_the_shift_start():
    for first, late in {"10:10 AM": 0, "10:11 AM": 15, "10:15 AM": 15, "10:20 AM": 30}.items():
        assert calculate_less_hours(day_record(first, "06:30 PM"), TEN_TO_SIX_THIRTY) == (late, 0, late), first


def test_early_check_out_runs_from_the_shift_end():
    # A 5:30 PM shift end: leaving at 5:29 is 15 minutes, 5:00 is 30.
    for last, early in {"05:30 PM": 0, "05:29 PM": 15, "05:15 PM": 15, "05:00 PM": 30}.items():
        assert calculate_less_hours(day_record("09:30 AM", last), NINE_THIRTY_TO_FIVE_THIRTY) == (0, early, early), last


def test_overtime_counts_from_the_shift_end():
    shift = NINE_THIRTY_TO_FIVE_THIRTY
    # Leaving at 6:30 PM is a full hour past a 5:30 shift.
    assert calculate_day_overtime(day_record("09:30 AM", "06:30 PM"), "Full Day Present", Decimal("10"), shift)[1] == 60
    assert calculate_day_overtime(day_record("09:30 AM", "05:59 PM"), "Full Day Present", Decimal("10"), shift)[1] == 0


def test_week_off_overtime_uses_the_shift_length():
    record = AttendanceRecord(date=date(2026, 7, 5), employee_id="5", actual_minutes=9 * 60, parse_status="OK")
    # Nine hours on an 8-hour shift is an hour of total-hours overtime.
    assert calculate_day_overtime(record, "Week Off Worked", Decimal("10"), NINE_THIRTY_TO_FIVE_THIRTY)[1] == 60
    assert calculate_day_overtime(record, "Week Off Worked", Decimal("10"), DEFAULT_SHIFT)[1] == 0


# --- Shifts per weekday ---

def add_shift(name, start, end):
    shift = Shift(name=name, start_minutes=clock(start), end_minutes=clock(end))
    db.session.add(shift)
    db.session.flush()
    return shift


def test_default_shift_is_created_and_unassigned_days_use_it(app):
    with app.app_context():
        default = Shift.query.filter_by(is_default=True).one()
        assert (default.name, default.start_minutes, default.end_minutes) == ("Normal Shift", 570, 1110)
        db.session.add(WeekOffRule(employee_id="5"))
        db.session.commit()
        assert shift_for_date("5", date(2026, 7, 6)).name == "Normal Shift"


def test_each_weekday_can_have_its_own_shift(app):
    with app.app_context():
        short = add_shift("Short Shift", "09:30 AM", "05:30 PM")
        db.session.add(WeekOffRule(employee_id="5", saturday_shift_id=short.id))
        db.session.commit()
        assert shift_for_date("5", date(2026, 7, 4)).name == "Short Shift"  # Saturday
        assert shift_for_date("5", date(2026, 7, 6)).name == "Normal Shift"  # Monday


def test_payroll_charges_less_hours_against_the_days_shift(app):
    with app.app_context():
        short = add_shift("Short Shift", "09:30 AM", "05:30 PM")
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly",
                                normalized_salary_type="MONTHLY", salary=Decimal("31000")))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow(), saturday_shift_id=short.id))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker",
                                    salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("31000")))
        # Saturday 4 July and Monday 6 July, both 9:30 to 5:30.
        for day in (4, 6):
            when = date(2026, 7, day)
            db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker",
                                            date=when, day=when.strftime("%A"), first_punch="09:30 AM",
                                            last_punch="05:30 PM", punches_json=["09:30 AM", "05:30 PM"],
                                            raw_working_hours="8h 00m", actual_minutes=480, parse_status="OK"))
        db.session.commit()
        calculate_payroll_month("2026-07")
        result = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        days = {item["date"]: item for item in result.detail_json}
        # Saturday is a full Short Shift; Monday left an hour before the Normal Shift ended.
        assert days["2026-07-04"]["shortage_minutes"] == 0
        assert days["2026-07-04"]["shift"] == "Short Shift (09:30 AM - 05:30 PM)"
        assert (days["2026-07-06"]["early_out_minutes"], days["2026-07-06"]["shortage_minutes"]) == (60, 60)
        assert result.early_out_minutes == 60


# --- Week Offs page ---

def seed_employee():
    db.session.add(Employee(id="5", name="Worker", salary_type="Monthly",
                            normalized_salary_type="MONTHLY", salary=Decimal("30000")))
    db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
    db.session.commit()


def test_shifts_can_be_added_and_edited(client, app):
    login(client)
    response = client.post("/weekoffs/shifts", data={"new_name": "Late Shift", "new_start": "10:00", "new_end": "18:30"},
                           follow_redirects=True)
    assert b"Shifts saved" in response.data
    with app.app_context():
        late = Shift.query.filter_by(name="Late Shift").one()
        assert (late.start_minutes, late.end_minutes, late.is_default) == (600, 1110, False)
        late_id = late.id
        default_id = Shift.query.filter_by(is_default=True).one().id
    client.post("/weekoffs/shifts", data={
        f"shift_{default_id}_name": "Normal Shift", f"shift_{default_id}_start": "09:30", f"shift_{default_id}_end": "18:30",
        f"shift_{late_id}_name": "Late Shift", f"shift_{late_id}_start": "10:00", f"shift_{late_id}_end": "18:00",
    })
    with app.app_context():
        assert db.session.get(Shift, late_id).end_minutes == 1080
        assert AuditLog.query.filter_by(action="Shifts Changed").count() == 2


def test_shift_must_end_after_it_starts_and_have_a_unique_name(client, app):
    login(client)
    response = client.post("/weekoffs/shifts", data={"new_name": "Night", "new_start": "22:00", "new_end": "06:00"},
                           follow_redirects=True)
    assert b"Overnight shifts are not supported" in response.data
    response = client.post("/weekoffs/shifts", data={"new_name": "normal shift", "new_start": "10:00", "new_end": "18:00"},
                           follow_redirects=True)
    assert b"already exists" in response.data
    with app.app_context():
        assert Shift.query.count() == 1


def test_week_offs_page_assigns_a_shift_per_weekday(client, app):
    with app.app_context():
        seed_employee()
        late_id = add_shift("Late Shift", "10:00 AM", "06:30 PM").id
        default_id = Shift.query.filter_by(is_default=True).one().id
        db.session.commit()
    login(client)
    page = client.get("/weekoffs").data.decode()
    assert ">Late Shift</option>" in page
    assert 'name="5_saturday_shift"' in page
    form = {"5_present": "1", "5_sunday": "WEEK_OFF_ALL"}
    for field in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        form[f"5_{field}_shift"] = str(late_id if field == "saturday" else default_id)
    client.post("/weekoffs", data=form)
    with app.app_context():
        rule = WeekOffRule.query.filter_by(employee_id="5").one()
        assert rule.saturday_shift_id == late_id
        # Choosing the default shift stores nothing, so it follows the default.
        assert rule.monday_shift_id is None
        assert "Saturday shift: Normal Shift -> Late Shift" in AuditLog.query.filter_by(action="Week Off Rules Changed").one().detail


def test_a_shift_in_use_cannot_be_deleted(client, app):
    with app.app_context():
        seed_employee()
        late = add_shift("Late Shift", "10:00 AM", "06:30 PM")
        WeekOffRule.query.filter_by(employee_id="5").one().friday_shift_id = late.id
        late_id = late.id
        db.session.commit()
    login(client)
    response = client.post(f"/weekoffs/shifts/{late_id}/delete", follow_redirects=True)
    assert b"still assigned" in response.data
    with app.app_context():
        WeekOffRule.query.filter_by(employee_id="5").one().friday_shift_id = None
        db.session.commit()
    client.post(f"/weekoffs/shifts/{late_id}/delete")
    with app.app_context():
        assert db.session.get(Shift, late_id) is None


def test_the_default_shift_cannot_be_deleted(client, app):
    with app.app_context():
        default_id = Shift.query.filter_by(is_default=True).one().id
    login(client)
    response = client.post(f"/weekoffs/shifts/{default_id}/delete", follow_redirects=True)
    assert b"default shift and cannot be deleted" in response.data


# --- Employee master Shift Pattern column ---

def test_shift_pattern_text_round_trips(app):
    with app.app_context():
        late = add_shift("Late Shift", "10:00 AM", "06:30 PM")
        rule = WeekOffRule(employee_id="5", saturday_shift_id=late.id)
        assert shift_pattern_text(rule) == "Normal Shift; Saturday=Late Shift"
        parsed = parse_shift_pattern("Normal Shift; Saturday=Late Shift")
        assert parsed["saturday"] == late.id and parsed["monday"] is None
        everyday = parse_shift_pattern("late shift")
        assert set(everyday.values()) == {late.id}


def test_master_import_sets_and_rejects_shift_patterns(client, app):
    with app.app_context():
        seed_employee()
        late_id = add_shift("Late Shift", "10:00 AM", "06:30 PM").id
        db.session.commit()
    login(client)
    export = client.get("/master/export.csv").data.decode()
    header = export.splitlines()[0]
    assert "Shift Pattern" in header
    row = next(line for line in export.splitlines() if line.startswith("5,"))
    assert ",Normal Shift," in row
    edited = export.replace(row, row.replace(",Normal Shift,", ",Normal Shift; Saturday=Late Shift,"))
    response = client.post("/master/import", data={"employee_master_csv": (BytesIO(edited.encode()), "master.csv")},
                           content_type="multipart/form-data", follow_redirects=True)
    assert b"imported" in response.data
    with app.app_context():
        assert WeekOffRule.query.filter_by(employee_id="5").one().saturday_shift_id == late_id
    bad = export.replace(row, row.replace(",Normal Shift,", ",Night Shift,"))
    response = client.post("/master/import", data={"employee_master_csv": (BytesIO(bad.encode()), "master.csv")},
                           content_type="multipart/form-data", follow_redirects=True)
    assert b"there is no shift named" in response.data
