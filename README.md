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

Change this immediately. Sign-in is protected by:

- A lockout after `LOGIN_MAX_ATTEMPTS` consecutive failures (default 5) for `LOGIN_LOCKOUT_MINUTES` (default 15). The counter and lockout are stored in the database, so restarting the service does not clear them.
- Identical responses and timing for an unknown username and a wrong password, so accounts cannot be probed.
- Every failed attempt and lockout recorded in Activity Logs.
- Session cookies set `HttpOnly` and `SameSite=Lax`. Set `SESSION_COOKIE_SECURE=true` once the app is served over HTTPS.
- Password changes require `MIN_PASSWORD_LENGTH` characters (default 10) with at least one letter and one number, and reject the shipped defaults.

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
- Short-hours rounding: the shortfall is rounded **up** to the next 15 minutes. 48 minutes short is charged as 60.
- Half day: 3h00m through below 6h00m.
- Less than 3h00m: the day earns no pay of its own. Available leave covers it; with no leave it is a full-day LOP.
- OT threshold: only after 9h15m / 555 minutes.
- OT rounding: the excess is floored to complete 15-minute blocks, the opposite of short hours and deliberately so.
- OT rate: twice the ordinary hourly rate.
- Sunday: default week off, configurable per employee, and awaiting confirmation until someone confirms it.
- A day is worth the month's salary divided by **the days in that month**, so February pays more per day than July. `Salary days per month` in Settings overrides this with a fixed figure if set to anything other than 0.
- Hourly rate: that daily rate divided by 9.
- A single In/Out pair longer than 12 hours is flagged as a punch error, not paid. This catches an Out punch entered before its In punch, which would otherwise roll past midnight and be paid with overtime.

Every value above lives in `attendance/settings.py` and is shown, with its effect, on the Settings page.

## Leave

- Earned: 2 days a full month, pro-rated by the days that end up paid and truncated to two decimals. The accrual is settled against the finished month, so leave granted during the month counts towards the days that earn it.
- Taken in half-day steps: a balance of 1.38 covers 1 day and keeps 0.38; 1.92 covers 1.5.
- Covers an absent day, the unworked half of a half day, and a day worked under the half-day minimum, oldest day first. A day set to `Unpaid Leave / LOP` by hand is never covered.
- Sandwich rule: a week off with an unpaid day on either side is charged to leave. With no balance behind it the day is loss of pay, and is shown as loss of pay rather than as leave.
- Beyond the balance: loss of pay at one day of salary.
- Carried forward in full once the month is finalized. A payroll month cannot be started until every earlier month is finalized, so a month never opens on a balance that can still move.
- Encashment is paid at the daily rate and is capped at the balance left after the month's leave has been taken.
- Daily wage employees have no leave and earn the monthly attendance bonus instead.

## Daily Rules

- Present days are paid at the daily rate; holidays are paid, week offs are not.
- Working a week off counts as a normal paid working day.
- No leave balance, leave earned, or leave encashment.
- Short-hours and overtime use the same thresholds as Monthly, against the daily rate.

## Working Days With No Punches

A working day with no punches is an absence: available leave covers it oldest day first, and whatever the balance cannot cover is loss of pay. Attendance Manager highlights these days and offers a `No punch days` filter.

Where the punch machine could not see someone - field staff on customer sites - use the **Attendance Register** on the same page. Download the month (or just the no-punch days), fill in the `Register Status` column from the handwritten register, and upload it back. A blank status leaves the day as the punches read it and `Auto` removes a status set earlier. Every bad line is reported by number and nothing is applied unless the whole file is good. Applying a register clears the month's calculated figures and reopens it, so nothing on screen can outlive the attendance behind it.

A day with several punch pairs totalling under three hours is neither paid nor guessed: it stays as `Needs Review` until someone sets the day.

## Workflow

The payroll month page shows these five steps and highlights the one you are on. A step is only actionable once every step before it is done; until then its buttons are dimmed and inert. Completed steps stay actionable so wages can be reloaded or attendance revisited.

0. **Open the month** - a new payroll month cannot be started while an earlier month is still open, because a month opens on the previous month's closing leave balance. The first month in the system has nothing behind it and starts freely.
1. **Import Attendance** - upload the punch report in Attendance Manager. The `.xlsx` is the punch machine's grid, with dates across the top; a `.csv` must be one row per employee per day. Every Employee ID in the sheet must already exist in Employee Master; unknown IDs abort the whole import and are listed on screen. A Sunday week off, awaiting confirmation, and a zero opening leave balance are created for employees that do not have them yet.
2. **Load Wages** - set wage type and salary in Employee Master, then `Load Wage From Master`. Employees with a zero salary or an inactive status are skipped.
3. **Review & Submit** - fix odd punches and no-punch days in Attendance Manager, then submit. Payroll cannot be calculated until attendance is submitted.
4. **Calculate Payroll** - two paths:
   - `Recalculate` in the Run calculation panel - re-runs against the current holiday calendar, week offs, loans, and advances while preserving manual overrides, adjustments, manual loan amounts, and leave encashment.
   - `Reset & Recalculate` in the page header, next to `Delete payroll` - clears all of those manual edits for the month first, then recalculates from scratch. It sits with the other destructive actions rather than beside the everyday one.
