from datetime import datetime
from decimal import Decimal

from attendance import db
from attendance.employee_defaults import ensure_employee_defaults
from attendance.models import AuditLog, Employee, SalaryRecord
from attendance.utils import clean, decimal_money, is_valid_payroll_month, normalize_salary_type, parse_csv_date

ACTIVE_STATUS = "ACTIVE"
# Only ACTIVE employees are included in payroll. INACTIVE is a reversible pause;
# LEFT and TERMINATED are end-of-employment states kept for history.
EMPLOYMENT_STATUSES = (
    ("ACTIVE", "Active"),
    ("INACTIVE", "Inactive"),
    ("LEFT", "Left"),
    ("TERMINATED", "Terminated"),
)
EMPLOYMENT_STATUS_KEYS = {key for key, _label in EMPLOYMENT_STATUSES}
DISABLED_STATUSES = {"INACTIVE", "LEFT", "TERMINATED"}
DISABLE_CONFIRMATION_TEXT = "confirm"
MASTER_IMPORT_REQUIRED_COLUMNS = {"Employee ID", "Wage Type", "Salary"}

# Monthly salary breakup. These are informational/compliance fields: payroll is still
# calculated from `salary`, and the components must add up to it exactly.
SALARY_COMPONENTS = (
    ("basic_salary", "Basic"),
    ("hra", "HRA"),
    ("allowance", "Allowance"),
)
# Files exported before Conveyance Allowance was folded into Allowance, and before
# "Basic Salary" was shortened, still import cleanly.
COMPONENT_COLUMN_ALIASES = {"basic_salary": ("Basic", "Basic Salary"), "hra": ("HRA",), "allowance": ("Allowance",)}
RETIRED_IMPORT_COLUMNS = ("Conveyance Allowance",)
COMPLIANCE_FLAGS = (("pf_enabled", "PF"), ("esic_enabled", "ESIC"))
# Monthly-only amounts that are typed in rather than derived. TDS is not
# calculated by the app: whatever is entered here is deducted each month.
MONTHLY_ONLY_AMOUNTS = (("tds", "TDS"),)
# Daily wage only, the mirror image of the monthly-only breakup and compliance fields.
DAILY_ONLY_FLAGS = (("bonus_ignored", "Ignore Monthly Bonus"),)
YES_VALUES = {"yes", "y", "true", "1", "enabled", "applicable"}
NO_VALUES = {"no", "n", "false", "0", "disabled", "not applicable", ""}


def parse_yes_no(value, field_name):
    text = clean(value).lower()
    if text in YES_VALUES:
        return True
    if text in NO_VALUES:
        return False
    raise ValueError(f"{field_name} must be Yes or No, got \"{value}\".")


def component_total(values):
    return sum((Decimal(value or 0) for value in values), Decimal("0"))


def validate_salary_breakup(salary, components, label=""):
    """Components must sum to salary, unless the breakup has not been entered at all.

    An all-zero breakup means "not captured yet", which keeps existing records and
    older import files valid. Once any component is filled in, the whole breakup has
    to reconcile so the payslip figures can never disagree with the salary.
    """
    total = component_total(components.values())
    if total == 0:
        return
    salary = Decimal(salary or 0)
    if total != salary:
        prefix = f"{label}: " if label else ""
        parts = ", ".join(f"{name} {Decimal(components[key] or 0):.2f}" for key, name in SALARY_COMPONENTS)
        raise ValueError(
            f"{prefix}Salary breakup must add up to the salary. {parts} total {total:.2f}, "
            f"but salary is {salary:.2f}. Difference {abs(total - salary):.2f}."
        )


def employee_sort_value(employee):
    return int(employee.id) if str(employee.id).isdigit() else str(employee.id).lower()


def active_master_employees():
    return sorted(Employee.query.filter_by(employment_status=ACTIVE_STATUS).all(), key=employee_sort_value)


def employee_active_for_payroll_month(employee, payroll_month):
    """Whether this employee belongs in a given payroll month.

    Someone who has left is still owed for the months they worked, so the test is
    against their last working day rather than against today: they take part in
    every month up to and including the one they left in, and none after it.

    With no last working day recorded there is nothing to compare, so they stay out
    of every month - the payroll page names anyone left out this way, so it is a
    prompt to enter the date rather than a silent omission.
    """
    if not employee:
        return True
    if employee.employment_status == ACTIVE_STATUS:
        return True
    left_on = getattr(employee, "left_on", None)
    if not left_on or not is_valid_payroll_month(payroll_month):
        return False
    return payroll_month <= left_on.strftime("%Y-%m")


