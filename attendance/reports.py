import calendar
import csv
import re
from io import BytesIO, StringIO
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from attendance import db
from attendance.loans import loan_installment_for_loan, loan_paid_before_month, loan_pending_after_month, loan_remaining_before_month, loan_repayment_schedule
from attendance.models import AttendanceRecord, Employee, Loan, PayrollResult, SalaryRecord
from attendance.utils import minutes_to_duration

ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

# Print palette mirrors the iOS system colours used by static/css/app.css, using the
# text-safe variants so the paper output keeps the same contrast as the screen.
INK = colors.HexColor("#1C1C1E")           # label
MUTED = colors.HexColor("#6B6B70")         # secondaryLabel, flattened for print
FAINT = colors.HexColor("#8E8E93")         # tertiaryLabel
SEPARATOR = colors.HexColor("#D8D8DC")     # hairline
SURFACE = colors.white
SURFACE_SOFT = colors.HexColor("#F2F2F7")  # secondarySystemBackground
ZEBRA = colors.HexColor("#FAFAFC")
TINT = colors.HexColor("#0069DE")          # system blue (fill)
TINT_TEXT = colors.HexColor("#0050C8")     # system blue (text-safe)
TINT_WASH = colors.HexColor("#EBF2FD")
GREEN_TEXT = colors.HexColor("#1B7032")
RED_TEXT = colors.HexColor("#B3000C")
ORANGE_TEXT = colors.HexColor("#9A4A00")
GREEN_WASH = colors.HexColor("#E8F7EC")
ORANGE_WASH = colors.HexColor("#FDF0E3")
RED_WASH = colors.HexColor("#FCEBEC")

# Kept for the brand lockup only; the report chrome itself is system-coloured.
BRAND_BLUE = colors.HexColor("#0C306A")
BRAND_GREEN = colors.HexColor("#A6CE15")
BRAND_MUTED = MUTED
CARD_RADIUS = 7
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "smartfill-logo.png"


def _status_colours(status):
    """Wash and text colour for a calculation or attendance status."""
    text = str(status or "").strip().lower()
    if text in {"calculated", "full day present", "full day", "week off worked", "paid"}:
        return GREEN_WASH, GREEN_TEXT
    if text in {"needs review", "punch error", "half day present", "half day", "pending"}:
        return ORANGE_WASH, ORANGE_TEXT
    if "lop" in text or text in {"absent / attendance missing", "attendance missing", "skipped"}:
        return RED_WASH, RED_TEXT
    return SURFACE_SOFT, MUTED


def payroll_month_days(month):
    year, month_number = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, month_number)[1]


def total_paid_days(result):
    if not result:
        return ""
    if result.payroll_rule_type == "DAILY":
        return Decimal(result.paid_working_days or 0) + Decimal(result.holidays or 0)
    return Decimal(result.paid_working_days or 0) + Decimal(result.week_offs or 0) + Decimal(result.paid_leaves or 0)


def result_has_loan(result):
    if not result:
        return False
    return bool(
        Decimal(getattr(result, "loan_deduction", 0) or 0) != 0
        or Decimal(getattr(result, "loan_pending_amount", 0) or 0) != 0
    )


def result_has_advance(result):
    if not result:
        return False
    return Decimal(getattr(result, "advance_deduction", 0) or 0) != 0


def payroll_summary_csv(month):
    out = StringIO()
    writer = csv.writer(out)
    month_days = payroll_month_days(month)
    writer.writerow([
        "Employee ID", "Name", "Department", "Designation", "Wage Type", "Payroll Rule Status", "Salary",
        "Month Days", "Paid Working Days", "Week Offs", "Total Paid Days", "Full Days", "Half Days", "Paid Leave", "LOP", "Opening Leave",
        "Leave Earned", "Leave Used", "Closing Leave", "Working Hour Deduction",
        "LOP Deduction", "Overtime", "Adjustment", "Leave Encashment Days", "Leave Encashment Amount", "Loan Deduction", "Advance Salary Deduction", "Final Salary", "Calculation Status",
    ])
    salaries = {s.employee_id: s for s in SalaryRecord.query.filter_by(payroll_month=month).all()}
    results = {r.employee_id: r for r in PayrollResult.query.filter_by(payroll_month=month).all()}
    # The Department column existed but was always blank; employee master now holds it.
    employees = {e.id: e for e in Employee.query.all()}
    for employee_id, salary in salaries.items():
        result = results.get(employee_id)
        employee = employees.get(employee_id)
        writer.writerow([
            employee_id,
            salary.name,
            (employee.department if employee else "") or "",
            (employee.designation if employee else "") or "",
            salary.salary_type,
            result.calculation_status if result else "Not Calculated",
            salary.salary,
            month_days if result and result.final_salary is not None else "",
            result.paid_working_days if result and result.final_salary is not None else "",
            result.week_offs if result and result.final_salary is not None else "",
            total_paid_days(result) if result and result.final_salary is not None else "",
            result.full_days if result and result.final_salary is not None else "",
            result.half_days if result and result.final_salary is not None else "",
            result.paid_leaves if result and result.final_salary is not None else "",
            result.lop_days if result and result.final_salary is not None else "",
            result.opening_leave if result and result.final_salary is not None else "",
            result.leave_earned if result and result.final_salary is not None else "",
            result.leave_used if result and result.final_salary is not None else "",
            result.closing_leave if result and result.final_salary is not None else "",
            result.less_hours_deduction if result and result.final_salary is not None else "",
            result.lop_deduction if result and result.final_salary is not None else "",
            result.ot_amount if result and result.final_salary is not None else "",
            salary.adjustment,
            result.leave_encashment_days if result and result.final_salary is not None else (salary.leave_encashment_days if getattr(salary, "leave_encashment_enabled", False) else ""),
            result.leave_encashment_amount if result and result.final_salary is not None else (salary.leave_encashment_amount if getattr(salary, "leave_encashment_enabled", False) else ""),
            result.loan_deduction if result and result.final_salary is not None else getattr(salary, "loan", ""),
            result.advance_deduction if result and result.final_salary is not None else "",
            result.final_salary if result and result.final_salary is not None else "",
            result.calculation_status if result else "Payroll Rules Not Configured",
        ])
    return out.getvalue()


