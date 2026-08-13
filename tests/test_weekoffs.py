from datetime import date, datetime

from attendance import db
from attendance.employee_defaults import backfill_default_weekoffs
from attendance.models import AuditLog, AttendanceRecord, Employee, WeekOffRule
from attendance.payroll_rules import classify_monthly_attendance
from attendance.weekoffs import is_week_off_for_date, selected_weekoff_codes


def test_weekoff_rule_supports_second_and_fourth_saturday(app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", saturday="WEEK_OFF_2_4", sunday="WORKING"))
        db.session.commit()
        assert is_week_off_for_date("5", date(2026, 7, 11)) is True
        assert is_week_off_for_date("5", date(2026, 7, 25)) is True
        assert is_week_off_for_date("5", date(2026, 7, 5)) is False
        record = AttendanceRecord(employee_id="5", date=date(2026, 7, 11), parse_status="NEEDS_REVIEW", warning="Missing punch")
        assert classify_monthly_attendance(record)["status"] == "Week Off"
        worked_record = AttendanceRecord(employee_id="5", date=date(2026, 7, 11), actual_minutes=540, parse_status="OK")
        worked = classify_monthly_attendance(worked_record)
        assert worked["status"] == "Week Off Worked"
        assert "compensatory leave earned" in worked["explanation"]
        assert selected_weekoff_codes("WEEK_OFF_2_4") == ["WORKING", "WEEK_OFF_2", "WEEK_OFF_4"]


def test_weekoff_rule_supports_multiple_selected_occurrences(app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", saturday="WEEK_OFF_1,WEEK_OFF_3,WEEK_OFF_5", sunday="WORKING"))
        db.session.commit()
        assert is_week_off_for_date("5", date(2026, 8, 1)) is True
        assert is_week_off_for_date("5", date(2026, 8, 8)) is False
        assert is_week_off_for_date("5", date(2026, 8, 15)) is True
        assert is_week_off_for_date("5", date(2026, 8, 29)) is True


def test_weekoff_page_saves_and_audits(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/weekoffs", data={
        "5_present": "1",
        "5_monday": "WORKING",
        "5_tuesday": "WORKING",
        "5_wednesday": "WORKING",
        "5_thursday": "WORKING",
        "5_friday": "WORKING",
        "5_saturday": ["WEEK_OFF_2", "WEEK_OFF_4"],
        "5_sunday": "WEEK_OFF_ALL",
    }, follow_redirects=True)
    assert b"Week off settings saved" in response.data
    with app.app_context():
        rule = WeekOffRule.query.filter_by(employee_id="5").one()
        assert rule.saturday == "WEEK_OFF_2,WEEK_OFF_4"
        assert rule.confirmed_at is not None
        assert AuditLog.query.filter_by(action="Week Off Rules Changed").count() == 1
    assert b"Normal Shift" in response.data
    assert b"Factory Shift" not in response.data
    assert b"sticky-id-name-table" in response.data
    html = response.data.decode()
    assert html.index("Sunday") < html.index("Monday")


def test_backfill_default_weekoffs_assigns_sunday_to_existing_employees(app):
    with app.app_context():
        db.session.add(Employee(id="8", name="Existing Worker"))
        db.session.commit()

        created = backfill_default_weekoffs()
        db.session.commit()

        assert created == ["8 - Existing Worker"]
        rule = WeekOffRule.query.filter_by(employee_id="8").one()
        assert rule.sunday == "WEEK_OFF_ALL"
        assert rule.monday == "WORKING"
        assert rule.confirmed_at is not None
        assert is_week_off_for_date("8", date(2026, 7, 5)) is True
        assert is_week_off_for_date("8", date(2026, 7, 6)) is False
        assert AuditLog.query.filter_by(action="Default Week Off Backfilled").count() == 1


def test_an_employee_the_form_did_not_carry_keeps_their_week_off(client, app):
    """A partial save must not read a missing row as "works every day".

    Week offs decide which days are paid, so silently turning someone's Sunday into
    an unpaid absence is thousands of rupees a month.
    """
    with app.app_context():
        db.session.add(Employee(id="5", name="Saved Worker"))
        db.session.add(Employee(id="6", name="Untouched Worker"))
        db.session.add(WeekOffRule(employee_id="6", sunday="WEEK_OFF_ALL", confirmed_at=datetime.utcnow()))
        db.session.commit()

    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/weekoffs", data={
        "5_present": "1",
        "5_sunday": "WEEK_OFF_ALL",
    }, follow_redirects=True)
    assert b"Week off settings saved" in response.data
    with app.app_context():
        assert WeekOffRule.query.filter_by(employee_id="5").one().sunday == "WEEK_OFF_ALL"
        # Employee 6 was not in the form at all and must be exactly as they were.
        assert WeekOffRule.query.filter_by(employee_id="6").one().sunday == "WEEK_OFF_ALL"