def employees_left_out_of_month(payroll_month):
    """Employees with wages for the month that payroll will not include.

    Only those whose last working day is missing or earlier than the month, so the
    list is a to-do rather than a roll of everyone who has ever left.
    """
    left_out = []
    for salary in SalaryRecord.query.filter_by(payroll_month=payroll_month).all():
        employee = db.session.get(Employee, salary.employee_id)
        if employee and not employee_active_for_payroll_month(employee, payroll_month):
            left_out.append({
                "employee": employee,
                "name": salary.name or employee.name,
                "status": employee.employment_status,
                "left_on": employee.left_on,
            })
    return sorted(left_out, key=lambda row: employee_sort_value(row["employee"]))


def employee_master_export_rows():
    rows = []
    for employee in sorted(Employee.query.all(), key=employee_sort_value):
        wage_group = normalize_salary_type(employee.salary_type)
        monthly = wage_group == "MONTHLY"
        daily = wage_group == "DAILY"
        rows.append({
            "Employee ID": employee.id,
            "Name": employee.name,
            "Department": employee.department or "",
            "Designation": employee.designation or "",
            "Wage Type": employee.salary_type or "",
            "Salary": employee.salary or Decimal("0"),
            # Breakup and compliance apply to monthly wage only; leave them blank for
            # daily so the file cannot suggest they are editable there.
            "Basic": (employee.basic_salary or Decimal("0")) if monthly else "",
            "HRA": (employee.hra or Decimal("0")) if monthly else "",
            "Allowance": (employee.allowance or Decimal("0")) if monthly else "",
            "TDS": (employee.tds or Decimal("0")) if monthly else "",
            "PF": ("Yes" if employee.pf_enabled else "No") if monthly else "",
            "ESIC": ("Yes" if employee.esic_enabled else "No") if monthly else "",
            "Ignore OT": "Yes" if employee.ot_ignored else "No",
            "Ignore Less Hours": "Yes" if employee.less_hours_ignored else "No",
            # The attendance bonus is a daily wage rule, so the column is blank for
            # monthly just as the breakup columns are blank for daily.
            "Ignore Monthly Bonus": ("Yes" if employee.bonus_ignored else "No") if daily else "",
            "Status": employee.employment_status or ACTIVE_STATUS,
            # Blank for anyone still working. For a leaver this is the date that
            # decides their final payroll month, so it has to survive the round trip.
            "Last Working Day": employee.left_on.strftime("%d-%m-%Y") if employee.left_on else "",
        })
    return list(EMPLOYEE_MASTER_SAMPLE_ROWS) + rows


EMPLOYEE_MASTER_EXPORT_COLUMNS = [
    "Employee ID", "Name", "Department", "Designation", "Wage Type", "Salary",
    "Basic", "HRA", "Allowance", "TDS", "PF", "ESIC", "Ignore OT", "Ignore Less Hours",
    "Ignore Monthly Bonus", "Status", "Last Working Day",
]

# Illustrative rows shipped at the top of the export so the expected shape of every
# column is obvious without opening the docs. They are not database records: the
# import skips any row matching one of them, so an exported file round-trips safely.
# The IDs are deliberately unlike a real employee number. They used to be 1 and 2,
# which collide with real employees, and a file showing "1" twice reads as a
# duplicate record rather than as a worked example.
EMPLOYEE_MASTER_SAMPLE_ROWS = [
    {
        "Employee ID": "EXAMPLE-MONTHLY", "Name": "Example Monthly Employee", "Department": "Accounts",
        "Designation": "Accounts Executive", "Wage Type": "Monthly", "Salary": "50000",
        "Basic": "35000", "HRA": "10000", "Allowance": "5000", "TDS": "2500",
        "PF": "Yes", "ESIC": "No", "Ignore OT": "Yes", "Ignore Less Hours": "No",
        "Ignore Monthly Bonus": "",
    "Status": "ACTIVE", "Last Working Day": "",
    },
    {
        "Employee ID": "EXAMPLE-DAILY", "Name": "Example Daily Employee", "Department": "Mechanical Production",
        "Designation": "Helper", "Wage Type": "Daily", "Salary": "5000",
        "Basic": "0", "HRA": "0", "Allowance": "0", "TDS": "",
        "PF": "No", "ESIC": "No", "Ignore OT": "Yes", "Ignore Less Hours": "No",
        "Ignore Monthly Bonus": "No",
    "Status": "ACTIVE", "Last Working Day": "",
    },
]
# Exports taken before the rename still carry the old rows, so they stay recognised
# and skipped; otherwise re-importing an older file would fail on the name check.
LEGACY_SAMPLE_ROW_KEYS = {("1", "John C Smith"), ("2", "Elvis D Grey")}
SAMPLE_ROW_KEYS = (
    {(row["Employee ID"], row["Name"]) for row in EMPLOYEE_MASTER_SAMPLE_ROWS} | LEGACY_SAMPLE_ROW_KEYS
)