def attendance_detail_csv(month):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["Employee ID", "Date", "Day", "First Punch", "Last Punch", "Raw Working Hours", "Actual Duration", "Status", "Warning"])
    for rec in AttendanceRecord.query.filter_by(payroll_month=month).order_by(AttendanceRecord.employee_id, AttendanceRecord.date):
        writer.writerow([rec.employee_id, rec.date, rec.day, rec.first_punch, rec.last_punch, rec.raw_working_hours, minutes_to_duration(rec.actual_minutes), rec.parse_status, rec.warning])
    return out.getvalue()


def employee_name_map(month):
    salary_names = {salary.employee_id: salary.name for salary in SalaryRecord.query.filter_by(payroll_month=month).all()}
    employee_names = {employee.id: employee.name for employee in Employee.query.all()}
    employee_names.update(salary_names)
    return employee_names


def overtime_report_csv(month):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["Employee ID", "Employee Name", "Date", "In Time", "Out Time", "Working Hours", "OT Paid Minutes", "OT Paid Amount"])
    names = employee_name_map(month)
    for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id):
        for item in result.detail_json or []:
            payable_ot = int(item.get("payable_ot") or 0)
            ot_amount = Decimal(str(item.get("ot_amount") or "0"))
            if payable_ot <= 0 and ot_amount <= 0:
                continue
            writer.writerow([
                result.employee_id,
                names.get(result.employee_id, ""),
                item.get("date", ""),
                item.get("first_punch", ""),
                item.get("last_punch", ""),
                item.get("actual_duration") or item.get("raw_working_hours", ""),
                payable_ot,
                ot_amount,
            ])
    return out.getvalue()


def less_hours_report_csv(month):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["Employee ID", "Employee Name", "Date", "In Time", "Out Time", "Working Hours", "Less Hours Minutes", "Less Hours Deduction"])
    names = employee_name_map(month)
    for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id):
        for item in result.detail_json or []:
            shortage = int(item.get("shortage_minutes") or 0)
            deduction = Decimal(str(item.get("shortage_deduction") or "0"))
            if shortage <= 0 and deduction <= 0:
                continue
            writer.writerow([
                result.employee_id,
                names.get(result.employee_id, ""),
                item.get("date", ""),
                item.get("first_punch", ""),
                item.get("last_punch", ""),
                item.get("rounded_duration") or item.get("actual_duration") or item.get("raw_working_hours", ""),
                shortage,
                deduction,
            ])
    return out.getvalue()


def error_report_csv(month):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["Area", "Employee ID", "Issue"])
    for salary in SalaryRecord.query.filter_by(payroll_month=month):
        if salary.warning:
            writer.writerow(["Salary", salary.employee_id, salary.warning])
    for rec in AttendanceRecord.query.filter_by(payroll_month=month):
        if rec.warning:
            writer.writerow(["Attendance", rec.employee_id, f"{rec.date}: {rec.warning}"])
    for result in PayrollResult.query.filter_by(payroll_month=month):
        if result.calculation_status != "Calculated":
            writer.writerow(["Payroll", result.employee_id, result.message or result.calculation_status])
    return out.getvalue()


def display_month(month):
    try:
        year, month_number = (int(part) for part in month.split("-"))
    except (AttributeError, ValueError):
        return month
    return f"{calendar.month_name[month_number]} {year}"


def calculated_results_for_month(month):
    return [
        result for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id)
        if result.final_salary is not None
    ]


