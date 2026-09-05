import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import ZipFile

from attendance import db
from attendance.employee_defaults import ensure_employee_defaults
from attendance.models import AttendanceRecord, AuditLog, Employee, PayrollMonth, SalaryRecord
from attendance.settings import MONTHLY_RULES as CFG
from attendance.utils import clean, decimal_money, minutes_to_duration, normalize_salary_type, parse_csv_date, parse_duration

ATTENDANCE_REQUIRED = [
    "Employee ID",
    "Employee Name",
    "Department",
    "Designation",
    "Date",
    "Day",
    "Shift",
    "From",
    "To",
    "First Punch",
    "Last Punch",
    "Total Working Hours",
]

SALARY_ALIASES = {
    "ID": "Employee ID",
    "Employee ID": "Employee ID",
    "Name": "Name",
    "Type": "Type",
    "Salary": "Salary",
    "Adjustment": "Manual Adjustment",
    "Manual Adjustment": "Manual Adjustment",
}


def ensure_month(month):
    existing = PayrollMonth.query.get(month)
    if not existing:
        existing = PayrollMonth(month=month)
        db.session.add(existing)
        db.session.flush()
    return existing


def _dict_reader(path):
    return csv.DictReader(open(path, newline="", encoding="utf-8-sig"))


XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "office_rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
PUNCH_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.IGNORECASE)


def _cell_column_index(cell_ref):
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter.upper()) - ord("A") + 1)
    return index


def _xlsx_shared_strings(zf):
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(data)
    strings = []
    for item in root.findall("main:si", XML_NS):
        parts = [node.text or "" for node in item.findall(".//main:t", XML_NS)]
        strings.append("".join(parts))
    return strings


def _first_sheet_path(zf):
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relation_targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall("rel:Relationship", XML_NS)
    }
    first_sheet = workbook.find("main:sheets/main:sheet", XML_NS)
    if first_sheet is None:
        raise ValueError("Attendance XLSX has no sheets")
    rel_id = first_sheet.attrib.get(f"{{{XML_NS['office_rel']}}}id")
    target = relation_targets.get(rel_id)
    if not target:
        raise ValueError("Attendance XLSX sheet relationship is missing")
    # A relationship target is either relative to the workbook part
    # ("worksheets/sheet1.xml") or absolute from the package root
    # ("/xl/worksheets/sheet1.xml"). Both are legal and both are written in practice -
    # punch machines tend to write the first, Excel and most libraries the second - so
    # a file that has been opened and re-saved has to keep working.
    path = target[1:] if target.startswith("/") else "xl/" + target
    if path not in zf.namelist():
        raise ValueError(f"Attendance XLSX is missing its worksheet ({target})")
    return path


def _xlsx_cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", XML_NS))
    value = cell.find("main:v", XML_NS)
    if value is None or value.text is None:
        return ""
    text = value.text
    if cell_type == "s":
        try:
            return shared_strings[int(text)]
        except (ValueError, IndexError):
            return ""
    return text


def _xlsx_rows(path):
    with ZipFile(path) as zf:
        shared_strings = _xlsx_shared_strings(zf)
        sheet_path = _first_sheet_path(zf)
        sheet = ElementTree.fromstring(zf.read(sheet_path))
        rows = []
        for row in sheet.findall(".//main:sheetData/main:row", XML_NS):
            values_by_column = {}
            for cell in row.findall("main:c", XML_NS):
                column_index = _cell_column_index(cell.attrib.get("r", ""))
                if column_index:
                    values_by_column[column_index] = _xlsx_cell_value(cell, shared_strings)
            if not values_by_column:
                rows.append([])
                continue
            max_column = max(values_by_column)
            rows.append([values_by_column.get(index, "") for index in range(1, max_column + 1)])
        return rows


def _daily_punch_date_header(value):
    first_line = clean(str(value).splitlines()[0] if value is not None else "")
    if not first_line:
        return None
    try:
        parsed_date = parse_csv_date(first_line)
    except ValueError:
        return None
    lines = [clean(line) for line in str(value).splitlines() if clean(line)]
    day = lines[1] if len(lines) > 1 else parsed_date.strftime("%A")
    return parsed_date, day


