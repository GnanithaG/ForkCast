"""KPI catalogue and OKRs for the Harbor & Hearth chain.

Every KPI is computed from summed daily columns (analytics.daily_location), so
the same definition works for a day, a week, a month or a year, for one store
or the whole chain. The evaluator loads these into analytics.kpi_definitions,
analytics.okr_objectives and analytics.okr_key_results so Power BI shows the
same definitions the Python code uses.
"""
from __future__ import annotations


def _div(a, b):
    return None if not b else a / b


# kpi_id: (name, category, unit, direction, target, warn_limit, min_days, formula, why it matters)
#   direction  'up' = higher is better, 'down' = lower is better
#   target     good at or beyond this value; warn_limit = boundary between warning and critical
#   min_days   shortest period the KPI is meaningful for (invoice-based costs need ~4 weeks)
KPIS = {
    "net_sales": ("Net sales", "Revenue", "$", "up", None, None, 1,
                  "Item sales minus discounts, excl. tax and tips", "Top line; judged against last year"),
    "covers": ("Covers", "Revenue", "#", "up", None, None, 1, "Guests served", "Traffic, separate from price"),
    "avg_check": ("Average check", "Revenue", "$", "up", None, None, 1, "Net sales / checks", "Party spend"),
    "sales_per_cover": ("Sales per cover", "Revenue", "$", "up", None, None, 1, "Net sales / covers",
                        "Spend per guest: pricing and upselling"),
    "offprem_share": ("Off-premise share", "Revenue", "%", "up", None, None, 1,
                      "(Takeout + delivery sales) / net sales", "Growth channel that needs no seats"),
    "seat_utilization": ("Seat utilization", "Floor", "%", "up", 0.22, 0.17, 1,
                         "Occupied seat-minutes / available seat-minutes", "How much of the dining room earns money"),
    "revpash": ("RevPASH", "Floor", "$", "up", 9.5, 8.0, 1,
                "Dine-in sales / available seat-hours", "Revenue yield per seat per open hour"),
    "table_turns": ("Table turns per day", "Floor", "x", "up", 2.2, 1.8, 1, "Dine-in parties / tables", "Throughput"),
    "avg_dwell": ("Average dwell", "Floor", "min", "down", 75, 85, 1, "Minutes from seating to check close",
                  "Long dwell at peak blocks the next party"),
    "walkaway_rate": ("Walk-away rate", "Guest experience", "%", "down", 0.01, 0.02, 1,
                      "Parties that left / parties that arrived", "Lost guests and lost revenue"),
    "large_party_loss": ("Large-party walk-away rate", "Guest experience", "%", "down", 0.15, 0.25, 7,
                         "Parties of 5-6 that left / parties of 5-6 that arrived", "Table mix problem at peak"),
    "avg_wait": ("Average wait (parties that waited)", "Guest experience", "min", "down", 8, 12, 1,
                 "Mean quoted-to-seated wait", "Guest satisfaction"),
    "food_cost_pct": ("Food cost %", "Cost of goods", "%", "down", 0.33, 0.35, 28,
                      "Food purchases / food sales", "Largest controllable cost"),
    "food_variance_pts": ("Food cost variance", "Cost of goods", "pts", "down", 0.03, 0.04, 28,
                          "Actual food cost % minus recipe (theoretical) food cost %",
                          "Waste, over-portioning, theft and vendor price creep"),
    "bev_cost_pct": ("Beverage cost %", "Cost of goods", "%", "down", 0.24, 0.27, 28,
                     "Beverage & alcohol purchases / beverage sales", "Bar margin"),
    "waste_pct": ("Food waste %", "Cost of goods", "%", "down", 0.045, 0.06, 7,
                  "Logged waste / food purchases", "Money thrown away"),
    "labor_pct": ("Labor cost %", "Labor", "%", "down", 0.32, 0.35, 1,
                  "Wages + burden / net sales", "Second-largest cost"),
    "splh": ("Sales per labor hour", "Labor", "$", "up", 55.0, 50.0, 1, "Net sales / labor hours",
             "Scheduling efficiency"),
    "prime_cost_pct": ("Prime cost %", "Profit", "%", "down", 0.65, 0.68, 28,
                       "(COGS + labor) / net sales", "The number operators manage to"),
    "discount_rate": ("Discount & comp rate", "Profit", "%", "down", 0.02, 0.035, 1,
                      "Discounts / gross sales", "Leakage from comps and promos"),
    "delivery_fee_pct": ("Delivery commissions % of sales", "Profit", "%", "down", 0.02, 0.03, 1,
                         "Third-party commissions / net sales", "Margin given to platforms"),
}