def report_pdf(title, subtitle, headers, rows, col_widths=None, font_size=7, landscape_page=True, kpis=None, status_column=None):
    buffer = BytesIO()
    pagesize = landscape(A4) if landscape_page else A4
    doc = SimpleDocTemplate(buffer, pagesize=pagesize, leftMargin=11 * mm, rightMargin=11 * mm, topMargin=11 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("ReportCell", parent=styles["Normal"], fontSize=font_size, leading=font_size + 2.2, textColor=INK)
    header_style = ParagraphStyle(
        "ReportHeaderCell",
        parent=cell_style,
        fontName="Helvetica-Bold",
        fontSize=font_size - 0.4,
        textColor=MUTED,
    )
    empty_style = ParagraphStyle("ReportEmpty", parent=cell_style, textColor=FAINT)
    available_width = pagesize[0] - doc.leftMargin - doc.rightMargin
    if not col_widths:
        col_widths = [available_width / len(headers)] * len(headers)

    table_rows = [[Paragraph(str(value).upper(), header_style) for value in headers]]
    if rows:
        for row in rows:
            table_rows.append([Paragraph(str(value if value not in (None, "") else "—"), cell_style) for value in row])
    else:
        table_rows.append([Paragraph("No records found", empty_style)] + [Paragraph("", cell_style)] * (len(headers) - 1))

    story = [_report_brand_header(title, subtitle, styles, available_width), Spacer(1, 9)]
    if kpis:
        story.extend([_kpi_row(kpis, available_width), Spacer(1, 9)])
    story.append(_table(table_rows, col_widths=col_widths, font_size=font_size, status_column=status_column if rows else None))
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()


def build_payroll_summary_pdf(month):
    names = employee_name_map(month)
    designations = {employee.id: employee.designation or "" for employee in Employee.query.all()}
    rows = []
    total_payable = Decimal("0")
    total_deduction = Decimal("0")
    for result in calculated_results_for_month(month):
        salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=result.employee_id).first()
        total_payable += Decimal(result.final_salary or 0)
        total_deduction += Decimal(result.total_deduction or 0)
        rows.append([
            result.employee_id,
            names.get(result.employee_id, result.employee_id),
            designations.get(result.employee_id, ""),
            salary.salary_type if salary else "",
            pdf_money(salary.salary) if salary else "",
            payroll_month_days(month),
            result.paid_working_days,
            result.week_offs,
            total_paid_days(result),
            result.paid_leaves,
            result.lop_days,
            pdf_money(result.total_deduction),
            pdf_money(result.total_addition),
            pdf_money(result.final_salary),
            result.calculation_status,
        ])
    return report_pdf(
        "Payroll Summary",
        display_month(month),
        ["ID", "Employee", "Designation", "Wage Type", "Base", "Days", "Working", "Week Off", "Total Paid", "Leave", "LOP", "Deduction", "Addition", "Payable", "Status"],
        rows,
        col_widths=[11 * mm, 30 * mm, 28 * mm, 16 * mm, 18 * mm, 11 * mm, 15 * mm, 15 * mm, 16 * mm, 12 * mm, 11 * mm, 20 * mm, 18 * mm, 21 * mm, 21 * mm],
        font_size=6.2,
        status_column=14,
        kpis=[
            ("Employees", len(rows)),
            ("Total payable", pdf_money(total_payable)),
            ("Total deductions", pdf_money(total_deduction)),
            ("Payroll month", display_month(month)),
        ],
    )


def build_attendance_detail_pdf(month):
    rows = []
    names = employee_name_map(month)
    for rec in AttendanceRecord.query.filter_by(payroll_month=month).order_by(AttendanceRecord.employee_id, AttendanceRecord.date):
        rows.append([
            rec.employee_id,
            names.get(rec.employee_id, rec.employee_name or rec.employee_id),
            rec.date,
            rec.day,
            rec.first_punch or "-",
            rec.last_punch or "-",
            minutes_to_duration(rec.actual_minutes),
            rec.parse_status,
            rec.warning or "",
        ])
    review_rows = sum(1 for row in rows if row[7] != "OK")
    return report_pdf(
        "Detailed Attendance",
        f"{display_month(month)} · Every imported punch day",
        ["ID", "Employee", "Date", "Day", "In", "Out", "Working Hours", "Status", "Warning"],
        rows,
        col_widths=[13 * mm, 42 * mm, 22 * mm, 22 * mm, 20 * mm, 20 * mm, 25 * mm, 24 * mm, 86 * mm],
        font_size=6.2,
        kpis=[
            ("Attendance rows", f"{len(rows):,}"),
            ("Employees", len({row[0] for row in rows})),
            ("Needs review", f"{review_rows:,}"),
            ("Payroll month", display_month(month)),
        ],
    )


def build_overtime_report_pdf(month):
    rows = []
    names = employee_name_map(month)
    for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id):
        for item in result.detail_json or []:
            payable_ot = int(item.get("payable_ot") or 0)
            ot_amount = Decimal(str(item.get("ot_amount") or "0"))
            if payable_ot <= 0 and ot_amount <= 0:
                continue
            rows.append([
                result.employee_id,
                names.get(result.employee_id, ""),
                item.get("date", ""),
                item.get("first_punch", "") or "-",
                item.get("last_punch", "") or "-",
                item.get("actual_duration") or item.get("raw_working_hours", ""),
                payable_ot,
                pdf_money(ot_amount),
            ])
    total_minutes = sum(int(row[6] or 0) for row in rows)
    total_amount = sum(Decimal(str(row[7]).replace(",", "") or 0) for row in rows)
    return report_pdf(
        "Overtime Report",
        f"{display_month(month)} · Only days with payable overtime",
        ["ID", "Employee", "Date", "In Time", "Out Time", "Working Hours", "OT Paid Minutes", "OT Amount"],
        rows,
        col_widths=[16 * mm, 58 * mm, 26 * mm, 26 * mm, 26 * mm, 32 * mm, 36 * mm, 32 * mm],
        font_size=7,
        kpis=[
            ("Overtime days", len(rows)),
            ("Employees", len({row[0] for row in rows})),
            ("Payable minutes", f"{total_minutes:,}"),
            ("Overtime amount", pdf_money(total_amount)),
        ],
    )


