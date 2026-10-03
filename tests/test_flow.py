from datetime import datetime
from decimal import Decimal

from attendance import db
from attendance.models import Employee, PayrollMonth, PayrollResult, SalaryRecord, WeekOffRule
from attendance.utils import indian_number, money_text, review_items


def login(client):
    client.post("/login", data={"username": "admin", "password": "12345"})


def test_amounts_use_indian_grouping():
    assert money_text(Decimal("811000")) == "8,11,000.00"
    assert money_text(Decimal("12345678.5")) == "1,23,45,678.50"
    assert money_text(Decimal("999.99")) == "999.99"
    assert money_text(Decimal("-1800")) == "-1,800.00"
    assert money_text(None) == "0.00"
    assert indian_number(Decimal("100000"), places=0) == "1,00,000"


def test_review_message_reads_as_days():
    result = PayrollResult(message="2026-09-09: Needs Review; 2026-09-12: Punch Error")
    items = review_items(result)
    assert [item["label"] for item in items] == ["Wed 09 Sep", "Sat 12 Sep"]
    assert items[1]["reason"] == "Punch Error"


def seed_review_month():
    db.session.add(PayrollMonth(month="2026-09"))
    for employee_id, name, status in (("1", "Asha", "Calculated"), ("2", "Ravi", "Needs Review"), ("3", "Kiran", "Needs Review")):
        db.session.add(Employee(id=employee_id, name=name, salary_type="Monthly",
                                normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(WeekOffRule(employee_id=employee_id, confirmed_at=datetime.utcnow()))
        db.session.add(SalaryRecord(payroll_month="2026-09", employee_id=employee_id, name=name,
                                    salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.add(PayrollResult(payroll_month="2026-09", employee_id=employee_id, payroll_rule_type="MONTHLY",
                                     calculation_status=status, final_salary=Decimal("30000"),
                                     message="2026-09-09: Needs Review" if status != "Calculated" else ""))
    db.session.commit()


def test_payroll_menu_opens_the_month_being_worked_on(client, app):
    login(client)
    assert client.get("/payroll/").headers["Location"].endswith("/payroll/new")
    with app.app_context():
        db.session.add(PayrollMonth(month="2026-08", status="FINALIZED"))
        db.session.add(PayrollMonth(month="2026-09"))
        db.session.commit()
    assert client.get("/payroll/").headers["Location"].endswith("/payroll/2026-09")


def test_payroll_month_filters_to_employees_needing_review(client, app):
    with app.app_context():
        seed_review_month()
    login(client)
    page = client.get("/payroll/2026-09").data.decode()
    assert "Needs review (2)" in page
    assert "1 day to review" in page
    filtered = client.get("/payroll/2026-09?review=1").data.decode()
    assert ">Ravi<" in filtered and ">Kiran<" in filtered
    assert ">Asha<" not in filtered


def test_employee_page_steps_through_the_review_queue(client, app):
    with app.app_context():
        seed_review_month()
    login(client)
    page = client.get("/payroll/2026-09/employee/2").data.decode()
    assert "1 of 2" in page
    assert "/payroll/2026-09/employee/3" in page
    assert "Wed 09 Sep" in page
    calculated = client.get("/payroll/2026-09/employee/1").data.decode()
    # A cleanly calculated employee still points on to the next one to review.
    assert "2 employees still need review" in calculated
