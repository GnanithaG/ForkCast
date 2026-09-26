"""Evaluation engine: turns the live POS data into KPIs, OKR progress, pain-point
alerts, intraday pacing and forecasts, all written back to Postgres for Power BI.

Each run:
  1. refresh the analytics facts incrementally (analytics.refresh from yesterday)
  2. forecast daily sales & covers per store (once per business day)
  3. build today's expected sales curve (analytics.pacing_today)
  4. compute every KPI x store x period, with last-year comparison and status (analytics.kpi_snapshot)
  5. score each OKR key result against its target and pace (analytics.okr_progress)
  6. detect pain points and size their annual $ impact (analytics.alerts)

Run:
    python realtime/evaluator.py --once          # single evaluation
    python realtime/evaluator.py --interval 120  # every 2 minutes (default 300)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from db import connect, run_sql_file  # noqa: E402
from definitions import KPIS, compute_kpis, status_of, OKR_CYCLE, OBJECTIVES, KEY_RESULTS  # noqa: E402
import forecast as FC  # noqa: E402

SUM_COLS = ["net_sales", "gross_sales", "discounts", "checks", "covers", "dine_in_sales", "takeout_sales",
            "delivery_sales", "dine_in_checks", "dine_large_parties", "dwell_min_total", "parties_waited",
            "wait_min_total", "lost_parties", "lost_covers", "lost_large_parties", "available_seat_min",
            "occupied_seat_min", "labor_hours", "labor_cost", "hourly_labor_hours", "hourly_labor_cost", "food_sales",
            "beverage_sales", "theoretical_food_cost", "purchases_food", "purchases_bev", "purchases_packaging",
            "waste_cost", "delivery_commission_est"]
STATUS_SORT = {"critical": 1, "warning": 2, "good": 3, "info": 4, "n/a": 5}
SEV_SORT = {"critical": 1, "warning": 2, "info": 3}
FLOW_THROUGH = 0.34      # profit on an incremental dollar of sales after food and variable labor


# ------------------------------------------------------------------ helpers
def q(con, sql, params=None) -> pd.DataFrame:
    cur = con.execute(sql, params)
    cols = [c.name for c in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


class Daily:
    """In-memory copy of analytics.daily_location for fast window sums."""
    def __init__(self, con):
        df = q(con, "SELECT * FROM analytics.daily_location")
        for c in SUM_COLS + ["tables"]:
            df[c] = pd.to_numeric(df[c]).astype(float)
        df["business_date"] = pd.to_datetime(df["business_date"]).dt.date
        self.df = df

    def sums(self, scope: str, d0: date, d1: date):
        m = (self.df.business_date >= d0) & (self.df.business_date <= d1)
        if scope != "ALL":
            m &= self.df.location_id == scope
        part = self.df[m]
        if part.empty or part.net_sales.sum() == 0:
            return None
        s = part[SUM_COLS].sum().to_dict()
        s["table_days"] = part["tables"].sum()
        s["days"] = part.business_date.nunique()
        return s

    def kpis(self, scope, d0, d1):
        s = self.sums(scope, d0, d1)
        return (compute_kpis(s), s) if s else (None, None)


def upsert_alert(rows, rule_id, loc, category, severity, title, metric, value, benchmark, impact, evidence, action, horizon):
    rows.append(dict(alert_key=f"{rule_id}:{loc}", rule_id=rule_id, location_id=loc, category=category,
                     severity=severity, severity_sort=SEV_SORT[severity], title=title, metric=metric,
                     value=None if value is None else float(value), benchmark=None if benchmark is None else float(benchmark),
                     est_annual_impact=None if impact is None else round(float(impact)), evidence=evidence,
                     recommended_action=action, horizon=horizon))


# ------------------------------------------------------------------ steps
def write_definitions(con):
    with con.cursor() as cur:
        cur.execute("DELETE FROM analytics.kpi_definitions")
        cur.executemany("INSERT INTO analytics.kpi_definitions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(k, *v, i) for i, (k, v) in enumerate(KPIS.items(), 1)])
        cycle, cs, ce = OKR_CYCLE
        cur.execute("DELETE FROM analytics.okr_objectives")
        cur.executemany("INSERT INTO analytics.okr_objectives VALUES (%s,%s,%s,%s,%s,%s)",
                        [(o, t, own, cycle, cs, ce) for o, t, own in OBJECTIVES])
        cur.execute("DELETE FROM analytics.okr_key_results")
        cur.executemany("INSERT INTO analytics.okr_key_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", KEY_RESULTS)


def run_forecast(con, today: date, daily: Daily, horizon_days=366, sims=300):
    """Fit the seasonal regression per store on completed days and store a daily forecast."""
    if con.execute("SELECT 1 FROM analytics.forecast_daily WHERE run_date=%s LIMIT 1", (today,)).fetchone():
        return False
    df = daily.df[daily.df.business_date < today].copy()
    df["business_date"] = pd.to_datetime(df.business_date)
    locs = q(con, "SELECT location_id, open_date FROM pos.dim_locations ORDER BY 1")
    mature = [l for l, od in zip(locs.location_id, locs.open_date) if pd.Timestamp(od) < FC.EPOCH]
    horizon = pd.date_range(pd.Timestamp(today), periods=horizon_days, freq="D")
    closed = {pd.Timestamp(x) for x in ("2026-11-26", "2026-12-25", "2027-11-25", "2027-12-25")}
    horizon = horizon[~horizon.isin(list(closed))]
    out = {}
    for metric in ("net_sales", "covers"):
        pooled = []
        for lid in mature:
            s = df[df.location_id == lid].set_index("business_date")[metric].rename("y").to_frame()
            m, _ = FC.fit_location(s, pd.Timestamp("2019-01-01"))
            pooled.append(m.params[[c for c in m.params.index if c.startswith(("sin", "cos")) or c in ("trend", "price_step")]])
        coef = pd.concat(pooled, axis=1).mean(axis=1)

        def chain_season(dates, coef=coef):
            dates = pd.DatetimeIndex(dates)
            X = FC.fourier(dates)
            X["trend"] = (dates - FC.EPOCH).days / 365.25
            X["price_step"] = (dates >= pd.Timestamp(FC.MENU_PRICE_INCREASE_DATE)).astype(float)
            return X[coef.index] @ coef

        for lid, od in zip(locs.location_id, locs.open_date):
            s = df[df.location_id == lid].set_index("business_date")[metric].rename("y").to_frame()
            s = s[s.y > 0]
            cs = None if lid in mature else chain_season
            m, ramp = FC.fit_location(s, pd.Timestamp(od), cs)
            lp = FC.predict(m, horizon, pd.Timestamp(od), ramp, cs).to_numpy()
            sim = np.exp(lp[None, :] + FC.block_bootstrap(m.resid.to_numpy(), len(horizon), sims=sims))
            out[(lid, metric)] = (np.percentile(sim, 10, axis=0), np.percentile(sim, 50, axis=0), np.percentile(sim, 90, axis=0))
    rows = []
    for lid in locs.location_id:
        p10, p50, p90 = out[(lid, "net_sales")]
        c50 = out[(lid, "covers")][1]
        for i, d in enumerate(horizon):
            rows.append((today, lid, d.date(), round(p10[i], 2), round(p50[i], 2), round(p90[i], 2), round(c50[i], 1)))
    with con.cursor() as cur:
        cur.execute("DELETE FROM analytics.forecast_daily WHERE run_date=%s", (today,))
        cur.executemany("INSERT INTO analytics.forecast_daily VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
    return True


def build_pacing(con, today: date):
    """Expected sales per hour today = average of the last 4 same weekdays, scaled to today's forecast."""
    ref = [today - timedelta(days=7 * k) for k in range(1, 5)]
    h = q(con, """SELECT location_id, business_date, hr, net_sales, covers FROM analytics.hourly_floor
                  WHERE business_date = ANY(%s)""", (ref,))
    if h.empty:
        return
    h[["net_sales", "covers"]] = h[["net_sales", "covers"]].astype(float)
    ndays = h.groupby("location_id").business_date.nunique().rename("ndays")
    prof = h.groupby(["location_id", "hr"], as_index=False)[["net_sales", "covers"]].sum().join(ndays, on="location_id")
    prof["net_sales"] /= prof["ndays"]
    prof["covers"] /= prof["ndays"]
    fc = q(con, "SELECT location_id, sales_p50, covers_p50 FROM analytics.forecast_latest WHERE date=%s", (today,))
    fc = fc.set_index("location_id").astype(float) if not fc.empty else pd.DataFrame()
    rows = []
    for lid, g in prof.groupby("location_id"):
        g = g.sort_values("hr")
        scale = 1.0
        if lid in fc.index and g.net_sales.sum() > 0:
            scale = fc.loc[lid, "sales_p50"] / g.net_sales.sum()
        cscale = fc.loc[lid, "covers_p50"] / g.covers.sum() if lid in fc.index and g.covers.sum() > 0 else 1.0
        cum = 0.0
        for r in g.itertuples():
            es = r.net_sales * scale
            cum += es
            rows.append((lid, today, int(r.hr), round(es, 2), round(r.covers * cscale, 1), round(cum, 2)))
    with con.cursor() as cur:
        cur.execute("DELETE FROM analytics.pacing_today")
        cur.executemany("INSERT INTO analytics.pacing_today VALUES (%s,%s,%s,%s,%s,%s)", rows)


