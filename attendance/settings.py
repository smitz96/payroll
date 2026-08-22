# Printed on the salary slip under the logo.
COMPANY_ADDRESS = (
    "Survey No. 242/3, Panchratna Industrial Estate Lane, Near Ramol Cross Road, "
    "Ramol, Ahmedabad, India 382445"
)

MONTHLY_RULES = {
    "FULL_DAY_MINUTES": 9 * 60,
    "FULL_DAY_REQUIRED_MINUTES": 8 * 60 + 50,
    "HALF_DAY_MINIMUM_MINUTES": 3 * 60,
    "LESS_HOURS_RULE_MINIMUM_MINUTES": 6 * 60,
    "OVERTIME_START_MINUTES": 9 * 60 + 15,
    "ROUNDING_INTERVAL_MINUTES": 15,
    # 0 means "use the actual number of days in the payroll month", which is what the
    # manual salary sheet does. A fixed number can still be set here, but no single
    # figure is right for both a 28-day and a 31-day month.
    "SALARY_CALCULATION_DAYS": 0,
    "SALARY_HOURS_PER_DAY": 9,
    # Overtime is paid at this multiple of the ordinary hourly rate. Set to 1 by the
    # company: statutory overtime in India is generally payable at twice ordinary
    # wages, so this is the figure to revisit if that ever has to be met.
    "OVERTIME_MULTIPLIER": 1,
    "LEAVE_EARNED_PER_MONTH": 2,
    # A single In/Out pair longer than this is treated as a punch error rather than
    # paid time. Sized to clear a genuine overnight shift (typically 8-12h) while
    # catching a reversed pair, which rolls past midnight into a 13h+ "session".
    "MAX_SESSION_MINUTES": 12 * 60,
}

# Human-readable labels for the Settings page. Every key here is read by
# attendance/payroll_rules.py or attendance/parser.py; nothing is display-only.
MONTHLY_RULE_LABELS = {
    "FULL_DAY_MINUTES": ("Full working day", "Target hours used to size the short-hours deduction."),
    "FULL_DAY_REQUIRED_MINUTES": ("Full-day grace threshold", "At or above this, the day is paid full with no short-hours deduction."),
    "HALF_DAY_MINIMUM_MINUTES": ("Half-day minimum", "Below this a worked day earns no pay of its own. For monthly wage it is covered by available leave, and is loss of pay only if there is none."),
    "LESS_HOURS_RULE_MINIMUM_MINUTES": ("Short-hours floor", "Between this and the grace threshold, the day is paid full with a short-hours deduction."),
    "OVERTIME_START_MINUTES": ("Overtime starts after", "Overtime accrues only beyond this daily duration."),
    "ROUNDING_INTERVAL_MINUTES": ("Rounding interval", "Short hours are rounded up to this interval; overtime is floored to it. A 48-minute shortfall is charged as 60; 29 minutes of overtime is paid as 15."),
    "SALARY_CALCULATION_DAYS": ("Salary days per month", "Monthly salary is divided by this for the daily LOP rate. Set to 0 to divide by the actual days in each month."),
    "SALARY_HOURS_PER_DAY": ("Salary hours per day", "Hourly rate is the daily rate / this."),
    "OVERTIME_MULTIPLIER": ("Overtime multiplier", "Overtime is paid at this multiple of the ordinary hourly rate."),
    "LEAVE_EARNED_PER_MONTH": ("Leave earned per full month", "Pro-rated by the days that end up paid, and truncated rather than rounded so the accrual never overshoots."),
    "MAX_SESSION_MINUTES": ("Maximum overnight session", "An In/Out pair that crosses midnight and runs longer than this is flagged as a punch error instead of being paid. Long same-day shifts are unaffected."),
}

# Attendance bonus for daily wage ("cash salary") workers, per the notice of
# 08/12/2023. Absence is measured in minutes short of a full working day, and the
# notice states it covers late reporting and leaving early. The notice writes the
# allowance as "one and a half days (twelve hours)", which assumes an eight-hour day;
# a day here is FULL_DAY_MINUTES, so the allowance is one and a half of those.
DAILY_BONUS_RULES = {
    "FULL_ATTENDANCE_BONUS_PERCENT": 10,
    "PARTIAL_ATTENDANCE_BONUS_PERCENT": 5,
    "PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES": (MONTHLY_RULES["FULL_DAY_MINUTES"] * 3) // 2,
}