def build_less_hours_report_pdf(month):
    rows = []
    names = employee_name_map(month)
    for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id):
        for item in result.detail_json or []:
            shortage = int(item.get("shortage_minutes") or 0)
            deduction = Decimal(str(item.get("shortage_deduction") or "0"))
            if shortage <= 0 and deduction <= 0:
                continue
            rows.append([
                result.employee_id,
                names.get(result.employee_id, ""),
                item.get("date", ""),
                item.get("first_punch", "") or "-",
                item.get("last_punch", "") or "-",
                item.get("rounded_duration") or item.get("actual_duration") or item.get("raw_working_hours", ""),
                shortage,
                pdf_money(deduction),
            ])
    total_minutes = sum(int(row[6] or 0) for row in rows)
    total_deduction = sum(Decimal(str(row[7]).replace(",", "") or 0) for row in rows)
    return report_pdf(
        "Less Hours Report",
        f"{display_month(month)} · Only days with a short-hours deduction",
        ["ID", "Employee", "Date", "In Time", "Out Time", "Working Hours", "Less Minutes", "Deduction"],
        rows,
        col_widths=[16 * mm, 58 * mm, 26 * mm, 26 * mm, 26 * mm, 32 * mm, 32 * mm, 36 * mm],
        font_size=7,
        kpis=[
            ("Short days", len(rows)),
            ("Employees", len({row[0] for row in rows})),
            ("Short minutes", f"{total_minutes:,}"),
            ("Total deduction", pdf_money(total_deduction)),
        ],
    )


def build_error_report_pdf(month):
    rows = []
    for salary in SalaryRecord.query.filter_by(payroll_month=month).order_by(SalaryRecord.employee_id):
        if salary.warning:
            rows.append(["Salary", salary.employee_id, salary.name, salary.warning])
    names = employee_name_map(month)
    for rec in AttendanceRecord.query.filter_by(payroll_month=month).order_by(AttendanceRecord.employee_id, AttendanceRecord.date):
        if rec.warning:
            rows.append(["Attendance", rec.employee_id, names.get(rec.employee_id, rec.employee_name or ""), f"{rec.date}: {rec.warning}"])
    for result in PayrollResult.query.filter_by(payroll_month=month).order_by(PayrollResult.employee_id):
        if result.calculation_status != "Calculated":
            rows.append(["Payroll", result.employee_id, names.get(result.employee_id, ""), result.message or result.calculation_status])
    area_counts = {area: sum(1 for row in rows if row[0] == area) for area in ("Salary", "Attendance", "Payroll")}
    return report_pdf(
        "Error Report",
        f"{display_month(month)} · Items to resolve before finalizing",
        ["Area", "Employee ID", "Employee", "Issue"],
        rows,
        col_widths=[28 * mm, 26 * mm, 56 * mm, 160 * mm],
        font_size=7,
        kpis=[
            ("Total issues", len(rows)),
            ("Attendance", area_counts["Attendance"]),
            ("Payroll", area_counts["Payroll"]),
            ("Wage data", area_counts["Salary"]),
        ],
    )


def display_attendance_status(status):
    mapping = {
        "Full Day Present": "Full Day",
        "Half Day Present": "Half Day",
        "Paid Leave": "Paid Leave",
        "Half-Day Paid Leave": "Half-Day Paid Leave",
        "Full Day LOP": "Full Day LOP",
        "Half Day LOP": "Half Day LOP",
        "Unpaid Leave / LOP": "LOP",
        "Holiday": "Holiday",
        "Week Off": "Week Off",
        "Week Off Worked": "Week Off Worked",
        "Sandwich Leave": "Sandwich Leave",
        "Needs Review": "Needs Review",
        "Punch Error": "Punch Error",
        "Absent / Attendance Missing": "Attendance Missing",
        "Work From Home": "Work From Home",
        "Ignore": "Ignore",
    }
    return mapping.get(status or "", status or "N/A")


def pdf_money(value):
    if value is None:
        return "N/A"
    return f"{Decimal(value):,.2f}"


