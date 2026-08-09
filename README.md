# SMARTfill Attendance & Payroll Management

SMARTfill is a local Flask and SQLite web application for importing monthly attendance, maintaining employee wages, calculating Monthly and Daily payroll, preserving leave balances, and opening auditable payroll PDF reports.

Current supported automatic payroll rules:

```text
Wage Type = Monthly
Wage Type = Daily
```

All other wage types are imported, preserved, displayed, and marked as `Payroll Rules Not Configured`. They are never calculated with Monthly or Daily rules and never shown as a zero final salary.

## Initial Login

```text
Username: admin
Password: 12345
```

Change this from `Settings -> Security -> Change Password`. Passwords are stored with Werkzeug password hashing.

## Local Setup

```bash
cd "Payroll WebApp"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
python app.py
```

Open:

```text
http://localhost:3000
```

## CSV Formats

Attendance CSV columns:

```text
Employee ID,Employee Name,Department,Designation,Date,Day,Shift,From,To,Break From,Break To,First Punch,Last Punch,Total Working Hours,Total Break Hours
```

Dates are `DD-MM-YYYY`. Durations such as `8h 57m` are converted to integer minutes.

Legacy Salary CSV columns:

```text
ID,Name,Type,Salary,Adjustment
```

Aliases supported:

```text
ID -> Employee ID
Adjustment -> Manual Adjustment
```

Wage type is normalized with `strip().upper()`. `MONTHLY` and `DAILY` resolve to configured payroll rules.

## Monthly Rules

- Full day: 9 hours / 540 minutes.
- Grace: no short-hours deduction at or above 8h50m / 530 minutes.
- Short-hours rule: applies only from 6h00m to below 8h50m.
- Short-hours rounding: floor to previous 15-minute interval.
- Half day: 3h00m through below 6h00m.
- Less than 3h00m: full-day LOP.
- OT threshold: only after 9h15m / 555 minutes.
- OT rounding: complete 15-minute eligible blocks, floor only.
- Sunday: default week off, configurable per employee.
- Leave earned: `(paid days + week offs + holidays + leave used) / days in month x 2`, truncated to one decimal. A full month therefore earns 2 leaves.
- Current-month earned leave can be used in the same month.
- Full-day LOP: Monthly Salary / 30.
- Half-day LOP: Monthly Salary / 60.
- Hourly rate: Monthly Salary / (30 x 9).
- A single In/Out pair longer than 12 hours is flagged as a punch error, not paid. This catches an Out punch entered before its In punch, which would otherwise roll past midnight and be paid with overtime.

Every value above lives in `attendance/settings.py` and is shown, with its effect, on the Settings page.

## Daily Rules

- Present days are paid at the daily rate; holidays are paid, week offs are not.
- Working a week off counts as a normal paid working day.
- No leave balance, leave earned, or leave encashment.
- Short-hours and overtime use the same thresholds as Monthly, against the daily rate.

## Working Days With No Punches

A working day with no punches is neither paid nor deducted. It stays as `Needs Review` until punches are entered in Attendance Manager, or an override (`Paid Leave`, `Unpaid Leave / LOP`, and so on) is set on the employee payroll page. Attendance Manager highlights these days and offers a `No punch days` filter.

## Workflow

The payroll month page shows these five steps and highlights the one you are on. A step is only actionable once every step before it is done; until then its buttons are dimmed and inert. Completed steps stay actionable so wages can be reloaded or attendance revisited.

1. **Import Attendance** - upload the punch CSV or XLSX in Attendance Manager. Every Employee ID in the sheet must already exist in Employee Master; unknown IDs abort the whole import and are listed on screen. A default Sunday week off and a zero opening leave balance are created for employees that do not have them yet.
2. **Load Wages** - set wage type and salary in Employee Master, then `Load Wage From Master`. Employees with a zero salary or an inactive status are skipped.
3. **Review & Submit** - fix odd punches and no-punch days in Attendance Manager, then submit. Payroll cannot be calculated until attendance is submitted.
4. **Calculate Payroll** - two paths:
   - `Recalculate` in the Run calculation panel - re-runs against the current holiday calendar, week offs, loans, and advances while preserving manual overrides, adjustments, manual loan amounts, and leave encashment.
   - `Reset & Recalculate` in the page header, next to `Delete payroll` - clears all of those manual edits for the month first, then recalculates from scratch. It sits with the other destructive actions rather than beside the everyday one.
5. **Finalize** - Monthly and Daily wage payroll are finalized **separately**. Each has its own lock on the payroll month page, and locking one leaves the other open for edits and recalculation. Finalizing writes the summary and attendance CSVs to `output/csv`. Both finalizing and unlocking require the admin password.

The month as a whole only shows `Finalized` once every wage type that has employees is finalized. Recalculation never touches a finalized wage group, so Daily can be re-run after Monthly is signed off. `View monthly only` / `View daily only` filter the page to one wage type and scope both recalculate buttons to that group.

Recalculation replaces payroll results and leave ledger rows for that month, so type changes do not duplicate historical result rows.

### Employee Master Fields

Every employee has a wage type and a `Salary`, which is the figure payroll is calculated from.

Monthly wage employees additionally have:

