# SMARTfill Operations

## Initial Login

```text
Username: admin
Password: 12345
```

Change the password immediately:

```text
Settings -> Security -> Change Password
```

## Payroll Workflow

1. Create or open payroll month.
2. Import attendance in Bulk Attendance Manager.
3. Review and submit attendance.
4. Confirm week offs and holidays.
5. Maintain employee wage data in Employees.
6. Calculate payroll.
7. Review employee detail, leave encashment, loans, advances, and manual changes.
8. Use Recheck Holidays if holidays were added after attendance import or payroll calculation.
9. Finalize payroll when complete.
10. Open PDF reports from Reports or Payroll Month.

## Holiday Recheck

Use this when a holiday is added after attendance upload or payroll calculation:

```text
Payroll Month -> Recheck Holidays
```

This reruns payroll against the current holiday calendar, keeps manual employee payroll changes, and logs the action.

## Locked Payroll

Finalized payroll months are locked. Unlock requires the admin password and is recorded in Activity Logs.

## Backup & Restore

Before maintenance or server replacement, download a full backup:

```text
Settings -> Backup & restore -> Download backup
```

The ZIP includes the SQLite database, uploaded attendance/register files, and generated files from `output/`.

To recover on a new server:

1. Install and start SMARTfill.
2. Sign in as admin.
3. Open `Settings -> Backup & restore -> Restore backup`.
4. Upload the SMARTfill backup ZIP.
5. Enter the admin password and type `restore backup`.

Restore replaces the current server data. Keep a fresh backup of the current server before restoring over it.

## Git Pull Updates

Use:

```text
Settings -> Server Update -> Git Pull Update
```

The server must already have GitHub access. If templates, Python files, or CSS changed, restart the service:

```bash
sudo systemctl restart smartfill-attendance
```