def is_sample_row(row):
    """True for the illustrative rows the export adds, so import can skip them."""
    return (clean(row.get("Employee ID")), clean(row.get("Name"))) in SAMPLE_ROW_KEYS


def disabled_row_conflicts(employee, row):
    """Fields a disabled employee's import row tries to change, other than status.

    An export of the master carries everyone, leavers included, so re-importing an
    untouched file has to be a no-op rather than an error. Their status and last
    working day stay editable - a wrong leaving date has to be correctable, and it
    decides which payroll month they last belong to - but nothing else about someone
    off the payroll can be edited until they are enabled again.
    """
    conflicts = []

    def differs(column, current, parse=clean):
        if column not in row:
            return
        value = clean(row.get(column))
        if value and parse(value) != current:
            conflicts.append(column)

    differs("Department", employee.department or "")
    differs("Designation", employee.designation or "")
    if clean(row.get("Wage Type")) and normalize_salary_type(row.get("Wage Type")) != normalize_salary_type(employee.salary_type):
        conflicts.append("Wage Type")
    if clean(row.get("Salary")):
        try:
            if decimal_money(row.get("Salary")) != Decimal(employee.salary or 0):
                conflicts.append("Salary")
        except ValueError:
            conflicts.append("Salary")
    for key, label in SALARY_COMPONENTS + MONTHLY_ONLY_AMOUNTS:
        column = next((name for name in COMPONENT_COLUMN_ALIASES.get(key, (label,)) if name in row), None)
        if not column or not clean(row.get(column)):
            continue
        try:
            if decimal_money(row.get(column)) != Decimal(getattr(employee, key) or 0):
                conflicts.append(label)
        except ValueError:
            conflicts.append(label)
    for key, label in COMPLIANCE_FLAGS + DAILY_ONLY_FLAGS + (("ot_ignored", "Ignore OT"), ("less_hours_ignored", "Ignore Less Hours")):
        if label not in row or not clean(row.get(label)):
            continue
        try:
            if parse_yes_no(row.get(label), label) != bool(getattr(employee, key)):
                conflicts.append(label)
        except ValueError:
            conflicts.append(label)
    return conflicts


def apply_status_columns(employee, row, row_number, changes):
    """Read the Status and Last Working Day columns, if the file carries them.

    The two travel together: a leaver's final payroll month is decided by the date,
    so a status without one is rejected rather than half-applied.
    """
    if "Status" not in row or not clean(row.get("Status")):
        return
    requested = clean(row.get("Status")).upper()
    if requested not in EMPLOYMENT_STATUS_KEYS:
        raise ValueError(f"Row {row_number}: Status must be one of "
                         + ", ".join(key for key, _label in EMPLOYMENT_STATUSES) + ".")
    last_day = clean(row.get("Last Working Day"))
    if requested in DISABLED_STATUSES and not last_day:
        raise ValueError(f"Row {row_number}: Last Working Day is required to mark "
                         f"employee {employee.id} as {requested.lower()}.")
    parsed_day = None
    if last_day and requested != ACTIVE_STATUS:
        try:
            parsed_day = parse_csv_date(last_day)
        except ValueError as exc:
            raise ValueError(f"Row {row_number}: Last Working Day: {exc}") from exc
    if requested != (employee.employment_status or ACTIVE_STATUS):
        changes.append(f"Status {employee.employment_status or ACTIVE_STATUS} -> {requested}")
        employee.employment_status = requested
        employee.inactive_at = None if requested == ACTIVE_STATUS else datetime.utcnow()
    if parsed_day != employee.left_on:
        changes.append(f"Last Working Day {employee.left_on or 'Not Set'} -> {parsed_day or 'Not Set'}")
        employee.left_on = parsed_day


