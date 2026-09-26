"""Export every metric the dashboard needs into one JSON blob and bake it into dashboard/index.html.

Run:  python src/export_dashboard.py
"""
import json
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from config import WAREHOUSE, MARTS, DASHBOARD_DIR, CHAIN_NAME, START, END

TTM_START = "2025-09-01"
PRIOR_START, PRIOR_END = "2024-09-01", "2025-08-31"

SUM_COLS = ["net_sales", "covers", "checks", "total_cogs", "food_cogs", "beverage_cogs", "packaging_cogs",
            "theoretical_food_cost", "food_sales", "beverage_sales", "waste_cost", "labor_cost", "labor_hours",
            "opex", "occupancy", "delivery_commissions", "prime_cost", "four_wall_ebitda"]


def ratios(s: pd.Series | pd.DataFrame):
    """Compute KPI ratios from summed columns (works on a Series or DataFrame)."""
    f = lambda a, b: (a / b)
    return dict(
        net_sales=s["net_sales"], covers=s["covers"], checks=s["checks"],
        avg_check=f(s["net_sales"], s["checks"]), sales_per_cover=f(s["net_sales"], s["covers"]),
        food_cost_pct=f(s["food_cogs"], s["food_sales"]), theo_food_cost_pct=f(s["theoretical_food_cost"], s["food_sales"]),
        bev_cost_pct=f(s["beverage_cogs"], s["beverage_sales"]), cogs_pct=f(s["total_cogs"], s["net_sales"]),
        labor_pct=f(s["labor_cost"], s["net_sales"]), prime_cost_pct=f(s["prime_cost"], s["net_sales"]),
        occupancy_pct=f(s["occupancy"], s["net_sales"]), ebitda=s["four_wall_ebitda"],
        ebitda_margin=f(s["four_wall_ebitda"], s["net_sales"]), waste_pct=f(s["waste_cost"], s["food_cogs"]),
        splh=f(s["net_sales"], s["labor_hours"]), cplh=f(s["covers"], s["labor_hours"]),
        total_cogs=s["total_cogs"], labor_cost=s["labor_cost"], opex=s["opex"], waste_cost=s["waste_cost"],
        delivery_commissions=s["delivery_commissions"],
    )


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o != o) else round(float(o), 4)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp, date)):
        return o.isoformat()[:10]
    return o