def _parse_punch_times(value):
    text = clean(value)
    if not text:
        return []
    return [match.group(0).upper().replace("  ", " ") for match in PUNCH_TIME_RE.finditer(text)]


def _time_to_minutes(value):
    parsed = datetime.strptime(value, "%I:%M %p")
    return parsed.hour * 60 + parsed.minute


def _punch_sessions(punches):
    """Pair punches into In/Out sessions as (minutes, rolled_past_midnight)."""
    sessions = []
    for index in range(0, len(punches) - 1, 2):
        start = _time_to_minutes(punches[index])
        end = _time_to_minutes(punches[index + 1])
        rolled = end < start
        if rolled:
            end += 24 * 60
        sessions.append((end - start, rolled))
    return sessions


def _working_minutes_from_punches(punches):
    if len(punches) < 2:
        return None
    return sum(minutes for minutes, _rolled in _punch_sessions(punches))


def implausible_session_minutes(punches):
    """Length of the longest reversed-looking In/Out session, else 0.

    An Out punch typed before its In punch rolls past midnight and would otherwise
    be paid as a very long day plus overtime. Only sessions that actually rolled
    over are candidates: a long same-day shift (in 09:35, out 22:19) is real work
    and must still be paid. Of those, only ones longer than MAX_SESSION_MINUTES are
    flagged, so a genuine night shift is left alone.
    """
    if not punches or len(punches) < 2:
        return 0
    limit = CFG["MAX_SESSION_MINUTES"]
    rolled_over = [minutes for minutes, rolled in _punch_sessions(punches) if rolled and minutes > limit]
    return max(rolled_over, default=0)


def parse_punch_times(value):
    return _parse_punch_times(value)


def working_minutes_from_punches(punches):
    return _working_minutes_from_punches(punches)


def _attendance_rows_from_daily_punch_xlsx(path):
    rows = _xlsx_rows(path)
    if not rows:
        raise ValueError("Attendance XLSX is empty")
    header_index = next((index for index, row in enumerate(rows) if "Employee ID" in [clean(cell) for cell in row]), None)
    if header_index is None:
        raise ValueError("Attendance XLSX missing Employee ID header")
    headers = [clean(cell) for cell in rows[header_index]]
    required_static = ["Employee ID", "Employee Name", "Department", "Designation"]
    missing_static = [name for name in required_static if name not in headers]
    if missing_static:
        raise ValueError("Attendance XLSX missing columns: " + ", ".join(missing_static))
    column_indexes = {header: headers.index(header) for header in required_static}
    date_columns = []
    for index, header in enumerate(headers):
        parsed = _daily_punch_date_header(header)
        if parsed:
            date_columns.append((index, parsed[0], parsed[1]))
    if not date_columns:
        raise ValueError("Attendance XLSX has no daily punch date columns")

    normalized_rows = []
    for row in rows[header_index + 1:]:
        employee_id = clean(row[column_indexes["Employee ID"]] if len(row) > column_indexes["Employee ID"] else "")
        if not employee_id:
            continue
        base = {
            "Employee ID": employee_id,
            "Employee Name": clean(row[column_indexes["Employee Name"]] if len(row) > column_indexes["Employee Name"] else ""),
            "Department": clean(row[column_indexes["Department"]] if len(row) > column_indexes["Department"] else ""),
            "Designation": clean(row[column_indexes["Designation"]] if len(row) > column_indexes["Designation"] else ""),
        }
        for column_index, punch_date, day in date_columns:
            raw_value = row[column_index] if len(row) > column_index else ""
            punches = _parse_punch_times(raw_value)
            total_minutes = _working_minutes_from_punches(punches)
            warning_parts = []
            if len(punches) % 2 == 1:
                warning_parts.append("Odd punch count")
            long_session = implausible_session_minutes(punches)
            if long_session:
                warning_parts.append(f"Punch out before punch in ({minutes_to_duration(long_session)} session)")
            warning = "; ".join(warning_parts)
            normalized_rows.append({
                **base,
                "Date": punch_date.isoformat(),
                "Day": day,
                "Shift": "Normal Shift",
                "From": "",
                "To": "",
                "First Punch": punches[0] if punches else "",
                "Last Punch": punches[-1] if punches else "",
                "Total Working Hours": minutes_to_duration(total_minutes) if total_minutes is not None else "",
                "_Punch Count": str(len(punches)),
                "_Punches": punches,
                "_Punch Warning": warning,
            })
    return normalized_rows


