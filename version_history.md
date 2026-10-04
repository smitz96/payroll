# Version History

## V1.15

Current version.

- Each shift has three grace settings in the Shifts panel on the Week Offs page: check-in grace and check-out grace (0 unless set) and OT grace (30 minutes unless changed).
- A day is now judged on working hours against its shift, not on the check-in and check-out times:
  1. Hours at or above the shift length less its check-in and check-out grace carry no less hours. On a 9-hour shift with 10 minutes' check-in grace, 9:42 to 6:34 (8h52m) is not charged.
  2. Below that, the shortfall against the full shift length is charged, rounded up to 15 minutes: 8h40m is 20 minutes short, charged as 30. A long break is charged the same way.
  3. Overtime is paid once hours reach the shift length plus its OT grace, on the time beyond the shift length, rounded down to 15 minutes: 9h50m pays 45 minutes. Week offs and holidays worked follow the same rule.
- Late-in and early-out minutes are still shown on the employee page and in the Less Hours report, in clock minutes past each grace, to show where the time went.
- The daily wage attendance bonus uses the same required hours, so a day at or above them carries no absence.
- The fixed 10-minute check-in grace, the fixed 8h50m full-day grace and the fixed 30-minute overtime start are removed from Settings; each shift now carries its own.

Fixes for payroll logic bugs found by testing the rules against scenarios:

- A day still awaiting review was paid in full for Monthly wage but unpaid for Daily wage, and the month could be finalized with it. Such a day is now unpaid for both until someone sets it, and a wage group cannot be finalized while any employee in it needs review or is uncalculated.
- Overtime was paid on days set to leave, LOP, a half day or a week off when the day had long punches. It is now earned only on a full day, a week off worked or a holiday worked.
- Daily wage: a week off worked for 3 hours was paid a full day, and 6 hours paid less than 3. It is now a half or full day by the same thresholds as any day.
- Monthly wage: a week off worked for 3 hours earned a full compensatory leave. It now earns half a day for a half day's hours.
- Days after the last working day were absences covered by the leave balance, so a leaver was paid leave after leaving. Those days, and days before the new optional Date of Joining, are now Not Employed: deducted, never drawn from leave or sandwiched, and counted from the calendar even without attendance rows.
- Working a holiday earned nothing extra. It now earns compensatory leave for Monthly wage, and a day's wage on top of the holiday for Daily wage, half or full by hours.
- On a shift whose length is not a whole number of quarter hours, payable overtime minutes did not match what was paid. Overtime is now rounded on the time beyond the shift.
- The sandwich rule skipped week offs next to a holiday. It now reaches through holidays between two unpaid days, charging only the week offs.

## V1.14

- The Settings About panel showed V1.09 because the version was a fixed value in the code. It, and the version stamped into backup files (which still said V1.05), now read version.md, so they follow every release.

## V1.13

- Week Offs page renamed Week Offs & Shifts. The Shifts panel is folded into a one-line summary of every shift, so the employee grid comes first.
- Bulk shift assignment: tick employees, choose a shift and weekdays, and apply in one step before saving.
- Days on a non-default shift are highlighted, a shift filter lists who works each shift, and the shift on an always-off day is faded.
- A banner names any open payroll month calculated before the latest week off or shift change, with a link to recalculate it.
- The page warns before leaving with unsaved changes.
- Amounts on every screen use Indian digit grouping (8,11,000.00), matching the PDFs, and money columns are right-aligned.
- Payroll month table: the warnings column is replaced by a "N days to review" badge with the days in its tooltip, employee names link to their page, and a "Needs review" filter sits beside All / Monthly / Daily.
- Employee payroll page: a review stepper moves to the previous or next employee needing review, the days to review are listed at the top with the reason for each and a link to that day in the calendar, Final payable salary leads the summary, and the rarely used re-import panel is folded at the bottom.
- Payroll in the menu opens the month being worked on; "Other months" on that page opens the month picker.
- Dashboard: "Needs attention" links to the review list, review queue entries show the employee name and readable dates and open the employee, and the summary tiles no longer crush on 1024px screens.
- Attendance Manager: once a month is imported, the re-import panel is folded so the punch grid comes first.

## V1.12

