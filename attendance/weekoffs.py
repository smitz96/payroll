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
WEEKDAY_DISPLAY_FIELDS = [
    ("sunday", "Sunday"),
    ("monday", "Monday"),
    ("tuesday", "Tuesday"),
    ("wednesday", "Wednesday"),
    ("thursday", "Thursday"),
    ("friday", "Friday"),
    ("saturday", "Saturday"),
]

# The occurrence codes count that weekday within the month, not the date: WEEK_OFF_2
# on Saturday is the second Saturday. The labels used to read "2nd day of the month",
# which is a different thing entirely.
WEEK_OFF_OPTIONS = [
    ("WORKING", "Normal Shift"),
    ("WEEK_OFF_ALL", "Week Off every week"),
    ("WEEK_OFF_1", "Week Off on the 1st of this weekday"),
    ("WEEK_OFF_2", "Week Off on the 2nd of this weekday"),
    ("WEEK_OFF_3", "Week Off on the 3rd of this weekday"),
    ("WEEK_OFF_4", "Week Off on the 4th of this weekday"),
    ("WEEK_OFF_5", "Week Off on the 5th of this weekday"),
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


def weekoff_pattern_text(rule):
    """A rule as one readable field, for the employee master file.

    "Saturday=2,4; Sunday=All" reads as the second and fourth Saturday of the month
    plus every Sunday. Days that are worked are left out, so a plain Sunday week off
    is simply "Sunday=All" and an employee who works every day is blank.
    """
    if not rule:
        return ""
    parts = []
    for field, label in WEEKDAY_FIELDS:
        codes = selected_weekoff_codes(getattr(rule, field, None))
        if "WEEK_OFF_ALL" in codes:
            parts.append(f"{label}=All")
            continue
        occurrences = sorted(
            int(code.rsplit("_", 1)[1]) for code in codes
            if code.startswith("WEEK_OFF_") and code.rsplit("_", 1)[1].isdigit()
        )
        if occurrences:
            parts.append(f"{label}=" + ",".join(str(number) for number in occurrences))
    return "; ".join(parts)


def parse_weekoff_pattern(text, label="Week Off Pattern"):
    """The inverse of `weekoff_pattern_text`, as {field: stored code}.

    Every weekday is returned, so a day left out of the text is explicitly working -
    that is what makes an import able to remove a week off as well as add one.
    """
    fields = {field: "WORKING" for field, _label in WEEKDAY_FIELDS}
    by_name = {name.lower(): field for field, name in WEEKDAY_FIELDS}
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        day, separator, codes = chunk.partition("=")
        field = by_name.get(day.strip().lower())
        if not field or not separator:
            raise ValueError(f'{label}: "{chunk}" is not a weekday and a value, such as "Sunday=All".')
        codes = codes.strip()
        if codes.lower() in {"all", "week_off_all"}:
            fields[field] = "WEEK_OFF_ALL"
            continue
        occurrences = []
        for number in codes.split(","):
            number = number.strip()
            if not number.isdigit() or not 1 <= int(number) <= 5:
                raise ValueError(f'{label}: "{codes}" must be All, or occurrences 1 to 5 such as "2,4".')
            occurrences.append(f"WEEK_OFF_{int(number)}")
        fields[field] = normalize_weekoff_codes(occurrences)
    return fields