def attendance_rows_from_upload(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx":
        return _attendance_rows_from_daily_punch_xlsx(path)
    return list(_dict_reader(path))


def update_employee_from_attendance(employee, name, row):
    """Refresh an existing master record from the attendance sheet.

    Employees are never created here: they must be added to Employee Master first,
    so payroll always runs against a wage type and salary someone has reviewed.
    """
    employee.name = employee.name or name or employee.id
    department = clean(row.get("Department"))
    designation = clean(row.get("Designation"))
    employee.department = department or employee.department
    employee.designation = designation or employee.designation
    db.session.add(employee)
    return employee


def unknown_attendance_employees(rows):
    """Employee IDs present in the sheet but missing from Employee Master, in sheet order."""
    known = {employee.id for employee in Employee.query.all()}
    missing = {}
    for row in rows:
        employee_id = clean(row.get("Employee ID"))
        if not employee_id or employee_id in known or employee_id in missing:
            continue
        missing[employee_id] = clean(row.get("Employee Name")) or employee_id
    return missing


class UnknownEmployeesError(ValueError):
    """Raised when the attendance sheet references employees that are not in the master."""

    def __init__(self, missing):
        self.missing = missing
        listed = ", ".join(f"{employee_id} - {name}" for employee_id, name in list(missing.items())[:10])
        if len(missing) > 10:
            listed += f", and {len(missing) - 10} more"
        super().__init__(
            f"{len(missing)} employee(s) in the attendance sheet are not in Employee Master: {listed}. "
            "Add them under Employees first, then upload the attendance sheet again. "
            "No attendance data was changed."
        )


def _validate_attendance_rows(rows, path):
    missing = [name for name in ATTENDANCE_REQUIRED if name not in (rows[0].keys() if rows else [])]
    if not missing:
        return
    # The two formats are not interchangeable: .xlsx is read as the daily punch
    # grid (dates across the top), while .csv must be one row per employee-day.
    # Saying so here is the difference between "my file was rejected" and "the
    # upload did nothing", which is how a grid saved as CSV reads otherwise.
    hint = ("The daily punch report grid must be uploaded as .xlsx. A .csv file has to be "
            "one row per employee per day."
            if Path(path).suffix.lower() != ".xlsx" else
            "The .xlsx must be the daily punch report, with dates across the top row.")
    raise ValueError(f"Attendance file is missing column(s): {', '.join(missing)}. {hint}")


def _attendance_record_from_row(row, month, duplicate_keys, warnings, default_details):
    employee_id = clean(row.get("Employee ID"))
    name = clean(row.get("Employee Name"))
    if not employee_id:
        warnings.append("Attendance row missing Employee ID")
        return None
    update_employee_from_attendance(db.session.get(Employee, employee_id), name, row)
    created_defaults = ensure_employee_defaults(employee_id)
    if created_defaults:
        default_details.append(f"{employee_id} - {name or employee_id}: {', '.join(created_defaults)}")
    warning_parts = []
    status = "OK"
    try:
        record_date = parse_csv_date(row.get("Date"))
    except ValueError as exc:
        warnings.append(f"Employee {employee_id}: {exc}")
        return None
    first = clean(row.get("First Punch"))
    last = clean(row.get("Last Punch"))
    raw_hours = clean(row.get("Total Working Hours"))
    punches = row.get("_Punches") or [punch for punch in [first, last] if punch]
    try:
        actual = parse_duration(row.get("Total Working Hours"))
    except ValueError as exc:
        actual = None
        status = "NEEDS_REVIEW"
        warning_parts.append(str(exc))
    if actual is None and not raw_hours:
        actual = _working_minutes_from_punches(punches)
        if actual is not None:
            raw_hours = minutes_to_duration(actual)
    if duplicate_keys[(employee_id, clean(row.get("Date")))] > 1:
        status = "NEEDS_REVIEW"
        warning_parts.append("Duplicate employee/date")
    if not first and not last and not raw_hours:
        status = "NEEDS_REVIEW"
        warning_parts.append("Missing punch and working hours")
    elif (first and not last) or (last and not first):
        status = "NEEDS_REVIEW"
        warning_parts.append("Punch error")
    if clean(row.get("_Punch Warning")):
        status = "NEEDS_REVIEW"
        warning_parts.append(clean(row.get("_Punch Warning")))
    elif punches:
        long_session = implausible_session_minutes(punches)
        if long_session:
            status = "NEEDS_REVIEW"
            warning_parts.append(f"Punch out before punch in ({minutes_to_duration(long_session)} session)")
    return AttendanceRecord(
        payroll_month=month,
        employee_id=employee_id,
        employee_name=name,
        department=clean(row.get("Department")),
        designation=clean(row.get("Designation")),
        date=record_date,
        day=clean(row.get("Day")),
        shift=clean(row.get("Shift")),
        shift_from=clean(row.get("From")),
        shift_to=clean(row.get("To")),
        first_punch=first,
        last_punch=last,
        punches_json=punches,
        raw_working_hours=raw_hours,
        actual_minutes=actual,
        parse_status=status,
        warning="; ".join(warning_parts),
    )


def import_attendance_csv(path, month, actor="admin"):
    ensure_month(month)
    # Everything that can reject the upload runs before any existing data is touched,
    # so a failed import leaves the month exactly as it was.
    rows = attendance_rows_from_upload(path)
    _validate_attendance_rows(rows, path)
    unknown = unknown_attendance_employees(rows)
    if unknown:
        raise UnknownEmployeesError(unknown)

    payroll_month = PayrollMonth.query.get(month)
    if payroll_month:
        payroll_month.attendance_submitted = False
    AttendanceRecord.query.filter_by(payroll_month=month).delete()
    from attendance.models import AttendanceOverride, PayrollResult
    AttendanceOverride.query.filter_by(payroll_month=month).delete()
    PayrollResult.query.filter_by(payroll_month=month).delete()
    warnings = []
    default_details = []
    duplicate_keys = Counter((clean(r.get("Employee ID")), clean(r.get("Date"))) for r in rows)
    imported = 0
    for row in rows:
        record = _attendance_record_from_row(row, month, duplicate_keys, warnings, default_details)
        if record:
            db.session.add(record)
            imported += 1
    db.session.add(AuditLog(actor=actor, action="Attendance Uploaded", detail=f"{Path(path).name}: {imported} rows"))
    if default_details:
        db.session.add(AuditLog(actor=actor, action="Employee Defaults Created", detail=" | ".join(default_details)))
    db.session.commit()
    return imported, warnings


def import_employee_attendance(path, month, employee_id, actor="admin", clear_overrides=True):
    from attendance.calculator import calculate_employee_payroll
    from attendance.models import AttendanceOverride

    ensure_month(month)
    rows = attendance_rows_from_upload(path)
    _validate_attendance_rows(rows, path)
    selected_employee_id = clean(employee_id)
    employee_rows = [row for row in rows if clean(row.get("Employee ID")) == selected_employee_id]
    if not employee_rows:
        raise ValueError(f"Attendance file has no rows for Employee ID {selected_employee_id}.")
    unknown = unknown_attendance_employees(employee_rows)
    if unknown:
        raise UnknownEmployeesError(unknown)

    AttendanceRecord.query.filter_by(payroll_month=month, employee_id=selected_employee_id).delete()
    if clear_overrides:
        AttendanceOverride.query.filter_by(payroll_month=month, employee_id=selected_employee_id).delete()
    warnings = []
    default_details = []
    duplicate_keys = Counter((clean(r.get("Employee ID")), clean(r.get("Date"))) for r in employee_rows)
    imported = 0
    for row in employee_rows:
        record = _attendance_record_from_row(row, month, duplicate_keys, warnings, default_details)
        if record:
            db.session.add(record)
            imported += 1
    db.session.add(AuditLog(
        actor=actor,
        action="Employee Attendance Re-imported",
        detail=(
            f"{month}: Employee ID {selected_employee_id}; {imported} row(s); "
            f"source {Path(path).name}; overrides cleared: {'yes' if clear_overrides else 'no'}"
        ),
    ))
    if default_details:
        db.session.add(AuditLog(actor=actor, action="Employee Defaults Created", detail=" | ".join(default_details)))
    db.session.flush()
    calculate_employee_payroll(month, selected_employee_id, actor)
    return imported, warnings


def import_salary_csv(path, month, actor="admin"):
    ensure_month(month)
    SalaryRecord.query.filter_by(payroll_month=month).delete()
    rows = list(_dict_reader(path))
    if not rows:
        raise ValueError("Salary CSV is empty")
    normalized_rows = []
    for row in rows:
        normalized = {SALARY_ALIASES.get(k.strip(), k.strip()): clean(v) for k, v in row.items()}
        normalized_rows.append(normalized)
    required = ["Employee ID", "Name", "Type", "Salary", "Manual Adjustment"]
    missing = [name for name in required if name not in normalized_rows[0]]
    if missing:
        raise ValueError("Salary CSV missing columns: " + ", ".join(missing))
    warnings = []
    default_details = []
    duplicate_ids = Counter(r.get("Employee ID") for r in normalized_rows)
    imported = 0
    for row in normalized_rows:
        employee_id = clean(row.get("Employee ID"))
        if not employee_id:
            warnings.append("Salary row missing Employee ID")
            continue
        warning_parts = []
        if duplicate_ids[employee_id] > 1:
            warning_parts.append("Duplicate Employee ID")
        salary_type = clean(row.get("Type"))
        normalized_type = normalize_salary_type(salary_type)
        if not normalized_type:
            warning_parts.append("Missing Wage Type")
        elif normalized_type != "MONTHLY":
            warning_parts.append(f'Unsupported Wage Type "{salary_type}"')
            db.session.add(AuditLog(actor=actor, action="Unsupported Wage Type Detected", detail=f"{employee_id}: {salary_type}"))
        try:
            salary = decimal_money(row.get("Salary"))
        except ValueError as exc:
            salary = 0
            warning_parts.append(str(exc))
        try:
            adjustment = decimal_money(row.get("Manual Adjustment"))
        except ValueError as exc:
            adjustment = 0
            warning_parts.append(str(exc))
        employee = Employee.query.get(employee_id) or Employee(id=employee_id, name=clean(row.get("Name")) or employee_id)
        employee.name = clean(row.get("Name")) or employee.name
        db.session.merge(employee)
        created_defaults = ensure_employee_defaults(employee_id)
        if created_defaults:
            default_details.append(f"{employee_id} - {clean(row.get('Name')) or employee_id}: {', '.join(created_defaults)}")
        db.session.add(SalaryRecord(
            payroll_month=month,
            employee_id=employee_id,
            name=clean(row.get("Name")) or employee_id,
            salary_type=salary_type,
            normalized_salary_type=normalized_type,
            salary=salary,
            adjustment=adjustment,
            warning="; ".join(warning_parts),
        ))
        imported += 1
    db.session.add(AuditLog(actor=actor, action="Salary CSV Uploaded", detail=f"{Path(path).name}: {imported} rows"))
    if default_details:
        db.session.add(AuditLog(actor=actor, action="Employee Defaults Created", detail=" | ".join(default_details)))
    db.session.commit()
    return imported, warnings