def words_below_thousand(number):
    number = int(number)
    parts = []
    if number >= 100:
        parts.append(ONES[number // 100] + " hundred")
        number %= 100
    if number >= 20:
        tens = TENS[number // 10]
        ones = number % 10
        parts.append(tens + (f" {ONES[ones]}" if ones else ""))
    elif number:
        parts.append(ONES[number])
    return " ".join(parts)


def integer_to_indian_words(number):
    number = int(number)
    if number == 0:
        return "zero"
    groups = [
        (10000000, "crore"),
        (100000, "lakh"),
        (1000, "thousand"),
        (1, ""),
    ]
    parts = []
    for value, label in groups:
        chunk = number // value
        if chunk:
            parts.append(words_below_thousand(chunk) + (f" {label}" if label else ""))
            number %= value
    return " ".join(parts)


def money_in_words(value):
    if value is None:
        return "Not calculated"
    amount = Decimal(value).quantize(Decimal("0.01"))
    rupees = int(amount)
    paise = int((amount - Decimal(rupees)) * 100)
    words = f"Rupees {integer_to_indian_words(rupees)}"
    if paise:
        words += f" and {integer_to_indian_words(paise)} paise"
    return words.capitalize() + " only"


def result_for_employee(month, employee_id):
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    result = PayrollResult.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    employee = db.session.get(Employee, employee_id)
    return employee, salary, result


def employee_salary_summary_rows(salary, result):
    if not result:
        return [["Calculation Status", "Not Calculated"]]
    base_salary = pdf_money(salary.salary) if salary else "N/A"
    final_salary = pdf_money(result.final_salary) if result.final_salary is not None else "Not Calculated"
    rows = [
        ["Calculation Status", result.calculation_status],
        ["Wage Type", salary.salary_type if salary else "N/A"],
        ["Base Salary", base_salary],
        ["Days in Month", payroll_month_days(result.payroll_month)],
        ["Paid Working Days", result.paid_working_days],
        ["Week Offs", result.week_offs],
        ["Total Paid Days", total_paid_days(result)],
        ["Full Days", result.full_days],
        ["Half Days", result.half_days],
        ["Paid Leave", result.paid_leaves],
        ["LOP Days", result.lop_days],
        ["Leave Balance", result.opening_leave],
        ["Leave Earned This Month", result.leave_earned],
        ["Leave Used This Month", result.leave_used],
        ["Leave Carry Forwarded", result.closing_leave],
        ["Less-Hours Deduction", pdf_money(result.less_hours_deduction)],
        ["LOP Deduction", pdf_money(result.lop_deduction)],
        ["Over Time Amount", pdf_money(result.ot_amount)],
        ["Adjustment", pdf_money(result.manual_adjustment)],
        ["Leave Encashment Days", getattr(result, "leave_encashment_days", 0)],
        ["Leave Encashment Amount", pdf_money(getattr(result, "leave_encashment_amount", 0))],
    ]
    if result_has_loan(result):
        rows.extend([
            ["Loan Deduction", pdf_money(getattr(result, "loan_deduction", 0))],
            ["Pending Loan Amount", pdf_money(getattr(result, "loan_pending_amount", 0))],
        ])
    if result_has_advance(result):
        rows.append(["Advance Salary Deduction", pdf_money(getattr(result, "advance_deduction", 0))])
    rows.extend([
        ["Total Deduction", pdf_money(result.total_deduction)],
        ["Total Addition", pdf_money(result.total_addition)],
        ["Final Payable Salary", final_salary],
    ])
    return rows


def employee_compact_summary_rows(salary, result):
    if not result:
        return [["Status", "Not Calculated", "Wage Type", salary.salary_type if salary else "N/A"]]
    final_value = result.final_salary if result.final_salary is not None else None
    if result.payroll_rule_type == "DAILY":
        rows = [
            ["Payable Salary", pdf_money(final_value) if final_value is not None else "Not Calculated", "In Words", money_in_words(final_value)],
            ["Daily Wage", pdf_money(salary.salary) if salary else "N/A", "Wage Type", salary.salary_type if salary else "N/A"],
            ["Status", result.calculation_status, "Days in Month", payroll_month_days(result.payroll_month)],
            ["Paid Working Days", result.paid_working_days, "Paid Holidays", result.holidays],
            ["Payable Days", total_paid_days(result), "Week Offs", result.week_offs],
            ["Less Hours Deduction", pdf_money(result.less_hours_deduction), "Over Time", pdf_money(result.ot_amount)],
            ["Adjustment", pdf_money(result.manual_adjustment), "Leave Management", "Not applicable"],
        ]
        if result_has_loan(result):
            rows.append(["Loan Deduction", pdf_money(getattr(result, "loan_deduction", 0)), "Pending Loan Amount", pdf_money(getattr(result, "loan_pending_amount", 0))])
        if result_has_advance(result):
            rows.append(["Advance Salary Deduction", pdf_money(getattr(result, "advance_deduction", 0)), "", ""])
        return rows
    rows = [
        ["Payable Salary", pdf_money(final_value) if final_value is not None else "Not Calculated", "In Words", money_in_words(final_value)],
        ["Base Salary", pdf_money(salary.salary) if salary else "N/A", "Wage Type", salary.salary_type if salary else "N/A"],
        ["Status", result.calculation_status, "Days in Month", payroll_month_days(result.payroll_month)],
        ["Paid Working Days", result.paid_working_days, "Week Offs", result.week_offs],
        ["Total Paid Days", total_paid_days(result), "LOP Days", result.lop_days],
        ["Leave Balance", result.opening_leave, "Leave Earned This Month", result.leave_earned],
        ["Leave Used This Month", result.leave_used, "Leave Carry Forwarded", result.closing_leave],
        ["Less Hours Deduction", pdf_money(result.less_hours_deduction), "Over Time", pdf_money(result.ot_amount)],
        ["Adjustment", pdf_money(result.manual_adjustment), "Leave Encashed", f"{getattr(result, 'leave_encashment_days', 0)}d / {pdf_money(getattr(result, 'leave_encashment_amount', 0))}"],
    ]
    if result_has_loan(result):
        rows.append(["Loan Deduction", pdf_money(getattr(result, "loan_deduction", 0)), "Pending Loan Amount", pdf_money(getattr(result, "loan_pending_amount", 0))])
    if result_has_advance(result):
        rows.append(["Advance Salary Deduction", pdf_money(getattr(result, "advance_deduction", 0)), "", ""])
    return rows


def employee_detail_rows(result, compact=False):
    rows = [["Date", "Working Hours", "Status", "Paid Day", "Shortage", "Over Time"]]
    if not result or not result.detail_json:
        rows.append(["N/A", "N/A", "Not Calculated", "N/A", "N/A", "N/A"])
        return rows
    for item in result.detail_json:
        rows.append([
            item.get("date", ""),
            item.get("rounded_duration", ""),
            display_attendance_status(item.get("attendance_status")),
            item.get("paid_day_value", ""),
            item.get("shortage_minutes", 0),
            item.get("payable_ot", 0),
        ])
    return rows


def employee_detail_compact_rows(result):
    detail_rows = employee_detail_rows(result)[1:]
    split = (len(detail_rows) + 1) // 2
    numbered_rows = list(enumerate(detail_rows, start=1))
    left = numbered_rows[:split]
    right = numbered_rows[split:]
    rows = [["Sr No", "Date", "Working Hours", "Status", "Shortage", "Over Time", "", "Sr No", "Date", "Working Hours", "Status", "Shortage", "Over Time"]]
    max_len = max(len(left), len(right), 1)
    for index in range(max_len):
        l_index, l = left[index] if index < len(left) else ("", ["", "", "", "", "", ""])
        r_index, r = right[index] if index < len(right) else ("", ["", "", "", "", "", ""])
        rows.append([l_index, l[0], l[1], l[2], l[4], l[5], "", r_index, r[0], r[1], r[2], r[4], r[5]])
    return rows


def _brand_logo(width=30 * mm, height=10.5 * mm):
    if LOGO_PATH.exists():
        image = Image(str(LOGO_PATH), width=width, height=height)
        image.hAlign = "LEFT"
        return image
    return Paragraph("SMARTfill", ParagraphStyle("LogoFallback", fontName="Helvetica-Bold", fontSize=13, textColor=BRAND_BLUE))


def _salary_slip_header(month, salary, result, styles, compact=False):
    employee_id = salary.employee_id if salary else (result.employee_id if result else "")
    employee_name = salary.name if salary else employee_id
    title_size = 8.5 if compact else 12
    text_size = 5.4 if compact else 8
    title_style = ParagraphStyle(
        "SlipTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=title_size,
        leading=title_size + 1,
        textColor=BRAND_BLUE,
        alignment=TA_LEFT,
    )
    meta_style = ParagraphStyle(
        "SlipMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=text_size,
        leading=text_size + 1,
        textColor=BRAND_MUTED,
    )
    employee_style = ParagraphStyle(
        "SlipEmployee",
        parent=meta_style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#172033"),
    )
    employee = db.session.get(Employee, employee_id) if employee_id else None
    role_bits = [bit for bit in [(employee.designation if employee else ""), (employee.department if employee else "")] if bit]
    status = result.calculation_status if result else "Not Calculated"
    _wash, status_colour = _status_colours(status)
    status_style = ParagraphStyle("SlipStatus", parent=meta_style, fontName="Helvetica-Bold", textColor=status_colour)

    left = [
        Paragraph("Salary Slip", title_style),
        Paragraph(display_month(month), meta_style),
    ]
    middle = [
        Paragraph(f"{employee_id} &middot; {employee_name}", employee_style),
        Paragraph(" &middot; ".join(role_bits) if role_bits else "Designation not set", meta_style),
    ]
    right = [
        Paragraph(status, status_style),
        Paragraph("SMARTfill Payroll", meta_style),
    ]
    table = Table(
        [[left, middle, right, _brand_logo(width=26 * mm if compact else 30 * mm, height=9 * mm if compact else 10.5 * mm)]],
        colWidths=[40 * mm, 76 * mm, 44 * mm, 34 * mm] if compact else [44 * mm, 74 * mm, 40 * mm, 38 * mm],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.5, SEPARATOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("ALIGN", (3, 0), (3, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5 if compact else 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5 if compact else 8),
    ]))
    table.cornerRadii = [CARD_RADIUS] * 4
    return table


def _report_brand_header(title, subtitle, styles, available_width, compact=False):
    """iOS large-title masthead: heavy tight title, muted subtitle, logo to the right."""
    title_size = 12 if compact else 17
    subtitle_size = 6.5 if compact else 8.5
    title_style = ParagraphStyle(
        "BrandedReportTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=title_size,
        leading=title_size + 2,
        textColor=INK,
        spaceAfter=0,
    )
    subtitle_style = ParagraphStyle(
        "BrandedReportSubtitle",
        parent=styles["Normal"],
        fontSize=subtitle_size,
        leading=subtitle_size + 3,
        textColor=MUTED,
    )
    left = [Paragraph(title, title_style), Paragraph(subtitle, subtitle_style)]
    logo_width = 30 * mm if compact else 34 * mm
    table = Table(
        [[left, _brand_logo(width=logo_width, height=10 * mm if compact else 11.5 * mm)]],
        colWidths=[available_width - logo_width - 8 * mm, logo_width + 8 * mm],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, SEPARATOR),
    ]))
    return table


