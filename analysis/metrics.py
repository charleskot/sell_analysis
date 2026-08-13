"""Pure financial calculation functions for real estate investment analysis."""


def price_per_m2(price: float, area_m2: float) -> float | None:
    if not price or not area_m2 or area_m2 <= 0:
        return None
    return round(price / area_m2, 2)


def total_investment(purchase_price: float, purchase_costs_pct: float) -> float:
    """Total capital needed including transaction costs (ITP, notary, registro)."""
    return round(purchase_price * (1 + purchase_costs_pct), 2)


def gross_rental_yield(annual_rent: float, purchase_price: float, purchase_costs_pct: float = 0.10) -> float | None:
    """
    Rentabilidad bruta = Renta anual / Inversión total * 100
    Standard metric used by Spanish real estate market.
    """
    if not annual_rent or not purchase_price or purchase_price <= 0:
        return None
    inv = total_investment(purchase_price, purchase_costs_pct)
    return round((annual_rent / inv) * 100, 2)


def net_rental_yield(
    annual_rent: float,
    purchase_price: float,
    purchase_costs_pct: float = 0.10,
    expense_ratio: float = 0.25,
) -> float | None:
    """
    Rentabilidad neta = Renta anual neta / Inversión total * 100
    Expense ratio covers: IBI, comunidad, seguro, vacancia, mantenimiento (~25%).
    """
    if not annual_rent or not purchase_price or purchase_price <= 0:
        return None
    net_rent = annual_rent * (1 - expense_ratio)
    inv = total_investment(purchase_price, purchase_costs_pct)
    return round((net_rent / inv) * 100, 2)


def roi(annual_net_income: float, total_inv: float) -> float | None:
    """ROI = (Renta neta anual / Inversión total) * 100"""
    if not annual_net_income or not total_inv or total_inv <= 0:
        return None
    return round((annual_net_income / total_inv) * 100, 2)


def payback_period_years(total_inv: float, annual_net_income: float) -> float | None:
    """Años necesarios para recuperar la inversión con la renta neta."""
    if not annual_net_income or annual_net_income <= 0 or not total_inv:
        return None
    return round(total_inv / annual_net_income, 1)


def capital_appreciation_estimate(price: float, annual_growth_pct: float, years: int) -> float:
    """Plusvalía estimada en N años con crecimiento anual compuesto."""
    return round(price * ((1 + annual_growth_pct / 100) ** years) - price, 2)


def monthly_to_annual_rent(monthly_rent: float) -> float:
    return round(monthly_rent * 12, 2)


# ── Leverage / mortgage ──────────────────────────────────────────────────

def monthly_mortgage_payment(loan_amount: float, annual_rate_pct: float, years: int) -> float:
    """French amortisation (cuota constante) — the standard Spanish mortgage."""
    if not loan_amount or loan_amount <= 0 or years <= 0:
        return 0.0
    r = (annual_rate_pct / 100) / 12
    n = years * 12
    if r == 0:
        return round(loan_amount / n, 2)
    factor = (1 + r) ** n
    return round(loan_amount * r * factor / (factor - 1), 2)


def compute_leverage(
    price: float,
    monthly_rent: float,
    purchase_costs_pct: float,
    expense_ratio: float,
    ltv_pct: float,
    annual_rate_pct: float,
    years: int,
) -> dict:
    """Cash needed, monthly cashflow and cash-on-cash return with a mortgage.

    Banks in Spain lend against the *purchase price* (or appraisal, whichever
    is lower) — transaction costs always come out of the buyer's pocket.
    """
    if not price or price <= 0:
        return {}

    loan = price * (ltv_pct / 100)
    down_payment = price - loan
    costs = price * purchase_costs_pct
    cash_needed = round(down_payment + costs, 2)

    payment = monthly_mortgage_payment(loan, annual_rate_pct, years)

    net_monthly_rent = monthly_rent * (1 - expense_ratio)
    monthly_cashflow = round(net_monthly_rent - payment, 2)
    annual_cashflow = round(monthly_cashflow * 12, 2)

    coc = round((annual_cashflow / cash_needed) * 100, 2) if cash_needed > 0 else None

    # Debt service coverage — banks typically want >= 1.25
    dscr = round((net_monthly_rent / payment), 2) if payment > 0 else None

    return {
        "loan_amount": round(loan, 2),
        "down_payment": round(down_payment, 2),
        "purchase_costs": round(costs, 2),
        "cash_needed": cash_needed,
        "monthly_payment": payment,
        "monthly_cashflow": monthly_cashflow,
        "annual_cashflow": annual_cashflow,
        "cash_on_cash_pct": coc,
        "dscr": dscr,
    }


def compute_all_metrics(
    price: float,
    area_m2: float,
    estimated_monthly_rent: float,
    purchase_costs_pct: float = 0.10,
    expense_ratio: float = 0.25,
    capital_growth_pct: float = 2.5,
    financing: dict | None = None,
) -> dict:
    """Compute all investment metrics and return as dict.

    financing: {'ltv_pct', 'annual_rate_pct', 'years'} — when provided, adds
    leverage metrics (cuota, cashflow, cash-on-cash, DSCR).
    """
    if not price or not area_m2 or not estimated_monthly_rent:
        return {}

    annual_rent = monthly_to_annual_rent(estimated_monthly_rent)
    net_annual = annual_rent * (1 - expense_ratio)
    total_inv = total_investment(price, purchase_costs_pct)

    metrics = {
        "estimated_monthly_rent": round(estimated_monthly_rent, 2),
        "estimated_annual_rent": round(annual_rent, 2),
        "total_investment": total_inv,
        "gross_yield_pct": gross_rental_yield(annual_rent, price, purchase_costs_pct),
        "net_yield_pct": net_rental_yield(annual_rent, price, purchase_costs_pct, expense_ratio),
        "roi_pct": roi(net_annual, total_inv),
        "payback_years": payback_period_years(total_inv, net_annual),
        "price_per_m2": price_per_m2(price, area_m2),
        "appreciation_5y": capital_appreciation_estimate(price, capital_growth_pct, 5),
        "appreciation_10y": capital_appreciation_estimate(price, capital_growth_pct, 10),
    }

    if financing:
        metrics.update(
            compute_leverage(
                price=price,
                monthly_rent=estimated_monthly_rent,
                purchase_costs_pct=purchase_costs_pct,
                expense_ratio=expense_ratio,
                ltv_pct=financing.get("ltv_pct", 80),
                annual_rate_pct=financing.get("annual_rate_pct", 3.0),
                years=financing.get("years", 30),
            )
        )

    return metrics
