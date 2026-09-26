"""Cost projections, 12-month projected P&L, opportunity sizing and new-store economics.

Inputs : DuckDB marts + data/marts/forecast_monthly.csv
Outputs: data/marts/projected_pnl.csv, opportunities.csv, new_unit_economics.json, scenarios.csv

Cost drivers used for the projection
  * COGS %          trailing-6-month actual (already carries 2025 protein inflation and the Jan-2026 price increase)
  * Hourly labor    daily regression  hours = fixed + variable x covers  (per location), costed at the
                    projected wage: Florida minimum wage rises $14 -> $15 on 30 Sep 2026 (tipped $10.98 -> $11.98)
  * Manager labor   fixed hours x rate, +3% merit in Jan 2027
  * Opex            fixed lines (rent +3% escalator in Jan, insurance, software) + variable % of sales from TTM,
                    utilities from the same month last year +3%, delivery commissions 22% of projected delivery sales

Run:  python src/growth.py
"""
import json

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm

from config import WAREHOUSE, MARTS, END, PAYROLL_BURDEN

TTM_START = "2025-09-01"
T6M_START = "2026-03-01"
WAGE_STEP = 1.00              # $/hr on 30 Sep 2026 for min-wage-linked roles
NEW_UNIT_CAPEX = 2_300_000    # build-out + FF&E for a ~150-seat unit (assumption, stated in README)
PREOPENING = 275_000          # hiring, training, launch marketing (assumption)


def q(con, sql):
    return con.execute(sql).df()