def apply_employee_master_import(rows, actor):
    changed = []
    created_ids = []
    skipped_samples = 0
    for row_number, row in enumerate(rows, start=2):
        # The export leads with illustrative rows; ignore them on the way back in so
        # an untouched export can be re-imported without error. They are counted so
        # the import message adds up to the number of lines in the file, which is
        # otherwise two short of what the reader can see.
        if is_sample_row(row):
            skipped_samples += 1
            continue
        employee_id = clean(row.get("Employee ID"))
        if not employee_id:
            raise ValueError(f"Row {row_number}: Employee ID is required.")
        imported_name = clean(row.get("Name"))
        wage_type = clean(row.get("Wage Type"))
        normalized_type = normalize_salary_type(wage_type)
        if wage_type and not normalized_type:
            raise ValueError(f"Row {row_number}: Wage type is required.")

        employee = db.session.get(Employee, employee_id)
        is_new = employee is None
        if is_new:
            # An unknown Employee ID creates the record, so a bulk file can onboard
            # new starters. Name and wage type are mandatory because nothing exists
            # to fall back on.
            if not imported_name:
                raise ValueError(f"Row {row_number}: Name is required to add new Employee ID {employee_id}.")
            if not normalized_type:
                raise ValueError(f"Row {row_number}: Wage type is required to add new Employee ID {employee_id}.")
            employee = Employee(id=employee_id, name=imported_name, employment_status=ACTIVE_STATUS)
            db.session.add(employee)
            db.session.flush()
            created_ids.append(f"{employee_id} - {imported_name}")
        elif employee.employment_status in DISABLED_STATUSES:
            conflicts = disabled_row_conflicts(employee, row)
            if conflicts:
                raise ValueError(
                    f"Row {row_number}: Disabled employee {employee_id} cannot be edited "
                    f"({', '.join(conflicts)}). Set them back to Active first."
                )
            status_changes = []
            apply_status_columns(employee, row, row_number, status_changes)
            if status_changes:
                db.session.add(employee)
                db.session.add(AuditLog(
                    actor=actor,
                    action="Employee Master Bulk Updated",
                    detail=f"{employee.id} - {employee.name}; " + " | ".join(status_changes),
                ))
                changed.append(employee)
            continue

        existing_type = normalize_salary_type(employee.salary_type)
        if existing_type and normalized_type and existing_type != normalized_type:
            raise ValueError(
                f"Row {row_number}: Wage type cannot be changed for active employee {employee_id}. "
                "Mark the employee as left or terminated first, then add a new Employee ID."
            )
        # Identity is fixed once a record exists: the ID is the match key and can never
        # change, and the name can only be corrected on the employee page.
        if not is_new and imported_name and imported_name != (employee.name or ""):
            raise ValueError(
                f"Row {row_number}: Employee ID {employee_id} is already named \"{employee.name}\". "
                f"Import cannot rename it to \"{imported_name}\". Edit the name on the employee page instead, "
                "or correct the name in the file to match."
            )

        salary = decimal_money(row.get("Salary"))
        if salary < 0:
            raise ValueError(f"Row {row_number}: Salary cannot be negative.")

        changes = []
        old_salary = Decimal(employee.salary or 0)
        # Department and designation are optional in the import: a column that is
        # absent leaves the stored value alone, so an older export still round-trips.
        for field, label in (("Department", "Department"), ("Designation", "Designation")):
            if field not in row:
                continue
            new_value = clean(row.get(field))
            if new_value != (getattr(employee, field.lower()) or ""):
                changes.append(f"{label} {getattr(employee, field.lower()) or 'Not Set'} -> {new_value or 'Not Set'}")
                setattr(employee, field.lower(), new_value)
        if normalized_type and not existing_type:
            changes.append(f"Wage Type {employee.salary_type or 'Not Set'} -> {wage_type}")
            employee.salary_type = wage_type
            employee.normalized_salary_type = normalized_type
        if old_salary != salary:
            changes.append(f"Salary {old_salary} -> {salary}")
            employee.salary = salary

        # Conveyance Allowance is gone; an old file carrying it would silently lose
        # that amount, so say so instead of importing a breakup that no longer adds up.
        for retired in RETIRED_IMPORT_COLUMNS:
            if clean(row.get(retired)) and decimal_money(row.get(retired) or 0) != 0:
                raise ValueError(
                    f"Row {row_number}: {retired} has been merged into Allowance and is no longer imported. "
                    f"Add its amount to Allowance and remove the {retired} column."
                )

        effective_type = normalize_salary_type(employee.salary_type)
        if effective_type == "MONTHLY":
            components = {}
            for key, label in SALARY_COMPONENTS:
                column = next((name for name in COMPONENT_COLUMN_ALIASES[key] if name in row), None)
                if column is None:
                    components[key] = Decimal(getattr(employee, key) or 0)
                    continue
                try:
                    value = decimal_money(row.get(column) or 0)
                except ValueError as exc:
                    raise ValueError(f"Row {row_number}: {exc}") from exc
                if value < 0:
                    raise ValueError(f"Row {row_number}: {label} cannot be negative.")
                components[key] = value
            validate_salary_breakup(employee.salary, components, label=f"Row {row_number}")
            for key, label in SALARY_COMPONENTS:
                if components[key] != Decimal(getattr(employee, key) or 0):
                    changes.append(f"{label} {Decimal(getattr(employee, key) or 0)} -> {components[key]}")
                    setattr(employee, key, components[key])
            for key, label in COMPLIANCE_FLAGS:
                if label not in row:
                    continue
                try:
                    flag = parse_yes_no(row.get(label), f"Row {row_number}: {label}")
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                if flag != bool(getattr(employee, key)):
                    changes.append(f"{label} {'Yes' if getattr(employee, key) else 'No'} -> {'Yes' if flag else 'No'}")
                    setattr(employee, key, flag)
            for key, label in MONTHLY_ONLY_AMOUNTS:
                if label not in row:
                    continue
                try:
                    amount = decimal_money(row.get(label) or 0)
                except ValueError as exc:
                    raise ValueError(f"Row {row_number}: {exc}") from exc
                if amount < 0:
                    raise ValueError(f"Row {row_number}: {label} cannot be negative.")
                if amount != Decimal(getattr(employee, key) or 0):
                    changes.append(f"{label} {Decimal(getattr(employee, key) or 0)} -> {amount}")
                    setattr(employee, key, amount)
            for _key, label in DAILY_ONLY_FLAGS:
                if clean(row.get(label)):
                    raise ValueError(
                        f"Row {row_number}: {label} only applies to daily wage employees. "
                        f"Employee {employee_id} is {employee.salary_type or 'not set'}."
                    )
        else:
            # Reject breakup values on a daily wage row instead of silently dropping them.
            for key, label in SALARY_COMPONENTS:
                column = next((name for name in COMPONENT_COLUMN_ALIASES[key] if name in row), None)
                if column and clean(row.get(column)) and decimal_money(row.get(column) or 0) != 0:
                    raise ValueError(
                        f"Row {row_number}: {label} only applies to monthly wage employees. "
                        f"Employee {employee_id} is {employee.salary_type or 'not set'}."
                    )
            for _key, label in COMPLIANCE_FLAGS:
                if clean(row.get(label)):
                    raise ValueError(
                        f"Row {row_number}: {label} only applies to monthly wage employees. "
                        f"Employee {employee_id} is {employee.salary_type or 'not set'}."
                    )
            for _key, label in MONTHLY_ONLY_AMOUNTS:
                # An exported daily row carries a blank cell here, so only a real
                # amount is an error.
                if clean(row.get(label)) and decimal_money(row.get(label) or 0) != 0:
                    raise ValueError(
                        f"Row {row_number}: {label} only applies to monthly wage employees. "
                        f"Employee {employee_id} is {employee.salary_type or 'not set'}."
                    )
            for key, label in DAILY_ONLY_FLAGS:
                if label not in row:
                    continue
                flag = parse_yes_no(row.get(label), f"Row {row_number}: {label}")
                if flag != bool(getattr(employee, key)):
                    changes.append(f"{label} {'Yes' if getattr(employee, key) else 'No'} -> {'Yes' if flag else 'No'}")
                    setattr(employee, key, flag)

        apply_status_columns(employee, row, row_number, changes)

        for field, label in (("ot_ignored", "Ignore OT"), ("less_hours_ignored", "Ignore Less Hours")):
            if label not in row:
                continue
            flag = parse_yes_no(row.get(label), f"Row {row_number}: {label}")
            if flag != bool(getattr(employee, field)):
                changes.append(f"{label} {'Yes' if getattr(employee, field) else 'No'} -> {'Yes' if flag else 'No'}")
                setattr(employee, field, flag)

        if is_new:
            created_defaults = ensure_employee_defaults(employee_id)
            if created_defaults:
                changes.append(f"Defaults created: {', '.join(created_defaults)}")
        if not changes and not is_new:
            continue
        db.session.add(employee)
        db.session.add(AuditLog(
            actor=actor,
            action="Employee Master Bulk Created" if is_new else "Employee Master Bulk Updated",
            detail=f"{employee.id} - {employee.name}; " + " | ".join(changes or ["Created by import"]),
        ))
        changed.append(employee)
    if changed:
        db.session.add(AuditLog(
            actor=actor,
            action="Employee Master Imported",
            detail=(
                f"{len(created_ids)} added, {len(changed) - len(created_ids)} updated by bulk import."
                + (f" Added: {', '.join(created_ids)}" if created_ids else "")
            ),
        ))
    db.session.flush()
    return changed, created_ids, skipped_samples


