import re
from datetime import datetime
from decimal import Decimal, ROUND_DOWN


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


def minutes_to_duration(minutes):
    if minutes is None:
        return "-"
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}h {minutes % 60:02d}m"


def floor_to_interval(minutes, interval=15):
    return (int(minutes) // interval) * interval


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