def _kpi_row(items, available_width, compact=False):
    """A row of iOS metric tiles: muted caption above a large tight value."""
    if not items:
        return Spacer(1, 0)
    label_size = 5.6 if compact else 7
    value_size = 9 if compact else 12.5
    label_style = ParagraphStyle("KpiLabel", fontName="Helvetica", fontSize=label_size, leading=label_size + 2, textColor=MUTED)
    value_style = ParagraphStyle("KpiValue", fontName="Helvetica-Bold", fontSize=value_size, leading=value_size + 2, textColor=INK)
    cells = [[Paragraph(label, label_style), Paragraph(str(value), value_style)] for label, value in items]
    width = available_width / len(items)
    table = Table([[Table([[c[0]], [c[1]]], colWidths=[width - 6]) for c in cells]], colWidths=[width] * len(items))
    inner = TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.5, SEPARATOR),
    ])
    for cell_table in table._cellvalues[0]:
        cell_table.setStyle(inner)
        cell_table.cornerRadii = [CARD_RADIUS] * 4
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
        ("LEFTPADDING", (1, 0), (-1, 0), 3),
        ("RIGHTPADDING", (0, 0), (-2, 0), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _pdf_header_footer(canvas, doc):
    """Hairline rule with muted footer text, matching the app's separator treatment."""
    canvas.saveState()
    baseline = 7.5 * mm
    canvas.setStrokeColor(SEPARATOR)
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, baseline + 3.5 * mm, doc.pagesize[0] - doc.rightMargin, baseline + 3.5 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, baseline, "SMARTfill Payroll")
    canvas.setFillColor(FAINT)
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, baseline, f"Page {doc.page}")
    canvas.restoreState()


