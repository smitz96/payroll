"""PF and ESI contributions under the central Acts, as applied in Gujarat.

Provident fund is governed by the EPF & MP Act 1952 and ESI by the ESI Act 1948.
Both are central statutes with centrally notified rates, so Gujarat establishments
use the same percentages and ceilings as the rest of India.

The house rules chosen for this establishment, which the Acts leave to the employer:

* PF is contributed on the statutory ceiling, not on full basic. An employee whose
  earned basic exceeds the ceiling contributes on the ceiling only.
* The ceiling is applied to the basic actually *earned* in the month, so loss of pay
  reduces the PF wage once earned basic falls below the ceiling.
* PF wage is basic alone. HRA is excluded by statute and there is no DA component.
* The employer side includes EDLI and administration charges, so the figures match
  what is actually remitted in the ECR challan rather than only the 12%.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

# Rates and ceilings as notified. Percentages are of the respective wage.
STATUTORY_RULES = {
    # EPF & MP Act 1952
    "PF_WAGE_CEILING": Decimal("15000"),
    "PF_EMPLOYEE_PERCENT": Decimal("12"),
    "PF_EMPLOYER_PERCENT": Decimal("12"),
    # The employer 12% splits into pension and provident fund. Pension is capped at
    # the same ceiling, and whatever is left of the 12% goes to the fund.
    "EPS_PERCENT": Decimal("8.33"),
    # Employer-borne, on PF wages. EDLI is the insurance premium; admin is the EPFO
    # administration charge, which also carries an establishment-level monthly floor.
    "EDLI_PERCENT": Decimal("0.5"),
    "PF_ADMIN_PERCENT": Decimal("0.5"),
    "PF_ADMIN_MINIMUM_PER_MONTH": Decimal("500"),
    # ESI Act 1948, rates notified with effect from 1 July 2019.
    "ESI_EMPLOYEE_PERCENT": Decimal("0.75"),
    "ESI_EMPLOYER_PERCENT": Decimal("3.25"),
    "ESI_WAGE_CEILING": Decimal("21000"),
}

# Gujarat professional tax, as notified from 1 April 2022: nil up to 12,000 a month
# and 200 above it. Slabs are (monthly wage above which the amount applies, amount),
# highest first, and are charged on the wage actually earned in the month.
PROFESSIONAL_TAX_SLABS = (
    (Decimal("12000"), Decimal("200")),
)

STATUTORY_RULE_LABELS = {
    "PF_WAGE_CEILING": ("PF wage ceiling", "Contribution is calculated on earned basic up to this figure."),
    "PF_EMPLOYEE_PERCENT": ("PF employee share", "Deducted from the employee's salary."),
    "PF_EMPLOYER_PERCENT": ("PF employer share", "Paid by the company on top of salary, split into pension and fund."),
    "EPS_PERCENT": ("Pension (EPS) share", "Part of the employer 12%, capped at the PF wage ceiling."),
    "EDLI_PERCENT": ("EDLI", "Employer-borne insurance premium on PF wages."),
    "PF_ADMIN_PERCENT": ("PF admin charges", "Employer-borne EPFO administration charge on PF wages."),
    "PF_ADMIN_MINIMUM_PER_MONTH": ("PF admin minimum", "Establishment-level floor for the monthly admin charge."),
    "ESI_EMPLOYEE_PERCENT": ("ESI employee share", "Deducted from the employee's salary."),
    "ESI_EMPLOYER_PERCENT": ("ESI employer share", "Paid by the company on top of salary."),
    "ESI_WAGE_CEILING": ("ESI wage ceiling", "Above this monthly wage the employee is outside ESI coverage."),
}

RUPEE = Decimal("1")
PAISE = Decimal("0.01")


def _percent(amount, rate):
    return (Decimal(amount or 0) * Decimal(rate)) / Decimal("100")


def round_pf(amount):
    """EPFO reports contributions in whole rupees, rounded to the nearest rupee."""
    return Decimal(amount or 0).quantize(RUPEE, rounding=ROUND_HALF_UP)


def round_esi(amount):
    """ESIC requires contributions to be rounded up to the next rupee."""
    return Decimal(amount or 0).quantize(RUPEE, rounding=ROUND_CEILING)


def pf_wage(earned_basic, cfg=None):
    """PF wage for the month: earned basic, capped at the statutory ceiling.

    The cap is applied after loss of pay, so a month with heavy absence reduces the
    PF wage only once earned basic drops below the ceiling.
    """
    cfg = cfg or STATUTORY_RULES
    earned = max(Decimal("0"), Decimal(earned_basic or 0))
    return min(earned, cfg["PF_WAGE_CEILING"]).quantize(PAISE)


def pf_contributions(earned_basic, cfg=None):
    """Employee, pension, fund, EDLI and admin figures for one employee-month."""
    cfg = cfg or STATUTORY_RULES
    wage = pf_wage(earned_basic, cfg)
    employee = round_pf(_percent(wage, cfg["PF_EMPLOYEE_PERCENT"]))
    employer_total = round_pf(_percent(wage, cfg["PF_EMPLOYER_PERCENT"]))
    # Pension is capped at the same ceiling; the balance of the employer share goes
    # to the provident fund, so the two always add back to the employer total.
    pension = round_pf(_percent(wage, cfg["EPS_PERCENT"]))
    return {
        "wage": wage,
        "employee": employee,
        "employer": employer_total,
        "pension": pension,
        "fund": employer_total - pension,
        "edli": round_pf(_percent(wage, cfg["EDLI_PERCENT"])),
        "admin": round_pf(_percent(wage, cfg["PF_ADMIN_PERCENT"])),
    }


def esi_covered(eligibility_wage, cfg=None):
    """Whether the employee is inside ESI coverage this month.

    Coverage is decided on the monthly wage excluding overtime; overtime counts
    towards the contribution but must not push someone out of the scheme.
    """
    cfg = cfg or STATUTORY_RULES
    return Decimal(eligibility_wage or 0) <= cfg["ESI_WAGE_CEILING"]


def esi_contributions(contribution_wage, eligibility_wage, cfg=None):
    """Employee and employer ESI for one employee-month.

    `contribution_wage` includes overtime; `eligibility_wage` excludes it, because
    ESIC uses the two differently.
    """
    cfg = cfg or STATUTORY_RULES
    if not esi_covered(eligibility_wage, cfg):
        return {"wage": Decimal("0.00"), "employee": Decimal("0"), "employer": Decimal("0"), "covered": False}
    wage = max(Decimal("0"), Decimal(contribution_wage or 0)).quantize(PAISE)
    return {
        "wage": wage,
        "employee": round_esi(_percent(wage, cfg["ESI_EMPLOYEE_PERCENT"])),
        "employer": round_esi(_percent(wage, cfg["ESI_EMPLOYER_PERCENT"])),
        "covered": True,
    }


def professional_tax(earned_wage, slabs=PROFESSIONAL_TAX_SLABS):
    """Gujarat professional tax on the wage actually earned in the month."""
    wage = max(Decimal("0"), Decimal(earned_wage or 0))
    for threshold, amount in slabs:
        if wage > threshold:
            return amount
    return Decimal("0")


ZERO_PF = {"wage": Decimal("0.00"), "employee": Decimal("0"), "employer": Decimal("0"),
           "pension": Decimal("0"), "fund": Decimal("0"), "edli": Decimal("0"), "admin": Decimal("0")}
ZERO_ESI = {"wage": Decimal("0.00"), "employee": Decimal("0"), "employer": Decimal("0"), "covered": False}


def statutory_for_employee(employee, earned_basic, esi_contribution_wage, esi_eligibility_wage, cfg=None):
    """PF and ESI for one employee-month, honouring the master's per-employee flags.

    Both flags live on the employee master and apply to monthly wage employees only,
    so a daily wage record returns zeros throughout.
    """
    pf = pf_contributions(earned_basic, cfg) if (employee and employee.pf_enabled) else dict(ZERO_PF)
    esi = (
        esi_contributions(esi_contribution_wage, esi_eligibility_wage, cfg)
        if (employee and employee.esic_enabled) else dict(ZERO_ESI)
    )
    return pf, esi


def statutory_rule_rows():
    """Settings-page rows for the statutory rates."""
    rows = []
    for key, value in STATUTORY_RULES.items():
        label, detail = STATUTORY_RULE_LABELS.get(key, (key, ""))
        display = f"{value:,.0f}" if key.endswith(("CEILING", "MONTH")) else f"{value.normalize()}%"
        rows.append({"key": key, "label": label, "value": display, "detail": detail})
    for threshold, amount in PROFESSIONAL_TAX_SLABS:
        rows.append({
            "key": "PROFESSIONAL_TAX",
            "label": "Professional tax",
            "value": f"{amount:,.0f}",
            "detail": f"Gujarat professional tax, charged when earned wage exceeds {threshold:,.0f} a month.",
        })
    return rows
