import csv
import calendar
from io import BytesIO, StringIO
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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


def payroll_month_days(month):
    year, month_number = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, month_number)[1]


def total_paid_days(result):
    if not result:
        return ""
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
        "Employee ID", "Name", "Department", "Wage Type", "Payroll Rule Status", "Salary",
        "Month Days", "Paid Working Days", "Week Offs", "Total Paid Days", "Full Days", "Half Days", "Paid Leave", "LOP", "Opening Leave",
        "Leave Earned", "Leave Used", "Closing Leave", "Working Hour Deduction",
        "LOP Deduction", "Overtime", "Adjustment", "Leave Encashment Days", "Leave Encashment Amount", "Loan Deduction", "Advance Salary Deduction", "Final Salary", "Calculation Status",
    ])
    salaries = {s.employee_id: s for s in SalaryRecord.query.filter_by(payroll_month=month).all()}
    results = {r.employee_id: r for r in PayrollResult.query.filter_by(payroll_month=month).all()}
    for employee_id, salary in salaries.items():
        result = results.get(employee_id)
        writer.writerow([
            employee_id,
            salary.name,
            "",
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


def report_pdf(title, subtitle, headers, rows, col_widths=None, font_size=7, landscape_page=True):
    buffer = BytesIO()
    pagesize = landscape(A4) if landscape_page else A4
    doc = SimpleDocTemplate(buffer, pagesize=pagesize, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=13, leading=15, textColor=colors.HexColor("#0C306A"), spaceAfter=3)
    subtitle_style = ParagraphStyle("ReportSubtitle", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#657386"), spaceAfter=6)
    cell_style = ParagraphStyle("ReportCell", parent=styles["Normal"], fontSize=font_size, leading=font_size + 1.5)
    header_style = ParagraphStyle("ReportHeaderCell", parent=cell_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#0C306A"))
    if not rows:
        rows = [["No records found"] + [""] * (len(headers) - 1)]
    table_rows = [[Paragraph(str(value), header_style) for value in headers]]
    for row in rows:
        table_rows.append([Paragraph(str(value or ""), cell_style) for value in row])
    if not col_widths:
        available_width = pagesize[0] - doc.leftMargin - doc.rightMargin
        col_widths = [available_width / len(headers)] * len(headers)
    story = [
        Paragraph(title, title_style),
        Paragraph(subtitle, subtitle_style),
        _table(table_rows, col_widths=col_widths, font_size=font_size),
    ]
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()


def build_payroll_summary_pdf(month):
    names = employee_name_map(month)
    rows = []
    for result in calculated_results_for_month(month):
        salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=result.employee_id).first()
        rows.append([
            result.employee_id,
            names.get(result.employee_id, result.employee_id),
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
        f"Payroll Month: {display_month(month)}",
        ["ID", "Employee", "Wage Type", "Base", "Days", "Working", "Week Off", "Total Paid", "Leave", "LOP", "Deduction", "Addition", "Payable", "Status"],
        rows,
        col_widths=[13 * mm, 34 * mm, 19 * mm, 20 * mm, 12 * mm, 16 * mm, 17 * mm, 18 * mm, 14 * mm, 12 * mm, 22 * mm, 19 * mm, 22 * mm, 24 * mm],
        font_size=6.2,
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
    return report_pdf(
        "Detailed Attendance Report",
        f"Payroll Month: {display_month(month)}",
        ["ID", "Employee", "Date", "Day", "In", "Out", "Working Hours", "Status", "Warning"],
        rows,
        col_widths=[13 * mm, 42 * mm, 22 * mm, 22 * mm, 20 * mm, 20 * mm, 25 * mm, 24 * mm, 86 * mm],
        font_size=6.2,
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
    return report_pdf(
        "Overtime Report",
        f"Payroll Month: {display_month(month)} | Rows where payable overtime is greater than zero",
        ["ID", "Employee", "Date", "In Time", "Out Time", "Working Hours", "OT Paid Minutes", "OT Amount"],
        rows,
        col_widths=[16 * mm, 58 * mm, 26 * mm, 26 * mm, 26 * mm, 32 * mm, 36 * mm, 32 * mm],
        font_size=7,
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
    return report_pdf(
        "Less Hours Report",
        f"Payroll Month: {display_month(month)} | Rows where less-hours deduction is greater than zero",
        ["ID", "Employee", "Date", "In Time", "Out Time", "Working Hours", "Less Minutes", "Deduction"],
        rows,
        col_widths=[16 * mm, 58 * mm, 26 * mm, 26 * mm, 26 * mm, 32 * mm, 32 * mm, 36 * mm],
        font_size=7,
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
    return report_pdf(
        "Error Report",
        f"Payroll Month: {display_month(month)}",
        ["Area", "Employee ID", "Employee", "Issue"],
        rows,
        col_widths=[28 * mm, 26 * mm, 56 * mm, 160 * mm],
        font_size=7,
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


def _pdf_header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#657386"))
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 6 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _titled_page_callback(title):
    def callback(canvas, doc):
        canvas.setTitle(title)
        canvas.setAuthor("SMARTfill Attendance & Payroll Management")
        _pdf_header_footer(canvas, doc)

    return callback


def _table(data, col_widths=None, font_size=8, header=True, highlight_rows=None, blank_columns=None):
    table = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    pad = 1 if font_size <= 5.8 else 4
    highlight_rows = highlight_rows or []
    blank_columns = blank_columns or []
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF1F8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0C306A")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D9E2EC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]
    if not header:
        style[0] = ("FONTNAME", (0, 0), (-1, -1), "Helvetica")
        style[2] = ("BACKGROUND", (0, 0), (-1, -1), colors.white)
    for row in highlight_rows:
        style.extend([
            ("BACKGROUND", (0, row), (-1, row), colors.HexColor("#F5F9E8")),
            ("TEXTCOLOR", (0, row), (-1, row), colors.HexColor("#0C306A")),
            ("FONTNAME", (0, row), (-1, row), "Helvetica-Bold"),
        ])
    for column in blank_columns:
        style.extend([
            ("BACKGROUND", (column, 0), (column, -1), colors.white),
            ("TEXTCOLOR", (column, 0), (column, -1), colors.white),
            ("LINEBEFORE", (column, 0), (column, -1), 0.35, colors.white),
            ("LINEAFTER", (column, 0), (column, -1), 0.35, colors.white),
            ("LINEABOVE", (column, 0), (column, -1), 0.35, colors.white),
            ("LINEBELOW", (column, 0), (column, -1), 0.35, colors.white),
        ])
    table.setStyle(TableStyle(style))
    return table


def employee_report_block(month, salary, result, styles, compact=False):
    employee_id = salary.employee_id if salary else (result.employee_id if result else "")
    employee_name = salary.name if salary else employee_id
    title_style = ParagraphStyle("BlockTitle", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=8 if compact else 11, textColor=colors.HexColor("#0C306A"), spaceBefore=2, spaceAfter=3)
    label = Paragraph(f"{employee_id} - {employee_name} | Payroll Month: {month}", title_style)
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
        return KeepTogether([label, summary, Spacer(1, 1), detail])
    return KeepTogether([
        label,
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
    title_style = ParagraphStyle("LoanTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=colors.HexColor("#0C306A"), spaceAfter=8)
    story = [
        Paragraph(f"Loan #{loan.id} Summary", title_style),
        _table(loan_summary_rows(loan, employee, month, schedule), col_widths=[38 * mm, 58 * mm, 42 * mm, 58 * mm], font_size=8, header=False, highlight_rows=[5]),
        Spacer(1, 8),
        Paragraph("Installment Schedule", ParagraphStyle("LoanSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, textColor=colors.HexColor("#0C306A"), spaceAfter=4)),
        _table(loan_schedule_rows(schedule), col_widths=[12 * mm, 28 * mm, 28 * mm, 28 * mm, 25 * mm, 30 * mm, 35 * mm], font_size=7),
    ]
    page_callback = _titled_page_callback(title)
    doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
    buffer.seek(0)
    return buffer.getvalue()
