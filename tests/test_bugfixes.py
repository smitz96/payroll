"""Regression tests for defects found during the UI and workflow review."""
from datetime import date, datetime
from decimal import Decimal

from attendance import db
from attendance.calculator import calculate_payroll_month
from attendance.models import AttendanceRecord, AuditLog, Employee, PayrollMonth, PayrollResult, SalaryRecord, WeekOffRule
from attendance.parser import implausible_session_minutes, parse_punch_times, working_minutes_from_punches
from attendance.settings import MONTHLY_RULES, monthly_rule_rows
from attendance.utils import display_month, is_valid_payroll_month


def login(client):
    client.post("/login", data={"username": "admin", "password": "12345"})


def seed_month(month="2026-07"):
    db.session.add(PayrollMonth(month=month))
    db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
    db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
    db.session.commit()


# --- Malformed payroll month keys used to raise IndexError from calendar.month_name ---

def test_is_valid_payroll_month_accepts_only_year_month():
    assert is_valid_payroll_month("2026-07")
    assert is_valid_payroll_month("2026-12")
    assert not is_valid_payroll_month("2026-13")
    assert not is_valid_payroll_month("2026-00")
    assert not is_valid_payroll_month("not-a-month")
    assert not is_valid_payroll_month("")
    assert not is_valid_payroll_month(None)


def test_display_month_is_safe_for_malformed_input():
    assert display_month("2026-07") == "July 2026"
    assert display_month("2026-99") == "2026-99"
    assert display_month(None) == "Not started"


def test_malformed_month_returns_404_instead_of_500(client, app):
    login(client)
    for path in (
        "/payroll/2026-99",
        "/payroll/not-a-month",
        "/payroll/2026-99/employee/5",
        "/attendance/2026-99",
        "/reports/2026-99/payroll-summary.csv",
        "/reports/2026-99/final-report.pdf",
    ):
        assert client.get(path).status_code == 404, path


def test_invalid_month_is_rejected_when_creating_payroll(client, app):
    login(client)
    response = client.post("/payroll/new", data={"month": "2026-99"}, follow_redirects=True)
    assert b"valid payroll month" in response.data
    with app.app_context():
        assert PayrollMonth.query.count() == 0


# --- A reversed In/Out pair silently rolled past midnight and was paid as overtime ---

def test_reversed_punch_pair_is_flagged_instead_of_paid():
    punches = parse_punch_times("06:30 PM\n09:32 AM")
    # The rollover still happens so genuine night shifts keep working ...
    assert working_minutes_from_punches(punches) == 902
    # ... but the resulting 15h 02m session is reported as implausible.
    assert implausible_session_minutes(punches) == 902


def test_normal_and_overnight_shifts_are_not_flagged():
    assert implausible_session_minutes(parse_punch_times("09:32 AM\n06:30 PM")) == 0
    # A genuine night shift rolls past midnight but stays within the limit.
    assert implausible_session_minutes(parse_punch_times("10:00 PM\n06:00 AM")) == 0
    assert implausible_session_minutes(parse_punch_times("08:00 PM\n08:00 AM")) == 0
    assert implausible_session_minutes(parse_punch_times("09:30 AM\n01:00 PM\n02:00 PM\n06:30 PM")) == 0
    assert implausible_session_minutes([]) == 0
    assert implausible_session_minutes(["09:30 AM"]) == 0


def test_long_same_day_shift_is_still_paid():
    """Real case from the sample data: in 09:35, out 22:19 is 12h 44m of real work.

    It is longer than the limit but never rolled past midnight, so it is overtime,
    not a punch error, and must not be withheld from payroll.
    """
    punches = parse_punch_times("09:35 AM\n10:19 PM")
    assert working_minutes_from_punches(punches) == 764
    assert implausible_session_minutes(punches) == 0


def test_attendance_manager_marks_reversed_punches_as_needing_review(client, app):
    with app.app_context():
        seed_month()
        record = AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", parse_status="OK")
        db.session.add(record)
        db.session.commit()
        record_id = record.id

    login(client)
    client.post("/attendance/2026-07", data={"action": "save", f"punches_{record_id}": "06:30 PM\n09:32 AM"}, follow_redirects=True)
    with app.app_context():
        saved = db.session.get(AttendanceRecord, record_id)
        assert saved.parse_status == "NEEDS_REVIEW"
        assert "Punch out before punch in" in saved.warning


