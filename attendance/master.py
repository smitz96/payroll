from datetime import datetime
from decimal import Decimal

from attendance import db
from attendance.employee_defaults import ensure_employee_defaults
from attendance.models import AuditLog, Employee, SalaryRecord
from attendance.utils import clean, decimal_money, normalize_salary_type

ACTIVE_STATUS = "ACTIVE"
DISABLED_STATUSES = {"LEFT", "TERMINATED"}
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
    if not employee:
        return True
    if employee.employment_status == ACTIVE_STATUS:
        return True
    if employee.employment_status not in DISABLED_STATUSES:
        return True
    if not employee.inactive_at:
        return False
    inactive_month = employee.inactive_at.strftime("%Y-%m")
    return str(payroll_month or "") <= inactive_month


def employee_master_export_rows():
    rows = []
    for employee in sorted(Employee.query.all(), key=employee_sort_value):
        monthly = normalize_salary_type(employee.salary_type) == "MONTHLY"
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
            "PF": ("Yes" if employee.pf_enabled else "No") if monthly else "",
            "ESIC": ("Yes" if employee.esic_enabled else "No") if monthly else "",
            "Ignore OT": "Yes" if employee.ot_ignored else "No",
            "Ignore Less Hours": "Yes" if employee.less_hours_ignored else "No",
        })
    return list(EMPLOYEE_MASTER_SAMPLE_ROWS) + rows


EMPLOYEE_MASTER_EXPORT_COLUMNS = [
    "Employee ID", "Name", "Department", "Designation", "Wage Type", "Salary",
    "Basic", "HRA", "Allowance", "PF", "ESIC", "Ignore OT", "Ignore Less Hours",
]

# Illustrative rows shipped at the top of the export so the expected shape of every
# column is obvious without opening the docs. They are not database records: the
# import skips any row matching one of them, so an exported file round-trips safely.
EMPLOYEE_MASTER_SAMPLE_ROWS = [
    {
        "Employee ID": "1", "Name": "John C Smith", "Department": "Accounts",
        "Designation": "Accounts Executive", "Wage Type": "Monthly", "Salary": "50000",
        "Basic": "35000", "HRA": "10000", "Allowance": "5000",
        "PF": "Yes", "ESIC": "No", "Ignore OT": "Yes", "Ignore Less Hours": "No",
    },
    {
        "Employee ID": "2", "Name": "Elvis D Grey", "Department": "Mechanical Production",
        "Designation": "Helper", "Wage Type": "Daily", "Salary": "5000",
        "Basic": "0", "HRA": "0", "Allowance": "0",
        "PF": "No", "ESIC": "No", "Ignore OT": "Yes", "Ignore Less Hours": "No",
    },
]
SAMPLE_ROW_KEYS = {(row["Employee ID"], row["Name"]) for row in EMPLOYEE_MASTER_SAMPLE_ROWS}


def is_sample_row(row):
    """True for the illustrative rows the export adds, so import can skip them."""
    return (clean(row.get("Employee ID")), clean(row.get("Name"))) in SAMPLE_ROW_KEYS


def apply_employee_master_import(rows, actor):
    changed = []
    for row_number, row in enumerate(rows, start=2):
        # The export leads with illustrative rows; ignore them on the way back in so
        # an untouched export can be re-imported without error.
        if is_sample_row(row):
            continue
        employee_id = clean(row.get("Employee ID"))
        if not employee_id:
            raise ValueError(f"Row {row_number}: Employee ID is required.")
        employee = db.session.get(Employee, employee_id)
        if not employee:
            raise ValueError(f"Row {row_number}: Employee ID {employee_id} was not found.")
        if employee.employment_status in DISABLED_STATUSES:
            raise ValueError(f"Row {row_number}: Disabled employee {employee_id} cannot be edited.")

        wage_type = clean(row.get("Wage Type"))
        normalized_type = normalize_salary_type(wage_type)
        existing_type = normalize_salary_type(employee.salary_type)
        if wage_type and not normalized_type:
            raise ValueError(f"Row {row_number}: Wage type is required.")
        if existing_type and normalized_type and existing_type != normalized_type:
            raise ValueError(
                f"Row {row_number}: Wage type cannot be changed for active employee {employee_id}. "
                "Mark the employee as left or terminated first, then add a new Employee ID."
            )
        # Identity is fixed once a record exists: the ID is the match key and the name
        # can only be corrected on the employee page, never silently by a bulk file.
        imported_name = clean(row.get("Name"))
        if imported_name and imported_name != (employee.name or ""):
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

        for field, label in (("ot_ignored", "Ignore OT"), ("less_hours_ignored", "Ignore Less Hours")):
            if label not in row:
                continue
            flag = parse_yes_no(row.get(label), f"Row {row_number}: {label}")
            if flag != bool(getattr(employee, field)):
                changes.append(f"{label} {'Yes' if getattr(employee, field) else 'No'} -> {'Yes' if flag else 'No'}")
                setattr(employee, field, flag)

        if not changes:
            continue
        db.session.add(employee)
        db.session.add(AuditLog(
            actor=actor,
            action="Employee Master Bulk Updated",
            detail=f"{employee.id} - {employee.name}; " + " | ".join(changes),
        ))
        changed.append(employee)
    if changed:
        db.session.add(AuditLog(
            actor=actor,
            action="Employee Master Imported",
            detail=f"{len(changed)} employee master row(s) updated by bulk import.",
        ))
    db.session.flush()
    return changed


