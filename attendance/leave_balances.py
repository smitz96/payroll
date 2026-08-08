from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from attendance import db
from attendance.models import AuditLog, Employee, LeaveLedger, PayrollMonth, PayrollResult, SalaryRecord


def format_leave(value):
    return Decimal(value or 0).quantize(Decimal("0.1"))


def parse_leave_balance(value):
    text = str(value).strip()
    if not text:
        raise ValueError("Leave balance cannot be blank.")
    try:
        balance = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid leave balance: {value}") from exc
    if balance < 0:
        raise ValueError("Leave balance cannot be negative.")
    return balance.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def latest_payroll_result(employee_id):
    return (
        PayrollResult.query.filter_by(employee_id=employee_id, payroll_rule_type="MONTHLY")
        .order_by(PayrollResult.payroll_month.desc(), PayrollResult.created_at.desc())
        .first()
    )


def latest_salary_record(employee_id):
    return (
        SalaryRecord.query.filter_by(employee_id=employee_id)
        .order_by(SalaryRecord.payroll_month.desc(), SalaryRecord.id.desc())
        .first()
    )


def manual_adjustments_after(employee_id, result):
    query = LeaveLedger.query.filter_by(employee_id=employee_id, transaction_type="MANUAL_ADJUSTMENT")
    if result:
        query = query.filter(LeaveLedger.created_at > result.created_at)
    return query.order_by(LeaveLedger.created_at.asc()).all()


def stored_leave_balance(employee_id):
    result = latest_payroll_result(employee_id)
    base = Decimal(result.closing_leave) if result else Decimal("0")
    for adjustment in manual_adjustments_after(employee_id, result):
        base += Decimal(adjustment.amount)
    return format_leave(base)


def leave_balance_rows():
    rows = []
    for employee in Employee.query.order_by(Employee.id.asc()).all():
        salary = latest_salary_record(employee.id)
        result = latest_payroll_result(employee.id)
        current = stored_leave_balance(employee.id)
        open_result = (
            db.session.query(PayrollResult)
            .join(PayrollMonth, PayrollMonth.month == PayrollResult.payroll_month)
            .filter(PayrollResult.employee_id == employee.id, PayrollMonth.status != "FINALIZED")
            .order_by(PayrollResult.payroll_month.desc())
            .first()
        )
        notes = []
        if open_result:
            notes.append("Open payroll calculation exists; recalculation may be required.")
        rows.append({
            "employee": employee,
            "salary": salary,
            "last_result": result,
            "opening_leave": format_leave(result.opening_leave) if result else Decimal("0.0"),
            "leave_earned": format_leave(result.leave_earned) if result else Decimal("0.0"),
            "leave_used": format_leave(result.leave_used) if result else Decimal("0.0"),
            "leave_encashed": format_leave(getattr(result, "leave_encashment_days", 0)) if result else Decimal("0.0"),
            "previous_closing_leave": format_leave(result.closing_leave) if result else None,
            "current_balance": current,
            "last_payroll_month": result.payroll_month if result else None,
            "salary_type": salary.salary_type if salary else "",
            "normalized_salary_type": salary.normalized_salary_type if salary else "",
            "notes": " ".join(notes),
        })
    return rows


def leave_balance_export_rows():
    rows = []
    for row in leave_balance_rows():
        employee = row["employee"]
        rows.append({
            "Employee ID": employee.id,
            "Employee Name": employee.name,
            "Current Leave Balance": row["current_balance"],
        })
    return rows


def apply_leave_balance_import(rows, username):
    changes = []
    for row_number, row in enumerate(rows, start=2):
        employee_id = str(row.get("Employee ID") or "").strip()
        if not employee_id:
            raise ValueError(f"Row {row_number}: Employee ID is required.")
        if not db.session.get(Employee, employee_id):
            raise ValueError(f"Row {row_number}: Employee ID {employee_id} was not found.")
        changes.append({
            "employee_id": employee_id,
            "new_balance": row.get("Current Leave Balance"),
            "reason": "Bulk leave balance import",
        })
    changed = apply_leave_balance_updates(changes, username, commit=False)
    if changed:
        db.session.add(AuditLog(
            actor=username,
            action="Leave Balance Imported",
            detail=f"{len(changed)} leave balance row(s) updated by bulk import.",
        ))
    db.session.commit()
    return changed


def apply_leave_balance_updates(changes, username, commit=True):
    changed = []
    today = date.today()
    try:
        for change in changes:
            employee = db.session.get(Employee, change["employee_id"])
            if not employee:
                raise ValueError(f"Employee ID {change['employee_id']} was not found.")
            old_balance = stored_leave_balance(employee.id)
            new_balance = parse_leave_balance(change["new_balance"])
            reason = str(change.get("reason") or "").strip()
            if new_balance == old_balance:
                continue
            if not reason:
                raise ValueError(f"Reason is required for Employee ID {employee.id}.")
            difference = (new_balance - old_balance).quantize(Decimal("0.1"))
            description = (
                f"Manual Leave Balance Correction | Previous Balance: {old_balance} | "
                f"New Balance: {new_balance} | Difference: {difference:+} | "
                f"Reason: {reason} | Changed By: {username} | Source: Leave Balance Management"
            )
            db.session.add(LeaveLedger(
                employee_id=employee.id,
                date=today,
                payroll_month="MANUAL",
                transaction_type="MANUAL_ADJUSTMENT",
                amount=difference,
                description=description,
            ))
            db.session.add(AuditLog(
                actor=username,
                action="LEAVE_BALANCE_MANUAL_UPDATE",
                detail=(
                    f"Employee ID: {employee.id}; Employee Name: {employee.name}; "
                    f"Old Leave Balance: {old_balance}; New Leave Balance: {new_balance}; "
                    f"Difference: {difference:+}; Reason: {reason}; Source: Leave Balance Management"
                ),
            ))
            changed.append((employee, old_balance, new_balance, difference))
        if commit:
            db.session.commit()
        return changed
    except Exception:
        db.session.rollback()
        raise


def leave_history(employee_id):
    items = LeaveLedger.query.filter_by(employee_id=employee_id).order_by(LeaveLedger.created_at.asc(), LeaveLedger.id.asc()).all()
    balance = Decimal("0")
    rows = []
    for item in items:
        amount = Decimal(item.amount)
        if item.transaction_type in {"OPENING", "CARRY_FORWARD"}:
            balance = amount
        elif item.transaction_type in {"USED", "ENCASHED"}:
            balance -= amount
        else:
            balance += amount
        rows.append({"item": item, "balance_after": format_leave(balance)})
    return list(reversed(rows))