# --- Attendance grid column order depended on which employee came first ---

def test_attendance_grid_dates_are_sorted_even_with_gaps(app):
    from routes.attendance_manager import attendance_grid

    with app.app_context():
        seed_month()
        db.session.add(Employee(id="9", name="Later Joiner", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("10000")))
        # Employee "5" sorts first but is missing 1 July, so first-seen ordering
        # would have appended that date after all of employee 5's dates.
        for day in (2, 3):
            db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, day)))
        for day in (1, 2, 3):
            db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="9", employee_name="Later Joiner", date=date(2026, 7, day)))
        db.session.commit()

        dates, rows = attendance_grid("2026-07")
        assert dates == [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        assert dates == sorted(dates)
        assert len(rows) == 2


def test_attendance_grid_separates_missing_punch_days_from_week_offs(app):
    from routes.attendance_manager import attendance_grid

    with app.app_context():
        seed_month()
        # 1 July 2026 is a Wednesday (working day), 5 July 2026 is a Sunday (week off).
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1)))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 5)))
        db.session.commit()

        _dates, rows = attendance_grid("2026-07")
        cells = rows[0]["cells"]
        assert cells[date(2026, 7, 1)]["missing"] is True
        assert cells[date(2026, 7, 5)]["missing"] is False
        assert cells[date(2026, 7, 5)]["week_off"] is True
        assert rows[0]["missing_count"] == 1
        assert rows[0]["needs_review"] is True


# --- Dashboard counted DAILY wage employees as unsupported ---

def test_dashboard_reports_daily_as_supported_wage_type(client, app):
    with app.app_context():
        seed_month()
        db.session.add(Employee(id="6", name="Day Worker", salary_type="Daily", normalized_salary_type="DAILY", salary=Decimal("500")))
        db.session.add(WeekOffRule(employee_id="6", confirmed_at=datetime.utcnow()))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="6", name="Day Worker", salary_type="Daily", normalized_salary_type="DAILY", salary=Decimal("500")))
        db.session.commit()

    login(client)
    page = client.get("/")
    assert b"1 monthly, 1 daily" in page.data
    assert b"unsupported" not in page.data


# --- Settings page listed rule keys that nothing read ---

def test_settings_rules_are_all_live_configuration():
    rows = monthly_rule_rows()
    assert {row["key"] for row in rows} == set(MONTHLY_RULES)
    assert all(row["label"] and row["detail"] for row in rows)
    for dead_key in ("SHIFT_START_MINUTES", "SHIFT_END_MINUTES", "GRACE_MINUTES", "LEAVE_EARN_DIVISOR"):
        assert dead_key not in MONTHLY_RULES


def test_settings_page_renders_rule_effects(client, app):
    login(client)
    page = client.get("/settings")
    assert b"Full-day grace threshold" in page.data
    assert b"8h 50m" in page.data
    assert b"SHIFT_START_MINUTES" not in page.data


# --- Leave accrual rate is configuration, not a magic number ---

def test_leave_earned_uses_configured_monthly_rate(app):
    from attendance.payroll_rules import calculate_monthly_leave_earned

    assert MONTHLY_RULES["LEAVE_EARNED_PER_MONTH"] == 2
    assert calculate_monthly_leave_earned(Decimal("31"), 31) == Decimal("2.0")
    assert calculate_monthly_leave_earned(Decimal("15.5"), 31) == Decimal("1.0")
    assert calculate_monthly_leave_earned(Decimal("0"), 31) == Decimal("0.0")
    assert calculate_monthly_leave_earned(Decimal("31"), 0) == Decimal("0.0")


# --- Flash messages could not be dismissed (Bootstrap markup, no Bootstrap JS) ---

def test_flash_messages_use_working_dismiss_hooks(client, app):
    login(client)
    response = client.post("/payroll/new", data={"month": ""}, follow_redirects=True)
    assert b"data-dismiss-alert" in response.data
    assert b"data-bs-dismiss" not in response.data


# --- The payroll month page now guides the user through the workflow ---