5. **Finalize** - Monthly and Daily wage payroll are finalized **separately**. Salary slips and the PF & ESIC salary sheet are only downloadable once the wage group is finalized: they state what someone is paid, and a draft month's figures can still move. The unbranded daily wage attendance summary carries no pay and stays available throughout. Each has its own lock on the payroll month page, and locking one leaves the other open for edits and recalculation. Finalizing writes the summary and attendance CSVs to `output/csv`. Both finalizing and unlocking require the admin password.

The month as a whole only shows `Finalized` once every wage type that has employees is finalized. Recalculation never touches a finalized wage group, so Daily can be re-run after Monthly is signed off. `View monthly only` / `View daily only` filter the page to one wage type and scope both recalculate buttons to that group.

Recalculation replaces payroll results and leave ledger rows for that month, so type changes do not duplicate historical result rows.

### Employee Master Fields

Every employee has a wage type and a `Salary`, which is the figure payroll is calculated from.

Monthly wage employees additionally have:

- **Salary breakup** - `Basic`, `HRA`, `Allowance`. All three must add up to `Salary` exactly. Leaving all three at zero means the breakup has not been captured yet and is allowed; once any one is filled in, the total has to reconcile. The form shows a running total and the shortfall or excess as you type.
- **Compliance** - `PF` and `ESIC` yes/no flags.

Every employee, monthly or daily, also has a **week off pattern**, a **status**, and - once they are no longer working - a **last working day**:

- The week off pattern is set on the Week Offs page and travels in the employee master export as one column, for example `Saturday=2,4; Sunday=All`. The occurrence numbers count that weekday within the month, so `Saturday=2,4` is the second and fourth Saturday.
- A rule the system created starts **unconfirmed**. Payroll still runs, but the employee is listed for review until someone confirms the pattern, because a Sunday default is a guess and some employees do not work a Sunday week.
- Marking someone `Left` or `Terminated` asks for their last working day. They stay in payroll for every month up to and including that one, and drop out afterwards. Anyone a month leaves out is named on the payroll page, so a person can never fall out of a run unnoticed.

Every employee also has **Payroll exceptions**:

- **Ignore OT** - overtime minutes are still reported as raw OT, but payable OT and OT amount stay zero.
- **Ignore Less Hours** - short-hours shortage is not deducted. Full-day and half-day counting still applies.

Both read as "skip this rule for this employee", and both default to off. `Ignore OT` replaced an inverted `OT eligible` flag; existing databases are migrated automatically, so an employee who previously had OT disabled comes through with `Ignore OT` ticked.

Both groups are hidden for Daily wage employees and are never stored for them. The breakup is recorded for compliance and payslip presentation; payroll continues to calculate from `Salary`.

### Employee Master Import Rules

`Employee ID` is the match key and can never be changed by an import.

- A new Employee ID adds that employee, so a single file can onboard new starters and update existing staff at once. `Name` and `Wage Type` are required to add one.
- `Employee ID` is permanent and can never be changed by any route.
- For an employee that already exists, a `Name` column must match the stored name. Import cannot rename anyone - use the employee edit window instead. This keeps a bulk file from silently reassigning payroll to a different person when a spelling changes.
- Wage type cannot be changed once set.
- Breakup columns must reconcile with `Salary`, and are rejected on a daily wage row. `Basic Salary` is still accepted as a column name for `Basic`, and a file that still carries the retired `Conveyance Allowance` column is rejected rather than silently dropping the amount.
- `Status` and `Last Working Day` travel together: a date is required to take someone off the payroll who is currently on it. A record that arrives already closed - a restore, or a bulk load of past staff - is accepted without one and stays out of payroll until a date is entered.
- `Week Off Pattern` replaces the whole pattern for that employee, which is what lets an import take a week off away as well as give one. A blank cell leaves the stored pattern alone.
- A disabled employee's row is read only for their status, last working day and week off pattern. Anything else about them is rejected rather than half-applied, so an export containing a leaver still re-imports.
- Any column missing from the file leaves the stored value untouched, so an older export still imports cleanly.

A rejected row aborts the whole import; nothing is partially applied.

Both the Employee Master and Leave Balance exports begin with two illustrative rows (`EXAMPLE-MONTHLY`, `EXAMPLE-DAILY`) showing every column filled in correctly, including a breakup that adds up. They are not database records - the import skips them, so an exported file can be edited and re-imported. Their IDs are deliberately unlike an employee number: they used to be 1 and 2, and a file showing `1` twice reads as a duplicate record rather than as a worked example.

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

### Rebuilding a month from exports

`data` is the real backup. If a month ever has to be rebuilt from files instead, four are needed, and the whole month reconstructs from them exactly:

```text
Employee Master CSV      employees, wages, breakup, compliance flags, week off pattern, status
Daily Punch Report XLSX  every punch of the month
Leave Balance CSV        opening balances
Attendance Register CSV  manual day statuses set by hand during the month
```

The register file is the one people forget. Any day whose status was set by hand - a site visit, a day the machine did not see - lives only in that export. Download `Download all days` from Attendance Manager before closing a month if the exports are being kept as a backup.

## Adding Future Wage Types

Add a new `PayrollRule` implementation in `attendance/payroll_rules.py` and register it:

```python
PAYROLL_RULES = {
    "MONTHLY": MonthlyPayrollRule(),
    "DAILY": DailyPayrollRule(),
}
```

Do not default unknown types to Monthly.
