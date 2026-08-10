import calendar
import re
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text == "-" else text


def normalize_salary_type(value):
    text = clean(value)
    return text.upper() if text else ""


def parse_duration(value):
    text = clean(value).lower().replace(" ", "")
    if not text:
        return None
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", text)
    if not match or not any(match.groups()):
        raise ValueError(f"Invalid duration: {value}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * 60 + minutes


def format_percent(value):
    """Percentage without trailing zeros: 10.00 reads as "10", 7.50 as "7.5".

    normalize() alone would turn Decimal("10.00") into 1E+1, so whole numbers are
    taken through to_integral_value() instead.
    """
    amount = Decimal(value or 0)
    whole = amount.to_integral_value()
    return str(whole if amount == whole else amount.normalize())


def minutes_to_duration(minutes):
    if minutes is None:
        return "-"
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}h {minutes % 60:02d}m"


def floor_to_interval(minutes, interval=15):
    return (int(minutes) // interval) * interval


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_valid_payroll_month(value):
    """True for a well-formed YYYY-MM payroll month key."""
    return bool(MONTH_RE.match(clean(value)))


def display_month(value):
    """Format a payroll month key as 'July 2026'; returns the input if malformed."""
    if not is_valid_payroll_month(value):
        return value or "Not started"
    year, month_number = (int(part) for part in str(value).split("-"))
    return f"{calendar.month_name[month_number]} {year}"


def parse_csv_date(value):
    text = clean(value)
    if not text:
        raise ValueError("Missing date")
    for date_format in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass
    raise ValueError(f"Invalid date: {value}")


def decimal_money(value):
    try:
        return Decimal(str(value).strip() or "0")
    except Exception as exc:
        raise ValueError(f"Invalid money value: {value}") from exc


def money(value):
    if value is None:
        return ""
    return Decimal(value).quantize(Decimal("0.01"))


def truncate_one_decimal(value):
    return Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def as_ist(value):
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def format_ist_datetime(value, date_format="%d-%m-%Y %H:%M:%S"):
    local_value = as_ist(value)
    return local_value.strftime(date_format) if local_value else ""


def ist_day_to_utc_bounds(day):
    start = datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(day, time.max, tzinfo=IST).astimezone(timezone.utc)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)
