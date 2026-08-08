from flask import has_app_context

from attendance import db
from attendance.models import WeekOffRule

WEEKDAY_FIELDS = [
    ("monday", "Monday"),
    ("tuesday", "Tuesday"),
    ("wednesday", "Wednesday"),
    ("thursday", "Thursday"),
    ("friday", "Friday"),
    ("saturday", "Saturday"),
    ("sunday", "Sunday"),
]

WEEK_OFF_OPTIONS = [
    ("WORKING", "Normal Shift"),
    ("WEEK_OFF_ALL", "Week Off"),
    ("WEEK_OFF_1", "Week Off on 1st day of the month"),
    ("WEEK_OFF_2", "Week Off on 2nd day of the month"),
    ("WEEK_OFF_3", "Week Off on 3rd day of the month"),
    ("WEEK_OFF_4", "Week Off on 4th day of the month"),
    ("WEEK_OFF_5", "Week Off on 5th day of the month"),
]

OPTION_LABELS = dict(WEEK_OFF_OPTIONS)


def default_weekoff_rule(employee_id):
    return WeekOffRule(employee_id=employee_id)


def get_or_create_weekoff_rule(employee_id):
    rule = WeekOffRule.query.filter_by(employee_id=employee_id).first()
    if not rule:
        rule = default_weekoff_rule(employee_id)
        db.session.add(rule)
        db.session.flush()
    return rule


def week_occurrence_in_month(day):
    return ((day.day - 1) // 7) + 1


def normalize_weekoff_codes(codes):
    if isinstance(codes, str):
        raw_codes = [code.strip() for code in codes.split(",")]
    else:
        raw_codes = [str(code).strip() for code in codes]
    expanded = []
    for code in raw_codes:
        if not code:
            continue
        if code == "WEEK_OFF_2_4":
            expanded.extend(["WEEK_OFF_2", "WEEK_OFF_4"])
        else:
            expanded.append(code)
    valid_codes = {code for code, _ in WEEK_OFF_OPTIONS}
    selected = [code for code in expanded if code in valid_codes]
    if "WEEK_OFF_ALL" in selected:
        return "WEEK_OFF_ALL"
    selected = [code for code in selected if code != "WORKING"]
    unique = []
    for code in selected:
        if code not in unique:
            unique.append(code)
    return ",".join(unique) if unique else "WORKING"


def selected_weekoff_codes(value):
    normalized = normalize_weekoff_codes(value)
    if normalized == "WORKING":
        return ["WORKING"]
    if normalized == "WEEK_OFF_ALL":
        return ["WEEK_OFF_ALL"]
    return ["WORKING"] + normalized.split(",")


def is_week_off_for_date(employee_id, day):
    if not has_app_context():
        return day.weekday() == 6
    rule = WeekOffRule.query.filter_by(employee_id=employee_id).first()
    if not rule:
        code = "WEEK_OFF_ALL" if day.weekday() == 6 else "WORKING"
    else:
        code = getattr(rule, WEEKDAY_FIELDS[day.weekday()][0])
    occurrence = week_occurrence_in_month(day)
    codes = selected_weekoff_codes(code)
    if "WEEK_OFF_ALL" in codes:
        return True
    for item in codes:
        if item.startswith("WEEK_OFF_"):
            if occurrence == int(item.rsplit("_", 1)[1]):
                return True
    return False


def describe_week_off(employee_id, day):
    if not has_app_context():
        code = "WEEK_OFF_ALL" if day.weekday() == 6 else "WORKING"
        return OPTION_LABELS.get(code, "Normal Shift")
    rule = WeekOffRule.query.filter_by(employee_id=employee_id).first()
    if not rule:
        code = "WEEK_OFF_ALL" if day.weekday() == 6 else "WORKING"
    else:
        code = getattr(rule, WEEKDAY_FIELDS[day.weekday()][0])
    labels = [OPTION_LABELS.get(item, "Normal Shift") for item in selected_weekoff_codes(code)]
    return ", ".join(labels)