def compute_kpis(s: dict) -> dict:
    """KPI values from a dict of summed daily_location columns."""
    food_pct = _div(s["purchases_food"], s["food_sales"])
    theo_pct = _div(s["theoretical_food_cost"], s["food_sales"])
    arrived_large = s.get("dine_large_parties", 0) + s["lost_large_parties"]
    return {
        "net_sales": s["net_sales"],
        "covers": s["covers"],
        "avg_check": _div(s["net_sales"], s["checks"]),
        "sales_per_cover": _div(s["net_sales"], s["covers"]),
        "offprem_share": _div(s["takeout_sales"] + s["delivery_sales"], s["net_sales"]),
        "seat_utilization": _div(s["occupied_seat_min"], s["available_seat_min"]),
        "revpash": _div(s["dine_in_sales"], s["available_seat_min"] / 60 if s["available_seat_min"] else 0),
        "table_turns": _div(s["dine_in_checks"], s["table_days"]),
        "avg_dwell": _div(s["dwell_min_total"], s["dine_in_checks"]),
        "walkaway_rate": _div(s["lost_parties"], s["dine_in_checks"] + s["lost_parties"]),
        "large_party_loss": _div(s["lost_large_parties"], arrived_large),
        "avg_wait": _div(s["wait_min_total"], s["parties_waited"]),
        "food_cost_pct": food_pct,
        "food_variance_pts": None if food_pct is None or theo_pct is None else food_pct - theo_pct,
        "bev_cost_pct": _div(s["purchases_bev"], s["beverage_sales"]),
        "waste_pct": _div(s["waste_cost"], s["purchases_food"]),
        "labor_pct": _div(s["labor_cost"], s["net_sales"]),
        "splh": _div(s["net_sales"], s["labor_hours"]),
        "prime_cost_pct": _div(s["purchases_food"] + s["purchases_bev"] + s["purchases_packaging"] + s["labor_cost"],
                               s["net_sales"]),
        "discount_rate": _div(s["discounts"], s["gross_sales"]),
        "delivery_fee_pct": _div(s["delivery_commission_est"], s["net_sales"]),
    }


def status_of(kpi_id: str, value, ly_value=None) -> str:
    """good / warning / critical / info."""
    if value is None:
        return "n/a"
    name, cat, unit, direction, target, warn, *_ = KPIS[kpi_id]
    if target is None:                      # growth KPIs are judged against last year
        if ly_value in (None, 0):
            return "info"
        chg = value / ly_value - 1
        return "good" if chg >= 0.02 else ("warning" if chg >= -0.03 else "critical")
    if direction == "up":
        return "good" if value >= target else ("warning" if value >= warn else "critical")
    return "good" if value <= target else ("warning" if value <= warn else "critical")


# ------------------------------------------------------------------ OKRs
# OKR cycle: the fall / holiday season. Improvement KRs are measured against the
# same dates last year so a September start is not penalised for being off-season.
OKR_CYCLE = ("Fall-Holiday 2026", "2026-09-01", "2026-12-31")

OBJECTIVES = [
    ("O1", "Grow profitable revenue through the holiday season", "COO"),
    ("O2", "Protect margins against food and wage inflation", "CFO"),
    ("O3", "Staff smarter and seat every guest who shows up", "VP Operations"),
]

# kr_id, objective, description, kpi, kind, amount, scope, owner
#   kind 'rel'  : improve vs same dates last year by `amount` (fraction, sign = direction of improvement)
#        'pts'  : improve vs same dates last year by `amount` points
#        'abs'  : reach an absolute level `amount`
#        'cum'  : cumulative total vs last year x (1 + amount), paced by last year's calendar
KEY_RESULTS = [
    ("KR1.1", "O1", "Grow net sales 8% over last year's Sep-Dec", "net_sales", "cum", 0.08, "ALL", "COO"),
    ("KR1.2", "O1", "Lift RevPASH 6% vs the same dates last year", "revpash", "rel", 0.06, "ALL", "GMs"),
    ("KR1.3", "O1", "Halve the walk-away rate of 5-6 guest parties", "large_party_loss", "rel", -0.50, "ALL", "VP Operations"),
    ("KR2.1", "O2", "Cut prime cost 1.5 pts vs the same dates last year", "prime_cost_pct", "pts", -0.015, "ALL", "CFO"),
    ("KR2.2", "O2", "Hold food cost within 3 pts of recipe cost", "food_variance_pts", "abs", 0.03, "ALL", "Exec Chef"),
    ("KR2.3", "O2", "Keep food waste at or below 4% of food purchases", "waste_pct", "abs", 0.04, "ALL", "Exec Chef"),
    ("KR3.1", "O3", "Raise sales per labor hour 5% vs last year", "splh", "rel", 0.05, "ALL", "VP Operations"),
    ("KR3.2", "O3", "Bring Mizner Park labor cost down 3 pts vs last year", "labor_pct", "pts", -0.03, "L02", "GM Mizner Park"),
    ("KR3.3", "O3", "Average wait for waiting parties at or under 8 minutes", "avg_wait", "abs", 8.0, "ALL", "GMs"),
]