def save_master_employee(form, actor):
    employee_id = clean(form.get("employee_id"))
    if not employee_id:
        raise ValueError("Employee ID is required.")
    name = clean(form.get("name"))
    if not name:
        raise ValueError("Employee name is required.")
    salary_type = clean(form.get("wage_type") or form.get("salary_type"))
    normalized_type = normalize_salary_type(salary_type)
    if not normalized_type:
        raise ValueError("Wage type is required.")
    salary = decimal_money(form.get("salary"))
    if salary < 0:
        raise ValueError("Salary cannot be negative.")
    employee = db.session.get(Employee, employee_id)
    created = employee is None
    if not employee:
        employee = Employee(id=employee_id, name=name)
    if employee.employment_status in DISABLED_STATUSES:
        raise ValueError("Disabled employees cannot be edited. Create a new employee record if needed.")
    existing_type = normalize_salary_type(employee.salary_type)
    if existing_type and existing_type != normalized_type:
        raise ValueError("Wage type cannot be changed for an active employee. Mark the employee as left or terminated first, then add the same employee under a new ID with the updated wage group.")
    old_values = {
        "name": employee.name,
        "salary_type": employee.salary_type,
        "salary": Decimal(employee.salary or 0),
        "department": employee.department or "",
        "designation": employee.designation or "",
        "ot_ignored": bool(employee.ot_ignored),
        "less_hours_ignored": bool(employee.less_hours_ignored),
        **{key: Decimal(getattr(employee, key) or 0) for key, _ in SALARY_COMPONENTS},
        **{key: bool(getattr(employee, key)) for key, _ in COMPLIANCE_FLAGS},
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
    else:
        # Daily wage has no breakup or statutory deductions; keep the columns clean.
        for key, _ in SALARY_COMPONENTS:
            setattr(employee, key, Decimal("0"))
        for key, _ in COMPLIANCE_FLAGS:
            setattr(employee, key, False)
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
    employee.employment_status = ACTIVE_STATUS
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
        for key, label in SALARY_COMPONENTS:
            new_value = Decimal(getattr(employee, key) or 0)
            if old_values[key] != new_value:
                changes.append(f"{label} {old_values[key]} -> {new_value}")
        for key, label in COMPLIANCE_FLAGS:
            if old_values[key] != bool(getattr(employee, key)):
                changes.append(f"{label} {'Yes' if old_values[key] else 'No'} -> {'Yes' if getattr(employee, key) else 'No'}")
    detail = (
        f"{employee_id} - {name}; Wage Type {salary_type}; Salary {salary}; "
        f"Department {employee.department or 'Not Set'}; Designation {employee.designation or 'Not Set'}; "
        f"Ignore OT {'Yes' if employee.ot_ignored else 'No'}; Ignore Less Hours {'Yes' if employee.less_hours_ignored else 'No'}; "
        f"PF {'Yes' if employee.pf_enabled else 'No'}; ESIC {'Yes' if employee.esic_enabled else 'No'}"
    )
    if changes:
        detail += "; Changes: " + " | ".join(changes)
    if created_defaults:
        detail += f"; Defaults created: {', '.join(created_defaults)}"
    db.session.add(AuditLog(actor=actor, action=action, detail=detail))
    return employee


def disable_master_employee(employee_id, status, reason, confirmation, actor):
    status = clean(status).upper()
    if status not in DISABLED_STATUSES:
        raise ValueError("Select Left or Terminated.")
    if clean(confirmation).lower() != DISABLE_CONFIRMATION_TEXT:
        raise ValueError('Type "confirm" to disable this employee.')
    employee = db.session.get(Employee, employee_id)
    if not employee:
        raise ValueError("Employee was not found.")
    employee.employment_status = status
    employee.inactive_at = datetime.utcnow()
    employee.inactive_reason = clean(reason)
    db.session.add(employee)
    db.session.add(AuditLog(actor=actor, action="Employee Master Disabled", detail=f"{employee.id} - {employee.name}; Status {status}; {employee.inactive_reason or 'No reason'}"))
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
    created = 0
    updated = 0
    skipped = 0
    for employee in sorted(Employee.query.all(), key=employee_sort_value):
        if not employee_active_for_payroll_month(employee, month):
            skipped += 1
            continue
        if not employee.normalized_salary_type or Decimal(employee.salary or 0) <= 0:
            skipped += 1
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
    db.session.add(AuditLog(actor=actor, action="Wage Master Loaded", detail=f"{month}: {created} created; {updated} updated; {skipped} skipped"))
    db.session.flush()
    return created, updated, skipped
