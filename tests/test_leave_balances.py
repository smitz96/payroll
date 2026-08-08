from datetime import date
from decimal import Decimal

from attendance import db
from attendance.calculator import opening_leave_for
from attendance.leave_balances import stored_leave_balance
from attendance.models import AuditLog, Employee, LeaveLedger, PayrollResult


def seed_employee_with_leave():
    db.session.add(Employee(id="5", name="Komal V Patel", department="Accounts"))
    db.session.add(PayrollResult(
        payroll_month="2026-07",
        employee_id="5",
        payroll_rule_type="MONTHLY",
        calculation_status="Calculated",
        closing_leave=Decimal("1.2"),
        final_salary=Decimal("1000"),
    ))
    db.session.commit()


def test_leave_balance_page_requires_login(client):
    assert client.get("/leave-balances").status_code == 302


def test_leave_balance_wrong_password_does_not_save(client, app):
    with app.app_context():
        seed_employee_with_leave()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/leave-balances/update", data={
        "employee_id": ["5"],
        "current_balance_5": "1.2",
        "new_balance_5": "1.7",
        "reason_5": "Opening balance correction",
        "password": "wrong",
    }, follow_redirects=True)
    assert b"Incorrect password" in response.data
    with app.app_context():
        assert LeaveLedger.query.filter_by(transaction_type="MANUAL_ADJUSTMENT").count() == 0
        assert stored_leave_balance("5") == Decimal("1.2")


def test_leave_balance_page_renders_after_login(client, app):
    with app.app_context():
        seed_employee_with_leave()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/leave-balances")
    assert response.status_code == 200
    assert b"Leave Balance Management" in response.data
    assert b"Komal V Patel" in response.data


def test_leave_balance_manual_adjustment_updates_authoritative_opening(client, app):
    with app.app_context():
        seed_employee_with_leave()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/leave-balances/update", data={
        "employee_id": ["5"],
        "current_balance_5": "1.2",
        "new_balance_5": "1.7",
        "reason_5": "Opening balance correction",
        "password": "12345",
    }, follow_redirects=True)
    assert b"Leave balances updated successfully" in response.data
    assert b"Leave Balance Log Report" in response.data
    assert b"Opening balance correction" in response.data
    with app.app_context():
        adjustment = LeaveLedger.query.filter_by(employee_id="5", transaction_type="MANUAL_ADJUSTMENT").one()
        assert adjustment.amount == Decimal("0.5")
        assert adjustment.date == date.today()
        assert "Opening balance correction" in adjustment.description
        assert stored_leave_balance("5") == Decimal("1.7")
        assert opening_leave_for("5", "2026-08") == Decimal("1.7")
        audit = AuditLog.query.filter_by(action="LEAVE_BALANCE_MANUAL_UPDATE").one()
        assert "Old Leave Balance: 1.2" in audit.detail
        assert "New Leave Balance: 1.7" in audit.detail


def test_leave_balance_rejects_negative_value(client, app):
    with app.app_context():
        seed_employee_with_leave()
    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post("/leave-balances/update", data={
        "employee_id": ["5"],
        "current_balance_5": "1.2",
        "new_balance_5": "-0.5",
        "reason_5": "Correction",
        "password": "12345",
    }, follow_redirects=True)
    assert b"Leave balance cannot be negative" in response.data