def test_payroll_month_page_shows_workflow_steps(client, app):
    with app.app_context():
        seed_month()

    login(client)
    page = client.get("/payroll/2026-07")
    for label in (b"Import attendance", b"Load wages", b"Review &amp; submit", b"Calculate payroll", b"Finalize"):
        assert label in page.data
    # First unfinished step is highlighted as the current one.
    assert b"workflow-step is-current" in page.data
    # Reset & Recalculate now sits in the header beside Delete payroll; the panel
    # keeps only the non-destructive recalculation.
    assert b"Reset &amp; Recalculate" in page.data
    assert b'id="run-calculation"' in page.data
    assert b"Manual overrides and adjustments are preserved" in page.data
    # The stale "Monthly only" claim is gone now that DAILY has a rule.
    assert b"Wage Type Monthly only" not in page.data


def test_payroll_workflow_steps_advance_with_progress(client, app):
    with app.app_context():
        seed_month()
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM", last_punch="06:30 PM", raw_working_hours="9h 00m", actual_minutes=540, parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        month = db.session.get(PayrollMonth, "2026-07")
        month.attendance_submitted = True
        db.session.commit()
        calculate_payroll_month("2026-07")

    login(client)
    page = client.get("/payroll/2026-07")
    # Four steps done, only Finalize outstanding.
    assert page.data.count(b"workflow-step is-done") == 4
    assert page.data.count(b"workflow-step is-current") == 1


# --- Department and designation are editable master fields, sourced from attendance ---

def test_attendance_import_populates_department_and_designation(client, app, tmp_path):
    from attendance.parser import import_attendance_csv

    csv_path = tmp_path / "attendance.csv"
    csv_path.write_text(
        "Employee ID,Employee Name,Department,Designation,Date,Day,Shift,From,To,First Punch,Last Punch,Total Working Hours\n"
        "5,Worker,Electrical Production,Wireman,01-07-2026,Wednesday,Normal,,,09:30 AM,06:30 PM,9h 00m\n",
        encoding="utf-8",
    )
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()
        import_attendance_csv(str(csv_path), "2026-07", "admin")
        employee = db.session.get(Employee, "5")
        assert employee.department == "Electrical Production"
        assert employee.designation == "Wireman"


def test_add_employee_accepts_department_and_designation(client, app):
    login(client)
    response = client.post("/master", data={
        "employee_id": "77",
        "name": "Manual Entry",
        "department": "Design",
        "designation": "Design Engineer",
        "wage_type": "Monthly",
        "salary": "25000",
    }, follow_redirects=True)
    assert b"Employee master saved" in response.data
    with app.app_context():
        employee = db.session.get(Employee, "77")
        assert employee.department == "Design"
        assert employee.designation == "Design Engineer"
        audit = AuditLog.query.filter_by(action="Employee Master Created").one()
        assert "Department Design" in audit.detail
        assert "Designation Design Engineer" in audit.detail


