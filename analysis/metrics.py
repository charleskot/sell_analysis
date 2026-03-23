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


def compute_all_metrics(
    price: float,
    area_m2: float,
    estimated_monthly_rent: float,
    purchase_costs_pct: float = 0.10,
    expense_ratio: float = 0.25,
    capital_growth_pct: float = 2.5,
) -> dict:
    """Compute all investment metrics and return as dict."""
    if not price or not area_m2 or not estimated_monthly_rent:
        return {}

    annual_rent = monthly_to_annual_rent(estimated_monthly_rent)
    net_annual = annual_rent * (1 - expense_ratio)
    total_inv = total_investment(price, purchase_costs_pct)

    return {
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