def _titled_page_callback(title):
    def callback(canvas, doc):
        canvas.setTitle(title)
        canvas.setAuthor("SMARTfill Attendance & Payroll Management")
        _pdf_header_footer(canvas, doc)

    return callback


def _status_column_style(data, column, header=True):
    """Colour a status column per row, mirroring the status badges in the web UI."""
    style = []
    start = 1 if header else 0
    for index in range(start, len(data)):
        cell = data[index][column] if column < len(data[index]) else ""
        text = getattr(cell, "text", cell)
        # Paragraph.text keeps the source markup, so strip any tags before matching.
        text = re.sub(r"<[^>]+>", "", str(text)).strip()
        if not text or text == "—":
            continue
        _wash, colour = _status_colours(text)
        if colour is not MUTED:
            style.append(("TEXTCOLOR", (column, index), (column, index), colour))
    return style


def _table(data, col_widths=None, font_size=8, header=True, highlight_rows=None, blank_columns=None, status_column=None):
    """An iOS-style list: rounded card, hairline row separators, no vertical rules.

    The previous look was a full grid with a tinted header band. Apple's tables
    separate rows with a single hairline and let whitespace do the column work, so
    the grid is dropped in favour of zebra banding and generous padding.
    """
    compact = font_size <= 5.8
    table = Table(
        data,
        colWidths=col_widths,
        repeatRows=1 if header else 0,
        cornerRadii=[CARD_RADIUS] * 4,
    )
    pad_y = 1.5 if compact else 4.5
    pad_x = 3 if compact else 6
    highlight_rows = highlight_rows or []
    blank_columns = blank_columns or []
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
        ("TOPPADDING", (0, 0), (-1, -1), pad_y),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad_y),
        ("BACKGROUND", (0, 0), (-1, -1), SURFACE),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, SEPARATOR),
        ("BOX", (0, 0), (-1, -1), 0.5, SEPARATOR),
    ]
    if header:
        style.extend([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), SURFACE_SOFT),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, SEPARATOR),
            ("TOPPADDING", (0, 0), (-1, 0), pad_y + 1),
            ("BOTTOMPADDING", (0, 0), (-1, 0), pad_y + 1),
        ])
        # Zebra banding, which reads better than rules on wide landscape tables.
        for index in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, index), (-1, index), ZEBRA))
    else:
        style.append(("FONTNAME", (0, 0), (-1, -1), "Helvetica"))
    for row in highlight_rows:
        style.extend([
            ("BACKGROUND", (0, row), (-1, row), TINT_WASH),
            ("TEXTCOLOR", (0, row), (-1, row), TINT_TEXT),
            ("FONTNAME", (0, row), (-1, row), "Helvetica-Bold"),
        ])
    if status_column is not None:
        style.extend(_status_column_style(data, status_column, header=header))
    # Spacer columns used by the two-up compact slip; keep them invisible.
    for column in blank_columns:
        style.extend([
            ("BACKGROUND", (column, 0), (column, -1), SURFACE),
            ("TEXTCOLOR", (column, 0), (column, -1), SURFACE),
            ("LINEBELOW", (column, 0), (column, -1), 0, SURFACE),
        ])
    table.setStyle(TableStyle(style))
    return table


