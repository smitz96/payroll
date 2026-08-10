import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from attendance import db
from attendance.models import AttendanceOverride, AttendanceRecord, Employee, Holiday, PayrollMonth, PayrollResult, SalaryRecord
from attendance.advances import advance_deduction_for_employee
from attendance.loans import loan_installment_for_employee, loan_pending_after_month_for_employee
from attendance.settings import DAILY_BONUS_RULES as BONUS_CFG
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

        # Leave is settled in two passes, because the leave earned this month depends
        # on how many days end up paid, which in turn depends on the opening balance.
        #   1. Explicit leave (overrides, sandwich days) draws on the opening balance.
        #   2. Whatever opening balance is left covers unexplained absences.
        #   3. Earned leave is computed from the resulting paid-day count.
        #   4. That earned leave covers the remaining absences.
        # Anything still absent afterwards is loss of pay.
        explicit_leave = sum(
            (Decimal(str(row["leave_used"])) for _rec, _ov, row in classified_rows
             if row["status"] in EXPLICIT_LEAVE_STATUSES),
            Decimal("0"),
        )
        opening_balance = Decimal(opening_leave or 0)
        opening_for_absences = max(Decimal("0"), opening_balance - explicit_leave)
        opening_applied = apply_leave_balance(classified_rows, opening_for_absences)

        interim_full = sum((Decimal("1") for _r, _o, row in classified_rows if row["status"] in FULL_DAY_PRESENT_STATUSES), Decimal("0"))
        interim_half = sum((Decimal("1") for _r, _o, row in classified_rows if row["status"] == "Half Day Present"), Decimal("0"))
        interim_weekoffs = sum((1 for _r, _o, row in classified_rows if row["status"] in {"Week Off", "Week Off Worked"}), 0)
        interim_holidays = sum((1 for _r, _o, row in classified_rows if row["status"] == "Holiday"), 0)
        interim_leave_used = explicit_leave + opening_applied
        interim_comp_off = sum((Decimal("1") for _r, _o, row in classified_rows if row["status"] == "Week Off Worked"), Decimal("0"))

        eligible_leave_days = (
            interim_full + (interim_half * Decimal("0.5"))
            + Decimal(interim_weekoffs) + Decimal(interim_holidays) + interim_leave_used
        )
        leave_earned = calculate_monthly_leave_earned(
            eligible_leave_days, days_in_payroll_month(salary_record.payroll_month)
        ) + interim_comp_off
        earned_applied = apply_leave_balance(classified_rows, leave_earned)

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
            elif row["status"] == HALF_LEAVE_HALF_LOP_STATUS:
                # Half the day is paid from leave, the other half is unpaid.
                leave_used += Decimal(str(row["leave_used"]))
                lop_days += Decimal("0.5")
            elif row["status"] in FULL_DAY_PRESENT_STATUSES:
                full_days += Decimal("1")
            elif row["status"] == "Half Day Present":
                half_days += Decimal("1")
            elif row["status"] == "Full Day LOP":
                lop_days += Decimal("1")
            elif row["status"] == "Half Day LOP":
                lop_days += Decimal("0.5")
            elif row["status"] == ABSENT_STATUS:
                # No leave left to cover it, so the whole day is loss of pay.
                lop_days += Decimal("1")
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
                "punches": list(rec.punches_json or []),
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
        # leave_earned was fixed before the earned balance was applied, so that
        # allocating it cannot feed back into how much is earned.
        available_leave = opening_balance + leave_earned
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
        absence_minutes = 0
        details = []
        needs_review = []
        employee = db.session.get(Employee, salary_record.employee_id)
        ot_ignored = bool(employee and employee.ot_ignored)
        less_hours_ignored = bool(employee and employee.less_hours_ignored)
        bonus_ignored = bool(employee and employee.bonus_ignored)

        for rec in sorted(attendance_records, key=lambda item: item.date):
            override = overrides.get(rec.date)
            row = classify_daily_attendance(rec, holidays, override, rec.employee_id)
            actual = rec.actual_minutes or 0
            actual_total += actual
            day_absence = daily_absence_minutes(row, rec)
            absence_minutes += day_absence
            if row["status"] == "Week Off":
                week_offs += 1
            elif row["status"] == "Week Off Worked":
                week_offs += 1
                full_days += Decimal("1")
            elif row["status"] == "Holiday":
                holiday_count += 1
            elif row["status"] in FULL_DAY_PRESENT_STATUSES:
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
                "punches": list(rec.punches_json or []),
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
                "bonus_absence_minutes": day_absence,
                "holiday": rec.date in holidays,
                "week_off": is_week_off_for_date(rec.employee_id, rec.date),
                "sandwich_leave": False,
                "override": override.manual_status if override else "",
                "explanation": row["explanation"],
            })

        paid_working_days = full_days + (half_days * Decimal("0.5"))
        payable_days = paid_working_days + Decimal(holiday_count)
        gross_salary = daily_rate * payable_days
        # Attendance bonus is a percentage of the wage actually earned for the month,
        # before any deduction, so a short month scales the bonus with it. Absence is
        # still tallied for an excluded employee, so the page and slip can show what
        # the month would have earned.
        bonus_percent = Decimal("0") if bonus_ignored else daily_attendance_bonus_percent(absence_minutes)
        attendance_bonus = (gross_salary * bonus_percent) / Decimal("100")
        manual = Decimal(salary_record.adjustment)
        loan = Decimal(getattr(salary_record, "loan", Decimal("0")) or 0) + loan_installment_for_employee(salary_record.employee_id, salary_record.payroll_month)
        loan_pending = loan_pending_after_month_for_employee(salary_record.employee_id, salary_record.payroll_month)
        advance = advance_deduction_for_employee(salary_record.employee_id, salary_record.payroll_month)
        manual_deduction = abs(manual) if manual < 0 else Decimal("0")
        total_deduction = less_deduction + loan + advance + manual_deduction
        total_addition = ot_amount + attendance_bonus + (manual if manual > 0 else Decimal("0"))
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
            absence_minutes=absence_minutes,
            attendance_bonus_percent=bonus_percent,
            attendance_bonus_amount=money(attendance_bonus),
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
            "Worked On-Site": (Decimal("1"), 0),
            "Work From Home": (Decimal("1"), 0),
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
        # A working day with no punches at all is an absence to be settled against the
        # leave balance, not a data problem. Only real punch anomalies need a human.
        if not has_any_punch(record):
            return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "No punches recorded on a working day."}
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
    if has_split_punches(record):
        return {"status": "Needs Review", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": SPLIT_PUNCH_REVIEW_EXPLANATION}
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
            "Worked On-Site": (Decimal("1"), "Worked on-site; counted as a full working day."),
            "Work From Home": (Decimal("1"), "Worked from home; counted as a full working day."),
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
        if not has_any_punch(record):
            return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": 0, "explanation": "No punches recorded on a working day."}
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
    if has_split_punches(record):
        return {"status": "Needs Review", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": SPLIT_PUNCH_REVIEW_EXPLANATION}
    return {"status": "Absent / Attendance Missing", "paid_day": Decimal("0"), "leave_used": Decimal("0"), "rounded_minutes": actual, "explanation": "Less than 3 hours is not payable for daily wage."}


ABSENT_STATUS = "Absent / Attendance Missing"
# Overrides that mean "this person worked a full day" even though the punch data
# cannot show it. Work From Home was already offered as an override but was never
# handled here, so such a day was silently neither paid nor deducted.
WORKED_ELSEWHERE_STATUSES = ("Worked On-Site", "Work From Home")
FULL_DAY_PRESENT_STATUSES = {"Full Day Present", *WORKED_ELSEWHERE_STATUSES}
HALF_LEAVE_HALF_LOP_STATUS = "Half-Day Paid Leave / Half-Day LOP"
EXPLICIT_LEAVE_STATUSES = {"Paid Leave", "Half-Day Paid Leave", "Sandwich Leave"}


# Days that are not scheduled working days, so they can never create absence for the
# daily wage attendance bonus. A week off that was worked is an extra day, not a
# short one, so it is exempt too.
BONUS_EXEMPT_STATUSES = {"Week Off", "Week Off Worked", "Holiday"}
# How much of a day a status is worth when there is no punch data to measure, which
# happens when the day was set by a manual override.
BONUS_CREDIT_DAYS = {
    "Full Day Present": Decimal("1"),
    "Half Day Present": Decimal("0.5"),
    "Half-Day Paid Leave": Decimal("0.5"),
    "Half-Day LOP": Decimal("0.5"),
    "Half Day LOP": Decimal("0.5"),
    HALF_LEAVE_HALF_LOP_STATUS: Decimal("0.5"),
}


def has_any_punch(record):
    return bool(record.punches_json or record.first_punch or record.last_punch or record.raw_working_hours)


def has_split_punches(record):
    """More than one in/out pair on the day.

    A single pair under the half-day floor is a genuinely short day. Several pairs
    that add up to the same total usually mean a punch was missed in the middle, so
    the duration alone cannot decide the day and a human has to look at it.
    """
    return len(record.punches_json or []) > 2


SPLIT_PUNCH_REVIEW_EXPLANATION = (
    "Multiple punch pairs totalling less than 3 hours. Check the punches and set the day manually."
)


def daily_absence_minutes(row, record):
    """Minutes short of a full working day, for the daily wage attendance bonus.

    The bonus notice states that late reporting and leaving early are covered by it,
    so a short day counts here even though the day itself is still paid. The full-day
    grace applies exactly as it does for pay: at or above it the day is a complete day
    with no short hours, so it carries no absence either. Week offs and holidays are
    not working days, so they never count.
    """
    status = row["status"]
    if status in BONUS_EXEMPT_STATUSES:
        return 0
    full_day = CFG["FULL_DAY_MINUTES"]
    if status in WORKED_ELSEWHERE_STATUSES:
        # Marked present without punch data by design; there is nothing to measure.
        return 0
    if has_any_punch(record):
        actual = record.actual_minutes or 0
    else:
        actual = int(Decimal(full_day) * BONUS_CREDIT_DAYS.get(status, Decimal("0")))
    if actual >= CFG["FULL_DAY_REQUIRED_MINUTES"]:
        return 0
    return max(0, full_day - actual)


def daily_attendance_bonus_percent(absence_minutes):
    """Bonus band for a month's total absence: 10% at zero, 5% within the allowance."""
    if absence_minutes <= 0:
        return Decimal(str(BONUS_CFG["FULL_ATTENDANCE_BONUS_PERCENT"]))
    if absence_minutes <= BONUS_CFG["PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES"]:
        return Decimal(str(BONUS_CFG["PARTIAL_ATTENDANCE_BONUS_PERCENT"]))
    return Decimal("0")


def daily_bonus_explanation(absence_minutes, bonus_ignored=False):
    """Plain-English reason for the band a daily wage month landed in."""
    if bonus_ignored:
        return "This employee is excluded from the attendance bonus in Employee Master."
    allowance = BONUS_CFG["PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES"]
    full_percent = BONUS_CFG["FULL_ATTENDANCE_BONUS_PERCENT"]
    partial_percent = BONUS_CFG["PARTIAL_ATTENDANCE_BONUS_PERCENT"]
    absence = minutes_to_duration(absence_minutes)
    if absence_minutes <= 0:
        return f"No absence this month, so the full {full_percent}% attendance bonus applies."
    if absence_minutes <= allowance:
        return (
            f"Absence of {absence} is within the {minutes_to_duration(allowance)} allowance, "
            f"so the {partial_percent}% attendance bonus applies."
        )
    return (
        f"Absence of {absence} is over the {minutes_to_duration(allowance)} allowance, "
        "so no attendance bonus is payable."
    )


def apply_leave_balance(classified_rows, available):
    """Settle unexplained absences against `available` leave, oldest day first.

    A day is covered in full while at least one leave remains. With half a day left
    the absence is split: half paid from leave, half loss of pay. Anything less
    leaves the day untouched, so it falls through to full-day LOP.

    Returns the leave consumed.
    """
    available = Decimal(available or 0)
    used = Decimal("0")
    if available <= 0:
        return used
    for _record, _override, row in classified_rows:
        if row["status"] != ABSENT_STATUS:
            continue
        remaining = available - used
        if remaining >= Decimal("1"):
            row.update({
                "status": "Paid Leave",
                "paid_day": Decimal("0"),
                "leave_used": Decimal("1"),
                "explanation": "Absent day covered by available leave balance.",
            })
            used += Decimal("1")
        elif remaining >= Decimal("0.5"):
            row.update({
                "status": HALF_LEAVE_HALF_LOP_STATUS,
                "paid_day": Decimal("0"),
                "leave_used": Decimal("0.5"),
                "half_lop": True,
                "explanation": "Half day covered by the remaining leave balance; the other half is loss of pay.",
            })
            used += Decimal("0.5")
        else:
            break
    return used


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
