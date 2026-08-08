from decimal import Decimal
from datetime import date, datetime
from pathlib import Path
from io import BytesIO
from html import escape
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfReader
from attendance import db
from attendance.calculator import calculate_payroll_month
from attendance.models import AdvanceSalary, AuditLog, AttendanceOverride, AttendanceRecord, Employee, Holiday, LeaveLedger, Loan, LoanInstallmentSkip, PayrollMonth, PayrollResult, SalaryRecord, User, WeekOffRule
from attendance.parser import import_attendance_csv, import_salary_csv
from attendance.reports import build_employee_pdf
from attendance.utils import parse_duration
from attendance.loans import loan_installment_for_employee
from routes.payroll import attendance_display_status, employee_attendance_rows, previous_calendar_month


def write_daily_punch_xlsx(path, headers, rows):
    def column_name(index):
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    def cell_xml(row_index, column_index, value):
        ref = f"{column_name(column_index)}{row_index}"
        if value is None:
            return f'<c r="{ref}"/>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'

    sheet_rows = []
    for row_index, row in enumerate([headers, *rows], start=1):
        cells = "".join(cell_xml(row_index, column_index, value) for column_index, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        workbook.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        workbook.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Daily Punch Report" sheetId="1" r:id="rId1"/></sheets></workbook>')
        workbook.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def test_login_logout_and_password_change(client):
    assert client.get("/").status_code == 302
    bad = client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=True)
    assert b"Invalid username or password" in bad.data
    good = client.post("/login", data={"username": "admin", "password": "12345"}, follow_redirects=True)
    assert b"Dashboard" in good.data
    changed = client.post("/settings/security", data={"current_password": "12345", "new_password": "newpassword", "confirm_password": "newpassword"}, follow_redirects=True)
    assert b"Password changed successfully" in changed.data
    client.get("/logout")
    assert b"Invalid username" in client.post("/login", data={"username": "admin", "password": "12345"}, follow_redirects=True).data
    assert b"Dashboard" in client.post("/login", data={"username": "admin", "password": "newpassword"}, follow_redirects=True).data


def test_inactive_session_auto_logs_out(client, app):
    client.post("/login", data={"username": "admin", "password": "12345"})
    with client.session_transaction() as user_session:
        user_session["last_activity_at"] = datetime.utcnow().timestamp() - 301
    response = client.get("/", follow_redirects=True)
    assert b"Your session expired after 5 minutes of inactivity" in response.data
    assert b"Username" in response.data
    with app.app_context():
        audit = AuditLog.query.filter_by(action="User Auto Logout").one()
        assert audit.actor == "admin"


def test_single_active_admin_session_requires_takeover_confirmation(app):
    first_browser = app.test_client()
    second_browser = app.test_client()

    assert b"Dashboard" in first_browser.post("/login", data={"username": "admin", "password": "12345"}, follow_redirects=True).data
    blocked = second_browser.post("/login", data={"username": "admin", "password": "12345"})
    assert b"already logged in from another browser or device" in blocked.data
    assert b"Logout There and Login Here" in blocked.data

    takeover = second_browser.post("/login", data={"action": "force_login"}, follow_redirects=True)
    assert b"Dashboard" in takeover.data

    old_session = first_browser.get("/", follow_redirects=True)
    assert b"opened in another browser or device" in old_session.data
    assert b"Username" in old_session.data

    with app.app_context():
        replaced_log = AuditLog.query.filter_by(action="User Session Replaced").one()
        assert replaced_log.actor == "admin"
        forced_login = AuditLog.query.filter_by(action="User Login", detail="Successful login after logging out previous active session").one()
        assert forced_login.actor == "admin"