def employee_report_block(month, salary, result, styles, compact=False):
    header = _salary_slip_header(month, salary, result, styles, compact=compact)
    if compact:
        words_style = ParagraphStyle("PayWords", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=5.4, leading=6.2, textColor=colors.HexColor("#0C306A"))
        summary_rows = employee_compact_summary_rows(salary, result)
        if result:
            summary_rows[0][3] = Paragraph(summary_rows[0][3], words_style)
        summary = _table(summary_rows, col_widths=[34 * mm, 28 * mm, 38 * mm, 94 * mm], font_size=5.0, header=False, highlight_rows=[0] if result else [])
        detail = _table(
            employee_detail_compact_rows(result),
            col_widths=[8 * mm, 18 * mm, 21 * mm, 21 * mm, 13 * mm, 14 * mm, 4 * mm, 8 * mm, 18 * mm, 21 * mm, 21 * mm, 13 * mm, 14 * mm],
            font_size=4.25,
            blank_columns=[6],
        )
        return KeepTogether([header, Spacer(1, 2), summary, Spacer(1, 1), detail])
    return KeepTogether([
        header,
        Spacer(1, 6),
        _table(employee_salary_summary_rows(salary, result), col_widths=[55 * mm, 65 * mm], header=False),
        Spacer(1, 6),
        _table(employee_detail_rows(result), col_widths=[32 * mm, 34 * mm, 42 * mm, 28 * mm, 30 * mm, 30 * mm], font_size=8),
    ])


def build_employee_pdf(month, employee_id):
    employee, salary, result = result_for_employee(month, employee_id)
    employee_name = salary.name if salary else (employee.name if employee else employee_id)
    title = f"{employee_name} Salary Report - {display_month(month)}"
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    story = [
        employee_report_block(month, salary, result, styles, compact=True),
    ]
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()


def build_all_employees_pdf(month):
    salaries = SalaryRecord.query.filter_by(payroll_month=month).order_by(SalaryRecord.employee_id).all()
    title = f"Final Salary Report - {display_month(month)}"
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    story = []
    results = {r.employee_id: r for r in PayrollResult.query.filter_by(payroll_month=month).all()}
    for index, salary in enumerate(salaries):
        result = results.get(salary.employee_id)
        if index and index % 2 == 0:
            story.append(PageBreak())
        elif index:
            story.append(Spacer(1, 8))
        story.append(employee_report_block(month, salary, result, styles, compact=True))
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()


def loan_summary_rows(loan, employee, month, schedule):
    return [
        ["Employee", f"{loan.employee_id} - {employee.name if employee else 'Unknown'}", "Status", "Active" if loan.is_active else "Inactive"],
        ["Loan Start Date", loan.start_date, "Expected End Date", schedule[-1]["date"] if schedule else "N/A"],
        ["Loan Amount", pdf_money(loan.amount), "Monthly Deduction", pdf_money(loan.monthly_deduction)],
        ["Planned Tenure", f"{loan.tenure_months} month(s)", "Report Month", month],
        ["Paid Before Report Month", pdf_money(loan_paid_before_month(loan, month)), "Remaining Before Report Month", pdf_money(loan_remaining_before_month(loan, month))],
        ["This Month Installment", pdf_money(loan_installment_for_loan(loan, month)), "Pending Loan Amount", pdf_money(loan_pending_after_month(loan, month))],
        ["Notes", loan.notes or "No notes", "", ""],
    ]


def loan_schedule_rows(schedule):
    rows = [["Sr No", "Installment Month", "Installment Date", "Amount", "Status", "Pending After", "Notes"]]
    for index, item in enumerate(schedule, start=1):
        rows.append([
            index,
            item["month"],
            item["date"],
            pdf_money(item["installment"]),
            item["status"],
            pdf_money(item["remaining_after"]),
            item["notes"],
        ])
    return rows


def build_loan_pdf(loan_id, month):
    loan = db.session.get(Loan, loan_id)
    if not loan:
        raise ValueError("Loan was not found.")
    employee = db.session.get(Employee, loan.employee_id)
    title = f"Loan #{loan.id} Summary - {employee.name if employee else loan.employee_id}"
    schedule = loan_repayment_schedule(loan, month)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    available_width = A4[0] - doc.leftMargin - doc.rightMargin
    story = [
        _report_brand_header(f"Loan #{loan.id} Summary", f"Employee: {employee.name if employee else loan.employee_id}", styles, available_width),
        Spacer(1, 8),
        _table(loan_summary_rows(loan, employee, month, schedule), col_widths=[38 * mm, 58 * mm, 42 * mm, 58 * mm], font_size=8, header=False, highlight_rows=[5]),
        Spacer(1, 8),
        Paragraph("Installment Schedule", ParagraphStyle("LoanSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#0C306A"), spaceAfter=4)),
        _table(loan_schedule_rows(schedule), col_widths=[12 * mm, 28 * mm, 28 * mm, 28 * mm, 25 * mm, 30 * mm, 35 * mm], font_size=7),
    ]
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()
