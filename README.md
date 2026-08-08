# SMARTfill Attendance & Payroll Management

SMARTfill is a local Flask and SQLite web application for importing monthly attendance, maintaining employee wages, calculating Monthly payroll, preserving leave balances, and opening auditable payroll PDF reports.

Current supported automatic payroll rule:

```text
Wage Type = Monthly
```

All other wage types are imported, preserved, displayed, and marked as `Payroll Rules Not Configured`. They are never calculated with Monthly rules and never shown as a zero final salary.

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

Wage type is normalized with `strip().upper()`. Only `MONTHLY` resolves to a configured payroll rule.

## Monthly Rules

- Full day: 9 hours / 540 minutes.
- Grace: no short-hours deduction at or above 8h50m / 530 minutes.
- Short-hours rule: applies only from 6h00m to below 8h50m.
- Short-hours rounding: floor to previous 15-minute interval.
- Half day: 3h00m through below 6h00m.
- Less than 3h00m: full-day LOP.
- OT threshold: only after 9h15m / 555 minutes.
- OT rounding: complete 15-minute eligible blocks, floor only.
- Sunday: week off.
- Leave earned: paid working days / 12, truncated to one decimal.
- Current-month earned leave can be used in the same month.
- Full-day LOP: Monthly Salary / 30.
- Half-day LOP: Monthly Salary / 60.
- Hourly rate: Monthly Salary / (30 x 9).

## Workflow

1. Login.
2. Open or create a payroll month.
3. Upload Attendance CSV.
4. Load wage data from Employee Master.
5. Review warnings and wage types.
6. Calculate payroll.
7. Review employee detail and overrides.
8. Recalculate after changes.
9. Export summary, detailed attendance, and error CSV reports.

Recalculation replaces payroll results and leave ledger rows for that month, so type changes do not duplicate historical result rows.

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
