"""Working shifts: the clock a day is measured against.

Every weekday of an employee's week off rule can carry a shift. A weekday without
one works to the default shift, the Normal Shift of 9:30 AM to 6:30 PM, so rules
written before shifts existed behave exactly as they did.

The day thresholds in MONTHLY_RULES are written for the 9-hour Normal Shift. A shift
of another length scales them by the same fractions rather than reusing the 9-hour
figures: a full day needs 2/3 of the shift and a half day 1/3, the full-day grace is
10 minutes short of the shift, and overtime on a week off or holiday worked starts
30 minutes past it. The fractions are kept exact, so the Normal Shift is still
exactly 6h and 3h, and an 8-hour shift needs 5h20m for a full day.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from math import ceil

from flask import g, has_app_context

from attendance import db
from attendance.models import Shift, WeekOffRule
from attendance.settings import MONTHLY_RULES as CFG

DEFAULT_SHIFT_NAME = "Normal Shift"
DEFAULT_SHIFT_START = 9 * 60 + 30
DEFAULT_SHIFT_END = 18 * 60 + 30

# Each threshold as a fraction of (or an offset from) the Normal Shift length, so a
# shift of any length scales them the same way.
FULL_DAY_FRACTION = Fraction(CFG["LESS_HOURS_RULE_MINIMUM_MINUTES"], CFG["FULL_DAY_MINUTES"])
HALF_DAY_FRACTION = Fraction(CFG["HALF_DAY_MINIMUM_MINUTES"], CFG["FULL_DAY_MINUTES"])
FULL_DAY_GRACE_GAP = CFG["FULL_DAY_MINUTES"] - CFG["FULL_DAY_REQUIRED_MINUTES"]
TOTAL_HOURS_OVERTIME_GAP = CFG["OVERTIME_START_MINUTES"] - CFG["FULL_DAY_MINUTES"]

# A shift has to leave room for a half day and a full day to mean anything, and a
# day shift cannot run past midnight.
MINIMUM_SHIFT_MINUTES = 60
SHIFT_NAME_MAX_LENGTH = 80


def format_clock(minutes):
    """Minutes after midnight as a 12-hour clock time: 570 reads as 09:30 AM."""
    hours, mins = divmod(int(minutes) % (24 * 60), 60)
    return f"{(hours % 12) or 12:02d}:{mins:02d} {'AM' if hours < 12 else 'PM'}"


def format_input_clock(minutes):
    """Minutes after midnight as HH:MM, the value an <input type="time"> expects."""
    hours, mins = divmod(int(minutes), 60)
    return f"{hours:02d}:{mins:02d}"


def format_threshold(minutes):
    """A threshold that may be a fraction of a minute, rounded up for display."""
    whole = ceil(minutes)
    return f"{whole // 60}h {whole % 60:02d}m"


@dataclass(frozen=True)
class ShiftTimes:
    name: str
    start: int
    end: int
    id: int = None

    @property
    def length(self):
        return self.end - self.start

    @property
    def full_day_minimum(self):
        return Fraction(self.length) * FULL_DAY_FRACTION

    @property
    def half_day_minimum(self):
        return Fraction(self.length) * HALF_DAY_FRACTION

    @property
    def full_day_grace(self):
        return self.length - FULL_DAY_GRACE_GAP

    @property
    def total_hours_overtime_start(self):
        return self.length + TOTAL_HOURS_OVERTIME_GAP

    def is_full_day(self, actual):
        return actual is not None and actual >= self.full_day_minimum

    def is_half_day(self, actual):
        return actual is not None and actual >= self.half_day_minimum

    @property
    def hours_label(self):
        return f"{format_clock(self.start)} - {format_clock(self.end)}"

    @property
    def label(self):
        return f"{self.name} ({self.hours_label})"


DEFAULT_SHIFT = ShiftTimes(DEFAULT_SHIFT_NAME, DEFAULT_SHIFT_START, DEFAULT_SHIFT_END)


def shift_times(shift):
    return ShiftTimes(shift.name, shift.start_minutes, shift.end_minutes, shift.id)


def ensure_default_shift():
    """Create the Normal Shift on first start, and keep exactly one default."""
    shifts = Shift.query.order_by(Shift.id).all()
    if not shifts:
        db.session.add(Shift(name=DEFAULT_SHIFT_NAME, start_minutes=DEFAULT_SHIFT_START,
                             end_minutes=DEFAULT_SHIFT_END, is_default=True))
        db.session.flush()
        return
    if not any(shift.is_default for shift in shifts):
        shifts[0].is_default = True
        db.session.flush()


def clear_shift_cache():
    if has_app_context():
        g.pop("_default_shift", None)


def default_shift():
    """The shift a weekday without its own works to."""
    if not has_app_context():
        return DEFAULT_SHIFT
    cached = g.get("_default_shift")
    if cached is not None:
        return cached
    shift = Shift.query.filter_by(is_default=True).order_by(Shift.id).first()
    times = shift_times(shift) if shift else DEFAULT_SHIFT
    g._default_shift = times
    return times


def all_shifts():
    """Every shift, default first, then by start time."""
    return sorted(Shift.query.all(), key=lambda shift: (not shift.is_default, shift.start_minutes, shift.name.lower()))


def weekday_shift_field(weekday_field):
    return f"{weekday_field}_shift_id"


def shift_for_rule_field(rule, weekday_field):
    shift_id = getattr(rule, weekday_shift_field(weekday_field), None) if rule else None
    if shift_id:
        shift = db.session.get(Shift, shift_id)
        if shift:
            return shift_times(shift)
    return default_shift()


def shift_for_date(employee_id, day):
    """The shift an employee works on a given date."""
    from attendance.weekoffs import WEEKDAY_FIELDS

    if not has_app_context():
        return DEFAULT_SHIFT
    rule = WeekOffRule.query.filter_by(employee_id=employee_id).first()
    return shift_for_rule_field(rule, WEEKDAY_FIELDS[day.weekday()][0])


CLOCK_INPUT_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%H:%M:%S")


def parse_clock_input(value, label):
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    for clock_format in CLOCK_INPUT_FORMATS:
        try:
            parsed = datetime.strptime(text, clock_format)
        except ValueError:
            continue
        return parsed.hour * 60 + parsed.minute
    raise ValueError(f'{label}: "{value}" is not a time, such as 09:30 or 6:30 PM.')


def validate_shift(name, start, end, shift_id=None):
    """Clean a shift's fields, or raise ValueError saying what is wrong."""
    name = re.sub(r"\s+", " ", str(name or "").strip())
    if not name:
        raise ValueError("Shift name is required.")
    if len(name) > SHIFT_NAME_MAX_LENGTH:
        raise ValueError(f"Shift name must be {SHIFT_NAME_MAX_LENGTH} characters or fewer.")
    if any(separator in name for separator in ";="):
        raise ValueError('Shift name cannot contain ";" or "=", which the employee master file uses as separators.')
    start_minutes = parse_clock_input(start, f"{name} start time")
    end_minutes = parse_clock_input(end, f"{name} end time")
    if end_minutes <= start_minutes:
        raise ValueError(f"{name}: the end time must be later than the start time on the same day. Overnight shifts are not supported.")
    if end_minutes - start_minutes < MINIMUM_SHIFT_MINUTES:
        raise ValueError(f"{name}: a shift must be at least 1 hour long.")
    for other in Shift.query.all():
        if other.id != shift_id and other.name.lower() == name.lower():
            raise ValueError(f'A shift named "{other.name}" already exists.')
    return name, start_minutes, end_minutes


