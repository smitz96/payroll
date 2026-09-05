from datetime import date
from decimal import Decimal

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from attendance.authentication import login_required
from attendance.calculator import attendance_missing_salary
from attendance import db
from attendance.master import employee_active_for_payroll_month
from attendance.models import AttendanceRecord, Employee, PayrollMonth, PayrollResult, SalaryRecord
from attendance.reports import (
    attendance_detail_csv,
    build_all_employees_pdf,
    build_attendance_summary_pdf,
    build_attendance_detail_pdf,
    build_department_wise_pdf,
    build_employee_pdf,
    build_error_report_pdf,
    build_less_hours_report_pdf,
    build_manual_override_report_pdf,
    build_loan_pdf,
    build_overtime_report_pdf,
    build_payroll_summary_pdf,
    build_salary_register_pdf,
    build_salary_register_xlsx,
    error_report_csv,
    less_hours_report_csv,
    manual_override_report_csv,
    overtime_report_csv,
    payroll_summary_csv,
)
from attendance.utils import display_month, is_valid_payroll_month
from attendance.wage_groups import GROUP_LABELS, MONTHLY, is_group_finalized

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.url_value_preprocessor
def validate_month_value(endpoint, values):
    # Every report route is keyed on <month>; a malformed value would otherwise
    # reach the report builders and end up verbatim in a Content-Disposition header.
    if values and "month" in values and not is_valid_payroll_month(values["month"]):
        abort(404)


def money(value):
    return f"{Decimal(value or 0):,.2f}"


def scoped_salaries(month):
    salaries = SalaryRecord.query.filter_by(payroll_month=month).all()
    return [salary for salary in salaries if employee_active_for_payroll_month(db.session.get(Employee, salary.employee_id), month)]


def scoped_results(month):
    results = PayrollResult.query.filter_by(payroll_month=month).all()
    return [result for result in results if employee_active_for_payroll_month(db.session.get(Employee, result.employee_id), month)]


def group_finalized(month, group):
    return is_group_finalized(db.session.get(PayrollMonth, month), group)


def pay_document_block(month, group, document, back_to=None):
    """Guard for documents that state an employee's pay.

    Salary slips and the statutory register are what an employee and the PF/ESIC
    office are handed, so they must not leave the building off a draft month whose
    figures can still move. Returns a redirect when the group is still open, else
    None so the caller goes on to build the document.
    """
    if group_finalized(month, group):
        return None
    flash(
        f"{document} for {display_month(month)} can be downloaded once "
        f"{GROUP_LABELS[group].lower()} wage payroll is finalized.",
        "warning",
    )
    return redirect(back_to or url_for("reports.index", month=month))


def csv_response(content, filename):
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


