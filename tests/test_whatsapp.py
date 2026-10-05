import re
from html import unescape
from urllib.parse import unquote

from attendance import db
from attendance.models import PayrollMonth
from attendance.wage_groups import finalize_group
from tests.test_logic_fixes import MONTH, calculate, seed


def login(client):
    client.post("/login", data={"username": "admin", "password": "12345"})


def share_link(page):
    match = re.search(r'href="(https://wa\.me/\?text=[^"]+)"[^>]*\s+data-whatsapp-share\s+data-pdf-url="([^"]+)"', page)
    assert match, "no WhatsApp share button"
    return unquote(unescape(match.group(1)).split("text=", 1)[1]), unescape(match.group(2))


def test_draft_month_shares_attendance_summary_without_net_pay(client, app):
    with app.app_context():
        seed(minutes={6: 660, 7: 480})
        calculate()
    login(client)
    text, pdf = share_link(client.get(f"/payroll/{MONTH}/employee/5").data.decode())
    assert "Salary summary - July 2026" in text
    assert "Worker (ID 5)" in text
    assert "Overtime: 2h 00m" in text and "Less hours: 1h 00m" in text
    assert "Net pay" not in text
    assert "attendance summary PDF is attached" in text
    assert pdf.endswith("/employee/5/attendance-summary.pdf")


def test_finalized_month_shares_the_slip_with_net_pay(client, app):
    with app.app_context():
        seed()
        calculate()
        finalize_group(db.session.get(PayrollMonth, MONTH), "MONTHLY", "admin")
        db.session.commit()
    login(client)
    text, pdf = share_link(client.get(f"/payroll/{MONTH}/employee/5").data.decode())
    assert "*Net pay: ₹" in text
    assert "salary slip PDF is attached" in text
    assert pdf.endswith(f"/reports/{MONTH}/employee/5.pdf")


def test_daily_wage_shares_an_attendance_summary(client, app):
    with app.app_context():
        seed(wage="DAILY", salary="600")
        calculate()
    login(client)
    text, pdf = share_link(client.get(f"/payroll/{MONTH}/employee/5").data.decode())
    assert "Attendance summary - July 2026" in text
    assert "Days worked:" in text
    assert pdf.endswith("/employee/5/attendance-summary.pdf")


def test_no_share_button_before_calculation(client, app):
    with app.app_context():
        seed()
    login(client)
    assert "data-whatsapp-share" not in client.get(f"/payroll/{MONTH}/employee/5").data.decode()