def main():
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    fc = pd.read_csv(MARTS / "forecast_monthly.csv", parse_dates=["month"])
    sales_fc = fc[fc.metric == "net_sales"].rename(columns={"p10": "sales_p10", "p50": "sales_p50", "p90": "sales_p90"})
    cov_fc = fc[fc.metric == "covers"][["location_id", "month", "p50"]].rename(columns={"p50": "covers_p50"})
    proj = sales_fc.drop(columns="metric").merge(cov_fc, on=["location_id", "month"])

    pnl = q(con, "SELECT * FROM mart_monthly_pnl")
    pnl["month"] = pd.to_datetime(pnl["month"])
    t6 = pnl[pnl.month >= T6M_START].groupby("location_id")
    cogs_pct = (t6.total_cogs.sum() / t6.net_sales.sum()).rename("cogs_pct")

    # ---------- hourly labor model: daily hours = a + b * covers
    lab = q(con, f"""
        SELECT l.location_id, l.business_date, sum(l.hours) AS hours, sum(l.hours*l.hourly_rate)/sum(l.hours) AS avg_rate,
               any_value(d.covers) AS covers
        FROM fact_labor_shifts l JOIN mart_daily_ops d USING (location_id, business_date)
        WHERE l.role <> 'Manager' AND l.business_date >= DATE '{TTM_START}'
        GROUP BY ALL""")
    labor_model = {}
    for lid, g in lab.groupby("location_id"):
        m = sm.OLS(g.hours, sm.add_constant(g.covers)).fit()
        recent_rate = g[pd.to_datetime(g.business_date) >= "2025-10-01"].avg_rate.mean()
        labor_model[lid] = dict(fixed_hours=m.params["const"], hours_per_cover=m.params["covers"],
                                r2=m.rsquared, rate_now=recent_rate)
    mgr = q(con, f"""SELECT location_id, sum(total_labor_cost)/count(DISTINCT business_date) AS mgr_cost_per_day
                     FROM fact_labor_shifts WHERE role='Manager' AND business_date >= DATE '{TTM_START}' GROUP BY 1""") \
        .set_index("location_id").mgr_cost_per_day

    # ---------- opex drivers from TTM
    ox = q(con, f"SELECT location_id, month, expense_category, amount FROM fact_operating_expenses WHERE month >= DATE '{TTM_START}'")
    ox["month"] = pd.to_datetime(ox["month"])
    ttm_sales = pnl[pnl.month >= TTM_START].groupby("location_id").net_sales.sum()
    var_cats = ["Marketing", "Repairs & Maintenance", "Supplies & Smallwares", "Card Processing Fees"]
    var_pct = ox[ox.expense_category.isin(var_cats)].groupby("location_id").amount.sum() / ttm_sales
    fixed = ox[ox.expense_category.isin(["Rent & CAM", "Insurance", "Software & POS"])] \
        .groupby(["location_id", "expense_category"]).amount.last().unstack()
    util = ox[ox.expense_category == "Utilities"].assign(m=lambda x: x.month.dt.month) \
        .set_index(["location_id", "m"]).amount
    ms = q(con, f"SELECT location_id, sum(delivery_sales)/sum(net_sales) AS delivery_share FROM mart_monthly_sales WHERE month >= DATE '{T6M_START}' GROUP BY 1") \
        .set_index("location_id").delivery_share

    rows = []
    for r in proj.itertuples():
        lid, m = r.location_id, r.month
        days = m.days_in_month - (1 if (m.month in (11, 12)) else 0)
        lm = labor_model[lid]
        # wage step applies from 30 Sep 2026 -> effectively all projected months except 29 days of Sep
        step = WAGE_STEP * (1 / 30 if m.month == 9 and m.year == 2026 else 1)
        hourly_hours = lm["fixed_hours"] * days + lm["hours_per_cover"] * r.covers_p50
        hourly_cost = hourly_hours * (lm["rate_now"] + step) * (1 + PAYROLL_BURDEN)
        mgr_cost = mgr[lid] * days * (1.03 if m >= pd.Timestamp("2027-01-01") else 1.0)
        rent = fixed.loc[lid, "Rent & CAM"] * (1.03 if m >= pd.Timestamp("2027-01-01") else 1.0)
        opex = (rent + fixed.loc[lid, "Insurance"] + fixed.loc[lid, "Software & POS"]
                + var_pct[lid] * r.sales_p50 + util.get((lid, m.month), util.loc[lid].mean()) * 1.03
                + 0.22 * ms[lid] * r.sales_p50)
        cogs = cogs_pct[lid] * r.sales_p50
        labor = hourly_cost + mgr_cost
        rows.append(dict(location_id=lid, month=m.date().isoformat(), sales_p10=r.sales_p10, sales_p50=r.sales_p50,
                         sales_p90=r.sales_p90, covers=r.covers_p50, cogs=cogs, labor=labor, labor_hours=hourly_hours,
                         opex=opex, prime_cost=cogs + labor, ebitda=r.sales_p50 - cogs - labor - opex))
    pp = pd.DataFrame(rows)
    pp.to_csv(MARTS / "projected_pnl.csv", index=False)

    # ---------- opportunity sizing (annualised, trailing twelve months)
    ttm = pnl[pnl.month >= TTM_START].groupby("location_id").sum(numeric_only=True)
    ttm["splh"] = ttm.net_sales / ttm.labor_hours
    ttm["waste_pct"] = ttm.waste_cost / ttm.food_cogs
    ttm["food_var"] = (ttm.food_cogs - ttm.theoretical_food_cost) / ttm.food_sales
    ttm["spc"] = ttm.net_sales / ttm.covers
    dl = q(con, f"""SELECT location_id, sum(hours) AS hours, sum(labor_cost) AS cost FROM mart_monthly_labor
                    WHERE month >= DATE '{TTM_START}' AND role <> 'Manager' GROUP BY 1""").set_index("location_id")
    lost = q(con, f"""SELECT location_id, count(*) AS parties, sum(party_size) AS covers,
                             avg(CASE WHEN party_size >= 5 THEN 1.0 ELSE 0 END) AS large_share
                      FROM fact_lost_demand WHERE business_date >= DATE '{TTM_START}' GROUP BY 1""").set_index("location_id")
    deliv = q(con, f"SELECT location_id, sum(delivery_sales) AS d FROM mart_monthly_sales WHERE month >= DATE '{TTM_START}' GROUP BY 1").set_index("location_id").d
    me = q(con, "SELECT * FROM mart_menu_engineering WHERE location_id <> 'ALL' AND menu_class = 'Plowhorse' AND category = 'Main'")

    opp = []
    hourly_splh = ttm.net_sales / dl.hours
    for lid in ttm.index:
        peers = [x for x in ttm.index if x != lid]
        # 1 capacity: recover 40% of walk-aways (waitlist app + convert 2-tops to combinable 4/6 tops)
        opp.append(dict(location_id=lid, lever="Recover walk-away demand",
                        driver=f"{int(lost.loc[lid,'parties']):,} parties ({lost.loc[lid,'large_share']*100:.0f}% were 5-6 guests) left without a table",
                        annual_value=0.40 * lost.loc[lid, "covers"] * ttm.loc[lid, "spc"] * 0.34,   # flow-through at contribution margin
                        type="Revenue (profit shown)"))
        # 2 labor: bring hourly-staff SPLH up to peer median
        target = np.median([hourly_splh[p] for p in peers])
        if hourly_splh[lid] < target:
            excess_hours = dl.loc[lid, "hours"] - ttm.loc[lid, "net_sales"] / target
            opp.append(dict(location_id=lid, lever="Staff to demand",
                            driver=f"Hourly-staff SPLH ${hourly_splh[lid]:.0f} vs peer median ${target:.0f}",
                            annual_value=excess_hours * dl.loc[lid, "cost"] / dl.loc[lid, "hours"], type="Cost"))
        # 3 waste: bring to peer median
        wt = np.median([ttm.loc[p, "waste_pct"] for p in peers])
        if ttm.loc[lid, "waste_pct"] > wt:
            opp.append(dict(location_id=lid, lever="Cut food waste",
                            driver=f"Waste {ttm.loc[lid,'waste_pct']*100:.1f}% of food spend vs peer median {wt*100:.1f}%",
                            annual_value=(ttm.loc[lid, "waste_pct"] - wt) * ttm.loc[lid, "food_cogs"], type="Cost"))
        # 4 portion control: close a quarter of the theoretical-vs-actual gap (excluding waste)
        gap = ttm.loc[lid, "food_cogs"] - ttm.loc[lid, "theoretical_food_cost"] - ttm.loc[lid, "waste_cost"]
        opp.append(dict(location_id=lid, lever="Portion & recipe control",
                        driver=f"Actual food cost runs {ttm.loc[lid,'food_var']*100:.1f} pts above recipe cost",
                        annual_value=0.25 * max(gap, 0), type="Cost"))
        # 5 menu: +$1 on plowhorse mains, assume 3% unit loss on those items
        mh = me[me.location_id == lid]
        opp.append(dict(location_id=lid, lever="Re-price plowhorse mains (+$1)",
                        driver=f"{len(mh)} popular, low-margin mains: {', '.join(mh.item_name.head(3))}",
                        annual_value=float((mh.units * 0.97 * 1.0 - mh.units * 0.03 * mh.cm_per_unit).sum()), type="Revenue (profit shown)"))
        # 6 delivery: move 20% of third-party orders to first-party ordering (22% -> 5% fee)
        opp.append(dict(location_id=lid, lever="Shift delivery to own channel",
                        driver=f"${deliv[lid]/1e3:,.0f}k third-party delivery sales at 22% commission",
                        annual_value=0.20 * deliv[lid] * (0.22 - 0.05), type="Cost"))
    op = pd.DataFrame(opp)
    op["annual_value"] = op.annual_value.round(0)
    op.to_csv(MARTS / "opportunities.csv", index=False)

    # ---------- new unit economics (from mature stores + Brickell's observed ramp)
    mature = ["L01", "L02", "L03", "L04", "L05"]
    seats = q(con, "SELECT location_id, seats FROM dim_locations").set_index("location_id").seats
    m_ttm = ttm.loc[mature]
    sales_per_seat = (m_ttm.net_sales / seats[mature]).median()
    margin = (m_ttm.four_wall_ebitda / m_ttm.net_sales).median()
    l6 = pnl[pnl.location_id == "L06"].sort_values("month").reset_index(drop=True)
    mature_run = l6.net_sales.iloc[-12:].mean() * 1.0
    ramp_curve = (l6.net_sales / l6.net_sales.iloc[-6:].mean()).clip(upper=1.2).round(3).tolist()
    ramp_margin = (l6.four_wall_ebitda / l6.net_sales).round(3).tolist()
    unit_seats = 150
    mature_sales = sales_per_seat * unit_seats
    y1_sales = mature_sales * float(np.mean(ramp_curve[:12]))
    y1_ebitda = float(np.sum(np.array(ramp_curve[:12]) * mature_sales / 12 * np.array(ramp_margin[:12])))
    months, cum = 0, -(NEW_UNIT_CAPEX + PREOPENING)
    cash_curve = [cum]
    while cum < 0 and months < 120:
        f = ramp_curve[months] if months < len(ramp_curve) else 1.0
        mg = ramp_margin[months] if months < len(ramp_margin) else margin
        cum += mature_sales / 12 * f * mg
        cash_curve.append(cum)
        months += 1
    nue = dict(seats=unit_seats, capex=NEW_UNIT_CAPEX, preopening=PREOPENING,
               sales_per_seat=round(sales_per_seat), mature_annual_sales=round(mature_sales),
               mature_ebitda_margin=round(margin, 3), year1_sales=round(y1_sales), year1_ebitda=round(y1_ebitda),
               mature_annual_ebitda=round(mature_sales * margin),
               cash_on_cash_mature=round(mature_sales * margin / (NEW_UNIT_CAPEX + PREOPENING), 3),
               payback_months=months, ramp_curve=ramp_curve, ramp_margin=ramp_margin,
               cash_curve=[round(c) for c in cash_curve],
               sales_to_investment=round(mature_sales / (NEW_UNIT_CAPEX + PREOPENING), 2))
    (MARTS / "new_unit_economics.json").write_text(json.dumps(nue, indent=2))

    # ---------- chain scenarios, next 12 months
    base = pp[["sales_p50", "cogs", "labor", "opex", "ebitda"]].sum()
    cons_sales = pp.sales_p10.sum()
    var_cost_ratio = (base.cogs + 0.35 * base.labor) / base.sales_p50
    fixed_cost = 0.65 * base.labor + base.opex
    captured = op.annual_value.sum() * 0.5
    scen = pd.DataFrame([
        dict(scenario="Conservative", sales=cons_sales, note="P10 sales; COGS +1 pt (protein inflation continues)",
             ebitda=cons_sales - cons_sales * (var_cost_ratio + 0.01) - fixed_cost),
        dict(scenario="Base", sales=base.sales_p50, note="P50 forecast; current cost structure + Sep-2026 wage step",
             ebitda=base.ebitda),
        dict(scenario="Optimistic", sales=pp.sales_p90.sum() + 0.5 * 0.40 * float((lost.covers * ttm.spc).sum()),
             note="P90 sales + half of the operating opportunities delivered",
             ebitda=pp.sales_p90.sum() - pp.sales_p90.sum() * var_cost_ratio - fixed_cost + captured),
    ])
    scen.to_csv(MARTS / "scenarios.csv", index=False)

    print("labor model:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in labor_model.items()})
    print(pp.groupby("location_id")[["sales_p50", "ebitda"]].sum().assign(m=lambda x: x.ebitda / x.sales_p50).round(3))
    print(op.groupby("lever").annual_value.sum().sort_values(ascending=False))
    print(scen)
    print({k: v for k, v in nue.items() if k not in ("ramp_curve", "ramp_margin", "cash_curve")})


if __name__ == "__main__":
    main()