def save_master_employee(form, actor):
    employee_id = clean(form.get("employee_id"))
    if not employee_id:
        raise ValueError("Employee ID is required.")
    existing = db.session.get(Employee, employee_id)
    # A non-active employee has every field but the status dropdown disabled, so the
    # browser posts only the status. Fall back to what is stored for the rest.
    name = clean(form.get("name")) or (existing.name if existing else "")
    if not name:
        raise ValueError("Employee name is required.")
    salary_type = clean(form.get("wage_type") or form.get("salary_type")) or (existing.salary_type if existing else "")
    normalized_type = normalize_salary_type(salary_type)
    if not normalized_type:
        raise ValueError("Wage type is required.")
    salary = decimal_money(form.get("salary")) if clean(form.get("salary")) else Decimal(existing.salary or 0) if existing else Decimal("0")
    if salary < 0:
        raise ValueError("Salary cannot be negative.")
    employee = existing
    created = employee is None
    if not employee:
        employee = Employee(id=employee_id, name=name)
    requested_status = clean(form.get("employment_status")).upper() or None
    if requested_status and requested_status not in EMPLOYMENT_STATUS_KEYS:
        raise ValueError(f"Unknown employment status: {form.get('employment_status')}")
    if employee.employment_status in DISABLED_STATUSES and requested_status is None:
        raise ValueError("Disabled employees cannot be edited. Set the status back to Active first.")
    existing_type = normalize_salary_type(employee.salary_type)
    if existing_type and existing_type != normalized_type:
        raise ValueError("Wage type cannot be changed for an active employee. Mark the employee as left or terminated first, then add the same employee under a new ID with the updated wage group.")
    old_values = {
        "name": employee.name,
        "salary_type": employee.salary_type,
        "salary": Decimal(employee.salary or 0),
        "department": employee.department or "",
        "designation": employee.designation or "",
        "employment_status": employee.employment_status or ACTIVE_STATUS,
        "ot_ignored": bool(employee.ot_ignored),
        "less_hours_ignored": bool(employee.less_hours_ignored),
        **{key: Decimal(getattr(employee, key) or 0) for key, _ in SALARY_COMPONENTS},
        **{key: bool(getattr(employee, key)) for key, _ in COMPLIANCE_FLAGS},
        **{key: Decimal(getattr(employee, key) or 0) for key, _ in MONTHLY_ONLY_AMOUNTS},
        **{key: bool(getattr(employee, key)) for key, _ in DAILY_ONLY_FLAGS},
    }

    controls_present = "master_controls_present" in form

    # The salary breakup and compliance flags only apply to monthly wage employees.
    if normalized_type == "MONTHLY":
        components = {}
        for key, label in SALARY_COMPONENTS:
            if key not in form:
                components[key] = Decimal(getattr(employee, key) or 0)
                continue
            value = decimal_money(form.get(key) or 0)
            if value < 0:
                raise ValueError(f"{label} cannot be negative.")
            components[key] = value
        validate_salary_breakup(salary, components)
        for key, value in components.items():
            setattr(employee, key, value)
        for key, label in COMPLIANCE_FLAGS:
            if controls_present or key in form:
                setattr(employee, key, form.get(key) == "on")
        for key, label in MONTHLY_ONLY_AMOUNTS:
            if key in form:
                amount = decimal_money(form.get(key) or 0)
                if amount < 0:
                    raise ValueError(f"{label} cannot be negative.")
                setattr(employee, key, amount)
        # The attendance bonus is a daily wage rule, so a monthly record never carries
        # the opt-out, even if a stale form field posts one.
        for key, _ in DAILY_ONLY_FLAGS:
            setattr(employee, key, False)
    else:
        # Daily wage has no breakup or statutory deductions; keep the columns clean.
        for key, _ in SALARY_COMPONENTS:
            setattr(employee, key, Decimal("0"))
        for key, _ in COMPLIANCE_FLAGS:
            setattr(employee, key, False)
        for key, _ in MONTHLY_ONLY_AMOUNTS:
            setattr(employee, key, Decimal("0"))
        for key, _ in DAILY_ONLY_FLAGS:
            if controls_present or key in form:
                setattr(employee, key, form.get(key) == "on")
    employee.name = name
    employee.salary_type = salary_type
    employee.normalized_salary_type = normalized_type
    employee.salary = salary
    # Department and designation arrive with the attendance sheet, but the form is
    # the manual source of truth, so a blank field here clears the stored value
    # rather than being ignored.
    if "department" in form:
        employee.department = clean(form.get("department"))
    if "designation" in form:
        employee.designation = clean(form.get("designation"))
    if controls_present or "ot_ignored" in form:
        employee.ot_ignored = form.get("ot_ignored") == "on"
    elif created:
        employee.ot_ignored = False
    if controls_present or "less_hours_ignored" in form:
        employee.less_hours_ignored = form.get("less_hours_ignored") == "on"
    elif created:
        employee.less_hours_ignored = False
    old_status = employee.employment_status or ACTIVE_STATUS
    new_status = requested_status or (ACTIVE_STATUS if created else old_status)
    employee.employment_status = new_status
    if new_status == ACTIVE_STATUS:
        employee.left_on = None
        employee.inactive_at = None
        employee.inactive_reason = None
    elif new_status != old_status:
        employee.inactive_at = datetime.utcnow()
        employee.inactive_reason = clean(form.get("inactive_reason")) or None
    db.session.add(employee)
    created_defaults = ensure_employee_defaults(employee_id)
    action = "Employee Master Created" if created else "Employee Master Updated"
    changes = []
    if not created:
        if old_values["name"] != employee.name:
            changes.append(f"Name {old_values['name']} -> {employee.name}")
        if old_values["salary_type"] != employee.salary_type:
            changes.append(f"Wage Type {old_values['salary_type']} -> {employee.salary_type}")
        if old_values["salary"] != employee.salary:
            changes.append(f"Salary {old_values['salary']} -> {employee.salary}")
        if old_values["department"] != (employee.department or ""):
            changes.append(f"Department {old_values['department'] or 'Not Set'} -> {employee.department or 'Not Set'}")
        if old_values["designation"] != (employee.designation or ""):
            changes.append(f"Designation {old_values['designation'] or 'Not Set'} -> {employee.designation or 'Not Set'}")
        if old_values["ot_ignored"] != employee.ot_ignored:
            changes.append(f"Ignore OT {'Yes' if old_values['ot_ignored'] else 'No'} -> {'Yes' if employee.ot_ignored else 'No'}")
        if old_values["less_hours_ignored"] != employee.less_hours_ignored:
            changes.append(f"Ignore Less Hours {'Yes' if old_values['less_hours_ignored'] else 'No'} -> {'Yes' if employee.less_hours_ignored else 'No'}")
        for key, label in SALARY_COMPONENTS + MONTHLY_ONLY_AMOUNTS:
            new_value = Decimal(getattr(employee, key) or 0)
            if old_values[key] != new_value:
                changes.append(f"{label} {old_values[key]} -> {new_value}")
        if old_values["employment_status"] != employee.employment_status:
            changes.append(f"Status {old_values['employment_status']} -> {employee.employment_status}")
        for key, label in COMPLIANCE_FLAGS + DAILY_ONLY_FLAGS:
            if old_values[key] != bool(getattr(employee, key)):
                changes.append(f"{label} {'Yes' if old_values[key] else 'No'} -> {'Yes' if getattr(employee, key) else 'No'}")
    detail = (
        f"{employee_id} - {name}; Wage Type {salary_type}; Salary {salary}; "
        f"Department {employee.department or 'Not Set'}; Designation {employee.designation or 'Not Set'}; "
        f"Ignore OT {'Yes' if employee.ot_ignored else 'No'}; Ignore Less Hours {'Yes' if employee.less_hours_ignored else 'No'}; "
        f"PF {'Yes' if employee.pf_enabled else 'No'}; ESIC {'Yes' if employee.esic_enabled else 'No'}; "
        f"TDS {Decimal(employee.tds or 0)}; "
        f"Ignore Monthly Bonus {'Yes' if employee.bonus_ignored else 'No'}"
    )
    if changes:
        detail += "; Changes: " + " | ".join(changes)
    if created_defaults:
        detail += f"; Defaults created: {', '.join(created_defaults)}"
    db.session.add(AuditLog(actor=actor, action=action, detail=detail))
    return employee


