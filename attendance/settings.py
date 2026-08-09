MONTHLY_RULES = {
    "FULL_DAY_MINUTES": 9 * 60,
    "FULL_DAY_REQUIRED_MINUTES": 8 * 60 + 50,
    "HALF_DAY_MINIMUM_MINUTES": 3 * 60,
    "LESS_HOURS_RULE_MINIMUM_MINUTES": 6 * 60,
    "OVERTIME_START_MINUTES": 9 * 60 + 15,
    "ROUNDING_INTERVAL_MINUTES": 15,
    "SALARY_CALCULATION_DAYS": 30,
    "SALARY_HOURS_PER_DAY": 9,
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
    "HALF_DAY_MINIMUM_MINUTES": ("Half-day minimum", "Below this, a worked day is not payable (full-day LOP for monthly wage)."),
    "LESS_HOURS_RULE_MINIMUM_MINUTES": ("Short-hours floor", "Between this and the grace threshold, the day is paid full with a short-hours deduction."),
    "OVERTIME_START_MINUTES": ("Overtime starts after", "Overtime accrues only beyond this daily duration."),
    "ROUNDING_INTERVAL_MINUTES": ("Rounding interval", "Short hours and overtime are floored to this interval."),
    "SALARY_CALCULATION_DAYS": ("Salary days per month", "Monthly salary is divided by this for the daily LOP rate."),
    "SALARY_HOURS_PER_DAY": ("Salary hours per day", "Hourly rate is monthly salary / (salary days x this)."),
    "LEAVE_EARNED_PER_MONTH": ("Leave earned per full month", "Pro-rated by paid days and truncated to one decimal."),
    "MAX_SESSION_MINUTES": ("Maximum overnight session", "An In/Out pair that crosses midnight and runs longer than this is flagged as a punch error instead of being paid. Long same-day shifts are unaffected."),
}

MINUTE_RULE_KEYS = {
    "FULL_DAY_MINUTES",
    "FULL_DAY_REQUIRED_MINUTES",
    "HALF_DAY_MINIMUM_MINUTES",
    "LESS_HOURS_RULE_MINIMUM_MINUTES",
    "OVERTIME_START_MINUTES",
    "MAX_SESSION_MINUTES",
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
            display = f"{value} days"
        elif key == "SALARY_HOURS_PER_DAY":
            display = f"{value} hours"
        elif key == "LEAVE_EARNED_PER_MONTH":
            display = f"{value} days"
        else:
            display = str(value)
        rows.append({"key": key, "label": label, "value": display, "detail": detail})
    return rows
