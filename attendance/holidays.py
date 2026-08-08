from attendance.models import Holiday

HOLIDAY_TYPE_RECURRING = "RECURRING"
HOLIDAY_TYPE_VARIABLE = "VARIABLE"
HOLIDAY_TYPES = {
    HOLIDAY_TYPE_VARIABLE: "Variable Holiday",
    HOLIDAY_TYPE_RECURRING: "Recurring Holiday",
}


def normalize_holiday_type(value):
    text = str(value or "").strip().upper()
    return text if text in HOLIDAY_TYPES else HOLIDAY_TYPE_VARIABLE


def holiday_dates_for_records(records):
    records = list(records or [])
    if not records:
        return set()
    exact_dates = set()
    recurring_days = set()
    for holiday in Holiday.query.all():
        holiday_type = normalize_holiday_type(getattr(holiday, "holiday_type", None))
        if holiday_type == HOLIDAY_TYPE_RECURRING:
            recurring_days.add((holiday.date.month, holiday.date.day))
        else:
            exact_dates.add(holiday.date)
    return {
        record.date
        for record in records
        if record.date in exact_dates or (record.date.month, record.date.day) in recurring_days
    }