def test_editing_employee_records_department_change_in_audit_log(client, app):
    with app.app_context():
        seed_month()
        employee = db.session.get(Employee, "5")
        employee.department = "Service"
        employee.designation = "Jr. Service Engineer"
        db.session.commit()

    login(client)
    client.post("/master", data={
        "employee_id": "5",
        "name": "Worker",
        "department": "Design",
        "designation": "Design Engineer",
        "wage_type": "Monthly",
        "salary": "30000",
        "master_controls_present": "1",
    }, follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.department == "Design"
        assert employee.designation == "Design Engineer"
        audit = AuditLog.query.filter_by(action="Employee Master Updated").first()
        assert "Department Service -> Design" in audit.detail
        assert "Designation Jr. Service Engineer -> Design Engineer" in audit.detail


def test_master_import_updates_new_columns_and_tolerates_old_format(client, app):
    from io import BytesIO

    with app.app_context():
        seed_month()
        employee = db.session.get(Employee, "5")
        employee.department = "Service"
        employee.designation = "Helper"
        db.session.commit()

    login(client)
    # New format carries both columns.
    client.post("/master/import", data={"employee_master_csv": (
        BytesIO(b"Employee ID,Name,Department,Designation,Wage Type,Salary\n5,Worker,Accounts,Accounts Executive,Monthly,31000\n"),
        "master.csv")}, content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.department == "Accounts"
        assert employee.designation == "Accounts Executive"
        assert Decimal(employee.salary) == Decimal("31000")

    # An older export without those columns must leave the stored values untouched.
    client.post("/master/import", data={"employee_master_csv": (
        BytesIO(b"Employee ID,Name,Wage Type,Salary\n5,Worker,Monthly,32000\n"),
        "old.csv")}, content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.department == "Accounts"
        assert employee.designation == "Accounts Executive"
        assert Decimal(employee.salary) == Decimal("32000")


# --- Monthly and Daily payroll are finalized independently ---

def seed_mixed_month(month="2026-07"):
    """One monthly and one daily employee with a full-day punch each."""
    db.session.add(PayrollMonth(month=month, attendance_submitted=True))
    for employee_id, name, wage, salary in (("5", "Monthly Worker", "Monthly", "30000"), ("6", "Daily Worker", "Daily", "600")):
        db.session.add(Employee(id=employee_id, name=name, salary_type=wage,
                                normalized_salary_type=wage.upper(), salary=Decimal(salary)))
        db.session.add(WeekOffRule(employee_id=employee_id, confirmed_at=datetime.utcnow()))
        db.session.add(SalaryRecord(payroll_month=month, employee_id=employee_id, name=name,
                                    salary_type=wage, normalized_salary_type=wage.upper(), salary=Decimal(salary)))
        db.session.add(AttendanceRecord(payroll_month=month, employee_id=employee_id, employee_name=name,
                                        date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM",
                                        last_punch="06:30 PM", raw_working_hours="9h 00m",
                                        actual_minutes=540, parse_status="OK"))
    db.session.commit()


def test_finalizing_monthly_leaves_daily_open(client, app):
    from attendance.wage_groups import is_group_finalized

    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    response = client.post("/payroll/2026-07", data={
        "action": "finalize", "wage_group": "MONTHLY", "admin_password": "12345",
    }, follow_redirects=True)
    assert b"Monthly wage payroll finalized and locked" in response.data

    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert is_group_finalized(month, "MONTHLY")
        assert not is_group_finalized(month, "DAILY")
        # The month as a whole is not finalized while Daily is still open.
        assert month.status == "DRAFT"


def test_month_status_finalizes_only_when_every_group_is_locked(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    for group in ("MONTHLY", "DAILY"):
        client.post("/payroll/2026-07", data={
            "action": "finalize", "wage_group": group, "admin_password": "12345",
        }, follow_redirects=True)
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.status == "FINALIZED"
        assert month.finalized_at is not None

    # Unlocking one group reopens the month.
    client.post("/payroll/2026-07", data={
        "action": "unlock", "wage_group": "DAILY", "admin_password": "12345",
    }, follow_redirects=True)
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.status == "DRAFT"
        assert month.finalized_at is None


def test_recalculation_never_touches_a_finalized_wage_group(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")
        monthly_before = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        frozen_salary = Decimal(monthly_before.final_salary)

    login(client)
    client.post("/payroll/2026-07", data={
        "action": "finalize", "wage_group": "MONTHLY", "admin_password": "12345",
    }, follow_redirects=True)

    with app.app_context():
        # Change something that would alter the monthly result if it were recalculated.
        salary = SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        salary.salary = Decimal("90000")
        db.session.commit()
        calculate_payroll_month("2026-07")
        monthly_after = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert Decimal(monthly_after.final_salary) == frozen_salary
        # Daily is still open and was recalculated.
        assert PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="6").count() == 1


def test_employee_edit_locks_follow_the_employees_wage_group(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    client.post("/payroll/2026-07", data={
        "action": "finalize", "wage_group": "MONTHLY", "admin_password": "12345",
    }, follow_redirects=True)

    monthly = client.post("/payroll/2026-07/employee/5", data={"action": "save", "adjustment": "500"}, follow_redirects=True)
    assert b"finalized and locked" in monthly.data
    daily = client.post("/payroll/2026-07/employee/6", data={"action": "save", "adjustment": "500"}, follow_redirects=True)
    assert b"Employee changes saved" in daily.data
    with app.app_context():
        assert Decimal(SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one().adjustment) == Decimal("0")
        assert Decimal(SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="6").one().adjustment) == Decimal("500")


def test_payroll_page_offers_separate_locks_and_a_wage_filter(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    page = client.get("/payroll/2026-07")
    assert b"Finalize monthly" in page.data
    assert b"Finalize daily" in page.data
    assert b"Monthly wage employees" in page.data
    assert b"Daily wage employees" in page.data

    monthly_only = client.get("/payroll/2026-07?wage=monthly")
    assert b"Monthly wage employees" in monthly_only.data
    assert b"Daily wage employees" not in monthly_only.data


# --- Redundant panels removed; the stepper carries the actions itself ---

def test_workflow_step_loads_wages_without_the_removed_panel(client, app):
    """The Wage Master panel is gone, so step 2 must run the sync, not just link to master."""
    with app.app_context():
        seed_mixed_month()
        SalaryRecord.query.delete()
        db.session.commit()
        assert SalaryRecord.query.filter_by(payroll_month="2026-07").count() == 0

    login(client)
    page = client.get("/payroll/2026-07")
    # The duplicated panels are gone ...
    assert b"Import or re-import the attendance CSV/XLSX" not in page.data
    assert b"Load wage from master" not in page.data
    # ... and step 2 offers the action that panel used to own.
    assert b"Load wages from master" in page.data

    response = client.post("/payroll/2026-07", data={"action": "salary"}, follow_redirects=True)
    assert b"Wage data loaded from master" in response.data
    with app.app_context():
        assert SalaryRecord.query.filter_by(payroll_month="2026-07").count() == 2


def test_wage_step_action_stays_available_after_completion(client, app):
    """Salaries change in master, so reloading wages must not disappear once done."""
    with app.app_context():
        seed_mixed_month()

    login(client)
    page = client.get("/payroll/2026-07")
    assert b"Reload wages" in page.data

    with app.app_context():
        employee = db.session.get(Employee, "5")
        employee.salary = Decimal("45000")
        db.session.commit()
    client.post("/payroll/2026-07", data={"action": "salary"}, follow_redirects=True)
    with app.app_context():
        salary = SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert Decimal(salary.salary) == Decimal("45000")


def test_wage_step_action_is_hidden_once_a_group_is_finalized(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    client.post("/payroll/2026-07", data={
        "action": "finalize", "wage_group": "MONTHLY", "admin_password": "12345",
    }, follow_redirects=True)
    page = client.get("/payroll/2026-07")
    assert b"Reload wages" not in page.data
    assert b"Employee Master" in page.data


# --- Monthly salary breakup and compliance flags ---

def add_employee(client, **overrides):
    data = {
        "employee_id": "80", "name": "Breakup Worker", "wage_type": "Monthly",
        "salary": "30000", "master_controls_present": "1",
    }
    data.update(overrides)
    return client.post("/master", data=data, follow_redirects=True)


def test_salary_breakup_must_add_up_to_salary(client, app):
    login(client)
    bad = add_employee(client, basic_salary="15000", hra="6000", allowance="4000", conveyance_allowance="1000")
    assert b"must add up to the salary" in bad.data
    assert b"Difference 4000.00" in bad.data
    with app.app_context():
        assert db.session.get(Employee, "80") is None

    good = add_employee(client, basic_salary="15000", hra="6000", allowance="6000", conveyance_allowance="3000")
    assert b"Employee master saved" in good.data
    with app.app_context():
        employee = db.session.get(Employee, "80")
        assert Decimal(employee.basic_salary) == Decimal("15000")
        assert Decimal(employee.hra) == Decimal("6000")
        assert Decimal(employee.allowance) == Decimal("6000")
        assert Decimal(employee.conveyance_allowance) == Decimal("3000")
        total = employee.basic_salary + employee.hra + employee.allowance + employee.conveyance_allowance
        assert Decimal(total) == Decimal(employee.salary)


def test_breakup_may_be_left_empty(client, app):
    """An all-zero breakup means "not captured yet" and must stay valid."""
    login(client)
    response = add_employee(client, basic_salary="0", hra="0", allowance="0", conveyance_allowance="0")
    assert b"Employee master saved" in response.data
    with app.app_context():
        assert Decimal(db.session.get(Employee, "80").basic_salary) == Decimal("0")


def test_compliance_flags_save_for_monthly(client, app):
    login(client)
    add_employee(client, pf_enabled="on", esic_enabled="on")
    with app.app_context():
        employee = db.session.get(Employee, "80")
        assert employee.pf_enabled is True
        assert employee.esic_enabled is True
        audit = AuditLog.query.filter_by(action="Employee Master Created").one()
        assert "PF Yes" in audit.detail and "ESIC Yes" in audit.detail


def test_daily_wage_never_stores_breakup_or_compliance(client, app):
    """The fields are monthly-only, so a daily row must not keep values for them."""
    login(client)
    client.post("/master", data={
        "employee_id": "81", "name": "Day Worker", "wage_type": "Daily", "salary": "600",
        "master_controls_present": "1",
        "basic_salary": "400", "hra": "100", "allowance": "50", "conveyance_allowance": "50",
        "pf_enabled": "on", "esic_enabled": "on",
    }, follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "81")
        assert Decimal(employee.basic_salary) == Decimal("0")
        assert Decimal(employee.hra) == Decimal("0")
        assert employee.pf_enabled is False
        assert employee.esic_enabled is False


# --- Import identity lock ---

def test_import_cannot_rename_an_existing_employee(client, app):
    from io import BytesIO

    with app.app_context():
        db.session.add(Employee(id="1", name="Manish Hirani", salary_type="Monthly",
                                normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()

    login(client)
    blocked = client.post("/master/import", data={"employee_master_csv": (
        BytesIO(b"Employee ID,Name,Wage Type,Salary\n1,Manish C Hirani,Monthly,32000\n"), "master.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"already named" in blocked.data
    assert b"Edit the name on the employee page instead" in blocked.data
    with app.app_context():
        employee = db.session.get(Employee, "1")
        assert employee.name == "Manish Hirani"
        # The whole import is rejected, so the salary change does not land either.
        assert Decimal(employee.salary) == Decimal("30000")

    # Matching name is accepted and the wage fields update.
    allowed = client.post("/master/import", data={"employee_master_csv": (
        BytesIO(b"Employee ID,Name,Wage Type,Salary\n1,Manish Hirani,Monthly,32000\n"), "master.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"Employee master imported" in allowed.data
    with app.app_context():
        assert Decimal(db.session.get(Employee, "1").salary) == Decimal("32000")

    # The name is still editable from the employee page.
    client.post("/master", data={"employee_id": "1", "name": "Manish C Hirani",
                                 "wage_type": "Monthly", "salary": "32000"}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(Employee, "1").name == "Manish C Hirani"


def test_import_round_trips_breakup_and_compliance(client, app):
    from io import BytesIO

    with app.app_context():
        db.session.add(Employee(id="1", name="Worker", salary_type="Monthly",
                                normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()

    login(client)
    ok = client.post("/master/import", data={"employee_master_csv": (BytesIO(
        b"Employee ID,Name,Wage Type,Salary,Basic Salary,HRA,Allowance,Conveyance Allowance,PF,ESIC\n"
        b"1,Worker,Monthly,30000,15000,6000,6000,3000,Yes,No\n"), "master.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"Employee master imported" in ok.data
    with app.app_context():
        employee = db.session.get(Employee, "1")
        assert Decimal(employee.basic_salary) == Decimal("15000")
        assert employee.pf_enabled is True
        assert employee.esic_enabled is False

    export = client.get("/master/export.csv")
    assert b"Basic Salary,HRA,Allowance,Conveyance Allowance,PF,ESIC" in export.data
    assert b"15000.00,6000.00,6000.00,3000.00,Yes,No" in export.data

    bad = client.post("/master/import", data={"employee_master_csv": (BytesIO(
        b"Employee ID,Name,Wage Type,Salary,Basic Salary,HRA,Allowance,Conveyance Allowance\n"
        b"1,Worker,Monthly,30000,15000,6000,6000,9000\n"), "master.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"must add up to the salary" in bad.data


def test_import_rejects_breakup_on_a_daily_employee(client, app):
    from io import BytesIO

    with app.app_context():
        db.session.add(Employee(id="2", name="Day Worker", salary_type="Daily",
                                normalized_salary_type="DAILY", salary=Decimal("600")))
        db.session.commit()

    login(client)
    response = client.post("/master/import", data={"employee_master_csv": (BytesIO(
        b"Employee ID,Name,Wage Type,Salary,Basic Salary\n2,Day Worker,Daily,600,400\n"), "master.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"only applies to monthly wage employees" in response.data
    with app.app_context():
        assert Decimal(db.session.get(Employee, "2").basic_salary) == Decimal("0")


def test_older_export_without_new_columns_still_imports(client, app):
    from io import BytesIO

    with app.app_context():
        db.session.add(Employee(id="1", name="Worker", salary_type="Monthly",
                                normalized_salary_type="MONTHLY", salary=Decimal("30000"),
                                basic_salary=Decimal("15000"), hra=Decimal("6000"),
                                allowance=Decimal("6000"), conveyance_allowance=Decimal("3000"),
                                pf_enabled=True))
        db.session.commit()

    login(client)
    response = client.post("/master/import", data={"employee_master_csv": (
        BytesIO(b"Employee ID,Name,Wage Type,Salary\n1,Worker,Monthly,30000\n"), "old.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"Employee master imported" in response.data
    with app.app_context():
        employee = db.session.get(Employee, "1")
        # Columns absent from the file leave the stored values untouched.
        assert Decimal(employee.basic_salary) == Decimal("15000")
        assert employee.pf_enabled is True


# --- "Ignore OT" replaced an inverted "OT eligible" flag ---

def test_ignore_ot_suppresses_payable_overtime(app):
    """Ticking Ignore OT must zero payable OT while still reporting raw OT."""
    with app.app_context():
        seed_month()
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker",
                                    salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker",
                                        date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM",
                                        last_punch="08:30 PM", raw_working_hours="11h 00m",
                                        actual_minutes=660, parse_status="OK"))
        db.session.commit()

        calculate_payroll_month("2026-07")
        paid = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert paid.ot_minutes > 0
        assert paid.payable_ot_minutes > 0
        assert Decimal(paid.ot_amount) > 0

        employee = db.session.get(Employee, "5")
        employee.ot_ignored = True
        db.session.commit()
        calculate_payroll_month("2026-07")
        ignored = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert ignored.ot_minutes > 0, "raw OT is still reported"
        assert ignored.payable_ot_minutes == 0
        assert Decimal(ignored.ot_amount) == Decimal("0")


def test_ignore_less_hours_suppresses_shortage_deduction(app):
    with app.app_context():
        seed_month()
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker",
                                    salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker",
                                        date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM",
                                        last_punch="05:00 PM", raw_working_hours="7h 30m",
                                        actual_minutes=450, parse_status="OK"))
        db.session.commit()

        calculate_payroll_month("2026-07")
        deducted = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert deducted.less_hours_minutes > 0
        assert Decimal(deducted.less_hours_deduction) > 0

        employee = db.session.get(Employee, "5")
        employee.less_hours_ignored = True
        db.session.commit()
        calculate_payroll_month("2026-07")
        ignored = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert ignored.less_hours_minutes == 0
        assert Decimal(ignored.less_hours_deduction) == Decimal("0")


def test_new_employees_default_to_ignoring_nothing(client, app):
    login(client)
    client.post("/master", data={
        "employee_id": "95", "name": "Default Worker", "wage_type": "Monthly",
        "salary": "20000", "master_controls_present": "1",
    }, follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "95")
        assert employee.ot_ignored is False
        assert employee.less_hours_ignored is False


def test_ignore_flags_save_and_are_audited(client, app):
    login(client)
    client.post("/master", data={
        "employee_id": "96", "name": "Exception Worker", "wage_type": "Monthly",
        "salary": "20000", "master_controls_present": "1",
        "ot_ignored": "on", "less_hours_ignored": "on",
    }, follow_redirects=True)
    with app.app_context():
        employee = db.session.get(Employee, "96")
        assert employee.ot_ignored is True
        assert employee.less_hours_ignored is True
        audit = AuditLog.query.filter_by(action="Employee Master Created").one()
        assert "Ignore OT Yes" in audit.detail
        assert "Ignore Less Hours Yes" in audit.detail


def test_legacy_ot_enabled_column_migrates_inverted(tmp_path):
    """An existing DB with the old ot_enabled column must flip cleanly to ot_ignored."""
    import sqlite3
    from app import create_app

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE employee (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            department VARCHAR(160),
            designation VARCHAR(160),
            ot_enabled BOOLEAN NOT NULL DEFAULT 1,
            less_hours_exempt BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO employee (id, name, ot_enabled, less_hours_exempt) VALUES
            ('1', 'OT Paid', 1, 0),
            ('2', 'OT Blocked', 0, 1);
        """
    )
    connection.commit()
    connection.close()

    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(db_path), "TESTING": True})
    with app.app_context():
        # ot_enabled=1 ("OT paid") becomes ot_ignored=0, and vice versa.
        assert db.session.get(Employee, "1").ot_ignored is False
        assert db.session.get(Employee, "1").less_hours_ignored is False
        assert db.session.get(Employee, "2").ot_ignored is True
        assert db.session.get(Employee, "2").less_hours_ignored is True


# --- Workflow steps are gated on the steps before them ---

def step_actions(html, label):
    """The buttons/links inside one workflow step, with their disabled state."""
    import re
    blocks = re.findall(rb'<li class="workflow-step[^>]*>(.*?)</li>', html, re.S)
    for block in blocks:
        if b"<strong>" + label.encode() + b"</strong>" in block:
            return [
                (re.sub(rb"<[^>]+>", b"", m.group(2)).strip().decode(), b"disabled" in m.group(1))
                for m in re.finditer(rb"<(?:button|a)([^>]*)>(.*?)</(?:button|a)>", block, re.S)
            ]
    return []


def test_later_steps_are_disabled_until_attendance_is_imported(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.commit()

    login(client)
    page = client.get("/payroll/2026-07").data

    # Step 1 has no prerequisite, so it stays actionable.
    assert step_actions(page, "Import attendance") == [("Import attendance", False)]
    # Everything after it is inert until attendance lands.
    for label in ("Load wages", "Review &amp; submit", "Calculate payroll", "Finalize"):
        actions = step_actions(page, label)
        assert actions, label
        assert all(disabled for _text, disabled in actions), f"{label} should be disabled: {actions}"
    assert b"disabled-link" in page
    assert b"Complete the earlier steps first" in page


def test_step_two_unlocks_once_attendance_is_imported(client, app):
    with app.app_context():
        seed_month()
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker",
                                        date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM",
                                        last_punch="06:30 PM", raw_working_hours="9h 00m",
                                        actual_minutes=540, parse_status="OK"))
        db.session.commit()

    login(client)
    page = client.get("/payroll/2026-07").data
    assert all(not disabled for _text, disabled in step_actions(page, "Load wages"))
    # Step 3 still waits on step 2.
    assert all(disabled for _text, disabled in step_actions(page, "Review &amp; submit"))


def test_completed_steps_stay_actionable(client, app):
    """A done step keeps its action so wages can be reloaded after a master change."""
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    page = client.get("/payroll/2026-07").data
    for label in ("Import attendance", "Load wages", "Review &amp; submit", "Calculate payroll", "Finalize"):
        actions = step_actions(page, label)
        assert actions, label
        assert all(not disabled for _text, disabled in actions), f"{label} should be enabled: {actions}"


def test_header_holds_reset_and_drops_the_duplicate_attendance_link(client, app):
    with app.app_context():
        seed_mixed_month()

    login(client)
    page = client.get("/payroll/2026-07").data
    header = page.split(b'<div class="page-head">')[1].split(b"</div>\n</div>")[0]
    assert b"Reset &amp; Recalculate" in header
    assert b"Delete payroll" in header
    # The header link duplicated steps 1 and 3, so it is gone.
    assert b"Attendance Manager" not in header


def test_finalize_step_cta_is_named_finalize(client, app):
    with app.app_context():
        seed_mixed_month()
        calculate_payroll_month("2026-07")

    login(client)
    page = client.get("/payroll/2026-07").data
    assert ("Finalize", False) in step_actions(page, "Finalize")
    assert b"Go to locks" not in page
