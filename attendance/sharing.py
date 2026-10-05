"""Readable PDF file names, and WhatsApp share links for an employee's documents."""
import re
from decimal import Decimal
from urllib.parse import quote

from flask import url_for

from attendance.utils import display_month, minutes_to_duration, money_text

PAY_SLIP = "Pay Slip"
ATTENDANCE_SUMMARY = "Attendance Summary"
UNSAFE_FILENAME_CHARACTERS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def short_name(name):
    """The name without single-letter middle initials: Asha G Chaudhary -> Asha Chaudhary.

    The first and last parts are always kept, so a name that is only initials or a
    single word is left as it is.
    """
    parts = str(name or "").replace(".", " ").split()
    if len(parts) <= 2:
        return " ".join(parts)
    middle = [part for part in parts[1:-1] if len(part) > 1]
    return " ".join([parts[0], *middle, parts[-1]])


def employee_document_filename(name, document, month):
    """e.g. "Asha Chaudhary Pay Slip for September 2026.pdf"."""
    title = f"{short_name(name) or 'Employee'} {document} for {display_month(month)}"
    return UNSAFE_FILENAME_CHARACTERS.sub(" ", title).strip() + ".pdf"


def content_disposition(filename, disposition="inline"):
    """A Content-Disposition header that keeps spaces and non-ASCII names intact.

    Browsers use the UTF-8 `filename*` form; the quoted ASCII `filename` is the
    fallback for anything older.
    """
    fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def whatsapp_share(month, employee_id, salary, result, is_finalized, total_paid_days, days_in_month):
    """Pre-filled WhatsApp message and the PDF to attach.

    WhatsApp's share link carries text only, so the page downloads the PDF for the
    sender to attach, then opens WhatsApp with the summary typed out. No number is
    stored, so WhatsApp asks who to send it to. Net pay is only quoted once the wage
    group is finalized, because a draft figure can still change.
    """
    if not result or not salary or salary.normalized_salary_type not in {"MONTHLY", "DAILY"}:
        return None
    daily = salary.normalized_salary_type == "DAILY"

    def days(value):
        text = f"{Decimal(value or 0).quantize(Decimal('0.01')):f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    lines = [
        f"*{'Attendance' if daily and not is_finalized else 'Salary'} summary - {display_month(month)}*",
        f"{salary.name} (ID {employee_id})",
        "",
        f"Paid days: {days(total_paid_days)} of {days_in_month}",
    ]
    if daily:
        lines.append(f"Days worked: {days(result.paid_working_days)}")
    else:
        lines.append(f"Loss of pay: {days(result.lop_days)} day(s)")
        lines.append(f"Leave used: {days(result.leave_used)} · Leave balance: {days(result.closing_leave)}")
    lines.append(f"Less hours: {minutes_to_duration(result.less_hours_minutes or 0)}")
    lines.append(f"Overtime: {minutes_to_duration(result.payable_ot_minutes or 0)}")
    if is_finalized and result.final_salary is not None:
        lines.append(f"*Net pay: ₹{money_text(result.final_salary)}*")
    if is_finalized and not daily:
        document, url = PAY_SLIP, url_for("reports.employee_pdf", month=month, employee_id=employee_id)
    else:
        document = ATTENDANCE_SUMMARY
        url = url_for("reports.employee_attendance_summary_pdf", month=month, employee_id=employee_id)
    lines += ["", f"Your {document.lower()} PDF is attached."]
    return {
        "url": "https://wa.me/?text=" + quote("\n".join(lines)),
        "pdf_url": url,
        "pdf_name": employee_document_filename(salary.name, document, month),
        "document": document.lower(),
    }
