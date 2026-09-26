"""Generate a realistic synthetic dataset for a 6-unit restaurant chain.

Outputs parquet files to data/raw/:
  dim_locations, dim_tables, dim_menu_items, dim_employees
  fact_checks          one row per guest check (dine-in, takeout, delivery)
  fact_check_items     one row per menu item on a check
  fact_lost_demand     walk-in parties that left because no table was free in time
  fact_labor_shifts    one row per employee shift
  fact_purchases       vendor invoices (food, beverage, packaging)
  fact_waste_log       daily food waste entries
  fact_operating_expenses  monthly opex by category

Floor usage is simulated table-by-table: each arriving party is seated at the
smallest free table that fits, waits if one frees up soon, or walks away. That
is what makes seat utilization, table turns, RevPASH and lost demand real
measurements instead of made-up columns.

Run:  python src/generate_data.py
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from config import (RAW, SEED, START, END, MENU_PRICE_INCREASE_DATE,
                    MENU_PRICE_INCREASE, TIP_CREDIT, min_wage_on)

rng = np.random.default_rng(SEED)

# --------------------------------------------------------------------------
# Locations and floor plans
# --------------------------------------------------------------------------
LOCATIONS = [
    # id, name, city, county, open_date, rent, scale, season_amp, growth, waste, staffing, layout
    dict(location_id="L01", name="Clematis Street", city="West Palm Beach", county="Palm Beach",
         open_date=date(2019, 4, 12), monthly_rent=38000, sqft=5200, scale=1.22, season_amp=1.0,
         growth=0.05, waste_rate=0.040, staffing=1.00,
         layout={"main": {2: 10, 4: 14, 6: 3}, "bar": {2: 8}, "patio": {2: 5, 4: 5}}),
    dict(location_id="L02", name="Mizner Park", city="Boca Raton", county="Palm Beach",
         open_date=date(2020, 11, 5), monthly_rent=42000, sqft=6100, scale=0.98, season_amp=1.0,
         growth=0.02, waste_rate=0.042, staffing=1.26,
         layout={"main": {2: 12, 4: 18, 6: 4}, "bar": {2: 10}, "patio": {4: 5}}),
    dict(location_id="L03", name="Las Olas", city="Fort Lauderdale", county="Broward",
         open_date=date(2021, 2, 18), monthly_rent=40000, sqft=5600, scale=1.05, season_amp=1.45,
         growth=0.06, waste_rate=0.038, staffing=1.02,
         layout={"main": {2: 10, 4: 12, 6: 2}, "bar": {2: 10}, "patio": {2: 9, 4: 8}}),
    dict(location_id="L04", name="Atlantic Ave", city="Delray Beach", county="Palm Beach",
         open_date=date(2022, 6, 1), monthly_rent=30000, sqft=4500, scale=0.80, season_amp=1.1,
         growth=0.01, waste_rate=0.092, staffing=1.03,
         layout={"main": {2: 10, 4: 12, 6: 2}, "bar": {2: 8}, "patio": {2: 4, 4: 4}}),
    dict(location_id="L05", name="Harbourside", city="Jupiter", county="Palm Beach",
         open_date=date(2022, 10, 20), monthly_rent=26000, sqft=4100, scale=0.80, season_amp=1.1,
         growth=0.045, waste_rate=0.035, staffing=0.94,
         layout={"main": {2: 8, 4: 11, 6: 2}, "bar": {2: 7}, "patio": {2: 4, 4: 4}}),
    dict(location_id="L06", name="Brickell", city="Miami", county="Miami-Dade",
         open_date=date(2025, 3, 1), monthly_rent=55000, sqft=6600, scale=1.08, season_amp=0.6,
         growth=0.03, waste_rate=0.050, staffing=1.10,
         layout={"main": {2: 14, 4: 18, 6: 4}, "bar": {2: 12}, "patio": {2: 3, 4: 4}}),
]

# Florida tourist season: winter "snowbird" peak, late-summer trough
SEASON = {1: 1.14, 2: 1.26, 3: 1.30, 4: 1.14, 5: 0.98, 6: 0.88,
          7: 0.85, 8: 0.80, 9: 0.77, 10: 0.90, 11: 1.00, 12: 1.10}
DOW = [0.74, 0.80, 0.88, 0.98, 1.30, 1.42, 1.06]            # Mon..Sun
HOURS = list(range(11, 23))                                  # 11:00 - 22:59 arrivals
DINE_PROFILE = dict(zip(HOURS, [0.55, 0.92, 0.75, 0.33, 0.24, 0.36, 0.72, 1.0, 0.95, 0.68, 0.38, 0.17]))
OFF_PROFILE = dict(zip(HOURS, [0.55, 1.0, 0.8, 0.35, 0.3, 0.45, 0.85, 0.95, 0.8, 0.5, 0.25, 0.1]))
BASE_DINE_PER_HOUR = 14.5
BASE_OFF_PER_DAY = 40.0

CLOSED = {date(2024, 11, 28), date(2024, 12, 25), date(2025, 11, 27), date(2025, 12, 25),
          date(2024, 10, 9), date(2024, 10, 10)}  # Thanksgiving, Christmas, Hurricane Milton
HOLIDAY_BOOST = {date(2025, 2, 14): ("dinner", 1.55), date(2026, 2, 14): ("dinner", 1.55),
                 date(2025, 5, 11): ("lunch", 1.7), date(2026, 5, 10): ("lunch", 1.7),
                 date(2024, 12, 31): ("dinner", 1.45), date(2025, 12, 31): ("dinner", 1.45),
                 date(2025, 7, 4): ("all", 1.2), date(2026, 7, 4): ("all", 1.2)}

# --------------------------------------------------------------------------
# Menu (price, plate cost, popularity weight). Weights are tuned so menu
# engineering produces real Stars / Plowhorses / Puzzles / Dogs.
# --------------------------------------------------------------------------
MENU = [
    ("M01", "Tuna Tartare", "Starter", 17.0, 5.60, 1.0),
    ("M02", "Crispy Calamari", "Starter", 15.0, 3.40, 1.6),
    ("M03", "Burrata & Heirloom Tomato", "Starter", 16.0, 5.90, 0.7),
    ("M04", "Conch Fritters", "Starter", 14.0, 3.10, 1.3),
    ("M05", "Soup du Jour", "Starter", 9.0, 2.00, 0.45),
    ("M06", "Grouper Sandwich", "Main", 24.0, 8.90, 1.5),
    ("M07", "Blackened Mahi Tacos", "Main", 21.0, 6.30, 1.6),
    ("M08", "Filet Mignon", "Main", 44.0, 17.50, 0.65),
    ("M09", "Braised Short Rib", "Main", 34.0, 10.20, 0.95),
    ("M10", "Shrimp & Grits", "Main", 28.0, 8.10, 1.2),
    ("M11", "Chicken Paillard", "Main", 23.0, 5.50, 0.9),
    ("M12", "Wagyu Burger", "Main", 22.0, 7.60, 1.7),
    ("M13", "Seared Scallops", "Main", 38.0, 15.20, 0.5),
    ("M14", "Vegan Grain Bowl", "Main", 19.0, 4.20, 0.35),
    ("M15", "Lobster Mac & Cheese", "Main", 36.0, 14.80, 0.8),
    ("M16", "Truffle Fries", "Side", 9.0, 1.90, 1.8),
    ("M17", "Grilled Asparagus", "Side", 9.0, 2.80, 0.6),
    ("M18", "Street Corn", "Side", 8.0, 1.80, 0.9),
    ("M19", "Key Lime Pie", "Dessert", 11.0, 2.30, 1.8),
    ("M20", "Chocolate Lava Cake", "Dessert", 12.0, 2.90, 1.0),
    ("M21", "Seasonal Sorbet", "Dessert", 9.0, 1.60, 0.4),
    ("M22", "Fresh Lemonade", "Beverage", 5.0, 0.70, 1.2),
    ("M23", "Iced Tea", "Beverage", 4.0, 0.40, 1.5),
    ("M24", "Espresso", "Beverage", 4.5, 0.60, 0.6),
    ("M25", "Sparkling Water", "Beverage", 6.0, 1.40, 0.7),
    ("M26", "House Margarita", "Bar", 14.0, 2.60, 1.6),
    ("M27", "Rosé by the Glass", "Bar", 13.0, 3.60, 1.1),
    ("M28", "Local Craft Beer", "Bar", 8.0, 2.10, 1.3),
    ("M29", "Old Fashioned", "Bar", 16.0, 3.30, 0.8),
    ("M30", "Bottle of Wine", "Bar", 58.0, 19.00, 0.25),
]
LUNCH_TILT = {"M06": 1.9, "M07": 1.5, "M12": 1.5, "M11": 1.3, "M14": 1.4,
              "M08": 0.35, "M09": 0.5, "M13": 0.4, "M15": 0.6, "M30": 0.3}

# expected units per cover by (channel group, daypart, category)
ATTACH = {
    ("dine", "lunch"):  {"Starter": .35, "Main": .97, "Side": .25, "Dessert": .15, "Beverage": .75, "Bar": .25},
    ("dine", "dinner"): {"Starter": .50, "Main": .98, "Side": .35, "Dessert": .30, "Beverage": .55, "Bar": .70},
    ("off", "lunch"):   {"Starter": .15, "Main": 1.0, "Side": .30, "Dessert": .08, "Beverage": .20, "Bar": 0.0},
    ("off", "dinner"):  {"Starter": .25, "Main": 1.0, "Side": .40, "Dessert": .12, "Beverage": .15, "Bar": 0.0},
}

# How each menu category's plate cost splits into purchasing categories
COST_SPLIT = {
    "Main": {"Protein": .58, "Produce": .17, "Dairy": .10, "Dry Goods": .15},
    "Starter": {"Protein": .45, "Produce": .25, "Dairy": .15, "Dry Goods": .15},
    "Side": {"Produce": .60, "Dairy": .15, "Dry Goods": .25},
    "Dessert": {"Dairy": .45, "Dry Goods": .40, "Produce": .15},
    "Beverage": {"Beverage": 1.0},
    "Bar": {"Alcohol": 1.0},
}
VENDORS = {"Protein": "Gulfstream Seafood & Meats", "Produce": "Sunshine Farms Produce",
           "Dairy": "Coastal Dairy Co.", "Dry Goods": "Atlantic Foodservice Supply",
           "Beverage": "Palm Beverage Distributors", "Alcohol": "Seaboard Wine & Spirits",
           "Packaging": "EcoPack Supply"}


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def years_since_start(d: date) -> float:
    return (d - START).days / 365.25


def build_tables():
    rows = []
    for loc in LOCATIONS:
        n = 0
        for zone, sizes in loc["layout"].items():
            for seats, count in sizes.items():
                for _ in range(count):
                    n += 1
                    rows.append(dict(location_id=loc["location_id"], table_id=f"{loc['location_id']}-T{n:02d}",
                                     zone=zone, seats=seats))
    return pd.DataFrame(rows)


def demand_multiplier(loc, d: date) -> float:
    if d < loc["open_date"] or d in CLOSED:
        return 0.0
    season = 1 + (SEASON[d.month] - 1) * loc["season_amp"]
    trend = (1 + loc["growth"]) ** years_since_start(d)
    ramp = 1.0
    age = (d - loc["open_date"]).days
    if age < 400:
        ramp = 0.58 + 0.42 * (1 - math.exp(-age / 130))
        if age < 21:
            ramp += 0.30                                  # grand-opening buzz
    price_elasticity = 0.985 if d >= MENU_PRICE_INCREASE_DATE else 1.0
    noise = rng.lognormal(0, 0.07)
    return loc["scale"] * season * DOW[d.weekday()] * trend * ramp * price_elasticity * noise


def party_sizes(n, daypart):
    p = [0.10, 0.46, 0.13, 0.21, 0.05, 0.05] if daypart == "lunch" else [0.06, 0.40, 0.14, 0.26, 0.07, 0.07]
    return rng.choice([1, 2, 3, 4, 5, 6], size=n, p=p)


def simulate_floor(loc, d, tables_df, mult):
    """Seat arriving parties table by table. Returns seated parties and lost parties."""
    t_ids = tables_df["table_id"].to_numpy()
    t_seats = tables_df["seats"].to_numpy()
    t_zone = tables_df["zone"].to_numpy()
    patio_closed_p = 0.32 if d.month in (6, 7, 8, 9) else 0.05
    patio_open = rng.random() > patio_closed_p
    usable = np.ones(len(t_ids), dtype=bool) if patio_open else (t_zone != "patio")
    next_free = np.zeros(len(t_ids))
    close_min = (23 if d.weekday() in (4, 5) else 22) * 60 + 30
    boost = HOLIDAY_BOOST.get(d)

    arrivals = []
    for h in HOURS:
        if h == 22 and d.weekday() not in (4, 5):
            continue
        daypart = "lunch" if h < 16 else "dinner"
        lam = BASE_DINE_PER_HOUR * DINE_PROFILE[h] * mult
        if boost and boost[0] in (daypart, "all"):
            lam *= boost[1]
        k = rng.poisson(lam)
        if k:
            mins = np.sort(rng.uniform(h * 60, h * 60 + 60, k))
            for m, ps in zip(mins, party_sizes(k, daypart)):
                arrivals.append((m, int(ps), daypart))

    seated, lost = [], []
    for m, ps, daypart in arrivals:
        fits = usable & (t_seats >= ps)
        prefer_bar = ps <= 2 and rng.random() < 0.33
        order = np.lexsort((rng.random(len(t_ids)), t_seats))  # smallest first, random tie-break
        order = order[fits[order]]
        if prefer_bar:
            bar_first = order[t_zone[order] == "bar"]
            order = np.concatenate([bar_first, order[t_zone[order] != "bar"]])
        else:
            order = order[(t_zone[order] != "bar") | (ps <= 2)]
        if len(order) == 0:
            lost.append((m, ps, None))
            continue
        free = order[next_free[order] <= m]
        tight = free[t_seats[free] <= ps + 2]
        if len(tight):
            ti, wait = tight[0], 0.0
        elif len(free):
            ti, wait = free[0], 0.0
        else:
            ti = order[np.argmin(next_free[order])]
            wait = next_free[ti] - m
            max_wait = 25 if (daypart == "dinner" and d.weekday() in (4, 5)) else 18
            accept_p = max(0.0, 1 - wait / max_wait)
            if wait > max_wait or rng.random() > accept_p ** 0.6:
                lost.append((m, ps, round(wait, 1)))
                continue
        seat_at = m + wait
        if seat_at > close_min - 30:
            lost.append((m, ps, round(wait, 1)))
            continue
        base = 50 if daypart == "lunch" else 80
        if t_zone[ti] == "bar":
            base -= 12
        dwell = rng.lognormal(math.log(base + 6 * (ps - 2) if ps > 1 else base - 8), 0.24)
        next_free[ti] = seat_at + dwell + 6          # 6 min bus/reset
        seated.append(dict(table_id=t_ids[ti], zone=t_zone[ti], party_size=ps, daypart=daypart,
                           arrival_min=m, seat_min=seat_at, close_min=seat_at + dwell,
                           wait_min=round(wait, 1)))
    return seated, lost


def to_ts(d: date, minutes: np.ndarray) -> pd.Series:
    return pd.Timestamp(d) + pd.to_timedelta(minutes, unit="m")


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    tables = build_tables()
    menu = pd.DataFrame(MENU, columns=["item_id", "item_name", "category", "base_price", "plate_cost", "weight"])

    checks, lost_rows, expected = [], [], []
    for loc in LOCATIONS:
        ltables = tables[tables.location_id == loc["location_id"]].reset_index(drop=True)
        for d in daterange(START, END):
            mult = demand_multiplier(loc, d)
            if mult == 0:
                continue
            seated, lost = simulate_floor(loc, d, ltables, mult)
            for s in seated:
                s.update(location_id=loc["location_id"], business_date=d, channel="Dine-in")
                checks.append(s)
            for m, ps, w in lost:
                lost_rows.append(dict(location_id=loc["location_id"], business_date=d,
                                      arrival_ts=pd.Timestamp(d) + pd.Timedelta(minutes=m),
                                      party_size=ps, quoted_wait_min=w))
            # off-premise orders (takeout + third-party delivery)
            n_off = rng.poisson(BASE_OFF_PER_DAY * mult / 1.03)
            if n_off:
                hrs = rng.choice(HOURS, size=n_off, p=np.array(list(OFF_PROFILE.values())) / sum(OFF_PROFILE.values()))
                mins = hrs * 60 + rng.uniform(0, 60, n_off)
                delivery_share = 0.45 + 0.13 * years_since_start(d) / 2
                for m in mins:
                    checks.append(dict(location_id=loc["location_id"], business_date=d,
                                       channel="Delivery" if rng.random() < delivery_share else "Takeout",
                                       table_id=None, zone=None,
                                       party_size=int(rng.choice([1, 2, 3, 4], p=[.42, .36, .12, .10])),
                                       daypart="lunch" if m < 16 * 60 else "dinner",
                                       arrival_min=m, seat_min=m, close_min=m + rng.uniform(12, 25), wait_min=0.0))
            expected.append((loc["location_id"], d, mult))
        print(f"  simulated {loc['name']}: {sum(1 for c in checks if c['location_id']==loc['location_id']):,} checks")

    ck = pd.DataFrame(checks)
    ck = ck.sort_values(["location_id", "business_date", "arrival_min"]).reset_index(drop=True)
    ck["check_id"] = [f"C{i:08d}" for i in range(1, len(ck) + 1)]
    bd = pd.to_datetime(ck["business_date"])
    ck["open_ts"] = bd + pd.to_timedelta(ck["seat_min"], unit="m")
    ck["close_ts"] = bd + pd.to_timedelta(ck["close_min"], unit="m")
    ck["arrival_ts"] = bd + pd.to_timedelta(ck["arrival_min"], unit="m")
    ck["open_ts"] = ck["open_ts"].dt.floor("min")
    ck["close_ts"] = ck["close_ts"].dt.floor("min")
    ck["arrival_ts"] = ck["arrival_ts"].dt.floor("min")
    ck["server_id"] = None

    # ---------------------------------------------------------------- items
    print("  building check items ...")
    n = len(ck)
    grp = np.where(ck["channel"].eq("Dine-in"), "dine", "off")
    dp = ck["daypart"].to_numpy()
    covers = ck["party_size"].to_numpy()
    is_bar = ck["zone"].eq("bar").to_numpy()
    after_increase = (bd >= pd.Timestamp(MENU_PRICE_INCREASE_DATE)).to_numpy()
    line_chk, line_item = [], []
    for cat in menu["category"].unique():
        items = menu[menu.category == cat]
        # vectorised attach rates
        rate = np.zeros(n)
        for (g, x), rates in ATTACH.items():
            mask = (grp == g) & (dp == x)
            rate[mask] = rates[cat]
        if cat == "Bar":
            rate = rate * np.where(is_bar, 1.6, 1.0)
            units = rng.poisson(covers * rate * 1.15)
        else:
            units = rng.binomial(covers, np.clip(rate, 0, 1))
        idx = np.repeat(np.arange(n), units)
        if len(idx) == 0:
            continue
        # daypart-tilted item choice
        w_l = np.array([row.weight * LUNCH_TILT.get(row.item_id, 1.0) for row in items.itertuples()])
        w_d = np.array([row.weight * (1 / LUNCH_TILT.get(row.item_id, 1.0)) ** 0.5 for row in items.itertuples()])
        lunch_mask = dp[idx] == "lunch"
        chosen = np.empty(len(idx), dtype=object)
        chosen[lunch_mask] = rng.choice(items.item_id.to_numpy(), size=lunch_mask.sum(), p=w_l / w_l.sum())
        chosen[~lunch_mask] = rng.choice(items.item_id.to_numpy(), size=(~lunch_mask).sum(), p=w_d / w_d.sum())
        line_chk.append(idx)
        line_item.append(chosen)
    li = pd.DataFrame({"row": np.concatenate(line_chk), "item_id": np.concatenate(line_item)})
    li = li.groupby(["row", "item_id"], as_index=False).size().rename(columns={"size": "quantity"})
    li = li.merge(menu[["item_id", "base_price", "plate_cost"]], on="item_id")
    li["unit_price"] = np.round(li["base_price"] * np.where(after_increase[li["row"]], 1 + MENU_PRICE_INCREASE, 1.0), 2)
    li["line_total"] = np.round(li["unit_price"] * li["quantity"], 2)
    li["theoretical_cost"] = np.round(li["plate_cost"] * li["quantity"], 2)
    li["check_id"] = ck["check_id"].to_numpy()[li["row"]]
    items_out = li[["check_id", "item_id", "quantity", "unit_price", "line_total", "theoretical_cost"]]

    sub = li.groupby("row")["line_total"].sum()
    ck["subtotal"] = np.round(sub.reindex(range(n)).fillna(0).to_numpy(), 2)
    comp = rng.random(n) < 0.03
    ck["discount"] = np.round(np.where(comp, ck["subtotal"] * rng.uniform(0.1, 0.25, n), 0), 2)
    ck["net_sales"] = ck["subtotal"] - ck["discount"]
    ck["tax"] = np.round(ck["net_sales"] * 0.07, 2)
    tip_rate = np.where(ck["channel"].eq("Dine-in"), rng.normal(0.195, 0.03, n),
                        np.where(ck["channel"].eq("Takeout"), rng.normal(0.08, 0.04, n), 0))
    ck["tip"] = np.round(np.clip(tip_rate, 0, 0.35) * ck["net_sales"], 2)
    ck["payment_type"] = np.where(rng.random(n) < 0.93, "Card", "Cash")
    ck.loc[ck["channel"].eq("Delivery"), "payment_type"] = "Platform"
    ck["delivery_platform"] = np.where(ck["channel"].eq("Delivery"),
                                       rng.choice(["DoorDash", "Uber Eats", "Grubhub"], n, p=[.55, .33, .12]), None)
    ck = ck[ck["subtotal"] > 0]
    ck_out = ck[["check_id", "location_id", "business_date", "channel", "delivery_platform", "table_id", "zone",
                 "party_size", "daypart", "arrival_ts", "open_ts", "close_ts", "wait_min", "subtotal",
                 "discount", "net_sales", "tax", "tip", "payment_type"]].rename(columns={"party_size": "covers"})
    items_out = items_out[items_out.check_id.isin(ck_out.check_id)]

    # ---------------------------------------------------------------- labor
    print("  building labor schedule ...")
    cov = ck_out.groupby(["location_id", "business_date", "daypart", "channel"])["covers"].sum().unstack(fill_value=0)
    cov["dine"] = cov.get("Dine-in", 0)
    cov["kitchen"] = cov.sum(axis=1) - cov["dine"]
    cov = cov[["dine", "kitchen"]].reset_index()
    roles = {  # role: (covers per head, basis, min heads, rate premium over min wage, tipped)
        "Server": (25, "dine", 2, 0.0, True), "Bartender": (90, "dine", 1, 0.0, True),
        "Host": (160, "dine", 1, 1.0, False), "Line Cook": (44, "kitchen", 2, 5.0, False),
        "Prep Cook": (130, "kitchen", 1, 3.0, False), "Dishwasher": (150, "kitchen", 1, 1.0, False),
    }
    staff_factor = {l["location_id"]: l["staffing"] for l in LOCATIONS}
    shift_rows = []
    emp_pool = {}
    for r in cov.itertuples():
        d = r.business_date
        mw = min_wage_on(d)
        start_h, hours = (10.5, 5.5) if r.daypart == "lunch" else (16.0, 7.0)
        for role, (ratio, basis, mn, prem, tipped) in roles.items():
            demand = (r.dine if basis == "dine" else r.dine + r.kitchen) * rng.lognormal(0, 0.08)
            heads = max(mn, math.ceil(demand / ratio * staff_factor[r.location_id]))
            rate = (mw - TIP_CREDIT) if tipped else mw + prem
            for _ in range(heads):
                h = round(hours + rng.normal(0, 0.35), 2)
                shift_rows.append((r.location_id, d, role, r.daypart, start_h, h, rate))
    for loc in LOCATIONS:  # two managers every open day
        for d in sorted(set(cov[cov.location_id == loc["location_id"]].business_date)):
            for sh in (9.0, 14.5):
                shift_rows.append((loc["location_id"], d, "Manager", "lunch" if sh < 12 else "dinner", sh, 9.0, 31.25))
    ls = pd.DataFrame(shift_rows, columns=["location_id", "business_date", "role", "daypart", "start_hour", "hours", "hourly_rate"])
    # assign employees from a pool per location/role
    emp_rows = []
    ids = []
    for (lid, role), g in ls.groupby(["location_id", "role"]):
        pool_n = max(3, int(g.groupby("business_date").size().max() * 2.2))
        pool = [f"{lid}-{role[:3].upper()}{i:03d}" for i in range(1, pool_n + 1)]
        emp_rows += [dict(employee_id=e, location_id=lid, role=role) for e in pool]
        ids.append(pd.Series(rng.choice(pool, len(g)), index=g.index))
    ls["employee_id"] = pd.concat(ids)
    ls["wage_cost"] = np.round(ls["hours"] * ls["hourly_rate"], 2)
    ls["burden_cost"] = np.round(ls["wage_cost"] * 0.12, 2)
    ls["total_labor_cost"] = ls["wage_cost"] + ls["burden_cost"]
    ls.insert(0, "shift_id", [f"S{i:07d}" for i in range(1, len(ls) + 1)])
    employees = pd.DataFrame(emp_rows)

    # ---------------------------------------------------------------- purchases & waste
    print("  building purchases and waste ...")
    li2 = items_out.merge(ck_out[["check_id", "location_id", "business_date"]], on="check_id") \
                   .merge(menu[["item_id", "category"]], on="item_id")
    daily_cost = li2.groupby(["location_id", "business_date", "category"])["theoretical_cost"].sum().reset_index()
    waste_rate = {l["location_id"]: l["waste_rate"] for l in LOCATIONS}
    rows = []
    for r in daily_cost.itertuples():
        for pcat, share in COST_SPLIT[r.category].items():
            rows.append((r.location_id, r.business_date, pcat, r.theoretical_cost * share))
    pc = pd.DataFrame(rows, columns=["location_id", "business_date", "purchase_category", "theo_cost"])
    pc = pc.groupby(["location_id", "business_date", "purchase_category"], as_index=False)["theo_cost"].sum()
    # Delray walk-in cooler failure July 2025 -> spoilage spike
    pc["waste_mult"] = 1.0
    cooler = (pc.location_id == "L04") & (pd.to_datetime(pc.business_date).between("2025-07-08", "2025-07-20"))
    pc.loc[cooler, "waste_mult"] = 3.5

    def inflation(cat, d):
        y = years_since_start(d)
        if cat == "Protein":
            return 1.09 if d >= date(2025, 6, 1) else 1.0
        if cat == "Produce":
            return 1 + 0.06 * math.sin(2 * math.pi * (d.month - 3) / 12) + 0.02 * y
        if cat == "Dairy":
            return 1 + 0.035 * y
        if cat in ("Alcohol", "Beverage"):
            return 1 + 0.02 * y
        return 1 + 0.025 * y
    pc["infl"] = [inflation(c, d) for c, d in zip(pc.purchase_category, pc.business_date)]
    food = ~pc.purchase_category.isin(["Beverage", "Alcohol"])
    wr = pc.location_id.map(waste_rate)
    pc["waste_cost"] = np.where(food, pc.theo_cost * pc.infl * wr * pc.waste_mult * rng.lognormal(0, 0.25, len(pc)), 0)
    pc["usage_cost"] = pc.theo_cost * pc.infl * 1.02 * rng.lognormal(0, 0.03, len(pc)) + pc.waste_cost

    # waste log (daily, food only)
    reasons = ["Overproduction", "Spoilage", "Prep error", "Returned by guest", "Expired"]
    wl = pc[food].groupby(["location_id", "business_date"], as_index=False)["waste_cost"].sum()
    wl["reason"] = rng.choice(reasons, len(wl), p=[.38, .27, .15, .08, .12])
    wl.loc[(wl.location_id == "L04") & pd.to_datetime(wl.business_date).between("2025-07-08", "2025-07-20"), "reason"] = "Spoilage"
    wl["waste_cost"] = wl["waste_cost"].round(2)

    # packaging for off-premise
    off = ck_out[ck_out.channel != "Dine-in"].groupby(["location_id", "business_date"]).size().reset_index(name="n")
    off["purchase_category"] = "Packaging"
    off["usage_cost"] = off["n"] * 1.35
    pc = pd.concat([pc[["location_id", "business_date", "purchase_category", "usage_cost"]],
                    off[["location_id", "business_date", "purchase_category", "usage_cost"]]])
    # roll daily usage into invoices: food twice a week (Mon/Thu), others weekly (Tue)
    bdt = pd.to_datetime(pc.business_date)
    is_food_inv = pc.purchase_category.isin(["Protein", "Produce", "Dairy"])
    inv_day = np.where(is_food_inv, np.where(bdt.dt.weekday < 3, 0, 3), 1)
    inv = bdt - pd.to_timedelta(bdt.dt.weekday, unit="D") + pd.to_timedelta(inv_day, unit="D")
    pc["invoice_date"] = inv.clip(lower=pd.Timestamp(START)).dt.date
    purchases = pc.groupby(["location_id", "invoice_date", "purchase_category"], as_index=False)["usage_cost"].sum()
    purchases = purchases[pd.to_datetime(purchases.invoice_date) <= pd.Timestamp(END)]
    purchases["vendor"] = purchases.purchase_category.map(VENDORS)
    purchases["amount"] = purchases["usage_cost"].round(2)
    purchases.insert(0, "invoice_id", [f"INV{i:07d}" for i in range(1, len(purchases) + 1)])
    purchases = purchases[["invoice_id", "location_id", "invoice_date", "vendor", "purchase_category", "amount"]]

    # ---------------------------------------------------------------- opex
    print("  building operating expenses ...")
    ck_out_m = ck_out.assign(month=pd.to_datetime(ck_out.business_date).dt.to_period("M").dt.to_timestamp())
    msales = ck_out_m.groupby(["location_id", "month"], as_index=False)["net_sales"].sum()
    card_base = ck_out_m[ck_out_m.payment_type == "Card"].assign(paid=lambda x: x.net_sales + x.tax + x.tip) \
        .groupby(["location_id", "month"])["paid"].sum()
    deliv = ck_out_m[ck_out_m.channel == "Delivery"].groupby(["location_id", "month"])["net_sales"].sum()
    loc_by_id = {l["location_id"]: l for l in LOCATIONS}
    ox = []
    for r in msales.itertuples():
        loc = loc_by_id[r.location_id]
        m = r.month
        seats = sum(s * c for z in loc["layout"].values() for s, c in z.items())
        util_mult = 1.35 if m.month in (6, 7, 8, 9) else (1.15 if m.month in (5, 10) else 1.0)
        entries = {
            "Rent & CAM": loc["monthly_rent"],
            "Utilities": seats * 38 * util_mult * rng.lognormal(0, 0.05),
            "Marketing": r.net_sales * 0.018 + (40000 if r.location_id == "L06" and m < pd.Timestamp("2025-06-01") else 0),
            "Repairs & Maintenance": r.net_sales * 0.009 * rng.lognormal(0, 0.3)
                                     + (18500 if r.location_id == "L04" and m == pd.Timestamp("2025-07-01") else 0),
            "Supplies & Smallwares": r.net_sales * 0.013 * rng.lognormal(0, 0.1),
            "Card Processing Fees": card_base.get((r.location_id, m), 0) * 0.026,
            "Delivery Commissions": deliv.get((r.location_id, m), 0) * 0.22,
            "Insurance": 3800,
            "Software & POS": 1450,
        }
        for cat, amt in entries.items():
            ox.append(dict(location_id=r.location_id, month=m.date(), expense_category=cat, amount=round(float(amt), 2)))
    opex = pd.DataFrame(ox)

    # ---------------------------------------------------------------- dims + write
    locs = pd.DataFrame([{k: v for k, v in l.items() if k not in ("layout", "scale", "season_amp", "growth",
                                                                      "waste_rate", "staffing")} for l in LOCATIONS])
    locs["seats"] = locs.location_id.map(tables.groupby("location_id")["seats"].sum())
    locs["tables"] = locs.location_id.map(tables.groupby("location_id").size())
    menu_out = menu.drop(columns="weight")
    lost_df = pd.DataFrame(lost_rows)

    out = {"dim_locations": locs, "dim_tables": tables, "dim_menu_items": menu_out, "dim_employees": employees,
           "fact_checks": ck_out, "fact_check_items": items_out, "fact_lost_demand": lost_df,
           "fact_labor_shifts": ls, "fact_purchases": purchases, "fact_waste_log": wl,
           "fact_operating_expenses": opex}
    for name, df in out.items():
        df.to_parquet(RAW / f"{name}.parquet", index=False)
        print(f"  wrote {name:<26} {len(df):>10,} rows")


if __name__ == "__main__":
    main()