def kpi_snapshot(con, daily: Daily, today: date, scopes, clock):
    periods = [
        ("Today", 1, today, today),
        ("Yesterday", 2, today - timedelta(days=1), today - timedelta(days=1)),
        ("Week to date", 3, today - timedelta(days=today.weekday()), today),
        ("Month to date", 4, today.replace(day=1), today),
        ("Last 28 days", 5, today - timedelta(days=28), today - timedelta(days=1)),
        ("Last 365 days", 6, today - timedelta(days=365), today - timedelta(days=1)),
    ]
    rows = []
    for scope in scopes:
        for pname, psort, d0, d1 in periods:
            cur, _ = daily.kpis(scope, d0, d1)
            if cur is None:
                continue
            ly = None
            if pname != "Today":
                ly, sly = daily.kpis(scope, d0 - timedelta(days=364), d1 - timedelta(days=364))
                if ly is not None and sly["days"] < 0.9 * ((d1 - d0).days + 1):
                    ly = None       # store wasn't open for the whole comparison period
            ndays = (d1 - d0).days + 1
            for k, v in cur.items():
                if v is None or ndays < KPIS[k][6]:
                    continue
                lyv = ly.get(k) if ly else None
                st = status_of(k, v, lyv)
                chg = (v / lyv - 1) if (lyv not in (None, 0) and KPIS[k][2] in ("$", "#", "x", "min")) else \
                      ((v - lyv) if lyv is not None else None)
                rows.append((clock, scope, pname, psort, d0, d1, k, float(v), None if lyv is None else float(lyv),
                             None if chg is None else float(chg), KPIS[k][4], st, STATUS_SORT[st]))
    with con.cursor() as cur:
        cur.execute("DELETE FROM analytics.kpi_snapshot")
        cur.executemany("INSERT INTO analytics.kpi_snapshot VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return len(rows)


def okr_progress(con, daily: Daily, today: date, scopes, clock):
    cycle, cs, ce = OKR_CYCLE
    cs, ce = date.fromisoformat(cs), date.fromisoformat(ce)
    d1 = min(today - timedelta(days=1), ce)
    if d1 < cs:
        return
    elapsed = ((d1 - cs).days + 1) / ((ce - cs).days + 1)
    rows = []
    for kr_id, obj, desc, kpi, kind, amount, kr_scope, owner in KEY_RESULTS:
        for scope in ([kr_scope] if kr_scope != "ALL" else scopes):
            cur, s = daily.kpis(scope, cs, d1)
            ly, sly = daily.kpis(scope, cs - timedelta(days=364), d1 - timedelta(days=364))
            if cur is None or cur.get(kpi) is None:
                continue
            c = cur[kpi]
            b = ly.get(kpi) if ly else None
            if kind == "cum":
                full_ly = daily.sums(scope, cs - timedelta(days=364), ce - timedelta(days=364))
                if not full_ly or b is None:
                    continue
                target = full_ly[kpi] * (1 + amount)
                progress = c / target
                expected = b / full_ly[kpi]              # share of last year's cycle sales done by this date
                ratio = progress / expected if expected else 0
                st = "on track" if ratio >= 0.99 else ("at risk" if ratio >= 0.95 else "off track")
                note = f"{c/1e6:.2f}M of {target/1e6:.2f}M target; last year had done {expected:.0%} of its total by this date"
                baseline = b
            else:
                if b is None and kind != "abs":
                    continue
                if kind == "rel":
                    target = b * (1 + amount)
                elif kind == "pts":
                    target = b + amount
                else:
                    target = amount
                baseline = b if b is not None else c
                direction = KPIS[kpi][3]
                met = c >= target if direction == "up" else c <= target
                if met:
                    progress = 1.0
                elif target == baseline:
                    progress = 0.0
                else:
                    progress = (c - baseline) / (target - baseline)
                progress = float(np.clip(progress, -1.0, 1.0))
                expected = elapsed
                st = "on track" if progress >= 0.7 else ("at risk" if progress >= 0.3 else "off track")
                fmt = (lambda v: f"{v:.1%}") if KPIS[kpi][2] in ("%", "pts") else (lambda v: f"{v:,.2f}")
                note = f"now {fmt(c)} vs {fmt(baseline)} same dates last year; target {fmt(target)}"
            rows.append((clock, today, kr_id, scope, baseline, target, c, progress, expected, st,
                         {"off track": 1, "at risk": 2, "on track": 3}[st], note))
    with con.cursor() as cur:
        cur.executemany("""INSERT INTO analytics.okr_progress VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (eval_date, kr_id, scope) DO UPDATE SET evaluated_at=EXCLUDED.evaluated_at,
                           baseline=EXCLUDED.baseline, target=EXCLUDED.target, current_value=EXCLUDED.current_value,
                           progress=EXCLUDED.progress, expected_progress=EXCLUDED.expected_progress,
                           status=EXCLUDED.status, status_sort=EXCLUDED.status_sort, note=EXCLUDED.note""",
                        [tuple(float(x) if isinstance(x, (np.floating,)) else x for x in r) for r in rows])


def detect_pain_points(con, daily: Daily, today: date, locs: pd.DataFrame, clock: datetime):
    rows = []
    names = dict(zip(locs.location_id, locs.name))
    ids = list(locs.location_id)
    w28 = (today - timedelta(days=28), today - timedelta(days=1))
    ann28 = 365 / 28
    k28 = {l: daily.kpis(l, *w28) for l in ids}

    def peer_median(kpi, lid):
        vals = [k28[p][0][kpi] for p in ids if p != lid and k28[p][0] and k28[p][0].get(kpi) is not None]
        return float(np.median(vals)) if vals else None

    # ---- live, today ---------------------------------------------------------
    live = q(con, "SELECT * FROM analytics.live_today")
    pace = q(con, "SELECT * FROM analytics.live_hourly_today")
    now_hr = clock.hour
    for r in live.itertuples():
        lid, n = r.location_id, names[r.location_id]
        # compare only hours whose checks have had time to close (dine-in parties stay ~1-1.5 h)
        p = pace[(pace.location_id == lid) & (pace.hr < now_hr - 1)]
        if not p.empty and clock.date() == today:
            exp = float(p.expected_cum_sales.iloc[-1] or 0)
            act = float(p.cum_sales.iloc[-1] or 0)
            day_exp = float(pace[pace.location_id == lid].expected_sales.astype(float).sum())
            if exp > 1500 and act / exp < 0.88:
                sev = "critical" if act / exp < 0.80 else "warning"
                upsert_alert(rows, "LIVE_PACE", lid, "Revenue", sev, f"{n}: sales {1 - act/exp:.0%} behind today's pace",
                             "Sales vs expected, today", act / exp, 1.0, None,
                             f"${act:,.0f} from checks opened before {now_hr - 1}:00 vs ${exp:,.0f} expected (forecast-scaled average of the last 4 {clock:%A}s). "
                             f"Day at risk: about ${day_exp * (1 - act/exp):,.0f}.",
                             "Check for an outage, a closed section or weather; push a same-day offer or waitlist texts.", "Today")
        if r.lost_parties_today >= 6:
            sev = "critical" if r.lost_parties_today >= 12 else "warning"
            spc = float(r.sales_today) / max(float(r.covers_today), 1)
            upsert_alert(rows, "LIVE_WALKAWAY", lid, "Guest experience", sev,
                         f"{n}: {r.lost_parties_today} parties walked away today", "Walk-away parties, today",
                         r.lost_parties_today, 5, None,
                         f"{int(r.lost_covers_today)} guests left without a table (about ${r.lost_covers_today * spc:,.0f} in sales).",
                         "Quote realistic waits, text guests when a table frees, and pre-set combinable tables for large parties.", "Today")
        if r.avg_wait_min is not None and r.parties_waited >= 5 and float(r.avg_wait_min) > 12:
            upsert_alert(rows, "LIVE_WAIT", lid, "Guest experience", "warning",
                         f"{n}: average wait {float(r.avg_wait_min):.0f} min today", "Average wait, today",
                         r.avg_wait_min, 8, None, f"{r.parties_waited} parties waited today.",
                         "Tighten turn times at peak: pre-bus, drop checks with dessert, stagger reservations.", "Today")

    # staffing vs demand in the last completed hour, against that store's usual for the hour
    hf = q(con, """SELECT location_id, business_date, hr, covers, staff_hours FROM analytics.hourly_floor
                   WHERE business_date >= %s AND business_date <= %s""", (today - timedelta(days=56), today))
    if not hf.empty and 11 < now_hr <= 23 and clock.date() == today:
        hf[["covers", "staff_hours"]] = hf[["covers", "staff_hours"]].astype(float)
        hf["cplh"] = hf.covers / hf.staff_hours.replace(0, np.nan)
        last = now_hr - 1
        for lid in ids:
            cur = hf[(hf.location_id == lid) & (hf.business_date == today) & (hf.hr == last)]
            hist = hf[(hf.location_id == lid) & (hf.business_date < today) & (hf.hr == last)]
            if cur.empty or hist.cplh.dropna().empty or cur.staff_hours.iloc[0] < 5:
                continue
            c, med = cur.cplh.iloc[0], hist.cplh.median()
            if pd.notna(c) and c < 0.6 * med:
                upsert_alert(rows, "LIVE_OVERSTAFF", lid, "Labor", "warning",
                             f"{names[lid]}: overstaffed at {last}:00", "Covers per staff hour, last hour", c, med, None,
                             f"{cur.staff_hours.iloc[0]:.0f} staff hours for {cur.covers.iloc[0]:.0f} covers "
                             f"({c:.2f}/hr vs a usual {med:.2f}).", "Send one or two people home early or move them to prep.", "Now")
            elif pd.notna(c) and c > 1.6 * med:
                upsert_alert(rows, "LIVE_UNDERSTAFF", lid, "Labor", "warning",
                             f"{names[lid]}: understaffed at {last}:00", "Covers per staff hour, last hour", c, med, None,
                             f"{cur.covers.iloc[0]:.0f} covers on {cur.staff_hours.iloc[0]:.0f} staff hours "
                             f"({c:.2f}/hr vs a usual {med:.2f}).", "Call in an on-call server or cook; watch ticket times.", "Now")

    # ---- trend, last 28 days -------------------------------------------------
    for lid in ids:
        kp, s = k28[lid]
        if not kp:
            continue
        n = names[lid]
        splh_peer = peer_median("splh", lid)
        if kp["labor_pct"] > KPIS["labor_pct"][4] + 0.02 and splh_peer and kp["splh"] < 0.93 * splh_peer:
            excess_hours = s["labor_hours"] - s["net_sales"] / splh_peer
            impact = excess_hours * (s["labor_cost"] / s["labor_hours"]) * ann28
            upsert_alert(rows, "LABOR_EFFICIENCY", lid, "Labor", "critical" if kp["labor_pct"] > 0.36 else "warning",
                         f"{n}: labor {kp['labor_pct']:.1%} of sales, scheduling above demand", "Labor % (28 days)",
                         kp["labor_pct"], KPIS["labor_pct"][4], impact,
                         f"Sales per labor hour ${kp['splh']:.0f} vs ${splh_peer:.0f} at peer stores; "
                         f"about {excess_hours:,.0f} extra hours in 28 days.",
                         "Rebuild the schedule from the covers forecast by daypart; cut the first shift of slow afternoons.", "28 days")
        if kp["food_variance_pts"] is not None and kp["food_variance_pts"] > 0.04:
            impact = (kp["food_variance_pts"] - 0.03) * s["food_sales"] * ann28
            upsert_alert(rows, "FOOD_VARIANCE", lid, "Cost of goods", "warning",
                         f"{n}: food cost {kp['food_variance_pts']*100:.1f} pts above recipe cost", "Food cost variance (28 days)",
                         kp["food_variance_pts"], 0.03, impact,
                         f"Actual {kp['food_cost_pct']:.1%} vs recipe {kp['food_cost_pct'] - kp['food_variance_pts']:.1%} of food sales.",
                         "Weekly inventory counts on protein, portion scales on the line, and re-cost recipes at current vendor prices.", "28 days")
        wp = peer_median("waste_pct", lid)
        if kp["waste_pct"] is not None and wp and kp["waste_pct"] > max(0.06, 1.5 * wp):
            impact = (kp["waste_pct"] - wp) * s["purchases_food"] * ann28
            upsert_alert(rows, "WASTE", lid, "Cost of goods", "critical" if kp["waste_pct"] > 0.08 else "warning",
                         f"{n}: food waste {kp['waste_pct']:.1%} of purchases", "Waste % (28 days)", kp["waste_pct"], wp, impact,
                         f"Peer stores waste {wp:.1%}. ${s['waste_cost']:,.0f} logged in 28 days.",
                         "Par-based prep sheets from the covers forecast, FIFO labels, and a daily waste review with the chef.", "28 days")
        if kp["large_party_loss"] is not None and kp["large_party_loss"] > 0.20:
            impact = 0.4 * s["lost_large_parties"] * 5.5 * kp["sales_per_cover"] * FLOW_THROUGH * ann28
            upsert_alert(rows, "LARGE_PARTY_LOSS", lid, "Floor", "critical" if kp["large_party_loss"] > 0.3 else "warning",
                         f"{n}: {kp['large_party_loss']:.0%} of 5-6 guest parties walk away", "Large-party walk-away rate (28 days)",
                         kp["large_party_loss"], KPIS["large_party_loss"][4], impact,
                         f"{int(s['lost_large_parties'])} large parties lost in 28 days while seat utilization was only {kp['seat_utilization']:.0%}.",
                         "Convert 2-tops to combinable 4/6-tops in the main room, take large-party reservations, and hold one 6-top at peak.", "28 days")
        if kp["prime_cost_pct"] is not None and kp["prime_cost_pct"] > 0.68:
            upsert_alert(rows, "PRIME_COST", lid, "Profit", "critical", f"{n}: prime cost {kp['prime_cost_pct']:.1%}",
                         "Prime cost % (28 days)", kp["prime_cost_pct"], 0.65,
                         None,   # a roll-up of the labor and food alerts; not sized separately to avoid double counting
                         f"COGS {(kp['prime_cost_pct'] - kp['labor_pct']):.1%} + labor {kp['labor_pct']:.1%} of sales.",
                         "Tackle the larger of the two: labor scheduling first if labor is over 33%, otherwise purchasing and portions.", "28 days")
        if kp["discount_rate"] is not None and kp["discount_rate"] > 0.035:
            upsert_alert(rows, "DISCOUNTS", lid, "Profit", "warning", f"{n}: discounts {kp['discount_rate']:.1%} of gross sales",
                         "Discount rate (28 days)", kp["discount_rate"], 0.02,
                         (kp["discount_rate"] - 0.02) * s["gross_sales"] * ann28, "Comps and promos above plan.",
                         "Require manager codes for comps and review the top comping staff weekly.", "28 days")
        if kp["delivery_fee_pct"] is not None and kp["delivery_fee_pct"] > 0.03:
            upsert_alert(rows, "DELIVERY_FEES", lid, "Profit", "info", f"{n}: {kp['delivery_fee_pct']:.1%} of sales paid to delivery apps",
                         "Delivery commissions % (28 days)", kp["delivery_fee_pct"], 0.02,
                         0.2 * s["delivery_sales"] * (0.22 - 0.05) * ann28,
                         f"${s['delivery_commission_est']:,.0f} in commissions in 28 days at 22%.",
                         "Push first-party online ordering (bag inserts, loyalty points) to move 20% of delivery orders off the apps.", "28 days")
        # same-store sales trend, last 7 days vs the same dates last year
        cur7, _ = daily.kpis(lid, today - timedelta(days=7), today - timedelta(days=1))
        ly7, sly7 = daily.kpis(lid, today - timedelta(days=371), today - timedelta(days=365))
        if cur7 and ly7 and sly7["days"] >= 6:
            chg = cur7["net_sales"] / ly7["net_sales"] - 1
            if chg < -0.05:
                upsert_alert(rows, "SALES_DECLINE", lid, "Revenue", "critical" if chg < -0.10 else "warning",
                             f"{n}: sales {chg:.0%} vs the same week last year", "Net sales vs LY (7 days)", chg, 0,
                             -chg * ly7["net_sales"] * 52 * FLOW_THROUGH,
                             f"${cur7['net_sales']:,.0f} vs ${ly7['net_sales']:,.0f}; covers {cur7['covers']/ly7['covers'] - 1:+.0%}, "
                             f"spend per cover {cur7['sales_per_cover']/ly7['sales_per_cover'] - 1:+.0%}.",
                             "Split the drop into traffic vs spend; if traffic, check reviews and local events; if spend, check upsell and menu mix.", "7 days")

    # ---- menu, last 90 days --------------------------------------------------
    me = q(con, """SELECT location_id, item_name, category, units, cm_per_unit, menu_class FROM analytics.menu_engineering
                   WHERE period = 'Last 90 days' AND location_id <> 'ALL'""")
    if not me.empty:
        me[["units", "cm_per_unit"]] = me[["units", "cm_per_unit"]].astype(float)
        for lid, g in me.groupby("location_id"):
            ph = g[(g.menu_class == "Plowhorse") & (g.category == "Main")]
            dogs = g[g.menu_class == "Dog"]
            if ph.empty:
                continue
            impact = float((ph.units * 0.97 - ph.units * 0.03 * ph.cm_per_unit).sum()) * 365 / 90
            upsert_alert(rows, "MENU_MARGIN", lid, "Menu", "info",
                         f"{names[lid]}: {len(ph)} best-selling mains earn below-average margin", "Plowhorse mains (90 days)",
                         len(ph), None, impact,
                         f"Plowhorses: {', '.join(ph.sort_values('units', ascending=False).item_name.head(3))}. "
                         f"Dogs: {', '.join(dogs.item_name.head(3)) or 'none'}.",
                         "Add $1 to plowhorse mains or re-cost the plate; rework or drop dogs.", "90 days")

    # ---- persist: keep first_seen for recurring alerts, resolve what cleared ----
    with con.cursor() as cur:
        for r in rows:
            cur.execute("""INSERT INTO analytics.alerts (alert_key, rule_id, location_id, category, severity, severity_sort, title,
                              metric, value, benchmark, est_annual_impact, evidence, recommended_action, horizon, first_seen, last_seen, status)
                           VALUES (%(alert_key)s, %(rule_id)s, %(location_id)s, %(category)s, %(severity)s, %(severity_sort)s, %(title)s,
                              %(metric)s, %(value)s, %(benchmark)s, %(est_annual_impact)s, %(evidence)s, %(recommended_action)s,
                              %(horizon)s, %(ts)s, %(ts)s, 'open')
                           ON CONFLICT (alert_key) DO UPDATE SET severity=EXCLUDED.severity, severity_sort=EXCLUDED.severity_sort,
                              title=EXCLUDED.title, value=EXCLUDED.value, benchmark=EXCLUDED.benchmark,
                              est_annual_impact=EXCLUDED.est_annual_impact, evidence=EXCLUDED.evidence, last_seen=EXCLUDED.last_seen,
                              first_seen=CASE WHEN analytics.alerts.status='resolved' THEN EXCLUDED.first_seen ELSE analytics.alerts.first_seen END,
                              status='open'""", {**r, "ts": clock})
        cur.execute("UPDATE analytics.alerts SET status='resolved' WHERE status='open' AND NOT (alert_key = ANY(%s))",
                    ([r["alert_key"] for r in rows],))
    return len(rows)


# ------------------------------------------------------------------ orchestration
def evaluate(con, verbose=True):
    t = time.time()
    clk = con.execute("SELECT now_local, business_date FROM analytics.v_clock").fetchone()
    clock, today = clk
    con.execute("SELECT analytics.refresh(%s)", (today - timedelta(days=1),))
    write_definitions(con)
    daily = Daily(con)
    fc_new = run_forecast(con, today, daily)
    build_pacing(con, today)
    locs = q(con, "SELECT location_id, name FROM pos.dim_locations ORDER BY 1")
    scopes = ["ALL"] + list(locs.location_id)
    n_kpi = kpi_snapshot(con, daily, today, scopes, clock)
    okr_progress(con, daily, today, scopes, clock)
    n_alerts = detect_pain_points(con, daily, today, locs, clock)
    secs = time.time() - t
    con.execute("INSERT INTO analytics.evaluator_runs VALUES (now(), %s, %s, %s, %s, %s)",
                (clock, round(secs, 2), n_kpi, n_alerts, "forecast refreshed" if fc_new else None))
    con.commit()
    if verbose:
        print(f"[{datetime.now():%H:%M:%S}] restaurant time {clock:%a %H:%M} · {n_kpi} KPI values · "
              f"{n_alerts} open pain points{' · new forecast' if fc_new else ''} · {secs:.1f}s")


def backfill(con, days: int):
    """Replay past days so the dashboard has history on day one: one-day-ahead forecasts
    (scored in analytics.forecast_vs_actual) and daily OKR progress since the cycle began."""
    today = con.execute("SELECT business_date FROM analytics.v_clock").fetchone()[0]
    write_definitions(con)
    daily = Daily(con)
    scopes = ["ALL"] + [r[0] for r in con.execute("SELECT location_id FROM pos.dim_locations ORDER BY 1")]
    t = time.time()
    for k in range(days, 0, -1):
        d = today - timedelta(days=k)
        run_forecast(con, d, daily, horizon_days=7, sims=200)
        okr_progress(con, daily, d, scopes, datetime.combine(d, datetime.min.time()) + timedelta(hours=23))
        con.commit()
    print(f"backfilled {days} days of forecasts and OKR history in {time.time() - t:.0f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", type=int, default=0, help="replay this many past days of forecasts and OKR progress, then exit")
    ap.add_argument("--interval", type=float, default=300, help="seconds between evaluations")
    args = ap.parse_args()
    con = connect()
    run_sql_file(con, "20_evaluation.sql")
    con.commit()
    if args.backfill:
        backfill(con, args.backfill)
        return
    while True:
        try:
            evaluate(con)
        except Exception as e:  # keep the loop alive; the next run retries
            con.rollback()
            print("evaluation failed:", repr(e))
            if args.once:
                raise
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