def shift_usage(shift_id):
    """Employee IDs with at least one weekday on this shift."""
    from attendance.weekoffs import WEEKDAY_FIELDS

    used_by = []
    for rule in WeekOffRule.query.all():
        if any(getattr(rule, weekday_shift_field(field)) == shift_id for field, _label in WEEKDAY_FIELDS):
            used_by.append(rule.employee_id)
    return used_by


def stored_shift_id(shift_id):
    """What a weekday stores for a chosen shift: blank for the default shift."""
    if not shift_id:
        return None
    shift = db.session.get(Shift, int(shift_id))
    if not shift:
        raise ValueError(f"Shift {shift_id} does not exist.")
    return None if shift.is_default else shift.id


def shift_pattern_text(rule):
    """A rule's shifts as one readable field, for the employee master file.

    The shift most of the week works is written on its own, followed by any weekday
    that differs: "Normal Shift; Saturday=Short Shift". Every row carries at least
    the base shift, so the column always states the shift rather than implying it.
    """
    from attendance.weekoffs import WEEKDAY_FIELDS

    names = [shift_for_rule_field(rule, field).name for field, _label in WEEKDAY_FIELDS]
    base = max(dict.fromkeys(names), key=names.count)
    parts = [base]
    for (field, label), name in zip(WEEKDAY_FIELDS, names):
        if name != base:
            parts.append(f"{label}={name}")
    return "; ".join(parts)


def parse_shift_pattern(text, label="Shift Pattern"):
    """The inverse of `shift_pattern_text`, as {weekday field: stored shift id}.

    A part without a weekday sets every weekday not named on its own; with no such
    part, unnamed weekdays work the default shift. Every weekday is returned, so an
    import replaces the whole pattern.
    """
    from attendance.weekoffs import WEEKDAY_FIELDS

    by_name = {shift.name.lower(): shift for shift in Shift.query.all()}
    by_day = {name.lower(): field for field, name in WEEKDAY_FIELDS}

    def lookup(name):
        shift = by_name.get(name.strip().lower())
        if not shift:
            known = ", ".join(sorted(shift.name for shift in by_name.values()))
            raise ValueError(f'{label}: there is no shift named "{name.strip()}". Shifts: {known}.')
        return None if shift.is_default else shift.id

    base = None
    per_day = {}
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        day, separator, name = chunk.partition("=")
        if not separator:
            if base is not None:
                raise ValueError(f"{label}: only one shift can be given without a weekday.")
            base = lookup(chunk)
            continue
        field = by_day.get(day.strip().lower())
        if not field:
            raise ValueError(f'{label}: "{chunk}" is not a weekday and a shift, such as "Saturday=Short Shift".')
        per_day[field] = lookup(name)
    return {field: per_day.get(field, base) for field, _label in WEEKDAY_FIELDS}
