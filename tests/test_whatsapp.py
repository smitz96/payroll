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
    assert "pay slip PDF is attached" in text
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


def test_document_file_names_read_like_a_title():
    from attendance.sharing import employee_document_filename, short_name
    assert short_name("Asha G Chaudhary") == "Asha Chaudhary"
    assert short_name("Mohit R. Panjabi") == "Mohit Panjabi"
    assert short_name("Pradip Makwana") == "Pradip Makwana"
    assert employee_document_filename("Asha G Chaudhary", "Pay Slip", "2026-09") == "Asha Chaudhary Pay Slip for September 2026.pdf"
    assert employee_document_filename("Asha G Chaudhary", "Attendance Summary", "2026-09") == "Asha Chaudhary Attendance Summary for September 2026.pdf"
    assert "/" not in employee_document_filename("A/B Worker", "Pay Slip", "2026-09")


def test_pdf_downloads_carry_the_readable_name(client, app):
    with app.app_context():
        seed()
        calculate()
    login(client)
    response = client.get(f"/reports/{MONTH}/employee/5/attendance-summary.pdf")
    assert "Worker Attendance Summary for July 2026.pdf" in unquote(response.headers["Content-Disposition"])


def test_salary_slips_page_has_a_share_button_per_issued_slip(client, app):
    with app.app_context():
        seed()
        calculate()
        finalize_group(db.session.get(PayrollMonth, MONTH), "MONTHLY", "admin")
        db.session.commit()
    login(client)
    page = client.get("/salary-slips/5").data.decode()
    text, pdf = share_link(page)
    assert "*Net pay: ₹" in text and "pay slip PDF is attached" in text
    assert pdf.endswith(f"/reports/{MONTH}/employee/5.pdf")
    assert 'data-pdf-name="Worker Pay Slip for July 2026.pdf"' in page