- Added multiple shifts. Shifts (name, start time, end time) are managed in a new Shifts panel on the Week Offs page; `Normal Shift` 9:30 AM to 6:30 PM is created automatically as the default.
- Each weekday of each employee can work a different shift, chosen on the Week Offs grid. Weekdays without one work the default shift, so existing employees are unchanged.
- Late in (10-minute grace), early out and overtime are measured from each day's own shift start and end.
- Full day and half day scale with the shift length at 2/3 and 1/3; the daily wage attendance bonus and week off overtime use the shift length too. The hourly rate divisors (8 and 8.5) are unchanged.
- Added a `Shift Pattern` column to the Employee Master export and import.
- The working-day option on the Week Offs grid now reads "Working day" instead of "Normal Shift".
- Shift is shown on the employee calculation detail and in the Less Hours CSV.

## V1.11

- Raised the PF wage ceiling from ₹15,000 to ₹25,000 per S.O. 5109(E), effective 17 September 2026. The whole September 2026 payroll month onwards uses ₹25,000; August 2026 and earlier keep ₹15,000.
- Pension (EPS), EDLI and PF admin charges are capped at the same ceiling: maximum employee and employer PF ₹3,000, EPS ₹2,083, EDLI and admin ₹125 each.
- The PF ceiling now depends on the payroll month, so salary slip Yearly CTC for finalized months is unchanged.

## V1.10

- Split less hours into Late In and Early Out, measured against a 9:30 AM to 6:30 PM shift. Check-in has a 10-minute grace (9:40 AM); after it, late time is charged from 9:30. Checkout before 6:30 PM is charged with no grace. Each part is rounded up to 15 minutes and the two are added for the total less hours.
- The 8h50m full-day grace no longer waives less hours. It still decides the daily wage attendance bonus, and less hours for a day that has working hours but no punch times.
- Overtime on a full day is now counted from 6:30 PM checkout, once checkout is 7:00 PM or later, floored to 15 minutes. Week offs and holidays worked keep overtime on total working hours.
- Added Late In and Early Out to the Less Hours report (PDF and CSV) and to the employee payroll detail page.
- New shift settings are listed on the Settings page.

## V1.09

- Changed Yearly CTC statutory amounts to use full contracted wages rather than attendance-paid payroll values.
- CTC PF now uses full contracted Basic, while CTC ESIC uses full contracted monthly salary; PF Admin/EDLI and the annual bonus use the corresponding full-salary statutory amounts.
- Kept actual monthly payroll PF, ESIC, Professional Tax, deductions, and net salary attendance-based and unchanged.
- Added regression coverage for the updated August CTC figures for Jayesh, Narendra, Sunil, and Bhavesh.

## V1.08

- Revised Yearly Cost to Company so monthly employer cost includes salary, employer PF, PF Admin/EDLI charges, and employer ESIC for all 12 months.
- Revised the annual bonus to equal monthly salary less employee PF, employee ESIC, and Professional Tax; TDS is not deducted from the bonus.
- Added a regression example for Bijal that produces a Yearly CTC of `3,13,357.00`.

## V1.07

- Added an employee-level Annual CTC Bonus option for Monthly wage employees, including Employee Master forms, CSV import/export, schema migration, and audit details.
- Added Yearly Cost to Company to salary slips using `(monthly salary + employer PF + employer ESIC) x 12`, plus one plain monthly salary when the annual bonus option is enabled.
- Redesigned the salary slip header and attendance summary, added company contact details, and kept each employee slip within one A4 page.
- Moved Yearly Cost to Company below the amount in words and formatted it consistently with Net Pay.
- Updated PDF currency values to use Indian lakh/crore digit grouping.
- Kept short-hours deductions separate from the PF wage base while continuing to reduce PF wages for loss-of-pay days.

## V1.06

- Updated less-hours and overtime payroll amounts to use an 8-hour divisor for Monthly wage employees and an 8.5-hour divisor for Daily wage employees, while keeping the 9-hour attendance full-day criteria unchanged.
- Improved Payroll Summary report columns by adding Holidays, moving Leave before Total Paid, and removing Designation and Wage Type.
- Split combined deductions in Payroll Summary into separate Less Hours and Compliance heads.
- Compliance now includes employee PF, employee ESIC, and Professional Tax.
- Renamed Addition to Over Time in Payroll Summary and displayed Over Time and Less Hours as `minutes/amount`.

## V1.05

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