def disable_master_employee(employee_id, status, reason, confirmation, actor, left_on=None):
    status = clean(status).upper()
    if status not in DISABLED_STATUSES:
        raise ValueError("Select Left or Terminated.")
    if clean(confirmation).lower() != DISABLE_CONFIRMATION_TEXT:
        raise ValueError('Type "confirm" to disable this employee.')
    employee = db.session.get(Employee, employee_id)
    if not employee:
        raise ValueError("Employee was not found.")
    # The last working day decides the final month they are paid for, so it is
    # required rather than optional: without it they drop out of payroll entirely.
    if not clean(left_on):
        raise ValueError("Enter the employee's last working day. It decides the last month they are paid for.")
    try:
        last_day = parse_csv_date(left_on)
    except ValueError as exc:
        raise ValueError(f"Last working day: {exc}") from exc
    employee.employment_status = status
    employee.left_on = last_day
    employee.inactive_at = datetime.utcnow()
    employee.inactive_reason = clean(reason)
    db.session.add(employee)
    db.session.add(AuditLog(
        actor=actor,
        action="Employee Master Disabled",
        detail=(f"{employee.id} - {employee.name}; Status {status}; "
                f"Last working day {last_day.strftime('%d-%m-%Y')}; {employee.inactive_reason or 'No reason'}"),
    ))
    return employee


