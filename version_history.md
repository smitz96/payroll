# Version History

## V1.05

Current version.

- Per-employee attendance reimport now auto-calculates Total Working Hours from First Punch and Last Punch when the hours cell is blank or `-`.
- Reload wages now recalculates existing open payroll results, so Employee Master changes such as PF/ESIC/TDS immediately update deductions before finalization.

## V1.04

- Split the Reports page into Standard Reports and Other Reports.
- Standard Reports now contains Attendance Summary for Monthly, Summary for Daily Wage Group, and Payroll Summary.
- Moved salary slips, department attendance, salary sheet, overtime, less-hours, manual override, and error reports into Other Reports.
- Fixed per-employee attendance reimport so edited CSV exports with slash dates such as `01/08/26` or `01/08/2026` are accepted.

## V1.03

- Added employee-specific punch-data reimport from the employee payroll detail page.
- Added employee-specific attendance CSV download so one employee's attendance can be exported, corrected, and reimported.
- Per-employee reimport replaces only that employee's attendance rows, clears only that employee's day overrides, and recalculates only that employee's payroll.
- Kept full-month attendance import behavior unchanged for normal monthly uploads.

## V1.02

- Added employee-specific attendance summary access from employee payroll detail for both Monthly and Daily wage groups.
- Removed the old Detailed Attendance report card from the Reports page.
- Beautified Overtime, Less Hours, and Error PDF reports with clearer labels, KPI summaries, readable durations, and stronger visual emphasis.
- Added Manual Override Report in PDF and CSV formats, comparing imported attendance with user-entered day-status overrides.
- Improved Error Report by excluding configured week-off attendance warnings and grouping issues by priority with issue counts.
- Improved the missing-punch register export with issue grouping and issue counts.
- Added Attendance Manager filters for Needs review, No punch days, Odd punch, and Other issues, with multi-select support.
- Made the website more mobile friendly with responsive layout, navigation, report, form, and attendance-grid improvements.
- Updated leave earning logic so employees receive the full monthly leave allotment when attended days plus week offs plus holidays reaches at least `days in month - 2`; otherwise leave remains pro-rated.
- Improved attendance summaries so short-hours values are displayed in hours/minutes and warning markers are easier for employees to notice.
- Fixed Total Paid Days to include holidays.
- Added calculation status indicators with a green tick for Calculated and a warning mark for Needs Review.

## V1.01

- Added full backup and restore support so payroll data can be exported from one server and restored on another server after a crash or migration.
- Added multiuser support with module-based access control.
- Added a Users management area for admin-controlled user and permission management.
- Removed the old Users & access entry from Settings security.
- Updated app documentation for backup/restore, local setup, and operational usage.

## V1.00

First released version.

- Imported monthly attendance and employee wage data.
- Calculated payroll for Monthly and Daily wage groups.
- Generated salary slips, payroll summaries, attendance summaries, overtime, less-hours, and error reports.
- Supported week offs, holidays, leave balances, loan/advance deductions, payroll finalization, and local SQLite storage.
