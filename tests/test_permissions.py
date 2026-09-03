from decimal import Decimal

from werkzeug.security import generate_password_hash

from attendance import db
from attendance.models import Employee, PayrollMonth, SalaryRecord, User


def add_user(username, permissions, password="password123", active=True):
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        is_active=active,
        is_admin=False,
        permissions_json=permissions,
    )
    db.session.add(user)
    return user


def login(client, username="admin", password="12345"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def test_admin_can_manage_users_and_create_module_user(client, app):
    login(client)
    page = client.get("/settings/users")
    assert page.status_code == 200
    assert b"Users &amp; module access" in page.data
    assert b"Admin master user" in page.data
    assert b">Users</span></a>" in page.data
    assert b'href="/settings/users"' in page.data

    response = client.post(
        "/settings/users",
        data={
            "action": "create",
            "username": "attendance-user",
            "new_password": "password123",
            "confirm_password": "password123",
            "is_active": "on",
            "permissions": ["attendance", "holidays"],
        },
        follow_redirects=True,
    )
    assert b"User created successfully" in response.data
    with app.app_context():
        user = User.query.filter_by(username="attendance-user").one()
        assert user.is_admin is False
        assert user.is_active is True
        assert set(user.permissions_json) == {"attendance", "holidays"}


def test_module_user_is_limited_to_assigned_pages(client, app):
    with app.app_context():
        add_user("attendance-user", ["attendance"])
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.commit()

    login(client, "attendance-user", "password123")
    attendance = client.get("/attendance/2026-07")
    assert attendance.status_code == 200
    assert b"Attendance" in attendance.data
    assert b">Payroll<" not in attendance.data
    assert b">Holidays<" not in attendance.data

    assert client.get("/holidays").status_code == 403
    assert client.get("/settings").status_code == 403
    assert client.get("/settings/security").status_code == 200


def test_payroll_and_finalization_are_separate_permissions(client, app):
    with app.app_context():
        add_user("payroll-user", ["payroll"])
        add_user("final-user", ["finalization"])
        db.session.add(PayrollMonth(month="2026-07"))
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(SalaryRecord(payroll_month="2026-07", employee_id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()

    login(client, "payroll-user", "password123")
    page = client.get("/payroll/2026-07")
    assert page.status_code == 200
    assert b"Finalize monthly" not in page.data
    denied_finalize = client.post("/payroll/2026-07", data={"action": "finalize", "wage_group": "MONTHLY"})
    assert denied_finalize.status_code == 403

    client.get("/logout")
    login(client, "final-user", "password123")
    final_page = client.get("/payroll/2026-07")
    assert final_page.status_code == 200
    assert b"Finalize monthly" in final_page.data
    assert b"Delete payroll" not in final_page.data
    denied_create = client.post("/payroll/new", data={"month": "2026-08"})
    assert denied_create.status_code == 403