def main():
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    q = lambda sql: con.execute(sql).df()

    locs = q("SELECT location_id, name, city, county, open_date, seats, tables, sqft FROM dim_locations ORDER BY 1")
    ids = locs.location_id.tolist()
    scopes = ["ALL"] + ids

    pnl = q("SELECT * FROM mart_monthly_pnl ORDER BY location_id, month")
    ms = q("SELECT * FROM mart_monthly_sales ORDER BY location_id, month")
    seats = locs.set_index("location_id").seats

    # ---------------- monthly series per scope
    def monthly_for(scope):
        p = pnl if scope == "ALL" else pnl[pnl.location_id == scope]
        m = ms if scope == "ALL" else ms[ms.location_id == scope]
        g = p.groupby("month")[SUM_COLS].sum()
        out = pd.DataFrame(ratios(g))
        m = m.assign(seat_hours=m.dine_in_sales / m.revpash)
        m = m.assign(util_w=m.seat_utilization * m.seat_hours, turns_w=m.table_turns_per_day * m.open_days)
        mm = m.groupby("month")[["dine_in_sales", "takeout_sales", "delivery_sales", "seat_hours", "util_w", "turns_w",
                                 "open_days", "lost_covers", "lost_parties", "lunch_sales", "dinner_sales"]].sum()
        for c in ["dine_in_sales", "takeout_sales", "delivery_sales", "lost_covers", "lunch_sales", "dinner_sales"]:
            out[c] = mm[c]
        out["revpash"] = mm.dine_in_sales / mm.seat_hours
        out["seat_utilization"] = mm.util_w / mm.seat_hours
        out["table_turns"] = mm.turns_w / mm.open_days
        out = out.reset_index()
        out["month"] = out["month"].astype(str).str[:10]
        return out.to_dict(orient="list")

    monthly = {s: monthly_for(s) for s in scopes}

    # ---------------- headline KPIs: TTM vs prior 12 months
    def window(scope, a, b):
        p = pnl if scope == "ALL" else pnl[pnl.location_id == scope]
        p = p[(p.month >= pd.Timestamp(a)) & (p.month <= pd.Timestamp(b))]
        if p.empty:
            return None
        r = ratios(p[SUM_COLS].sum())
        d = q(f"""SELECT sum(occupied_seat_min)/sum(available_seat_min) AS util, sum(dine_sales)/(sum(available_seat_min)/60) AS revpash
                  FROM mart_floor_hourly WHERE business_date BETWEEN DATE '{a}' AND DATE '{b}'
                  {'' if scope == 'ALL' else f"AND location_id = '{scope}'"}""").iloc[0]
        o = q(f"""SELECT sum(dine_in_parties)::DOUBLE/sum(tables) AS turns, sum(lost_covers) AS lost_covers,
                         sum(avg_dwell_min*dine_in_parties)/sum(dine_in_parties) AS dwell,
                         sum(delivery_sales+takeout_sales)/sum(net_sales) AS offprem_share
                  FROM mart_daily_ops WHERE business_date BETWEEN DATE '{a}' AND DATE '{b}'
                  {'' if scope == 'ALL' else f"AND location_id = '{scope}'"}""").iloc[0]
        r.update(seat_utilization=d.util, revpash=d.revpash, table_turns=o.turns, lost_covers=o.lost_covers,
                 avg_dwell=o.dwell, offprem_share=o.offprem_share)
        return r

    headline = {}
    for s in scopes:
        cur = window(s, TTM_START, END.isoformat())
        prior = None
        if s == "ALL" or pd.Timestamp(locs.set_index("location_id").open_date.get(s, START)) <= pd.Timestamp(PRIOR_START):
            prior = window(s, PRIOR_START, PRIOR_END)
        headline[s] = dict(ttm=cur, prior=prior)

    # ---------------- floor heatmap (dow x hour), TTM
    hm = q(f"""SELECT location_id, isodow(business_date) AS dow, hr,
                      sum(occupied_seat_min) AS occ, sum(available_seat_min) AS avail, sum(dine_sales) AS sales
               FROM mart_floor_hourly WHERE business_date >= DATE '{TTM_START}' GROUP BY ALL""")
    heat = {}
    for s in scopes:
        h = hm if s == "ALL" else hm[hm.location_id == s]
        g = h.groupby(["dow", "hr"])[["occ", "avail", "sales"]].sum()
        g["util"] = g.occ / g.avail
        g["revpash"] = g.sales / (g.avail / 60)
        heat[s] = [dict(dow=int(d), hr=int(hh), util=r.util, revpash=r.revpash) for (d, hh), r in g.iterrows()]

    # ---------------- spend breakdowns, TTM
    pur = q(f"SELECT location_id, purchase_category AS cat, sum(amount) AS amt FROM mart_monthly_purchases WHERE month >= DATE '{TTM_START}' GROUP BY ALL")
    opx = q(f"SELECT location_id, expense_category AS cat, sum(amount) AS amt FROM fact_operating_expenses WHERE month >= DATE '{TTM_START}' GROUP BY ALL")
    lab = q(f"SELECT location_id, role AS cat, sum(labor_cost) AS amt, sum(hours) AS hours FROM mart_monthly_labor WHERE month >= DATE '{TTM_START}' GROUP BY ALL")
    wst = q(f"SELECT location_id, reason AS cat, sum(waste_cost) AS amt FROM mart_monthly_waste WHERE month >= DATE '{TTM_START}' GROUP BY ALL")
    purm = q("SELECT location_id, month, purchase_category AS cat, sum(amount) AS amt FROM mart_monthly_purchases GROUP BY ALL")

    def by_cat(df, s, extra=None):
        d = df if s == "ALL" else df[df.location_id == s]
        cols = ["amt"] + ([extra] if extra else [])
        g = d.groupby("cat")[cols].sum().sort_values("amt", ascending=False)
        return [dict(cat=c, **{k: r[k] for k in cols}) for c, r in g.iterrows()]

    spend = {s: dict(purchases=by_cat(pur, s), opex=by_cat(opx, s), labor=by_cat(lab, s, "hours"), waste=by_cat(wst, s))
             for s in scopes}
    purchase_trend = {}
    for s in scopes:
        d = purm if s == "ALL" else purm[purm.location_id == s]
        g = d.groupby(["month", "cat"]).amt.sum().unstack(fill_value=0)
        purchase_trend[s] = dict(month=[str(x)[:10] for x in g.index], series={c: g[c].tolist() for c in g.columns})

    # ---------------- menu engineering
    me = q("SELECT location_id, item_name, category, units, sales, cm_per_unit, total_cm, menu_mix_pct, avg_cm_category, popularity_threshold, menu_class FROM mart_menu_engineering")
    menu = {s: me[me.location_id == s].drop(columns="location_id").to_dict(orient="records") for s in scopes}

    # ---------------- table fit & lost demand & zones & daypart labor
    tf = q("SELECT * FROM mart_table_fit")
    lost_ps = q(f"SELECT location_id, party_size, count(*) AS parties FROM fact_lost_demand WHERE business_date >= DATE '{TTM_START}' GROUP BY ALL")
    seated_ps = q(f"SELECT location_id, covers AS party_size, count(*) AS parties FROM fact_checks WHERE channel='Dine-in' AND business_date >= DATE '{TTM_START}' GROUP BY ALL")
    zones = q(f"""SELECT location_id, zone, sum(parties) AS parties, sum(covers) AS covers, sum(net_sales) AS sales,
                         sum(avg_dwell_min*parties)/sum(parties) AS dwell FROM mart_zone_usage WHERE month >= DATE '{TTM_START}' GROUP BY ALL""")
    zone_seats = q("SELECT location_id, zone, sum(seats) AS seats FROM dim_tables GROUP BY ALL")
    dpl = q("SELECT * FROM mart_daypart_labor")
    floor = {}
    for s in scopes:
        f = (lambda d: d if s == "ALL" else d[d.location_id == s])
        t = f(tf)
        fit = t.groupby("table_size").agg(parties=("parties", "sum"), empty_chairs=("empty_chairs", "sum")).reset_index()
        fit["seats_offered"] = fit.table_size * fit.parties
        fit["fill_rate"] = 1 - fit.empty_chairs / fit.seats_offered
        lp = f(lost_ps).groupby("party_size").parties.sum()
        sp = f(seated_ps).groupby("party_size").parties.sum()
        z = f(zones).groupby("zone")[["parties", "covers", "sales"]].sum().join(f(zone_seats).groupby("zone").seats.sum())
        z["sales_per_seat"] = z.sales / z.seats
        dl = f(dpl).groupby("daypart")[["hours", "labor_cost", "net_sales", "covers"]].sum()
        dl["splh"] = dl.net_sales / dl.hours
        dl["labor_pct"] = dl.labor_cost / dl.net_sales
        floor[s] = dict(
            table_fit=fit[["table_size", "parties", "fill_rate"]].to_dict(orient="records"),
            party_mix=[dict(party_size=int(p), seated=int(sp.get(p, 0)), lost=int(lp.get(p, 0))) for p in range(1, 7)],
            zones=[dict(zone=k, **r) for k, r in z.to_dict(orient="index").items()],
            daypart_labor=[dict(daypart=k, **r) for k, r in dl.to_dict(orient="index").items()],
        )

    # ---------------- projections
    fc = pd.read_csv(MARTS / "forecast_monthly.csv")
    pp = pd.read_csv(MARTS / "projected_pnl.csv")
    acc = pd.read_csv(MARTS / "forecast_accuracy.csv")
    mi = pd.read_csv(MARTS / "forecast_model_info.csv")
    bt = pd.read_csv(MARTS / "forecast_backtest.csv")
    proj = {}
    for s in scopes:
        f = fc[(fc.metric == "net_sales") & ((fc.location_id == s) | (s == "ALL"))].groupby("month")[["p10", "p50", "p90"]].sum()
        c = fc[(fc.metric == "covers") & ((fc.location_id == s) | (s == "ALL"))].groupby("month").p50.sum()
        p = pp[(pp.location_id == s) | (s == "ALL")].groupby("month")[["sales_p50", "cogs", "labor", "opex", "ebitda", "prime_cost"]].sum()
        b = bt[(bt.metric == "net_sales") & ((bt.location_id == s) | (s == "ALL"))].groupby("month")[["actual", "model", "naive"]].sum()
        proj[s] = dict(month=f.index.tolist(), p10=f.p10.tolist(), p50=f.p50.tolist(), p90=f.p90.tolist(),
                       covers=c.tolist(), cogs=p.cogs.tolist(), labor=p.labor.tolist(), opex=p.opex.tolist(),
                       ebitda=p.ebitda.tolist(),
                       backtest=[dict(month=k, **r) for k, r in b.to_dict(orient="index").items()])

    op = pd.read_csv(MARTS / "opportunities.csv")
    sc = pd.read_csv(MARTS / "scenarios.csv")
    nue = json.loads((MARTS / "new_unit_economics.json").read_text())

    data = clean(dict(
        meta=dict(chain=CHAIN_NAME, start=START.isoformat(), end=END.isoformat(), ttm_start=TTM_START,
                  generated=date.today().isoformat(),
                  locations=locs.assign(open_date=locs.open_date.astype(str)).to_dict(orient="records")),
        headline=headline, monthly=monthly, heat=heat, spend=spend, purchase_trend=purchase_trend,
        menu=menu, floor=floor, projections=proj,
        accuracy=acc.to_dict(orient="records"), model_info=mi.to_dict(orient="records"),
        opportunities=op.to_dict(orient="records"), scenarios=sc.to_dict(orient="records"), new_unit=nue,
    ))
    MARTS.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":"))
    (MARTS / "dashboard_data.json").write_text(payload)
    tpl = (DASHBOARD_DIR / "template.html").read_text()
    (DASHBOARD_DIR / "index.html").write_text(tpl.replace("/*__DATA__*/null", payload))
    print(f"  dashboard data {len(payload)/1024:.0f} KB -> dashboard/index.html")


if __name__ == "__main__":
    main()
