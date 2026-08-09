import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from attendance import db
from attendance.models import AttendanceOverride, AttendanceRecord, Employee, Holiday, PayrollMonth, PayrollResult, SalaryRecord
from attendance.advances import advance_deduction_for_employee
from attendance.loans import loan_installment_for_employee, loan_pending_after_month_for_employee
from attendance.settings import MONTHLY_RULES as CFG
from attendance.utils import floor_to_interval, minutes_to_duration, money, truncate_one_decimal
from attendance.weekoffs import is_week_off_for_date


class UnsupportedPayrollResult:
    def __init__(self, salary_record):
        self.salary_record = salary_record

    def to_model(self):
        raw_type = self.salary_record.salary_type or "Missing Wage Type"
        message = (
            f'Payroll rules for wage type "{raw_type}" are not configured yet. '
            "Employee has not been included in automatic salary calculation."
        )
        status = "Needs Review" if not self.salary_record.normalized_salary_type else "Payroll Rules Not Configured"
        return PayrollResult(
            payroll_month=self.salary_record.payroll_month,
            employee_id=self.salary_record.employee_id,
            payroll_rule_type=self.salary_record.normalized_salary_type or None,
            calculation_status=status,
            message=message,
            final_salary=None,
            manual_adjustment=self.salary_record.adjustment,
            leave_encashment_days=getattr(self.salary_record, "leave_encashment_days", Decimal("0")) if getattr(self.salary_record, "leave_encashment_enabled", False) else Decimal("0"),
            leave_encashment_amount=getattr(self.salary_record, "leave_encashment_amount", Decimal("0")) if getattr(self.salary_record, "leave_encashment_enabled", False) else Decimal("0"),
            loan_deduction=getattr(self.salary_record, "loan", Decimal("0")),
        )


class PayrollRule:
    salary_type = None

    def calculate_employee_month(self, salary_record, attendance_records, opening_leave, holidays, overrides):
        raise NotImplementedError