def test_login_form_supports_password_managers(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b'id="loginForm"' in response.data
    assert b'method="post"' in response.data
    assert b'action="/login"' in response.data
    assert b'autocomplete="on"' in response.data
    assert b'id="username"' in response.data
    assert b'name="username"' in response.data
    assert b'autocomplete="username"' in response.data
    assert b'autocapitalize="none"' in response.data
    assert b'id="password"' in response.data
    assert b'name="password"' in response.data
    assert b'type="password"' in response.data
    assert b'autocomplete="current-password"' in response.data
    assert b"form.requestSubmit" in response.data


def test_authenticated_layout_has_theme_toggle_before_user_name(client):
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/")
    assert response.status_code == 200
    theme_index = response.data.index(b'id="themeToggle"')
    admin_index = response.data.index(b"<strong>admin</strong>")
    assert theme_index < admin_index
    assert b"smartfill-theme" in response.data
    assert b"data-theme-label" in response.data


def test_payroll_new_defaults_to_previous_calendar_month(client):
    assert previous_calendar_month(date(2026, 8, 8)) == "2026-07"
    assert previous_calendar_month(date(2026, 1, 5)) == "2025-12"
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/payroll/new")
    assert response.status_code == 200


def test_dashboard_displays_payroll_month_name(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/")
    assert response.status_code == 200
    assert b"Payroll month July 2026 is draft" in response.data
    assert b"<strong>July 2026</strong>" in response.data
    assert b'<option value="2026-07" selected>July 2026</option>' in response.data


def test_dashboard_progress_includes_attendance_employees_missing_wage(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.add(PayrollResult(payroll_month="2026-07", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", final_salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="6", employee_name="Missing Wage", date=date(2026, 7, 1), day="Wednesday"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/")
    assert response.status_code == 200
    assert b"50%" in response.data
    assert b"1 of 2 employees processed" in response.data


def test_settings_app_update_requires_admin_password_and_logs(client, app, monkeypatch):
    monkeypatch.setattr("routes.settings.latest_git_release_datetime", lambda app_root: "08-08-2026 15:12:30")
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get("/settings")
    assert b"About" in page.data
    assert b"V0.01" in page.data
    assert b"08-08-2026 15:12:30" in page.data
    assert b"Update App" in page.data
    assert b'name="admin_password"' in page.data

    bad = client.post("/settings/git-pull", data={"admin_password": "wrong"}, follow_redirects=True)
    assert b"Admin password is incorrect" in bad.data

    monkeypatch.setattr("routes.settings.run_app_update", lambda app_root: (True, "App update completed."))
    good = client.post("/settings/git-pull", data={"admin_password": "12345"}, follow_redirects=True)
    assert b"App update completed" in good.data
    with app.app_context():
        audit = AuditLog.query.filter_by(action="App Update").one()
        assert audit.actor == "admin"


def test_reports_page_has_dedicated_route_and_pdf_cards(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    dashboard = client.get("/")
    assert b'href="/reports/"' in dashboard.data
    assert b'href="/#reports"' not in dashboard.data

    response = client.get("/reports/")
    assert response.status_code == 200
    assert b"<h1" in response.data
    assert b"Reports" in response.data
    assert b"Payroll PDF Reports" in response.data
    assert b"Final Salary Report" in response.data
    assert b"Payroll Summary" in response.data
    assert b"Detailed Attendance" in response.data
    assert b"/reports/2026-07/final-report.pdf" in response.data


def test_employee_master_locks_wage_type_and_requires_disable_confirmation(client, app):
    client.post("/login", data={"username": "admin", "password": "12345"})
    created = client.post("/master", data={
        "employee_id": "5",
        "name": "Worker",
        "salary_type": "Monthly",
        "salary": "30000",
    }, follow_redirects=True)
    assert b"Employee master saved" in created.data
    assert b"Department" not in created.data
    assert b"Designation" not in created.data
    blocked = client.post("/master", data={
        "employee_id": "5",
        "name": "Worker",
        "salary_type": "Daily",
        "salary": "1200",
    }, follow_redirects=True)
    assert b"Wage type cannot be changed" in blocked.data
    bad_disable = client.post("/master", data={
        "action": "disable",
        "employee_id": "5",
        "employment_status": "LEFT",
        "inactive_reason": "resigned",
        "disable_confirmation": "wrong",
    }, follow_redirects=True)
    assert b"disable this employee" in bad_disable.data
    good_disable = client.post("/master", data={
        "action": "disable",
        "employee_id": "5",
        "employment_status": "LEFT",
        "inactive_reason": "resigned",
        "disable_confirmation": "confirm",
    }, follow_redirects=True)
    assert b"Employee marked as inactive" in good_disable.data
    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.normalized_salary_type == "MONTHLY"
        assert employee.ot_enabled is True
        assert employee.less_hours_exempt is False
        assert employee.employment_status == "LEFT"
        assert AuditLog.query.filter_by(action="Employee Master Disabled").count() == 1
    bad_enable = client.post("/master", data={
        "action": "enable",
        "employee_id": "5",
        "enable_confirmation": "wrong",
    }, follow_redirects=True)
    assert b"enable this employee" in bad_enable.data
    with app.app_context():
        assert db.session.get(Employee, "5").employment_status == "LEFT"
    good_enable = client.post("/master", data={
        "action": "enable",
        "employee_id": "5",
        "enable_confirmation": "confirm",
    }, follow_redirects=True)
    assert b"Employee enabled successfully" in good_enable.data
    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.employment_status == "ACTIVE"
        assert employee.inactive_at is None
        assert employee.inactive_reason is None
        assert AuditLog.query.filter_by(action="Employee Master Enabled").count() == 1


def test_employee_master_detail_updates_salary_and_payroll_controls(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), employment_status="ACTIVE"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})

    page = client.get("/master/5")
    assert page.status_code == 200
    assert b"OT Eligible" in page.data
    assert b"Ignore Less Hours Deduction" in page.data

    response = client.post("/master/5", data={
        "master_controls_present": "1",
        "employee_id": "5",
        "name": "Worker Updated",
        "salary_type": "Monthly",
        "salary": "32000",
        "less_hours_exempt": "on",
    }, follow_redirects=True)
    assert b"Employee master updated" in response.data

    with app.app_context():
        employee = db.session.get(Employee, "5")
        assert employee.name == "Worker Updated"
        assert employee.salary == Decimal("32000.00")
        assert employee.ot_enabled is False
        assert employee.less_hours_exempt is True
        audit = AuditLog.query.filter_by(action="Employee Master Updated").one()
        assert "OT Eligible Yes -> No" in audit.detail
        assert "Less Hours Deduction Applied -> Ignored" in audit.detail


def test_employee_master_form_keeps_payroll_controls_on_detail_page(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), employment_status="ACTIVE"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})

    master_page = client.get("/master")
    assert b"Add / Update Employee" not in master_page.data
    assert b"Save Master" not in master_page.data
    assert b'id="employeeMasterForm"' not in master_page.data
    assert b'data-dialog-id="addEmployeeDrawer"' in master_page.data
    add_dialog_html = master_page.data.split(b'id="addEmployeeDrawer"')[1].split(b'id="employeeDrawer')[0]
    assert b'name="ot_enabled"' not in add_dialog_html
    assert b'name="less_hours_exempt"' not in add_dialog_html
    assert b'name="ot_enabled"' in master_page.data
    assert b'name="less_hours_exempt"' in master_page.data
    assert b"Edit Details Worker" in master_page.data
    assert b"under a new Employee ID with the updated wage group" in master_page.data

    detail_page = client.get("/master/5")
    assert b'name="ot_enabled"' in detail_page.data
    assert b'name="less_hours_exempt"' in detail_page.data


def test_payroll_month_loads_salary_from_active_master(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), employment_status="ACTIVE"))
        db.session.add(Employee(id="6", name="Left Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("25000"), employment_status="LEFT"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get("/payroll/2026-07")
    assert b"Load Wage From Master" in page.data
    response = client.post("/payroll/2026-07", data={"action": "salary"}, follow_redirects=True)
    assert b"Wage data loaded from master: 1 created, 0 updated, 0 skipped" in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert salary.name == "Worker"
        assert salary.normalized_salary_type == "MONTHLY"
        assert salary.salary == Decimal("30000.00")
        assert SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="6").count() == 0
        assert AuditLog.query.filter_by(action="Wage Master Loaded").count() == 1


def test_payroll_month_employee_table_has_sortable_id_column(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="10", name="Zed Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="2", name="Alpha Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("25000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get("/payroll/2026-07")
    assert b">ID" in page.data
    assert b"sort=id" in page.data
    assert b"sort=name" in page.data
    assert page.data.index(b"<td>2</td>") < page.data.index(b"<td>10</td>")
    descending = client.get("/payroll/2026-07?sort=id&order=desc")
    assert descending.data.index(b"<td>10</td>") < descending.data.index(b"<td>2</td>")


def test_employee_attendance_rows_show_calculated_status_and_errors_first(app):
    with app.app_context():
        ok_record = AttendanceRecord(
            payroll_month="2026-07",
            employee_id="5",
            employee_name="Worker",
            date=date(2026, 7, 1),
            day="Wednesday",
            raw_working_hours="9h 00m",
            actual_minutes=parse_duration("9h 00m"),
            parse_status="OK",
        )
        error_record = AttendanceRecord(
            payroll_month="2026-07",
            employee_id="5",
            employee_name="Worker",
            date=date(2026, 7, 2),
            day="Thursday",
            raw_working_hours="",
            actual_minutes=None,
            parse_status="NEEDS_REVIEW",
            warning="Missing punch and working hours",
        )
        week_off_record = AttendanceRecord(
            payroll_month="2026-07",
            employee_id="5",
            employee_name="Worker",
            date=date(2026, 7, 5),
            day="Sunday",
            raw_working_hours="",
            actual_minutes=None,
            parse_status="NEEDS_REVIEW",
            warning="Missing punch and working hours",
        )
        shortage_record = AttendanceRecord(
            payroll_month="2026-07",
            employee_id="5",
            employee_name="Worker",
            date=date(2026, 7, 4),
            day="Saturday",
            raw_working_hours="8h 33m",
            actual_minutes=parse_duration("8h 33m"),
            parse_status="OK",
        )
        result = PayrollResult(
            payroll_month="2026-07",
            employee_id="5",
            payroll_rule_type="MONTHLY",
            calculation_status="Needs Review",
            detail_json=[
                {"date": "2026-07-01", "attendance_status": "Full Day Present", "explanation": "Actual duration meets 8h50m grace threshold."},
                {"date": "2026-07-02", "attendance_status": "Needs Review", "explanation": "Missing punch and working hours"},
                {"date": "2026-07-04", "attendance_status": "Full Day Present", "shortage_minutes": 30, "explanation": "Short-hours rule applies with 15-minute floor."},
                {"date": "2026-07-05", "attendance_status": "Week Off", "explanation": "Sunday week off."},
            ],
        )
        rows = employee_attendance_rows([ok_record, error_record, week_off_record, shortage_record], result)
        assert rows[0]["is_error"] is True
        assert rows[0]["display_status"] == "Needs Review"
        assert rows[1]["display_status"] == "Full Day"
        assert rows[2]["display_status"] == "Full Day"
        assert rows[2]["status_tone"] == "shortage"
        assert rows[2]["shortage_minutes"] == 30
        assert rows[3]["display_status"] == "Week Off"
        assert rows[3]["is_error"] is False
        assert rows[1]["status_tone"] == "ok"
        assert attendance_display_status("Half Day Present") == "Half Day"


def test_holiday_management_supports_recurring_and_variable_types(client, app):
    client.post("/login", data={"username": "admin", "password": "12345"})
    recurring = client.post("/holidays", data={
        "date": "26-01-2026",
        "name": "Republic Day",
        "holiday_type": "RECURRING",
        "notes": "fixed",
    }, follow_redirects=True)
    assert b"Recurring Holiday" in recurring.data
    variable = client.post("/holidays", data={
        "date": "20-10-2026",
        "name": "Diwali",
        "holiday_type": "VARIABLE",
        "notes": "changes every year",
    }, follow_redirects=True)
    assert b"Variable Holiday" in variable.data
    with app.app_context():
        republic = Holiday.query.filter_by(name="Republic Day").one()
        diwali = Holiday.query.filter_by(name="Diwali").one()
        assert republic.holiday_type == "RECURRING"
        assert diwali.holiday_type == "VARIABLE"
        assert AuditLog.query.filter_by(action="Holiday Created").count() == 2


def test_recurring_holiday_applies_in_later_year_but_variable_does_not(app):
    with app.app_context():
        db.session.add(Holiday(date=date(2026, 1, 26), name="Republic Day", holiday_type="RECURRING"))
        db.session.add(Holiday(date=date(2026, 10, 20), name="Diwali", holiday_type="VARIABLE"))
        db.session.add(PayrollMonth(month="2027-01"))
        db.session.add(PayrollMonth(month="2027-10"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow(), monday="WORKING", tuesday="WORKING", wednesday="WORKING", thursday="WORKING", friday="WORKING", saturday="WORKING", sunday="WORKING"))
        db.session.add(AttendanceRecord(payroll_month="2027-01", employee_id="5", employee_name="Worker", date=date(2027, 1, 26), day="Tuesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(AttendanceRecord(payroll_month="2027-10", employee_id="5", employee_name="Worker", date=date(2027, 10, 20), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2027-01", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.add(SalaryRecord(payroll_month="2027-10", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        january = calculate_payroll_month("2027-01")[0]
        october = calculate_payroll_month("2027-10")[0]
        assert january.detail_json[0]["attendance_status"] == "Holiday"
        assert october.detail_json[0]["attendance_status"] == "Full Day Present"


def test_worked_weekoff_earns_one_compensatory_leave(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 5), day="Sunday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        result = calculate_payroll_month("2026-07")[0]
        assert result.detail_json[0]["attendance_status"] == "Week Off Worked"
        assert result.detail_json[0]["comp_off_earned"] == "1"
        assert result.week_offs == 1
        assert result.leave_earned == Decimal("1.0")
        assert result.closing_leave == Decimal("1.0")
        earned = LeaveLedger.query.filter_by(employee_id="5", payroll_month="2026-07", transaction_type="EARNED").one()
        assert earned.amount == Decimal("1.0")


def test_sandwich_leave_marks_weekoff_between_leave_days_and_pdf(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", closing_leave=Decimal("5.0"), final_salary=Decimal("30000")))
        for day, day_name in [(date(2026, 7, 4), "Saturday"), (date(2026, 7, 5), "Sunday"), (date(2026, 7, 6), "Monday")]:
            db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=day, day=day_name, raw_working_hours="0h 00m", actual_minutes=0, parse_status="OK"))
        db.session.add(AttendanceOverride(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 4), manual_status="Paid Leave"))
        db.session.add(AttendanceOverride(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 6), manual_status="Paid Leave"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()

        result = calculate_payroll_month("2026-07")[0]
        sandwich = next(row for row in result.detail_json if row["date"] == "2026-07-05")
        assert sandwich["attendance_status"] == "Sandwich Leave"
        assert sandwich["leave_used"] == "1"
        assert sandwich["sandwich_leave"] is True
        assert result.week_offs == 0
        assert result.leave_used == Decimal("3")
        assert result.paid_leaves == Decimal("3")
        assert result.lop_days == Decimal("0")

        pdf_bytes = build_employee_pdf("2026-07", "5")
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)
        assert "Sandwich Leave" in text
        assert "2026-07-05" in text


def test_salary_import_auto_creates_weekoff_and_leave_defaults(tmp_path, app):
    csv_path = tmp_path / "salary.csv"
    csv_path.write_text("Employee ID,Name,Type,Salary,Manual Adjustment\n51,New Worker,Monthly,25000,0\n", encoding="utf-8")
    with app.app_context():
        count, warnings = import_salary_csv(csv_path, "2026-07")
        assert count == 1
        assert warnings == []
        rule = WeekOffRule.query.filter_by(employee_id="51").one()
        assert rule.sunday == "WEEK_OFF_ALL"
        assert rule.confirmed_at is None
        opening = LeaveLedger.query.filter_by(employee_id="51", transaction_type="OPENING").one()
        assert opening.amount == Decimal("0.0")


def test_attendance_import_auto_creates_employee_master_defaults_and_audit(tmp_path, app):
    csv_path = tmp_path / "attendance.csv"
    csv_path.write_text(
        "Employee ID,Employee Name,Department,Designation,Date,Day,Shift,From,To,First Punch,Last Punch,Total Working Hours\n"
        "77,New Attendance Worker,Production,Operator,2026-07-01,Wednesday,Normal,09:00,18:00,09:00 AM,06:00 PM,9h 00m\n"
        "77,New Attendance Worker,Production,Operator,2026-07-02,Thursday,Normal,09:00,18:00,09:01 AM,06:01 PM,9h 00m\n",
        encoding="utf-8",
    )
    with app.app_context():
        count, warnings = import_attendance_csv(csv_path, "2026-07", actor="admin")
        assert count == 2
        assert warnings == []

        employee = db.session.get(Employee, "77")
        assert employee is not None
        assert employee.name == "New Attendance Worker"
        assert employee.department == "Production"
        assert employee.designation == "Operator"
        assert employee.salary_type == ""
        assert employee.normalized_salary_type == ""
        assert employee.salary == Decimal("0.00")
        assert employee.employment_status == "ACTIVE"
        assert employee.ot_enabled is True
        assert employee.less_hours_exempt is False

        rule = WeekOffRule.query.filter_by(employee_id="77").one()
        assert rule.sunday == "WEEK_OFF_ALL"
        opening = LeaveLedger.query.filter_by(employee_id="77", transaction_type="OPENING").one()
        assert opening.amount == Decimal("0.0")

        master_audit = AuditLog.query.filter_by(action="Employee Master Auto Created").one()
        assert "77 - New Attendance Worker" in master_audit.detail
        defaults_audit = AuditLog.query.filter_by(action="Employee Defaults Created").one()
        assert "77 - New Attendance Worker" in defaults_audit.detail

        db.session.add(AttendanceOverride(payroll_month="2026-07", employee_id="77", date=date(2026, 7, 1), manual_status="Half Day Present"))
        db.session.add(PayrollResult(payroll_month="2026-07", employee_id="77", calculation_status="Calculated"))
        db.session.commit()
        count, warnings = import_attendance_csv(csv_path, "2026-07", actor="admin")
        assert count == 2
        assert warnings == []
        assert AttendanceOverride.query.filter_by(payroll_month="2026-07").count() == 0
        assert PayrollResult.query.filter_by(payroll_month="2026-07").count() == 0


def test_daily_punch_xlsx_import_sums_multiple_punch_pairs(tmp_path, app):
    xlsx_path = tmp_path / "daily_punch_report.xlsx"
    headers = [
        "Employee ID",
        "Employee Name",
        "Department",
        "Designation",
        "28-07-2026 \n Tuesday",
        "29-07-2026 \n Wednesday",
    ]
    write_daily_punch_xlsx(
        xlsx_path,
        headers,
        [
            [
                "5",
                "Komal V Patel",
                "Sales",
                "Sales Engineer",
                "09:36 AM\n01:15 PM\n03:30 PM\n06:31 PM",
                "09:31 AM\n06:32 PM",
            ]
        ],
    )

    with app.app_context():
        count, warnings = import_attendance_csv(xlsx_path, "2026-07", actor="admin")
        assert count == 2
        assert warnings == []

        split_day = AttendanceRecord.query.filter_by(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 28)).one()
        assert split_day.first_punch == "09:36 AM"
        assert split_day.last_punch == "06:31 PM"
        assert split_day.actual_minutes == parse_duration("6h 40m")
        assert split_day.raw_working_hours == "6h 40m"
        assert split_day.parse_status == "OK"

        next_day = AttendanceRecord.query.filter_by(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 29)).one()
        assert next_day.actual_minutes == parse_duration("9h 01m")

        audit = AuditLog.query.filter_by(action="Attendance Uploaded").one()
        assert "daily_punch_report.xlsx: 2 rows" in audit.detail


def test_bulk_attendance_manager_submit_required_before_payroll_calculation(tmp_path, client, app):
    xlsx_path = tmp_path / "daily_punch_report.xlsx"
    headers = [
        "Employee ID",
        "Employee Name",
        "Department",
        "Designation",
        "28-07-2026 \n Tuesday",
    ]
    write_daily_punch_xlsx(
        xlsx_path,
        headers,
        [["5", "Komal V Patel", "Sales", "Sales Engineer", "09:36 AM\n01:15 PM\n03:30 PM\n06:31 PM"]],
    )
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Komal V Patel", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("50000")))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Komal V Patel", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("50000")))
        db.session.commit()

    client.post("/login", data={"username": "admin", "password": "12345"})
    with xlsx_path.open("rb") as attendance_file:
        upload = client.post(
            "/attendance/2026-07",
            data={"action": "import_attendance", "attendance_csv": (attendance_file, "daily_punch_report.xlsx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
    assert upload.status_code == 200
    assert b"Attendance Manager" in upload.data
    assert b"Re-import Attendance" in upload.data
    assert b"Submit & Calculate Payroll" in upload.data
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.attendance_submitted is False
        record = AttendanceRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert record.actual_minutes == parse_duration("6h 40m")
        assert record.punches_json == ["09:36 AM", "01:15 PM", "03:30 PM", "06:31 PM"]
        record_id = record.id

    blocked = client.post("/payroll/2026-07", data={"action": "calculate"}, follow_redirects=True)
    assert b"pending review" in blocked.data
    with app.app_context():
        assert PayrollResult.query.filter_by(payroll_month="2026-07").count() == 0

    submitted = client.post(
        "/attendance/2026-07",
        data={"action": "submit", f"punches_{record_id}": "09:36 AM\n01:15 PM\n03:30 PM\n06:31 PM"},
        follow_redirects=True,
    )
    assert submitted.status_code == 200
    assert b"Attendance submitted and payroll calculated" in submitted.data
    with app.app_context():
        assert db.session.get(PayrollMonth, "2026-07").attendance_submitted is True
        result = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert result.calculation_status in {"Calculated", "Needs Review"}
        submit_log = AuditLog.query.filter_by(action="Bulk Attendance Submitted").one()
        assert "2026-07" in submit_log.detail


def test_first_payroll_requires_confirmed_weekoff(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        try:
            calculate_payroll_month("2026-07")
        except ValueError as exc:
            assert "Week off must be selected before first payroll" in str(exc)
        else:
            raise AssertionError("Payroll calculation should require confirmed week off.")
        WeekOffRule.query.filter_by(employee_id="5").update({"confirmed_at": datetime.utcnow()})
        db.session.commit()
        results = calculate_payroll_month("2026-07")
        assert len(results) == 1


def test_type_change_recalculation(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=__import__("datetime").date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Daily", normalized_salary_type="DAILY", salary=Decimal("25000"), adjustment=Decimal("0")))
        db.session.commit()
        calculate_payroll_month("2026-07")
        result = PayrollResult.query.filter_by(employee_id="5").one()
        assert result.final_salary is None
        SalaryRecord.query.filter_by(employee_id="5").update({"salary_type": "Monthly", "normalized_salary_type": "MONTHLY", "salary": Decimal("30000")})
        db.session.commit()
        calculate_payroll_month("2026-07")
        result = PayrollResult.query.filter_by(employee_id="5").one()
        assert result.payroll_rule_type == "MONTHLY"
        assert result.final_salary is not None
        SalaryRecord.query.filter_by(employee_id="5").update({"salary_type": "Daily", "normalized_salary_type": "DAILY"})
        db.session.commit()
        calculate_payroll_month("2026-07")
        result = PayrollResult.query.filter_by(employee_id="5").one()
        assert result.final_salary is None


def test_employee_detail_common_save_recalculates_adjustment_and_loan(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", closing_leave=Decimal("3.0"), final_salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    with app.app_context():
        record = AttendanceRecord.query.filter_by(employee_id="5").one()
        record_id = record.id
    response = client.post("/payroll/2026-07/employee/5", data={
        "action": "recalculate",
        "adjustment": "100",
        "leave_encashment_enabled": "on",
        "leave_encashment_days": "3",
        "loan": "500",
        f"manual_status_{record_id}": "Auto",
        f"notes_{record_id}": "",
    }, follow_redirects=True)
    assert b"Employee changes saved and payroll recalculated" in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(employee_id="5").one()
        result = PayrollResult.query.filter_by(employee_id="5", payroll_month="2026-07").one()
        assert salary.adjustment == Decimal("100.00")
        assert salary.loan == Decimal("500.00")
        assert salary.leave_encashment_enabled is True
        assert salary.leave_encashment_days == Decimal("3.0")
        assert salary.leave_encashment_amount == Decimal("3000.00")
        assert result.manual_adjustment == Decimal("100.00")
        assert result.leave_encashment_days == Decimal("3.0")
        assert result.leave_encashment_amount == Decimal("3000.00")
        assert result.closing_leave == Decimal("0.0")
        assert result.loan_deduction == Decimal("500.00")
        assert result.final_salary == Decimal("32600.00")
        audit = AuditLog.query.filter_by(action="Employee Payroll Data Changed").one()
        assert "Adjustment: 0.00 -> 100.00" in audit.detail
        assert "Leave Encashment: Disabled 0 day(s) / 0.00 -> Enabled 3.0 day(s) / 3000.00" in audit.detail
        assert "Loan: 0.00 -> 500.00" in audit.detail


def test_loan_module_creates_loan_and_payroll_deducts_installment(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/loans", data={
        "action": "create",
        "employee_id": "5",
        "start_date": "2026-07-01",
        "amount": "12000",
        "tenure_months": "12",
        "monthly_deduction": "1000",
        "notes": "advance",
    }, follow_redirects=True)
    assert b"Loan created successfully" in response.data
    with app.app_context():
        loan = Loan.query.filter_by(employee_id="5").one()
        assert loan.monthly_deduction == Decimal("1000.00")
        assert AuditLog.query.filter_by(action="Loan Created").count() == 1


def test_loan_module_shows_in_progress_detail_schedule_and_delete(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        loan = Loan(employee_id="5", start_date=date(2026, 1, 1), amount=Decimal("2500"), tenure_months=3, monthly_deduction=Decimal("1000"), notes="medical")
        db.session.add(loan)
        db.session.flush()
        loan_id = loan.id
        db.session.add(LoanInstallmentSkip(payroll_month="2026-03", employee_id="5", skip=True, notes="approved skip"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    list_response = client.get("/loans?month=2026-04")
    assert list_response.status_code == 200
    assert b"Current Loans In Progress" in list_response.data
    assert f"Loan #{loan_id}".encode() in list_response.data
    assert b"In Progress" in list_response.data
    assert b"500.00" in list_response.data

    detail_response = client.get(f"/loans/{loan_id}?month=2026-04")
    assert detail_response.status_code == 200
    assert f"/reports/loans/{loan_id}.pdf?month=2026-04".encode() in detail_response.data
    assert b"Pending Loan Amount" in detail_response.data
    assert b"Expected End Date" in detail_response.data
    assert b"2026-05-01" in detail_response.data
    assert b"Paid" in detail_response.data
    assert b"Skipped" in detail_response.data
    assert b"Pending" in detail_response.data
    assert b"approved skip" in detail_response.data

    pdf_response = client.get(f"/reports/loans/{loan_id}.pdf?month=2026-04")
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"
    assert "inline" in pdf_response.headers["Content-Disposition"]
    assert pdf_response.data.startswith(b"%PDF")
    loan_reader = PdfReader(BytesIO(pdf_response.data))
    assert loan_reader.metadata.title == f"Loan #{loan_id} Summary - Worker"
    loan_text = "\n".join(page.extract_text() or "" for page in loan_reader.pages)
    assert f"Loan #{loan_id} Summary" in loan_text
    assert "Pending Loan Amount" in loan_text
    assert "500.00" in loan_text
    assert "Paid" in loan_text
    assert "Skipped" in loan_text
    assert "Pending" in loan_text

    bad_delete = client.post("/loans", data={"action": "delete", "loan_id": str(loan_id), "delete_confirmation": "delete"}, follow_redirects=True)
    assert b'to delete this loan' in bad_delete.data
    with app.app_context():
        assert db.session.get(Loan, loan_id) is not None

    good_delete = client.post("/loans", data={"action": "delete", "loan_id": str(loan_id), "delete_confirmation": "permanently delete"}, follow_redirects=True)
    assert b"Loan permanently deleted" in good_delete.data
    with app.app_context():
        assert db.session.get(Loan, loan_id) is None
        assert AuditLog.query.filter_by(action="Loan Deleted").count() == 1


def test_interest_free_loan_deducts_until_repaid_and_caps_final_installment(app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(Loan(employee_id="5", start_date=date(2026, 1, 1), amount=Decimal("2500"), tenure_months=2, monthly_deduction=Decimal("1000")))
        db.session.commit()
        assert loan_installment_for_employee("5", "2026-01") == Decimal("0.00")
        assert loan_installment_for_employee("5", "2026-02") == Decimal("1000.00")
        assert loan_installment_for_employee("5", "2026-03") == Decimal("1000.00")
        assert loan_installment_for_employee("5", "2026-04") == Decimal("500.00")
        assert loan_installment_for_employee("5", "2026-05") == Decimal("0.00")


def test_advance_salary_deducts_from_next_month_payroll(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/advances", data={
        "employee_id": "5",
        "advance_date": "2026-07-15",
        "amount": "2500",
        "notes": "festival",
    }, follow_redirects=True)
    assert b"deduct in payroll month 2026-08" in response.data
    with app.app_context():
        assert AdvanceSalary.query.filter_by(employee_id="5").count() == 1
        assert AuditLog.query.filter_by(action="Advance Salary Created").count() == 1


def test_advance_salary_delete_requires_confirmation_and_logs(client, app):
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker"))
        advance = AdvanceSalary(employee_id="5", advance_date=date(2026, 7, 15), amount=Decimal("2500"), notes="festival")
        db.session.add(advance)
        db.session.flush()
        advance_id = advance.id
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get("/advances")
    assert page.status_code == 200
    assert b">Loans</span>" in page.data
    assert b">Advances</span>" in page.data
    assert b"Current Advances Due" in page.data
    assert b"Advance Register" in page.data
    assert b"Delete Advance Salary" in page.data
    assert b"deleteAdvanceConfirmationInput" in page.data

    bad = client.post("/advances", data={"action": "delete", "advance_id": str(advance_id), "delete_confirmation": "delete"}, follow_redirects=True)
    assert b"to delete this advance salary record" in bad.data
    with app.app_context():
        assert db.session.get(AdvanceSalary, advance_id) is not None

    good = client.post("/advances", data={"action": "delete", "advance_id": str(advance_id), "delete_confirmation": "permanently delete"}, follow_redirects=True)
    assert b"Advance salary permanently deleted" in good.data
    with app.app_context():
        assert db.session.get(AdvanceSalary, advance_id) is None
        assert AuditLog.query.filter_by(action="Advance Salary Deleted").count() == 1


def test_payroll_summary_includes_loan_and_advance_deductions(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-08"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(Loan(employee_id="5", start_date=date(2026, 7, 1), amount=Decimal("12000"), tenure_months=12, monthly_deduction=Decimal("1000")))
        db.session.add(AdvanceSalary(employee_id="5", advance_date=date(2026, 7, 15), amount=Decimal("2500"), notes="festival"))
        db.session.add(AttendanceRecord(payroll_month="2026-08", employee_id="5", employee_name="Worker", date=date(2026, 8, 3), day="Monday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-08", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        calculate_payroll_month("2026-08")
        result = PayrollResult.query.filter_by(employee_id="5", payroll_month="2026-08").one()
        assert result.loan_deduction == Decimal("1000.00")
        assert result.loan_pending_amount == Decimal("11000.00")
        assert result.advance_deduction == Decimal("2500.00")
        assert result.total_deduction == Decimal("3500.00")
        assert result.final_salary == Decimal("26500.00")
        reader = PdfReader(BytesIO(build_employee_pdf("2026-08", "5")))
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "Loan Deduction" in pdf_text
        assert "Pending Loan Amount" in pdf_text
        assert "11,000.00" in pdf_text
        assert "Advance Salary Deduction" in pdf_text


def test_employee_payroll_can_skip_loan_installment_for_month(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(Loan(employee_id="5", start_date=date(2026, 6, 1), amount=Decimal("12000"), tenure_months=12, monthly_deduction=Decimal("1000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    with app.app_context():
        record_id = AttendanceRecord.query.filter_by(employee_id="5").one().id
    response = client.post("/payroll/2026-07/employee/5", data={
        "action": "recalculate",
        "adjustment": "0",
        "loan": "0",
        "loan_installment_skip": "on",
        "loan_skip_notes": "defer once",
        f"manual_status_{record_id}": "Auto",
        f"notes_{record_id}": "",
    }, follow_redirects=True)
    assert b"Employee changes saved and payroll recalculated" in response.data
    with app.app_context():
        result = PayrollResult.query.filter_by(employee_id="5", payroll_month="2026-07").one()
        skip = LoanInstallmentSkip.query.filter_by(employee_id="5", payroll_month="2026-07").one()
        assert skip.skip is True
        assert result.loan_deduction == Decimal("0.00")
        assert result.final_salary == Decimal("30000.00")
        audit = AuditLog.query.filter_by(action="Employee Payroll Data Changed").one()
        assert "Loan Installment Skip: No -> Yes" in audit.detail


def test_month_recalculate_clears_manual_modifications(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07", attendance_submitted=True))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("100"), loan=Decimal("500"), leave_encashment_enabled=True, leave_encashment_days=Decimal("3.0"), leave_encashment_amount=Decimal("3000")))
        db.session.add(AttendanceOverride(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 1), manual_status="Half Day Present", notes="manual"))
        db.session.add(LoanInstallmentSkip(payroll_month="2026-07", employee_id="5", skip=True))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/payroll/2026-07", data={"action": "calculate"}, follow_redirects=True)
    assert b"Payroll recalculated from CSV data" in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(employee_id="5").one()
        result = PayrollResult.query.filter_by(employee_id="5").one()
        assert salary.adjustment == Decimal("0.00")
        assert salary.loan == Decimal("0.00")
        assert salary.leave_encashment_enabled is False
        assert salary.leave_encashment_days == Decimal("0.0")
        assert salary.leave_encashment_amount == Decimal("0.00")
        assert AttendanceOverride.query.count() == 0
        assert LoanInstallmentSkip.query.count() == 0
        assert result.manual_adjustment == Decimal("0.00")
        assert result.leave_encashment_days == Decimal("0.0")
        assert result.leave_encashment_amount == Decimal("0.00")
        assert result.loan_deduction == Decimal("0.00")
        assert AuditLog.query.filter_by(action="Manual Payroll Modifications Cleared").count() == 1


def test_holiday_recheck_updates_payroll_without_clearing_manual_salary_changes(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07", attendance_submitted=True))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow(), monday="WORKING", tuesday="WORKING", wednesday="WORKING", thursday="WORKING", friday="WORKING", saturday="WORKING", sunday="WORKING"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("250"), loan=Decimal("0")))
        db.session.commit()
        result = calculate_payroll_month("2026-07")[0]
        assert result.detail_json[0]["attendance_status"] == "Full Day Present"
        db.session.add(Holiday(date=date(2026, 7, 1), name="Manual Holiday", holiday_type="VARIABLE"))
        db.session.commit()

    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/payroll/2026-07", data={"action": "recheck_holidays"}, follow_redirects=True)
    assert b"Holidays rechecked and payroll updated" in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(employee_id="5").one()
        result = PayrollResult.query.filter_by(employee_id="5", payroll_month="2026-07").one()
        assert salary.adjustment == Decimal("250.00")
        assert result.manual_adjustment == Decimal("250.00")
        assert result.detail_json[0]["attendance_status"] == "Holiday"
        assert AuditLog.query.filter_by(action="Holiday Recheck").count() == 1


def test_leave_encashment_rejects_more_days_than_available(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", closing_leave=Decimal("2.0"), final_salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    with app.app_context():
        record_id = AttendanceRecord.query.filter_by(employee_id="5").one().id
    response = client.post("/payroll/2026-07/employee/5", data={
        "action": "recalculate",
        "adjustment": "0",
        "leave_encashment_enabled": "on",
        "leave_encashment_days": "3",
        "loan": "0",
        f"manual_status_{record_id}": "Auto",
        f"notes_{record_id}": "",
    }, follow_redirects=True)
    assert b"Only 2.0 leave(s) available for encashment" in response.data


def test_global_leave_encashment_encashes_all_available_leaves(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", closing_leave=Decimal("3.0"), final_salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/payroll/2026-07", data={"action": "leave_encashment", "encash_all_leaves": "on"}, follow_redirects=True)
    assert b"Global leave encashment setting saved" in response.data
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.encash_all_leaves is True
        assert AuditLog.query.filter_by(action="Global Leave Encashment Changed").count() == 1
        calculate_payroll_month("2026-07")
        result = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert result.leave_encashment_days == Decimal("3.0")
        assert result.leave_encashment_amount == Decimal("3000.00")
        assert result.closing_leave == Decimal("0.0")
        assert result.final_salary == Decimal("33000.00")


def test_global_leave_encashment_can_be_disabled_per_employee(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07", encash_all_leaves=True))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(PayrollResult(payroll_month="2026-06", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", closing_leave=Decimal("3.0"), final_salary=Decimal("30000")))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        calculate_payroll_month("2026-07")
        record_id = AttendanceRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one().id
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/payroll/2026-07/employee/5", data={
        "action": "recalculate",
        "adjustment": "0",
        "loan": "0",
        f"manual_status_{record_id}": "Auto",
        f"notes_{record_id}": "",
    }, follow_redirects=True)
    assert b"Employee changes saved and payroll recalculated" in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        result = PayrollResult.query.filter_by(payroll_month="2026-07", employee_id="5").one()
        assert salary.leave_encashment_disabled is True
        assert result.leave_encashment_days == Decimal("0")
        assert result.leave_encashment_amount == Decimal("0.00")
        assert result.closing_leave == Decimal("3.0")
        assert result.final_salary == Decimal("30000.00")


def test_logs_page_paginates_at_50(client, app):
    with app.app_context():
        for index in range(55):
            db.session.add(AuditLog(actor="admin", action=f"Action {index:02d}", detail="test"))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    first = client.get("/logs")
    assert b"Page 1 of 2" in first.data
    assert b"Action 54" in first.data
    assert b"<td>Action 00</td>" not in first.data
    second = client.get("/logs?page=2")
    assert b"Page 2 of 2" in second.data
    assert b"Action 00" in second.data


def test_logs_page_filters_by_date_and_action(client, app):
    with app.app_context():
        db.session.add(AuditLog(actor="admin", action="Payroll Finalized", detail="july", created_at=datetime(2026, 7, 31, 10, 0, 0)))
        db.session.add(AuditLog(actor="admin", action="Payroll Deleted", detail="august", created_at=datetime(2026, 8, 1, 10, 0, 0)))
        db.session.add(AuditLog(actor="admin", action="Payroll Finalized", detail="september", created_at=datetime(2026, 9, 1, 10, 0, 0)))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/logs?start_date=2026-08-01&end_date=2026-09-01&action=Payroll+Finalized")
    assert response.status_code == 200
    assert b"Sr." in response.data
    assert b"Action / Type" in response.data
    assert b"september" in response.data
    assert b"july" not in response.data
    assert b"august" not in response.data


def test_pdf_reports_download(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(Employee(id="6", name="Worker Two"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(WeekOffRule(employee_id="6", confirmed_at=datetime.utcnow()))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="6", employee_name="Worker Two", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="6", name="Worker Two", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        calculate_payroll_month("2026-07")
    client.post("/login", data={"username": "admin", "password": "12345"})
    employee_pdf = client.get("/reports/2026-07/employee/5.pdf")
    final_pdf = client.get("/reports/2026-07/final-report.pdf")
    assert employee_pdf.status_code == 200
    assert employee_pdf.mimetype == "application/pdf"
    assert employee_pdf.data.startswith(b"%PDF")
    employee_reader = PdfReader(BytesIO(employee_pdf.data))
    assert employee_reader.metadata.title == "Worker Salary Report - July 2026"
    employee_text = "\n".join(page.extract_text() or "" for page in employee_reader.pages)
    assert "Final Salary Report" not in employee_text
    assert "Salary Slip" in employee_text
    assert "SMARTfill Payroll" in employee_text
    assert "Payable Salary" in employee_text
    assert "Days in Month" in employee_text
    assert "Paid Working Days" in employee_text
    assert "Week Offs" in employee_text
    assert "Total Paid Days" in employee_text
    assert "Sr No" in employee_text
    assert "Leave Balance" in employee_text
    assert "Leave Earned This Month" in employee_text
    assert "Leave Used This Month" in employee_text
    assert "Leave Carry Forwarded" in employee_text
    assert "In Words" in employee_text
    assert "Rupees" in employee_text
    assert "Loan Deduction" not in employee_text
    assert "Pending Loan Amount" not in employee_text
    assert "Advance Salary Deduction" not in employee_text
    assert final_pdf.status_code == 200
    assert final_pdf.mimetype == "application/pdf"
    assert final_pdf.data.startswith(b"%PDF")
    final_reader = PdfReader(BytesIO(final_pdf.data))
    assert final_reader.metadata.title == "Final Salary Report - July 2026"
    final_text = "\n".join(page.extract_text() or "" for page in final_reader.pages)
    assert len(final_reader.pages) == 1
    assert "Final Payroll Report" not in final_text
    assert "Salary Slip" in final_text
    assert "SMARTfill Payroll" in final_text
    assert "Payable Salary" in final_text
    assert "Total Paid Days" in final_text
    assert "Worker Two" in final_text
    assert "Loan Deduction" not in final_text
    assert "Pending Loan Amount" not in final_text
    assert "Advance Salary Deduction" not in final_text


def test_overtime_and_less_hours_reports_only_include_paid_rows(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow()))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", first_punch="09:30 AM", last_punch="06:30 PM", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 2), day="Thursday", first_punch="09:30 AM", last_punch="07:30 PM", raw_working_hours="10h 00m", actual_minutes=parse_duration("10h 00m"), parse_status="OK"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 3), day="Friday", first_punch="09:30 AM", last_punch="06:03 PM", raw_working_hours="8h 33m", actual_minutes=parse_duration("8h 33m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
        calculate_payroll_month("2026-07")
    client.post("/login", data={"username": "admin", "password": "12345"})
    page = client.get("/payroll/2026-07")
    assert b"Payroll Summary PDF" in page.data
    assert b"Detailed Attendance PDF" in page.data
    assert b"OT Report PDF" in page.data
    assert b"Less Hours Report PDF" in page.data
    assert b"Error Report PDF" in page.data
    assert b"OT Report CSV" not in page.data
    assert b"Less Hours Report CSV" not in page.data

    ot = client.get("/reports/2026-07/overtime.pdf")
    assert ot.status_code == 200
    assert ot.mimetype == "application/pdf"
    assert ot.data.startswith(b"%PDF")
    ot_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(ot.data)).pages)
    assert PdfReader(BytesIO(ot.data)).metadata.title == "Overtime Report"
    assert "Overtime Report" in ot_text
    assert "2026-07-02" in ot_text
    assert "45" in ot_text
    assert "2026-07-01" not in ot_text
    assert "2026-07-03" not in ot_text

    less_hours = client.get("/reports/2026-07/less-hours.pdf")
    assert less_hours.status_code == 200
    assert less_hours.mimetype == "application/pdf"
    assert less_hours.data.startswith(b"%PDF")
    less_hours_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(less_hours.data)).pages)
    assert "Less Hours Report" in less_hours_text
    assert "2026-07-03" in less_hours_text
    assert "30" in less_hours_text
    assert "2026-07-01" not in less_hours_text
    assert "2026-07-02" not in less_hours_text

    for url, title in [
        ("/reports/2026-07/payroll-summary.pdf", "Payroll Summary"),
        ("/reports/2026-07/attendance-detail.pdf", "Detailed Attendance Report"),
        ("/reports/2026-07/errors.pdf", "Error Report"),
    ]:
        report = client.get(url)
        assert report.status_code == 200
        assert report.mimetype == "application/pdf"
        assert report.data.startswith(b"%PDF")
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(report.data)).pages)
        assert title in text


def test_employee_payroll_controls_disable_ot_and_less_hours_deduction(app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(
            id="5",
            name="Worker",
            salary_type="Monthly",
            normalized_salary_type="MONTHLY",
            salary=Decimal("30000"),
            employment_status="ACTIVE",
            ot_enabled=False,
            less_hours_exempt=True,
        ))
        db.session.add(WeekOffRule(employee_id="5", confirmed_at=datetime.utcnow(), monday="WORKING", tuesday="WORKING", wednesday="WORKING", thursday="WORKING", friday="WORKING", saturday="WORKING", sunday="WORKING"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 2), day="Thursday", first_punch="09:30 AM", last_punch="07:30 PM", raw_working_hours="10h 00m", actual_minutes=parse_duration("10h 00m"), parse_status="OK"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 3), day="Friday", first_punch="09:30 AM", last_punch="06:03 PM", raw_working_hours="8h 33m", actual_minutes=parse_duration("8h 33m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()

        result = calculate_payroll_month("2026-07")[0]
        ot_row = next(row for row in result.detail_json if row["date"] == "2026-07-02")
        short_row = next(row for row in result.detail_json if row["date"] == "2026-07-03")
        assert ot_row["raw_ot"] > 0
        assert ot_row["payable_ot"] == 0
        assert result.payable_ot_minutes == 0
        assert result.ot_amount == Decimal("0.00")
        assert short_row["shortage_minutes"] == 0
        assert result.less_hours_minutes == 0
        assert result.less_hours_deduction == Decimal("0.00")
        assert result.final_salary == Decimal("30000.00")


def test_payroll_finalize_unlock_and_logs(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    blocked = client.post("/payroll/2026-07", data={"action": "finalize", "admin_password": "wrong"}, follow_redirects=True)
    assert b"Admin password is required to finalize payroll" in blocked.data
    with app.app_context():
        assert db.session.get(PayrollMonth, "2026-07").status == "DRAFT"
    finalized = client.post("/payroll/2026-07", data={"action": "finalize", "admin_password": "12345"}, follow_redirects=True)
    assert b"Payroll finalized and locked" in finalized.data
    assert b"Finalized / Locked" in finalized.data
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.status == "FINALIZED"
        assert month.finalized_at is not None
        assert AuditLog.query.filter_by(action="Payroll Finalized").count() == 1
    assert Path("output/csv/smartfill-payroll-summary-2026-07.csv").exists()
    assert Path("output/csv/smartfill-attendance-detail-2026-07.csv").exists()
    logs = client.get("/logs")
    assert logs.status_code == 200
    assert b"Payroll Finalized" in logs.data
    blocked_unlock = client.post("/payroll/2026-07", data={"action": "unlock", "admin_password": "wrong"}, follow_redirects=True)
    assert b"Admin password is required to unlock payroll" in blocked_unlock.data
    with app.app_context():
        assert db.session.get(PayrollMonth, "2026-07").status == "FINALIZED"
    unlocked = client.post("/payroll/2026-07", data={"action": "unlock", "admin_password": "12345"}, follow_redirects=True)
    assert b"Payroll unlocked" in unlocked.data
    with app.app_context():
        month = db.session.get(PayrollMonth, "2026-07")
        assert month.status == "DRAFT"
        assert month.finalized_at is None
        assert AuditLog.query.filter_by(action="Payroll Unlocked").count() == 1


def test_delete_payroll_requires_phrase_and_removes_month_data(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.add(PayrollResult(payroll_month="2026-07", employee_id="5", payroll_rule_type="MONTHLY", calculation_status="Calculated", final_salary=Decimal("30000")))
        db.session.add(AttendanceOverride(payroll_month="2026-07", employee_id="5", date=date(2026, 7, 1), manual_status="Full Day Present"))
        db.session.add(LeaveLedger(employee_id="5", date=date(2026, 7, 1), payroll_month="2026-07", transaction_type="OPENING", amount=Decimal("0.0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    bad = client.post("/payroll/2026-07", data={"action": "delete", "delete_confirmation": "delete"}, follow_redirects=True)
    assert b"to delete this payroll month" in bad.data
    with app.app_context():
        assert db.session.get(PayrollMonth, "2026-07") is not None
    good = client.post("/payroll/2026-07", data={"action": "delete", "delete_confirmation": "permanently delete"}, follow_redirects=True)
    assert b"Payroll 2026-07 permanently deleted" in good.data
    with app.app_context():
        assert db.session.get(PayrollMonth, "2026-07") is None
        assert AttendanceRecord.query.filter_by(payroll_month="2026-07").count() == 0
        assert SalaryRecord.query.filter_by(payroll_month="2026-07").count() == 0
        assert PayrollResult.query.filter_by(payroll_month="2026-07").count() == 0
        assert AttendanceOverride.query.filter_by(payroll_month="2026-07").count() == 0
        assert LeaveLedger.query.filter_by(payroll_month="2026-07").count() == 0
        assert AuditLog.query.filter_by(action="Payroll Deleted").count() == 1


def test_finalized_payroll_blocks_employee_changes(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07", status="FINALIZED"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    with app.app_context():
        record_id = AttendanceRecord.query.filter_by(employee_id="5").one().id
    response = client.post("/payroll/2026-07/employee/5", data={
        "action": "recalculate",
        "adjustment": "100",
        "loan": "500",
        f"manual_status_{record_id}": "Full Day Present",
        f"notes_{record_id}": "locked edit",
    }, follow_redirects=True)
    assert b"Payroll is finalized and locked" in response.data
    assert b"Save Changes" not in response.data
    with app.app_context():
        salary = SalaryRecord.query.filter_by(employee_id="5").one()
        assert salary.adjustment == Decimal("0.00")
        assert salary.loan == Decimal("0.00")
        assert AttendanceOverride.query.count() == 0


def test_finalized_payroll_blocks_calculation(client, app):
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-07", status="FINALIZED"))
        db.session.add(Employee(id="5", name="Worker"))
        db.session.add(AttendanceRecord(payroll_month="2026-07", employee_id="5", employee_name="Worker", date=date(2026, 7, 1), day="Wednesday", raw_working_hours="9h 00m", actual_minutes=parse_duration("9h 00m"), parse_status="OK"))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000"), adjustment=Decimal("0"), loan=Decimal("0")))
        db.session.commit()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/payroll/2026-07", data={"action": "calculate"}, follow_redirects=True)
    assert b"Payroll is finalized and locked" in response.data
    with app.app_context():
        assert PayrollResult.query.count() == 0
