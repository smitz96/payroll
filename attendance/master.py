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
        rows.append({
            "Employee ID": employee.id,
            "Name": employee.name,
            "Wage Type": employee.salary_type or "",
            "Salary": employee.salary or Decimal("0"),
        })
    return rows


def apply_employee_master_import(rows, actor):
    changed = []
    for row_number, row in enumerate(rows, start=2):
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
        salary = decimal_money(row.get("Salary"))
        if salary < 0:
            raise ValueError(f"Row {row_number}: Salary cannot be negative.")

        changes = []
        old_salary = Decimal(employee.salary or 0)
        if normalized_type and not existing_type:
            changes.append(f"Wage Type {employee.salary_type or 'Not Set'} -> {wage_type}")
            employee.salary_type = wage_type
            employee.normalized_salary_type = normalized_type
        if old_salary != salary:
            changes.append(f"Salary {old_salary} -> {salary}")
            employee.salary = salary
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
        "ot_enabled": bool(employee.ot_enabled),
        "less_hours_exempt": bool(employee.less_hours_exempt),
    }
    employee.name = name
    employee.salary_type = salary_type
    employee.normalized_salary_type = normalized_type
    employee.salary = salary
    controls_present = "master_controls_present" in form
    if controls_present or "ot_enabled" in form:
        employee.ot_enabled = form.get("ot_enabled") == "on"
    elif created:
        employee.ot_enabled = True
    if controls_present or "less_hours_exempt" in form:
        employee.less_hours_exempt = form.get("less_hours_exempt") == "on"
    elif created:
        employee.less_hours_exempt = False
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
        if old_values["ot_enabled"] != employee.ot_enabled:
            changes.append(f"OT Eligible {'Yes' if old_values['ot_enabled'] else 'No'} -> {'Yes' if employee.ot_enabled else 'No'}")
        if old_values["less_hours_exempt"] != employee.less_hours_exempt:
            changes.append(f"Less Hours Deduction {'Ignored' if old_values['less_hours_exempt'] else 'Applied'} -> {'Ignored' if employee.less_hours_exempt else 'Applied'}")
    detail = f"{employee_id} - {name}; Wage Type {salary_type}; Salary {salary}; OT {'Enabled' if employee.ot_enabled else 'Disabled'}; Less Hours {'Ignored' if employee.less_hours_exempt else 'Deducted'}"
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