def enable_master_employee(employee_id, confirmation, actor):
    if clean(confirmation).lower() != DISABLE_CONFIRMATION_TEXT:
        raise ValueError('Type "confirm" to enable this employee.')
    employee = db.session.get(Employee, employee_id)
    if not employee:
        raise ValueError("Employee was not found.")
    if employee.employment_status == ACTIVE_STATUS:
        raise ValueError("Employee is already active.")
    old_status = employee.employment_status
    old_reason = employee.inactive_reason
    employee.employment_status = ACTIVE_STATUS
    employee.left_on = None
    employee.inactive_at = None
    employee.inactive_reason = None
    db.session.add(employee)
    db.session.add(AuditLog(
        actor=actor,
        action="Employee Master Enabled",
        detail=f"{employee.id} - {employee.name}; Status {old_status} -> ACTIVE; Previous reason: {old_reason or 'No reason'}",
    ))
    return employee


def sync_salary_records_from_master(month, actor):
    """Copy wage data from Employee Master into the month's salary records.

    Returns (created, updated, skipped) where `skipped` lists each employee left
    out and why, so a silent "3 skipped" never hides a payroll gap.
    """
    created = 0
    updated = 0
    skipped = []
    for employee in sorted(Employee.query.all(), key=employee_sort_value):
        label = f"{employee.id} - {employee.name}"
        if not employee_active_for_payroll_month(employee, month):
            skipped.append(f"{label}: status is {(employee.employment_status or 'unknown').title()}")
            continue
        if not employee.normalized_salary_type:
            skipped.append(f"{label}: no wage type set")
            continue
        if Decimal(employee.salary or 0) <= 0:
            skipped.append(f"{label}: salary is zero")
            continue
        salary = SalaryRecord.query.filter_by(payroll_month=month, employee_id=employee.id).first()
        if salary:
            updated += 1
        else:
            salary = SalaryRecord(payroll_month=month, employee_id=employee.id, adjustment=0, loan=0)
            created += 1
        salary.name = employee.name
        salary.salary_type = employee.salary_type
        salary.normalized_salary_type = employee.normalized_salary_type
        salary.salary = employee.salary
        salary.warning = ""
        db.session.add(salary)
    detail = f"{month}: {created} created; {updated} updated; {len(skipped)} skipped"
    if skipped:
        detail += " | Skipped: " + " | ".join(skipped)
    db.session.add(AuditLog(actor=actor, action="Wage Master Loaded", detail=detail))
    db.session.flush()
    return created, updated, skipped