- **Salary breakup** - `Basic Salary`, `HRA`, `Allowance`, `Conveyance Allowance`. All four must add up to `Salary` exactly. Leaving all four at zero means the breakup has not been captured yet and is allowed; once any one is filled in, the total has to reconcile. The form shows a running total and the shortfall or excess as you type.
- **Compliance** - `PF` and `ESIC` yes/no flags.

Every employee, monthly or daily, also has **Payroll exceptions**:

- **Ignore OT** - overtime minutes are still reported as raw OT, but payable OT and OT amount stay zero.
- **Ignore Less Hours** - short-hours shortage is not deducted. Full-day and half-day counting still applies.

Both read as "skip this rule for this employee", and both default to off. `Ignore OT` replaced an inverted `OT eligible` flag; existing databases are migrated automatically, so an employee who previously had OT disabled comes through with `Ignore OT` ticked.

Both groups are hidden for Daily wage employees and are never stored for them. The breakup is recorded for compliance and payslip presentation; payroll continues to calculate from `Salary`.

### Employee Master Import Rules

`Employee ID` is the match key and can never be changed by an import.

- Unknown Employee IDs are rejected.
- A `Name` column must match the stored name. Import cannot rename an employee - correct the name on the employee page instead. This keeps a bulk file from silently reassigning payroll to a different person when a spelling changes.
- Wage type cannot be changed once set.
- Breakup columns must reconcile with `Salary`, and are rejected on a daily wage row.
- Any column missing from the file leaves the stored value untouched, so an older export still imports cleanly.

A rejected row aborts the whole import; nothing is partially applied.

### Import Order

Employee Master first. Attendance and Leave Balance imports both reject Employee IDs that are not already in the master, so employees are always added deliberately with a reviewed wage type and salary.

```text
Employee Master  ->  Attendance (CSV/XLSX)  ->  Leave Balance CSV
```

A rejected attendance upload changes nothing: validation of columns and Employee IDs runs before any existing attendance for the month is cleared.

## Tests

```bash
source venv/bin/activate
pytest
```

The tests cover wage type normalization, unsupported wage type protection, Monthly grace/rounding/half-day/overtime/leave rules, login, password change, protected route redirect, and wage type recalculation changes.

## Documentation

- [Development setup](docs/DEVELOPMENT.md)
- [Raspberry Pi deployment](docs/DEPLOYMENT.md)
- [Operations guide](docs/OPERATIONS.md)

## Linux / Raspberry Pi Deployment

Do not deploy until the local version is approved.

Suggested production path:

```text
/opt/smartfill-attendance
```

Before deployment, check port 3000 without touching any existing server:

```bash
sudo ss -ltnp
```

If port 3000 is occupied, set another port with `APP_PORT=3001` before installing.

### GitHub Access For Private Repo

GitHub login or a deploy key is required on the Linux server because the repository is private.

Recommended options:

- SSH deploy key: add the server public key to the private GitHub repository, then use `git@github.com:smitz96/payroll.git`.
- GitHub CLI / token: authenticate the server user with a GitHub token that can read the repository.

The in-app Git Pull Update button uses the server's existing Git credentials. It will not ask for a GitHub password in the browser.

### One-Click Installer

After GitHub access is configured on the server, run:

```bash
sudo REPO_URL=git@github.com:smitz96/payroll.git bash install.sh
```

Or choose a custom port:

```bash
sudo APP_PORT=3000 REPO_URL=git@github.com:smitz96/payroll.git bash install.sh
```

The installer:

- Installs Linux packages: `git`, `python3`, `python3-venv`, `python3-pip`, and build tools.
- Clones or updates the app in `/opt/smartfill-attendance`.
- Creates a Python virtual environment and installs `requirements.txt`.
- Initializes the SQLite database schema.
- Creates and starts a `smartfill-attendance` systemd service.
- Runs the web app on port `3000` by default.

### Manual Service Commands

```bash
sudo systemctl daemon-reload
sudo systemctl enable smartfill-attendance
sudo systemctl restart smartfill-attendance
sudo systemctl status smartfill-attendance
```

Logs:

```bash
journalctl -u smartfill-attendance -f
```

### In-App Git Pull Update

Open `Settings -> Server Update`, enter the admin password, then click `Git Pull Update`.

The update button:

- Runs `git status --porcelain` and blocks the update if local server changes exist.
- Runs `git pull --ff-only` using the configured Git remote.
- Runs `python scripts/init_db.py` after a successful pull.
- Logs success or failure in Activity Logs.

If GitHub authentication is missing on the server, the button will show a GitHub access error. Configure SSH deploy key or token access, then try again.

If Python, template, or CSS files changed, restart the service:

```bash
sudo systemctl restart smartfill-attendance
```

## Backup

Back up these directories:

```text
/opt/smartfill-attendance/data
/opt/smartfill-attendance/uploads
```

Do not delete historical payroll records automatically.

## Adding Future Wage Types

Add a new `PayrollRule` implementation in `attendance/payroll_rules.py` and register it:

```python
PAYROLL_RULES = {
    "MONTHLY": MonthlyPayrollRule(),
    "DAILY": DailyPayrollRule(),
}
```

Do not default unknown types to Monthly.