DAILY_BONUS_RULE_LABELS = {
    "FULL_ATTENDANCE_BONUS_PERCENT": ("Full attendance bonus", "Paid when the month has zero absence minutes."),
    "PARTIAL_ATTENDANCE_BONUS_PERCENT": ("Partial attendance bonus", "Paid when absence stays within the allowance below."),
    "PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES": ("Absence allowance", "One and a half working days. Absence up to this still earns the partial bonus; beyond it, no bonus."),
}

MINUTE_RULE_KEYS = {
    "FULL_DAY_MINUTES",
    "FULL_DAY_REQUIRED_MINUTES",
    "HALF_DAY_MINIMUM_MINUTES",
    "LESS_HOURS_RULE_MINIMUM_MINUTES",
    "OVERTIME_START_MINUTES",
    "MAX_SESSION_MINUTES",
    "PARTIAL_ATTENDANCE_MAX_ABSENCE_MINUTES",
}


def monthly_rule_rows():
    """Settings-page rows: label, formatted value, and what the rule actually does."""
    rows = []
    for key, value in MONTHLY_RULES.items():
        label, detail = MONTHLY_RULE_LABELS.get(key, (key, ""))
        if key in MINUTE_RULE_KEYS:
            display = f"{value // 60}h {value % 60:02d}m"
        elif key == "ROUNDING_INTERVAL_MINUTES":
            display = f"{value} minutes"
        elif key == "SALARY_CALCULATION_DAYS":
            display = "Days in the month" if not value else f"{value} days"
        elif key == "OVERTIME_MULTIPLIER":
            display = f"{value}x ordinary rate"
        elif key == "SALARY_HOURS_PER_DAY":
            display = f"{value} hours"
        elif key == "LEAVE_EARNED_PER_MONTH":
            display = f"{value} days"
        else:
            display = str(value)
        rows.append({"key": key, "label": label, "value": display, "detail": detail})
    return rows


def daily_bonus_rule_rows():
    """Settings-page rows for the daily wage attendance bonus."""
    rows = []
    for key, value in DAILY_BONUS_RULES.items():
        label, detail = DAILY_BONUS_RULE_LABELS.get(key, (key, ""))
        display = f"{value // 60}h {value % 60:02d}m" if key in MINUTE_RULE_KEYS else f"{value}% of earned wage"
        rows.append({"key": key, "label": label, "value": display, "detail": detail})
    return rows


# Leave policy, written out because none of it is a number on a dial: it is decided
# by how the calculation settles a month, and the Settings page is where anyone
# checking payroll goes looking for it.
LEAVE_RULES = [
    ("Earned each month", f"{MONTHLY_RULES['LEAVE_EARNED_PER_MONTH']} days",
     "Pro-rated by the days that end up paid, so a month with loss of pay earns less. "
     "Truncated rather than rounded, so the accrual never overshoots."),
    ("Taken in", "Half-day steps",
     "A balance is floored to the nearest half day before it can cover anything: 1.38 days "
     "covers 1 day and keeps 0.38, and 1.92 covers 1.5."),
    ("Covers", "Absence, and short days",
     "An absent day, the unworked half of a half day, and a day worked under the half-day "
     "minimum. Oldest day first. A day set to Unpaid Leave by hand is never covered."),
    ("Week off between unpaid days", "Charged to leave",
     "The sandwich rule: a week off with an unpaid day on either side is charged to leave. "
     "With no balance behind it the day is loss of pay, and it is shown that way."),
    ("Beyond the balance", "Loss of pay",
     "Deducted at one day of salary, where a day is the month's salary divided by the days "
     "in that month."),
    ("Carried forward", "In full",
     "Whatever is left is the next month's opening balance, once the month is finalized. "
     "A month cannot be started until the month before it is."),
    ("Encashed", "By employee, or the whole month",
     "Encashment is paid at the same daily rate and is capped at the balance left after the "
     "month's leave has been taken."),
    ("Daily wage", "No leave",
     "Daily wage employees are paid for the days they work and earn the monthly attendance "
     "bonus below in place of leave."),
]


def leave_rule_rows():
    """Settings-page rows for the leave policy."""
    return [{"label": label, "value": value, "detail": detail} for label, value, detail in LEAVE_RULES]