def pdf_response(content, filename):
    return Response(content, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={filename}"})


@bp.route("/")
@login_required
def index():
    months = PayrollMonth.query.order_by(PayrollMonth.month.desc()).all()
    requested_month = request.args.get("month", "").strip()
    selected_month = requested_month if any(item.month == requested_month for item in months) else (months[0].month if months else None)
    selected = PayrollMonth.query.filter_by(month=selected_month).first() if selected_month else None
    salaries = scoped_salaries(selected_month) if selected_month else []
    results = scoped_results(selected_month) if selected_month else []
    calculated = [result for result in results if result.final_salary is not None and result.calculation_status in {"Calculated", "Needs Review"}]
    missing_salary = attendance_missing_salary(selected_month) if selected_month else {}
    report_sections = [
        {
            "title": "Standard Reports",
            "cards": [
                {"title": "Attendance Summary for Monthly", "detail": "Attendance sheet per monthly wage employee, one to a page.", "icon": "attendance", "href": "reports.monthly_attendance_summary_pdf"},
                {"title": "Summary for Daily Wage Group", "detail": "Attendance sheet per daily wage employee. Carries no company branding.", "icon": "attendance", "href": "reports.daily_attendance_summary_pdf"},
                {"title": "Payroll Summary", "detail": "Employee-wise salary summary and deductions.", "icon": "salary", "href": "reports.payroll_summary_pdf"},
            ],
        },
        {
            "title": "Other Reports",
            "cards": [
                {"title": "Salary Slips (Monthly)", "detail": "One salary slip per monthly wage employee. Daily wage employees do not receive a slip.", "icon": "pdf", "href": "reports.final_report_pdf", "requires_final": MONTHLY},
                {"title": "Department Wise Attendance", "detail": "Attendance and leave by department, with each employee's calendar. No salary figures.", "icon": "users", "href": "reports.department_wise_pdf"},
                {"title": "Salary Sheet (PF & ESIC)", "detail": "Full payroll register with PF, ESIC and professional tax. Also downloadable as XLSX.", "icon": "salary", "href": "reports.salary_register_pdf", "xlsx": "reports.salary_register_xlsx", "requires_final": MONTHLY},
                {"title": "Overtime Report", "detail": "Only employees and dates with paid overtime.", "icon": "overtime", "href": "reports.overtime_pdf"},
                {"title": "Less Hours Report", "detail": "Shortage rows with less-hours deductions.", "icon": "less-hours", "href": "reports.less_hours_pdf"},
                {"title": "Manual Override Report", "detail": "User day-status changes compared with imported attendance.", "icon": "review", "href": "reports.manual_overrides_pdf"},
                {"title": "Error Report", "detail": "Missing salary, attendance, and payroll review items.", "icon": "review", "href": "reports.errors_pdf"},
            ],
        },
    ]
    for section in report_sections:
        for card in section["cards"]:
            group = card.pop("requires_final", None)
            card["locked"] = bool(group and selected_month and not group_finalized(selected_month, group))
            card["locked_note"] = (f"Available once {GROUP_LABELS[group].lower()} wage payroll is finalized."
                                   if card["locked"] else "")
    snapshot = {
        "month": selected,
        "month_label": display_month(selected_month) if selected_month else "Not started",
        "salary_count": len(salaries),
        "attendance_count": AttendanceRecord.query.filter_by(payroll_month=selected_month).count() if selected_month else 0,
        "processed_count": len(calculated),
        "review_count": len([result for result in results if result.calculation_status != "Calculated"]) + len(missing_salary),
        "total_payable": money(sum((Decimal(result.final_salary) for result in calculated), Decimal("0"))),
        "total_deductions": money(sum((Decimal(result.total_deduction) for result in calculated), Decimal("0"))),
        "status": selected.status if selected else "DRAFT",
    }
    return render_template(
        "reports.html",
        report_sections=report_sections,
        month=selected_month,
        month_options=[{"value": item.month, "label": display_month(item.month)} for item in months],
        snapshot=snapshot,
    )


@bp.route("/<month>/payroll-summary.csv")
@login_required
def payroll_summary(month):
    return csv_response(payroll_summary_csv(month), f"smartfill-payroll-summary-{month}.csv")


@bp.route("/<month>/payroll-summary.pdf")
@login_required
def payroll_summary_pdf(month):
    return pdf_response(build_payroll_summary_pdf(month), f"smartfill-payroll-summary-{month}.pdf")


@bp.route("/<month>/salary-sheet.pdf")
@login_required
def salary_register_pdf(month):
    blocked = pay_document_block(month, MONTHLY, "The salary sheet")
    if blocked:
        return blocked
    return pdf_response(build_salary_register_pdf(month), f"smartfill-salary-sheet-{month}.pdf")


@bp.route("/<month>/salary-sheet.xlsx")
@login_required
def salary_register_xlsx(month):
    blocked = pay_document_block(month, MONTHLY, "The salary sheet")
    if blocked:
        return blocked
    return Response(
        build_salary_register_xlsx(month),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=smartfill-salary-sheet-{month}.xlsx"},
    )


@bp.route("/<month>/attendance-detail.csv")
@login_required
def attendance_detail(month):
    return csv_response(attendance_detail_csv(month), f"smartfill-attendance-detail-{month}.csv")


@bp.route("/<month>/attendance-detail.pdf")
@login_required
def attendance_detail_pdf(month):
    return pdf_response(build_attendance_detail_pdf(month), f"smartfill-attendance-detail-{month}.pdf")


@bp.route("/<month>/errors.csv")
@login_required
def errors(month):
    return csv_response(error_report_csv(month), f"smartfill-errors-{month}.csv")


@bp.route("/<month>/errors.pdf")
@login_required
def errors_pdf(month):
    return pdf_response(build_error_report_pdf(month), f"smartfill-errors-{month}.pdf")


@bp.route("/<month>/manual-overrides.csv")
@login_required
def manual_overrides(month):
    return csv_response(manual_override_report_csv(month), f"smartfill-manual-overrides-{month}.csv")


@bp.route("/<month>/manual-overrides.pdf")
@login_required
def manual_overrides_pdf(month):
    return pdf_response(build_manual_override_report_pdf(month), f"smartfill-manual-overrides-{month}.pdf")


@bp.route("/<month>/overtime.csv")
@login_required
def overtime(month):
    return csv_response(overtime_report_csv(month), f"smartfill-overtime-{month}.csv")


@bp.route("/<month>/overtime.pdf")
@login_required
def overtime_pdf(month):
    return pdf_response(build_overtime_report_pdf(month), f"smartfill-overtime-{month}.pdf")


@bp.route("/<month>/less-hours.csv")
@login_required
def less_hours(month):
    return csv_response(less_hours_report_csv(month), f"smartfill-less-hours-{month}.csv")


@bp.route("/<month>/less-hours.pdf")
@login_required
def less_hours_pdf(month):
    return pdf_response(build_less_hours_report_pdf(month), f"smartfill-less-hours-{month}.pdf")


@bp.route("/<month>/employee/<employee_id>.pdf")
@login_required
def employee_pdf(month, employee_id):
    # A daily wage employee gets their unbranded attendance summary, so the filename
    # must not carry the company name either.
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    daily = bool(salary and salary.normalized_salary_type == "DAILY")
    if not daily:
        # Only the monthly document is a salary slip. A daily wage employee's PDF is an
        # attendance summary with no pay on it, so it stays open through the draft month.
        blocked = pay_document_block(month, MONTHLY, "This salary slip",
                                     back_to=url_for("payroll.employee", month=month, employee_id=employee_id))
        if blocked:
            return blocked
    filename = (f"attendance-summary-{month}-{employee_id}.pdf" if daily
                else f"smartfill-salary-slip-{month}-{employee_id}.pdf")
    return pdf_response(build_employee_pdf(month, employee_id), filename)


@bp.route("/<month>/employee/<employee_id>/attendance-summary.pdf")
@login_required
def employee_attendance_summary_pdf(month, employee_id):
    salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee_id).first()
    if not salary or salary.normalized_salary_type not in {"MONTHLY", "DAILY"}:
        abort(404)
    wage_group = salary.normalized_salary_type
    filename = (
        f"attendance-summary-{month}-{employee_id}.pdf"
        if wage_group == "DAILY"
        else f"smartfill-attendance-summary-{month}-{employee_id}.pdf"
    )
    return pdf_response(build_attendance_summary_pdf(month, wage_group, employee_id=employee_id), filename)


