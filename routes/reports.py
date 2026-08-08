from datetime import date

import calendar
from decimal import Decimal

from flask import Blueprint, Response, render_template, request

from attendance.authentication import login_required
from attendance.calculator import attendance_missing_salary
from attendance import db
from attendance.master import employee_active_for_payroll_month
from attendance.models import AttendanceRecord, Employee, PayrollMonth, PayrollResult, SalaryRecord
from attendance.reports import (
    attendance_detail_csv,
    build_all_employees_pdf,
    build_attendance_detail_pdf,
    build_employee_pdf,
    build_error_report_pdf,
    build_less_hours_report_pdf,
    build_loan_pdf,
    build_overtime_report_pdf,
    build_payroll_summary_pdf,
    error_report_csv,
    less_hours_report_csv,
    overtime_report_csv,
    payroll_summary_csv,
)

bp = Blueprint("reports", __name__, url_prefix="/reports")


def display_month(month):
    if not month:
        return "Not started"
    try:
        year, month_number = (int(part) for part in month.split("-"))
    except ValueError:
        return month
    return f"{calendar.month_name[month_number]} {year}"


def money(value):
    return f"{Decimal(value or 0):,.2f}"


def scoped_salaries(month):
    salaries = SalaryRecord.query.filter_by(payroll_month=month).all()
    return [salary for salary in salaries if employee_active_for_payroll_month(db.session.get(Employee, salary.employee_id), month)]


def scoped_results(month):
    results = PayrollResult.query.filter_by(payroll_month=month).all()
    return [result for result in results if employee_active_for_payroll_month(db.session.get(Employee, result.employee_id), month)]


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
    cards = [
        {"title": "Final Salary Report", "detail": "One printable salary report for all employees.", "icon": "pdf", "href": "reports.final_report_pdf"},
        {"title": "Payroll Summary", "detail": "Employee-wise salary summary and deductions.", "icon": "salary", "href": "reports.payroll_summary_pdf"},
        {"title": "Detailed Attendance", "detail": "Daily working hours, status, shortage, and overtime.", "icon": "attendance", "href": "reports.attendance_detail_pdf"},
        {"title": "Overtime Report", "detail": "Only employees and dates with paid overtime.", "icon": "overtime", "href": "reports.overtime_pdf"},
        {"title": "Less Hours Report", "detail": "Shortage rows with less-hours deductions.", "icon": "less-hours", "href": "reports.less_hours_pdf"},
        {"title": "Error Report", "detail": "Missing salary, attendance, and payroll review items.", "icon": "review", "href": "reports.errors_pdf"},
    ]
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
        cards=cards,
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
    return pdf_response(build_employee_pdf(month, employee_id), f"smartfill-{month}-employee-{employee_id}.pdf")


@bp.route("/<month>/final-report.pdf")
@login_required
def final_report_pdf(month):
    return pdf_response(build_all_employees_pdf(month), f"smartfill-final-report-{month}.pdf")


@bp.route("/loans/<int:loan_id>.pdf")
@login_required
def loan_pdf(loan_id):
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    return pdf_response(build_loan_pdf(loan_id, month), f"smartfill-loan-{loan_id}-{month}.pdf")