class MonthlyPayrollRule(PayrollRule):
    salary_type = "MONTHLY"

    def calculate_employee_month(self, salary_record, attendance_records, opening_leave=Decimal("0"), holidays=None, overrides=None):
        holidays = holidays or set()
        overrides = overrides or {}
        salary = Decimal(salary_record.salary)
        hourly_rate = salary / Decimal(CFG["SALARY_CALCULATION_DAYS"] * CFG["SALARY_HOURS_PER_DAY"])
        quarter_rate = hourly_rate / Decimal(4)
        daily_rate = salary / Decimal(CFG["SALARY_CALCULATION_DAYS"])
        full_days = Decimal("0")
        half_days = Decimal("0")
        week_offs = 0
        holiday_count = 0
        leave_used = Decimal("0")
        comp_off_earned = Decimal("0")
        lop_days = Decimal("0")
        actual_total = 0
        less_minutes = 0
        less_deduction = Decimal("0")
        ot_minutes = 0
        payable_ot = 0
        ot_amount = Decimal("0")
        details = []
        needs_review = []
        employee = db.session.get(Employee, salary_record.employee_id)
        ot_ignored = bool(employee and employee.ot_ignored)
        less_hours_ignored = bool(employee and employee.less_hours_ignored)

        classified_rows = []
        for rec in sorted(attendance_records, key=lambda item: item.date):
            override = overrides.get(rec.date)
            row = classify_monthly_attendance(rec, holidays, override, rec.employee_id)
            classified_rows.append((rec, override, row))
        apply_sandwich_leave_policy(classified_rows)

        for rec, override, row in classified_rows:
            actual = rec.actual_minutes or 0
            actual_total += actual
            if row["status"] == "Week Off":
                week_offs += 1
            elif row["status"] == "Week Off Worked":
                week_offs += 1
                comp_off_earned += Decimal("1")
            elif row["status"] == "Holiday":
                holiday_count += 1
            elif row["status"] in {"Paid Leave", "Half-Day Paid Leave", "Sandwich Leave"}:
                leave_used += Decimal(str(row["leave_used"]))
            elif row["status"] == "Full Day Present":
                full_days += Decimal("1")
            elif row["status"] == "Half Day Present":
                half_days += Decimal("1")
            elif row["status"] == "Full Day LOP":
                lop_days += Decimal("1")
            elif row["status"] == "Half Day LOP":
                lop_days += Decimal("0.5")
            elif row["status"] in {"Punch Error", "Needs Review"}:
                needs_review.append(f"{rec.date}: {row['status']}")

            shortage = 0 if less_hours_ignored else calculate_monthly_shortage(actual)
            shortage_amount = Decimal("0")
            rounded = row["rounded_minutes"]
            if shortage and row["status"] == "Full Day Present":
                shortage_amount = quarter_rate * Decimal(shortage // CFG["ROUNDING_INTERVAL_MINUTES"])
                less_minutes += shortage
                less_deduction += shortage_amount
            raw_ot, rounded_ot, ot_value = calculate_monthly_overtime(actual, quarter_rate)
            ot_minutes += raw_ot
            if ot_ignored:
                rounded_ot = 0
                ot_value = Decimal("0")
            payable_ot += rounded_ot
            ot_amount += ot_value
            details.append({
                "date": rec.date.isoformat(),
                "day": rec.day,
                "first_punch": rec.first_punch,
                "last_punch": rec.last_punch,
                "raw_working_hours": rec.raw_working_hours,
                "actual_minutes": actual,
                "actual_duration": minutes_to_duration(actual),
                "rounded_minutes": rounded,
                "rounded_duration": minutes_to_duration(rounded),
                "attendance_status": row["status"],
                "paid_day_value": str(row["paid_day"]),
                "shortage_minutes": shortage,
                "shortage_deduction": str(money(shortage_amount)),
                "raw_ot": raw_ot,
                "payable_ot": rounded_ot,
                "ot_amount": str(money(ot_value)),
                "leave_used": str(row["leave_used"]),
                "comp_off_earned": str(Decimal("1") if row["status"] == "Week Off Worked" else Decimal("0")),
                "holiday": rec.date in holidays,
                "week_off": is_week_off_for_date(rec.employee_id, rec.date),
                "sandwich_leave": bool(row.get("sandwich_leave")),
                "override": override.manual_status if override else "",
                "explanation": row["explanation"],
            })

        paid_working_days = full_days + (half_days * Decimal("0.5"))
        eligible_leave_days = paid_working_days + Decimal(week_offs) + Decimal(holiday_count) + leave_used
        leave_earned = calculate_monthly_leave_earned(eligible_leave_days, days_in_payroll_month(salary_record.payroll_month)) + comp_off_earned
        available_leave = Decimal(opening_leave) + leave_earned
        paid_leave = min(leave_used, available_leave)
        excess_leave = max(Decimal("0"), leave_used - available_leave)
        lop_days += excess_leave
        lop_deduction = (daily_rate * lop_days).quantize(Decimal("0.01"))
        closing_leave_before_encashment = (available_leave - paid_leave).quantize(Decimal("0.1"))
        manual = Decimal(salary_record.adjustment)
        loan = Decimal(getattr(salary_record, "loan", Decimal("0")) or 0) + loan_installment_for_employee(salary_record.employee_id, salary_record.payroll_month)
        loan_pending = loan_pending_after_month_for_employee(salary_record.employee_id, salary_record.payroll_month)
        advance = advance_deduction_for_employee(salary_record.employee_id, salary_record.payroll_month)
        payroll_month = PayrollMonth.query.filter_by(month=salary_record.payroll_month).first()
        global_leave_encashment = bool(payroll_month and payroll_month.encash_all_leaves)
        if global_leave_encashment and not getattr(salary_record, "leave_encashment_disabled", False):
            leave_encashment_days = closing_leave_before_encashment
        else:
            leave_encashment_days = Decimal(getattr(salary_record, "leave_encashment_days", Decimal("0")) or 0) if getattr(salary_record, "leave_encashment_enabled", False) else Decimal("0")
        if leave_encashment_days < 0:
            raise ValueError(f"Employee ID {salary_record.employee_id}: Leave encashment days cannot be negative.")
        if leave_encashment_days > closing_leave_before_encashment:
            if closing_leave_before_encashment <= 0:
                raise ValueError(f"Employee ID {salary_record.employee_id}: No leaves available for encashment.")
            raise ValueError(f"Employee ID {salary_record.employee_id}: Only {closing_leave_before_encashment} leave(s) available for encashment.")
        leave_encashment = daily_rate * leave_encashment_days
        closing_leave = (closing_leave_before_encashment - leave_encashment_days).quantize(Decimal("0.1"))
        manual_deduction = abs(manual) if manual < 0 else Decimal("0")
        total_deduction = lop_deduction + less_deduction + loan + advance + manual_deduction
        total_addition = ot_amount + leave_encashment + (manual if manual > 0 else Decimal("0"))
        final_salary = salary - total_deduction + total_addition
        status = "Needs Review" if needs_review else "Calculated"
        return PayrollResult(
            payroll_month=salary_record.payroll_month,
            employee_id=salary_record.employee_id,
            payroll_rule_type="MONTHLY",
            calculation_status=status,
            message="; ".join(needs_review),
            full_days=full_days,
            half_days=half_days,
            paid_working_days=paid_working_days,
            week_offs=week_offs,
            holidays=holiday_count,
            paid_leaves=paid_leave,
            lop_days=lop_days,
            opening_leave=opening_leave,
            leave_earned=leave_earned,
            leave_used=leave_used,
            closing_leave=closing_leave,
            actual_working_minutes=actual_total,
            less_hours_minutes=less_minutes,
            less_hours_deduction=money(less_deduction),
            ot_minutes=ot_minutes,
            payable_ot_minutes=payable_ot,
            ot_amount=money(ot_amount),
            lop_deduction=money(lop_deduction),
            manual_adjustment=manual,
            leave_encashment_days=leave_encashment_days,
            leave_encashment_amount=money(leave_encashment),
            loan_deduction=money(loan),
            loan_pending_amount=money(loan_pending),
            advance_deduction=money(advance),
            total_deduction=money(total_deduction),
            total_addition=money(total_addition),
            final_salary=money(final_salary),
            detail_json=details,
        )


class DailyPayrollRule(PayrollRule):
    salary_type = "DAILY"

    def calculate_employee_month(self, salary_record, attendance_records, opening_leave=Decimal("0"), holidays=None, overrides=None):
        holidays = holidays or set()
        overrides = overrides or {}
        daily_rate = Decimal(salary_record.salary)
        hourly_rate = daily_rate / Decimal(CFG["SALARY_HOURS_PER_DAY"])
        quarter_rate = hourly_rate / Decimal(4)
        full_days = Decimal("0")
        half_days = Decimal("0")
        week_offs = 0
        holiday_count = 0
        actual_total = 0
        less_minutes = 0
        less_deduction = Decimal("0")
        ot_minutes = 0
        payable_ot = 0
        ot_amount = Decimal("0")
        details = []
        needs_review = []
        employee = db.session.get(Employee, salary_record.employee_id)
        ot_ignored = bool(employee and employee.ot_ignored)
        less_hours_ignored = bool(employee and employee.less_hours_ignored)

        for rec in sorted(attendance_records, key=lambda item: item.date):
            override = overrides.get(rec.date)
            row = classify_daily_attendance(rec, holidays, override, rec.employee_id)
            actual = rec.actual_minutes or 0
            actual_total += actual
            if row["status"] == "Week Off":
                week_offs += 1
            elif row["status"] == "Week Off Worked":
                week_offs += 1
                full_days += Decimal("1")
            elif row["status"] == "Holiday":
                holiday_count += 1
            elif row["status"] == "Full Day Present":
                full_days += Decimal("1")
            elif row["status"] == "Half Day Present":
                half_days += Decimal("1")
            elif row["status"] in {"Punch Error", "Needs Review"}:
                needs_review.append(f"{rec.date}: {row['status']}")

            shortage = 0 if less_hours_ignored else calculate_monthly_shortage(actual)
            shortage_amount = Decimal("0")
            rounded = row["rounded_minutes"]
            if shortage and row["status"] in {"Full Day Present", "Week Off Worked"}:
                shortage_amount = quarter_rate * Decimal(shortage // CFG["ROUNDING_INTERVAL_MINUTES"])
                less_minutes += shortage
                less_deduction += shortage_amount
            raw_ot, rounded_ot, ot_value = calculate_monthly_overtime(actual, quarter_rate)
            ot_minutes += raw_ot
            if ot_ignored:
                rounded_ot = 0
                ot_value = Decimal("0")
            payable_ot += rounded_ot
            ot_amount += ot_value
            details.append({
                "date": rec.date.isoformat(),
                "day": rec.day,
                "first_punch": rec.first_punch,
                "last_punch": rec.last_punch,
                "raw_working_hours": rec.raw_working_hours,
                "actual_minutes": actual,
                "actual_duration": minutes_to_duration(actual),
                "rounded_minutes": rounded,
                "rounded_duration": minutes_to_duration(rounded),
                "attendance_status": row["status"],
                "paid_day_value": str(row["paid_day"]),
                "shortage_minutes": shortage,
                "shortage_deduction": str(money(shortage_amount)),
                "raw_ot": raw_ot,
                "payable_ot": rounded_ot,
                "ot_amount": str(money(ot_value)),
                "leave_used": "0",
                "comp_off_earned": "0",
                "holiday": rec.date in holidays,
                "week_off": is_week_off_for_date(rec.employee_id, rec.date),
                "sandwich_leave": False,
                "override": override.manual_status if override else "",
                "explanation": row["explanation"],
            })

        paid_working_days = full_days + (half_days * Decimal("0.5"))
        payable_days = paid_working_days + Decimal(holiday_count)
        gross_salary = daily_rate * payable_days
        manual = Decimal(salary_record.adjustment)
        loan = Decimal(getattr(salary_record, "loan", Decimal("0")) or 0) + loan_installment_for_employee(salary_record.employee_id, salary_record.payroll_month)
        loan_pending = loan_pending_after_month_for_employee(salary_record.employee_id, salary_record.payroll_month)
        advance = advance_deduction_for_employee(salary_record.employee_id, salary_record.payroll_month)
        manual_deduction = abs(manual) if manual < 0 else Decimal("0")
        total_deduction = less_deduction + loan + advance + manual_deduction
        total_addition = ot_amount + (manual if manual > 0 else Decimal("0"))
        final_salary = gross_salary - total_deduction + total_addition
        status = "Needs Review" if needs_review else "Calculated"
        return PayrollResult(
            payroll_month=salary_record.payroll_month,
            employee_id=salary_record.employee_id,
            payroll_rule_type="DAILY",
            calculation_status=status,
            message="; ".join(needs_review),
            full_days=full_days,
            half_days=half_days,
            paid_working_days=paid_working_days,
            week_offs=week_offs,
            holidays=holiday_count,
            paid_leaves=Decimal("0"),
            lop_days=Decimal("0"),
            opening_leave=Decimal("0"),
            leave_earned=Decimal("0"),
            leave_used=Decimal("0"),
            closing_leave=Decimal("0"),
            actual_working_minutes=actual_total,
            less_hours_minutes=less_minutes,
            less_hours_deduction=money(less_deduction),
            ot_minutes=ot_minutes,
            payable_ot_minutes=payable_ot,
            ot_amount=money(ot_amount),
            lop_deduction=Decimal("0"),
            manual_adjustment=manual,
            leave_encashment_days=Decimal("0"),
            leave_encashment_amount=Decimal("0"),
            loan_deduction=money(loan),
            loan_pending_amount=money(loan_pending),
            advance_deduction=money(advance),
            total_deduction=money(total_deduction),
            total_addition=money(total_addition),
            final_salary=money(final_salary),
            detail_json=details,
        )


def classify_monthly_attendance(record, holidays=None, override=None, employee_id=None):
    holidays = holidays or set()
    if override:
        status = override.manual_status
        mapping = {
            "Full Day Present": (Decimal("1"), 0),
            "Half Day Present": (Decimal("0.5"), 0),
            "Paid Leave": (Decimal("0"), Decimal("1")),
            "Half-Day Paid Leave": (Decimal("0"), Decimal("0.5")),
            "Unpaid Leave / LOP": (Decimal("0"), 0),
            "Half-Day LOP": (Decimal("0"), 0),
            "Holiday": (Decimal("0"), 0),
            "Week Off": (Decimal("0"), 0),
            "Week Off Worked": (Decimal("0"), 0),
        }
        paid_day, leave_used = mapping.get(status, (Decimal("0"), 0))
        return {"status": status, "paid_day": paid_day, "leave_used": Decimal(str(leave_used)), "rounded_minutes": record.actual_minutes or 0, "explanation": "Manual override applied."}
    if record.date in holidays:
        return {"status": "Holiday", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "Holiday calendar date."}
    if is_week_off_for_date(employee_id or record.employee_id, record.date):
        actual = record.actual_minutes or 0
        if record.parse_status == "OK" and actual >= CFG["HALF_DAY_MINIMUM_MINUTES"]:
            return {"status": "Week Off Worked", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Worked on configured week off. One compensatory leave earned."}
        return {"status": "Week Off", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "Configured week off."}
    if record.parse_status != "OK":
        return {"status": "Needs Review", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": record.warning or "Attendance needs review."}
    actual = record.actual_minutes
    if actual is None:
        return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "No working duration was imported."}
    if actual >= CFG["FULL_DAY_REQUIRED_MINUTES"]:
        return {"status": "Full Day Present", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Actual duration meets 8h50m grace threshold."}
    if actual >= CFG["LESS_HOURS_RULE_MINIMUM_MINUTES"]:
        rounded = floor_to_interval(actual, CFG["ROUNDING_INTERVAL_MINUTES"])
        return {"status": "Full Day Present", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": rounded, "explanation": "Short-hours rule applies with 15-minute floor."}
    if actual >= CFG["HALF_DAY_MINIMUM_MINUTES"]:
        return {"status": "Half Day Present", "paid_day": Decimal("0.5"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Under 6 hours, valid half-day duration."}
    return {"status": "Full Day LOP", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Less than 3 hours is full-day LOP."}


def classify_daily_attendance(record, holidays=None, override=None, employee_id=None):
    holidays = holidays or set()
    if override:
        status = override.manual_status
        mapping = {
            "Full Day Present": (Decimal("1"), "Manual override applied."),
            "Half Day Present": (Decimal("0.5"), "Manual override applied."),
            "Holiday": (Decimal("1"), "Manual holiday override applied."),
            "Week Off Worked": (Decimal("1"), "Manual week off worked override applied."),
            "Week Off": (Decimal("0"), "Manual week off override applied."),
            "Paid Leave": (Decimal("0"), "Daily wage employees do not use leave balance."),
            "Half-Day Paid Leave": (Decimal("0"), "Daily wage employees do not use leave balance."),
            "Unpaid Leave / LOP": (Decimal("0"), "Manual unpaid day override applied."),
            "Half-Day LOP": (Decimal("0"), "Manual unpaid half-day override applied."),
        }
        paid_day, explanation = mapping.get(status, (Decimal("0"), "Manual override applied."))
        return {"status": status, "paid_day": paid_day, "leave_used": Decimal("0"), "rounded_minutes": record.actual_minutes or 0, "explanation": explanation}
    if record.date in holidays:
        return {"status": "Holiday", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "Paid holiday for daily wage employee."}
    if is_week_off_for_date(employee_id or record.employee_id, record.date):
        actual = record.actual_minutes or 0
        if record.parse_status == "OK" and actual >= CFG["HALF_DAY_MINIMUM_MINUTES"]:
            return {"status": "Week Off Worked", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Worked on configured week off; counted as a working day for daily wage."}
        return {"status": "Week Off", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "Configured week off is not payable for daily wage."}
    if record.parse_status != "OK":
        return {"status": "Needs Review", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": record.warning or "Attendance needs review."}
    actual = record.actual_minutes
    if actual is None:
        return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "No working duration was imported."}
    if actual >= CFG["FULL_DAY_REQUIRED_MINUTES"]:
        return {"status": "Full Day Present", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Daily full-day working duration."}
    if actual >= CFG["LESS_HOURS_RULE_MINIMUM_MINUTES"]:
        rounded = floor_to_interval(actual, CFG["ROUNDING_INTERVAL_MINUTES"])
        return {"status": "Full Day Present", "paid_day": Decimal("1"), "leave_used": Decimal("0"), "rounded_minutes": rounded, "explanation": "Daily wage short-hours rule applies with 15-minute floor."}
    if actual >= CFG["HALF_DAY_MINIMUM_MINUTES"]:
        return {"status": "Half Day Present", "paid_day": Decimal("0.5"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Daily half-day working duration."}
    return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Less than 3 hours is not payable for daily wage."}


def apply_sandwich_leave_policy(classified_rows):
    leave_like_statuses = {
        "Paid Leave",
        "Half-Day Paid Leave",
        "Unpaid Leave / LOP",
        "Full Day LOP",
        "Half Day LOP",
        "Absent / Attendance Missing",
    }
    index = 0
    while index < len(classified_rows):
        _record, _override, row = classified_rows[index]
        if row["status"] != "Week Off":
            index += 1
            continue
        block_start = index
        while index < len(classified_rows) and classified_rows[index][2]["status"] == "Week Off":
            index += 1
        block_end = index - 1
        before = classified_rows[block_start - 1][2] if block_start > 0 else None
        after = classified_rows[index][2] if index < len(classified_rows) else None
        if before and after and before["status"] in leave_like_statuses and after["status"] in leave_like_statuses:
            for block_index in range(block_start, block_end + 1):
                sandwich_row = classified_rows[block_index][2]
                sandwich_row.update({
                    "status": "Sandwich Leave",
                    "paid_day": Decimal("0"),
                    "leave_used": Decimal("1"),
                    "rounded_minutes": 0,
                    "sandwich_leave": True,
                    "explanation": "Sandwich leave policy applied: week off between leave/LOP days is counted as leave.",
                })


def calculate_monthly_shortage(actual_minutes):
    if actual_minutes is None or actual_minutes < CFG["LESS_HOURS_RULE_MINIMUM_MINUTES"] or actual_minutes >= CFG["FULL_DAY_REQUIRED_MINUTES"]:
        return 0
    rounded = floor_to_interval(actual_minutes, CFG["ROUNDING_INTERVAL_MINUTES"])
    return max(0, CFG["FULL_DAY_MINUTES"] - rounded)


def calculate_monthly_overtime(actual_minutes, quarter_rate=Decimal("0")):
    if actual_minutes is None or actual_minutes <= CFG["OVERTIME_START_MINUTES"]:
        return 0, 0, Decimal("0")
    raw = actual_minutes - CFG["OVERTIME_START_MINUTES"]
    rounded = floor_to_interval(raw, CFG["ROUNDING_INTERVAL_MINUTES"])
    amount = quarter_rate * Decimal(rounded // CFG["ROUNDING_INTERVAL_MINUTES"])
    return raw, rounded, amount


def days_in_payroll_month(payroll_month):
    year, month = [int(part) for part in payroll_month.split("-", 1)]
    return calendar.monthrange(year, month)[1]


def calculate_monthly_leave_earned(eligible_leave_days, days_in_month):
    if not days_in_month:
        return Decimal("0.0")
    earn_rate = Decimal(CFG["LEAVE_EARNED_PER_MONTH"])
    return truncate_one_decimal((Decimal(eligible_leave_days) / Decimal(days_in_month)) * earn_rate)


PAYROLL_RULES = {"MONTHLY": MonthlyPayrollRule(), "DAILY": DailyPayrollRule()}


def resolve_payroll_rule(normalized_salary_type):
    return PAYROLL_RULES.get(normalized_salary_type)