@bp.route("/<month>/attendance-summary-monthly.pdf")
@login_required
def monthly_attendance_summary_pdf(month):
    return pdf_response(build_attendance_summary_pdf(month, "MONTHLY"), f"smartfill-attendance-summary-monthly-{month}.pdf")


@bp.route("/<month>/summary-daily-wage.pdf")
@login_required
def daily_attendance_summary_pdf(month):
    # Deliberately no "smartfill" in the filename: the document must not carry the
    # company name anywhere for daily wage workers.
    return pdf_response(build_attendance_summary_pdf(month, "DAILY"), f"attendance-summary-daily-{month}.pdf")


@bp.route("/<month>/department-wise.pdf")
@login_required
def department_wise_pdf(month):
    return pdf_response(build_department_wise_pdf(month), f"smartfill-department-wise-{month}.pdf")


@bp.route("/<month>/final-report.pdf")
@login_required
def final_report_pdf(month):
    blocked = pay_document_block(month, MONTHLY, "Salary slips")
    if blocked:
        return blocked
    return pdf_response(build_all_employees_pdf(month), f"smartfill-salary-slips-{month}.pdf")


@bp.route("/loans/<int:loan_id>.pdf")
@login_required
def loan_pdf(loan_id):
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    return pdf_response(build_loan_pdf(loan_id, month), f"smartfill-loan-{loan_id}-{month}.pdf")
