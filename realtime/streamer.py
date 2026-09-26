"""Live POS event streamer for the Harbor & Hearth chain.

It runs the same demand, seating and cost model as the history generator, but
event by event against the wall clock, writing to Postgres the way a real POS,
time-clock and accounts-payable feed would:

  * guest parties arrive, get seated (pos.orders status='open', pos.table_status),
    wait, or walk away (pos.lost_parties)
  * checks close with items, totals, tax and tips (status='closed', pos.order_items)
  * takeout / third-party delivery orders
  * staff clock in and out (pos.labor_shifts scheduled from expected covers)
  * vendor invoices (Mon/Thu food, Tue beverage & supplies), nightly waste log,
    month-end operating expenses
  * a heartbeat row (pos.stream_status) so dashboards can show data freshness

On start it catches up from the last processed time to "now" in restaurant time
(America/New_York), then keeps going in real time.

Run:
    python realtime/streamer.py                 # catch up, then live (Ctrl+C to stop)
    python realtime/streamer.py --speed 60      # demo: 1 real second = 1 simulated minute
    python realtime/streamer.py --catchup-only  # fill the gap to now and exit
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import math
import signal
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
import generate_data as G  # noqa: E402
from config import (min_wage_on, TIP_CREDIT, PAYROLL_BURDEN, MENU_PRICE_INCREASE_DATE,  # noqa: E402
                    MENU_PRICE_INCREASE)
from db import connect, BUSINESS_TZ  # noqa: E402

rng = np.random.default_rng()
G.rng = rng  # the shared demand functions draw from this generator

# calendar beyond the history window
G.CLOSED |= {date(2026, 11, 26), date(2026, 12, 25), date(2027, 11, 25), date(2027, 12, 25)}
G.HOLIDAY_BOOST.update({date(2026, 12, 31): ("dinner", 1.45), date(2027, 2, 14): ("dinner", 1.55),
                        date(2027, 5, 9): ("lunch", 1.7), date(2027, 7, 4): ("all", 1.2)})

ROLES = {  # role: (covers per head, basis, min heads, $ over min wage, tipped)
    "Server": (25, "dine", 2, 0.0, True), "Bartender": (90, "dine", 1, 0.0, True),
    "Host": (160, "dine", 1, 1.0, False), "Line Cook": (44, "kitchen", 2, 5.0, False),
    "Prep Cook": (130, "kitchen", 1, 3.0, False), "Dishwasher": (150, "kitchen", 1, 1.0, False),
}
MEAN_PARTY = {"lunch": 2.80, "dinner": 3.09}
FOOD_CATS = ("Protein", "Produce", "Dairy", "Dry Goods")
LOC = {l["location_id"]: l for l in G.LOCATIONS}


def inflation(cat: str, d: date) -> float:
    y = (d - G.START).days / 365.25
    if cat == "Protein":
        return 1.09 if d >= date(2025, 6, 1) else 1.0
    if cat == "Produce":
        return 1 + 0.06 * math.sin(2 * math.pi * (d.month - 3) / 12) + 0.02 * y
    if cat == "Dairy":
        return 1 + 0.035 * y
    if cat in ("Alcohol", "Beverage"):
        return 1 + 0.02 * y
    return 1 + 0.025 * y


def now_local() -> datetime:
    return datetime.now(ZoneInfo(BUSINESS_TZ)).replace(tzinfo=None, microsecond=0)


class Floor:
    """Tables of one location and when each frees up."""
    def __init__(self, rows):
        self.ids = np.array([r[0] for r in rows])
        self.zone = np.array([r[1] for r in rows])
        self.seats = np.array([r[2] for r in rows])
        self.next_free = np.full(len(rows), -1e12)    # epoch minutes
        self.patio_open = True


class Streamer:
    def __init__(self, con):
        self.con = con
        self.heap: list = []
        self.seq = itertools.count()
        self.menu = self._load_menu()
        self.floors = {}
        for lid in LOC:
            rows = con.execute("SELECT table_id, zone, seats FROM pos.dim_tables WHERE location_id=%s ORDER BY table_id",
                               (lid,)).fetchall()
            self.floors[lid] = Floor(rows)
        self.employees = defaultdict(list)
        for eid, lid, role in con.execute("SELECT employee_id, location_id, role FROM pos.dim_employees"):
            self.employees[(lid, role)].append(eid)
        nxt = lambda sql: (con.execute(sql).fetchone()[0] or 0) + 1
        self.ck_no = nxt("SELECT max(substr(check_id, 2)::bigint) FROM pos.orders")
        self.sh_no = nxt("SELECT max(substr(shift_id, 2)::bigint) FROM pos.labor_shifts")
        self.inv_no = nxt("SELECT max(substr(invoice_id, 4)::bigint) FROM pos.purchases")
        self.day_mult = {}
        self.started = set()
        self.usage = defaultdict(float)        # (loc, purchase category, date) -> recipe cost used that day
        self.food_today = defaultdict(float)   # loc -> recipe food cost today (for waste)
        self.open_rows = {}                    # check_id -> order row not yet flushed
        self.open_meta = {}                    # check_id -> dict of what we need to close it
        self.new_items, self.order_updates, self.lost, self.table_updates = [], [], {}, {}
        self.shift_new, self.shift_updates, self.purch, self.waste = {}, [], [], []
        self.orders_written = 0

    # ------------------------------------------------------------------ setup
    def _load_menu(self):
        m = defaultdict(lambda: {"ids": [], "price": [], "cost": [], "wl": [], "wd": []})
        for iid, name, cat, price, cost, w in G.MENU:
            c = m[cat]
            c["ids"].append(iid); c["price"].append(price); c["cost"].append(cost)
            tilt = G.LUNCH_TILT.get(iid, 1.0)
            c["wl"].append(w * tilt); c["wd"].append(w * (1 / tilt) ** 0.5)
        for c in m.values():
            for k in ("wl", "wd"):
                a = np.array(c[k]); c[k] = a / a.sum()
        return m

    def push(self, ts: datetime, kind: str, **payload):
        heapq.heappush(self.heap, (ts, next(self.seq), kind, payload))

    @staticmethod
    def em(ts: datetime) -> float:
        return ts.timestamp() / 60.0 if ts.tzinfo else (ts - datetime(1970, 1, 1)).total_seconds() / 60.0

    # ------------------------------------------------------------------ day setup
    def start_day(self, lid: str, d: date, after: datetime):
        """Create the day's demand level, staff schedule and back-office events (only those after `after`)."""
        loc = LOC[lid]
        mult = G.demand_multiplier(loc, d)
        self.day_mult[(lid, d)] = mult
        self.started.add((lid, d))
        later = lambda ts, kind, **p: self.push(ts, kind, **p) if ts >= after else None
        fl = self.floors[lid]
        fl.patio_open = rng.random() > (0.32 if d.month in (6, 7, 8, 9) else 0.05)
        day0 = datetime.combine(d, datetime.min.time())
        if mult > 0:
            have = self.con.execute("SELECT count(*) FROM pos.labor_shifts WHERE location_id=%s AND business_date=%s",
                                    (lid, d)).fetchone()[0]
            if not have:
                self.schedule_staff(lid, d, mult, day0, after)
            later(day0 + timedelta(hours=23, minutes=50), "waste", lid=lid, d=d)
        wd = d.weekday()
        if wd in (0, 3):
            later(day0 + timedelta(hours=7), "invoice", lid=lid, cats=FOOD_CATS)
        if wd == 1:
            later(day0 + timedelta(hours=7), "invoice", lid=lid, cats=("Beverage", "Alcohol", "Packaging"))
        if d.day == 1:
            later(day0 + timedelta(hours=6), "opex", lid=lid, month=(day0 - timedelta(days=1)).replace(day=1).date())

    def expected_covers(self, lid, d, mult, daypart):
        boost = G.HOLIDAY_BOOST.get(d)
        dine = 0.0
        for h in G.HOURS:
            if h == 22 and d.weekday() not in (4, 5):
                continue
            dp = "lunch" if h < 16 else "dinner"
            if dp != daypart:
                continue
            lam = G.BASE_DINE_PER_HOUR * G.DINE_PROFILE[h] * mult
            if boost and boost[0] in (dp, "all"):
                lam *= boost[1]
            dine += lam * MEAN_PARTY[dp]
        prof = np.array(list(G.OFF_PROFILE.values()))
        share = prof[: 5].sum() / prof.sum() if daypart == "lunch" else prof[5:].sum() / prof.sum()
        kitchen = G.BASE_OFF_PER_DAY * mult / 1.03 * share * 1.9
        return dine * 0.97, kitchen   # ~3% of dine-in demand walks away

    def schedule_staff(self, lid, d, mult, day0, after):
        mw = min_wage_on(d)
        staff = LOC[lid]["staffing"]
        for daypart, (start_h, hours) in (("lunch", (10.5, 5.5)), ("dinner", (16.0, 7.0))):
            dine, kitchen = self.expected_covers(lid, d, mult, daypart)
            for role, (ratio, basis, mn, prem, tipped) in ROLES.items():
                # kitchen roles are sized on dine-in covers weighted double plus off-premise covers,
                # the same staffing standard the chain's history was scheduled with
                demand = (dine if basis == "dine" else 2 * dine + kitchen) * rng.lognormal(0, 0.08)
                heads = max(mn, math.ceil(demand / ratio * staff))
                rate = (mw - TIP_CREDIT) if tipped else mw + prem
                for _ in range(heads):
                    self._add_shift(lid, d, role, daypart, start_h, round(hours + rng.normal(0, 0.35), 2), rate, day0, after)
        for sh in (9.0, 14.5):
            self._add_shift(lid, d, "Manager", "lunch" if sh < 12 else "dinner", sh, 9.0, 31.25, day0, after)

    def _add_shift(self, lid, d, role, daypart, start_h, hours, rate, day0, after):
        sid = f"S{self.sh_no:07d}"; self.sh_no += 1
        pool = self.employees.get((lid, role)) or [f"{lid}-{role[:3].upper()}001"]
        cin = day0 + timedelta(hours=start_h)
        cout = cin + timedelta(hours=hours)
        row = dict(shift_id=sid, location_id=lid, business_date=d, role=role, daypart=daypart, start_hour=start_h,
                   hours=None, hourly_rate=rate, employee_id=str(rng.choice(pool)), wage_cost=None, burden_cost=None,
                   total_labor_cost=None, clock_in_ts=None, clock_out_ts=None, status="scheduled", planned=hours)
        self.shift_new[sid] = row
        self.push(max(cin, after), "clock_in", sid=sid, at=cin)
        self.push(max(cout, after), "clock_out", sid=sid, at=cout)

    # ------------------------------------------------------------------ demand in a window
    def generate_arrivals(self, lid, t0: datetime, t1: datetime):
        d = t0.date()
        mult = self.day_mult.get((lid, d), 0)
        if mult <= 0:
            return
        boost = G.HOLIDAY_BOOST.get(d)
        day0 = datetime.combine(d, datetime.min.time())
        off_prof = np.array(list(G.OFF_PROFILE.values()))
        off_prof = off_prof / off_prof.sum()
        for i, h in enumerate(G.HOURS):
            hs, he = day0 + timedelta(hours=h), day0 + timedelta(hours=h + 1)
            a, b = max(hs, t0), min(he, t1)
            if b <= a:
                continue
            frac = (b - a).total_seconds() / 3600
            dp = "lunch" if h < 16 else "dinner"
            if not (h == 22 and d.weekday() not in (4, 5)):
                lam = G.BASE_DINE_PER_HOUR * G.DINE_PROFILE[h] * mult * frac
                if boost and boost[0] in (dp, "all"):
                    lam *= boost[1]
                for _ in range(rng.poisson(lam)):
                    ts = a + timedelta(seconds=float(rng.uniform(0, (b - a).total_seconds())))
                    self.push(ts, "arrive", lid=lid, ps=int(G.party_sizes(1, dp)[0]), dp=dp)
            lam_off = G.BASE_OFF_PER_DAY * mult / 1.03 * off_prof[i] * frac
            for _ in range(rng.poisson(lam_off)):
                ts = a + timedelta(seconds=float(rng.uniform(0, (b - a).total_seconds())))
                self.push(ts, "offprem", lid=lid, dp=dp)

    # ------------------------------------------------------------------ event handlers
    def on_arrive(self, ts, lid, ps, dp):
        fl = self.floors[lid]
        d = ts.date()
        m = self.em(ts)
        usable = np.ones(len(fl.ids), bool) if fl.patio_open else (fl.zone != "patio")
        fits = usable & (fl.seats >= ps)
        order = np.lexsort((rng.random(len(fl.ids)), fl.seats))
        order = order[fits[order]]
        if ps <= 2 and rng.random() < 0.33:
            order = np.concatenate([order[fl.zone[order] == "bar"], order[fl.zone[order] != "bar"]])
        else:
            order = order[(fl.zone[order] != "bar") | (ps <= 2)]
        close_min = (23 if d.weekday() in (4, 5) else 22) * 60 + 30
        mins_of_day = ts.hour * 60 + ts.minute
        if len(order) == 0:
            return self._lose(lid, ts, ps, None)
        free = order[fl.next_free[order] <= m]
        tight = free[fl.seats[free] <= ps + 2]
        if len(tight):
            ti, wait = tight[0], 0.0
        elif len(free):
            ti, wait = free[0], 0.0
        else:
            ti = order[np.argmin(fl.next_free[order])]
            wait = fl.next_free[ti] - m
            max_wait = 25 if (dp == "dinner" and d.weekday() in (4, 5)) else 18
            accept = max(0.0, 1 - wait / max_wait)
            if wait > max_wait or rng.random() > accept ** 0.6:
                return self._lose(lid, ts, ps, round(wait, 1))
        if mins_of_day + wait > close_min - 30:
            return self._lose(lid, ts, ps, round(wait, 1))
        base = 50 if dp == "lunch" else 80
        if fl.zone[ti] == "bar":
            base -= 12
        dwell = rng.lognormal(math.log(base + 6 * (ps - 2) if ps > 1 else base - 8), 0.24)
        fl.next_free[ti] = m + wait + dwell + 6
        seat_ts = ts + timedelta(minutes=wait)
        self.push(seat_ts, "seat", lid=lid, ti=int(ti), ps=ps, dp=dp, arrival=ts, wait=round(wait, 1), dwell=dwell)

    def _lose(self, lid, ts, ps, wait):
        self.lost.setdefault("rows", []).append((lid, ts.date(), ts, ps, wait))

    def on_seat(self, ts, lid, ti, ps, dp, arrival, wait, dwell):
        fl = self.floors[lid]
        cid = f"C{self.ck_no:08d}"; self.ck_no += 1
        close = ts + timedelta(minutes=dwell)
        self.open_rows[cid] = dict(check_id=cid, location_id=lid, business_date=arrival.date(), channel="Dine-in",
                                   delivery_platform=None, table_id=str(fl.ids[ti]), zone=str(fl.zone[ti]), covers=ps,
                                   daypart=dp, arrival_ts=arrival, open_ts=ts, close_ts=None, wait_min=wait,
                                   status="open")
        self.open_meta[cid] = dict(grp="dine", zone=str(fl.zone[ti]))
        self.table_updates[str(fl.ids[ti])] = ("occupied", cid, ps, ts, close)
        self.push(close, "close", cid=cid, table=str(fl.ids[ti]))

    def on_offprem(self, ts, lid, dp):
        d = ts.date()
        cid = f"C{self.ck_no:08d}"; self.ck_no += 1
        delivery_share = 0.45 + 0.13 * ((d - G.START).days / 365.25) / 2
        ch = "Delivery" if rng.random() < min(delivery_share, 0.7) else "Takeout"
        ps = int(rng.choice([1, 2, 3, 4], p=[.42, .36, .12, .10]))
        self.open_rows[cid] = dict(check_id=cid, location_id=lid, business_date=d, channel=ch,
                                   delivery_platform=str(rng.choice(["DoorDash", "Uber Eats", "Grubhub"], p=[.55, .33, .12])) if ch == "Delivery" else None,
                                   table_id=None, zone=None, covers=ps, daypart=dp, arrival_ts=ts, open_ts=ts,
                                   close_ts=None, wait_min=0.0, status="open")
        self.open_meta[cid] = dict(grp="off", zone=None)
        self.push(ts + timedelta(minutes=float(rng.uniform(12, 25))), "close", cid=cid, table=None)

    def on_close(self, ts, cid, table):
        meta = self.open_meta.pop(cid, None)
        row = self.open_rows.get(cid)
        if meta is None:
            return
        lid = row["location_id"] if row else meta["lid"]
        covers = row["covers"] if row else meta["covers"]
        dp = row["daypart"] if row else meta["dp"]
        channel = row["channel"] if row else meta["channel"]
        d = (row["business_date"] if row else meta["business_date"])
        price_mult = 1 + MENU_PRICE_INCREASE if d >= MENU_PRICE_INCREASE_DATE else 1.0
        subtotal, lines = 0.0, defaultdict(int)
        for cat, rates in ((c, G.ATTACH[(meta["grp"], dp)][c]) for c in G.ATTACH[(meta["grp"], dp)]):
            if cat == "Bar":
                n = rng.poisson(covers * rates * (1.6 if meta["zone"] == "bar" else 1.0) * 1.15)
            else:
                n = rng.binomial(covers, min(rates, 1.0))
            if n == 0:
                continue
            c = self.menu[cat]
            picks = rng.choice(len(c["ids"]), size=n, p=c["wl"] if dp == "lunch" else c["wd"])
            for p in picks:
                lines[(cat, p)] += 1
        for (cat, p), q in lines.items():
            c = self.menu[cat]
            up = round(c["price"][p] * price_mult, 2)
            theo = round(c["cost"][p] * q, 2)
            self.new_items.append((cid, c["ids"][p], q, up, round(up * q, 2), theo))
            subtotal += up * q
            for pcat, share in G.COST_SPLIT[cat].items():
                self.usage[(lid, pcat, d)] += theo * share
                if pcat in FOOD_CATS:
                    self.food_today[lid] += theo * share
        if channel != "Dine-in":
            self.usage[(lid, "Packaging", d)] += 1.35
        subtotal = round(subtotal, 2)
        disc = round(subtotal * rng.uniform(0.1, 0.25), 2) if rng.random() < 0.03 else 0.0
        net = round(subtotal - disc, 2)
        tip_rate = rng.normal(0.195, 0.03) if channel == "Dine-in" else (rng.normal(0.08, 0.04) if channel == "Takeout" else 0)
        tip = round(max(0.0, min(tip_rate, 0.35)) * net, 2)
        pay = "Platform" if channel == "Delivery" else ("Card" if rng.random() < 0.93 else "Cash")
        vals = dict(close_ts=ts.replace(microsecond=0), subtotal=subtotal, discount=disc, net_sales=net,
                    tax=round(net * 0.07, 2), tip=tip, payment_type=pay, status="closed")
        if row:
            row.update(vals)
        else:
            self.order_updates.append((vals["close_ts"], subtotal, disc, net, vals["tax"], tip, pay, cid))
        if table:
            self.table_updates[table] = ("free", None, None, None, None)
        self.orders_written += 1

    def on_clock_in(self, ts, sid):
        r = self.shift_new.get(sid)
        if r and r["status"] == "scheduled":
            r["status"] = "on_clock"; r["clock_in_ts"] = ts

    def on_clock_out(self, ts, sid):
        r = self.shift_new.get(sid)
        if r is None:
            return
        hours = r["planned"]
        wage = round(hours * float(r["hourly_rate"]), 2)
        burden = round(wage * PAYROLL_BURDEN, 2)
        r.update(status="completed", clock_out_ts=ts, hours=hours, wage_cost=wage, burden_cost=burden,
                 total_labor_cost=round(wage + burden, 2))
        if r["clock_in_ts"] is None:
            r["clock_in_ts"] = ts - timedelta(hours=hours)

    def on_invoice(self, ts, lid, cats):
        """Vendors deliver ahead of demand: each invoice covers expected usage until the next delivery,
        estimated from the last 7 days of recipe-cost usage."""
        d = ts.date()
        wr = LOC[lid]["waste_rate"]
        cover_days = {0: 3, 3: 4}.get(d.weekday(), 7)
        for cat in cats:
            last7 = sum(self.usage.get((lid, cat, d - timedelta(days=k)), 0.0) for k in range(1, 8))
            amt = last7 / 7 * cover_days
            if amt <= 0:
                continue
            if cat != "Packaging":
                amt *= inflation(cat, d) * 1.02 * (1 + (wr if cat in FOOD_CATS else 0)) * rng.lognormal(0, 0.03)
            iid = f"INV{self.inv_no:07d}"; self.inv_no += 1
            self.purch.append((iid, lid, d, G.VENDORS[cat], cat, round(amt, 2)))

    def on_waste(self, ts, lid, d):
        food = self.food_today.pop(lid, 0.0)
        if food <= 0:
            return
        cost = food * inflation("Protein", d) * LOC[lid]["waste_rate"] * rng.lognormal(0, 0.25)
        reason = str(rng.choice(["Overproduction", "Spoilage", "Prep error", "Returned by guest", "Expired"],
                                p=[.38, .27, .15, .08, .12]))
        self.waste.append((lid, d, round(cost, 2), reason))

    def on_opex(self, ts, lid, month):
        self.flush()
        loc = LOC[lid]
        s = self.con.execute("""
            SELECT coalesce(sum(net_sales),0),
                   coalesce(sum(CASE WHEN payment_type='Card' THEN net_sales+tax+tip END),0),
                   coalesce(sum(CASE WHEN channel='Delivery' THEN net_sales END),0)
            FROM pos.orders WHERE location_id=%s AND status='closed'
              AND business_date >= %s AND business_date < (%s::date + interval '1 month')""", (lid, month, month)).fetchone()
        net, card, deliv = map(float, s)
        if net <= 0:
            return
        seats = sum(s_ * c for z in loc["layout"].values() for s_, c in z.items())
        um = 1.35 if month.month in (6, 7, 8, 9) else (1.15 if month.month in (5, 10) else 1.0)
        rent = loc["monthly_rent"] * (1.03 if month >= date(2027, 1, 1) else 1.0)
        entries = {"Rent & CAM": rent, "Utilities": seats * 38 * um * rng.lognormal(0, 0.05),
                   "Marketing": net * 0.018, "Repairs & Maintenance": net * 0.009 * rng.lognormal(0, 0.3),
                   "Supplies & Smallwares": net * 0.013 * rng.lognormal(0, 0.1), "Card Processing Fees": card * 0.026,
                   "Delivery Commissions": deliv * 0.22, "Insurance": 3800, "Software & POS": 1450}
        with self.con.cursor() as cur:
            cur.executemany("""INSERT INTO pos.operating_expenses VALUES (%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING""", [(lid, month, k, round(v, 2)) for k, v in entries.items()])

    # ------------------------------------------------------------------ main step
    def step(self, t0: datetime, t1: datetime):
        d0, d1 = t0.date(), t1.date()
        d = d0
        while d <= d1:
            if d == d1 and t1 == datetime.combine(d, datetime.min.time()):
                break                                  # window ends exactly at midnight
            for lid in LOC:
                if (lid, d) not in self.started:
                    self.start_day(lid, d, max(t0, datetime.combine(d, datetime.min.time())))
            a = max(t0, datetime.combine(d, datetime.min.time()))
            b = min(t1, datetime.combine(d + timedelta(days=1), datetime.min.time()))
            for lid in LOC:
                self.generate_arrivals(lid, a, b)
            d += timedelta(days=1)
        handlers = {"arrive": self.on_arrive, "seat": self.on_seat, "offprem": self.on_offprem, "close": self.on_close,
                    "clock_in": self.on_clock_in, "clock_out": self.on_clock_out, "invoice": self.on_invoice,
                    "waste": self.on_waste, "opex": self.on_opex}
        while self.heap and self.heap[0][0] < t1:
            ts, _, kind, p = heapq.heappop(self.heap)
            if kind in ("clock_in", "clock_out"):
                ts = p.pop("at")
            handlers[kind](ts, **p)
        self.flush(t1)
        # forget old day keys
        keep = d1 - timedelta(days=1)
        old = d1 - timedelta(days=9)
        self.usage = defaultdict(float, {k: v for k, v in self.usage.items() if k[2] >= old})
        self.day_mult = {k: v for k, v in self.day_mult.items() if k[1] >= keep}
        self.started = {k for k in self.started if k[1] >= keep}

    # ------------------------------------------------------------------ persistence
    ORDER_COLS = ["check_id", "location_id", "business_date", "channel", "delivery_platform", "table_id", "zone",
                  "covers", "daypart", "arrival_ts", "open_ts", "close_ts", "wait_min", "subtotal", "discount",
                  "net_sales", "tax", "tip", "payment_type", "status"]
    SHIFT_COLS = ["shift_id", "location_id", "business_date", "role", "daypart", "start_hour", "hours", "hourly_rate",
                  "employee_id", "wage_cost", "burden_cost", "total_labor_cost", "clock_in_ts", "clock_out_ts", "status"]

    def flush(self, clock: datetime | None = None):
        con = self.con
        with con.cursor() as cur:
            if self.open_rows:
                rows = [tuple(r.get(c) for c in self.ORDER_COLS) for r in self.open_rows.values()]
                cur.executemany(f"INSERT INTO pos.orders ({', '.join(self.ORDER_COLS)}) VALUES ({', '.join(['%s'] * len(self.ORDER_COLS))})", rows)
                # checks still open after this flush need row data to close later
                for cid, r in self.open_rows.items():
                    if r["status"] == "open" and cid in self.open_meta:
                        self.open_meta[cid].update(lid=r["location_id"], covers=r["covers"], dp=r["daypart"],
                                                   channel=r["channel"], business_date=r["business_date"])
                self.open_rows = {}
            if self.order_updates:
                cur.executemany("""UPDATE pos.orders SET close_ts=%s, subtotal=%s, discount=%s, net_sales=%s, tax=%s,
                                   tip=%s, payment_type=%s, status='closed' WHERE check_id=%s""", self.order_updates)
                self.order_updates = []
            if self.new_items:
                with cur.copy("COPY pos.order_items (check_id, item_id, quantity, unit_price, line_total, theoretical_cost) FROM STDIN") as cp:
                    for r in self.new_items:
                        cp.write_row(r)
                self.new_items = []
            if self.lost.get("rows"):
                cur.executemany("INSERT INTO pos.lost_parties (location_id, business_date, arrival_ts, party_size, quoted_wait_min) VALUES (%s,%s,%s,%s,%s)",
                                self.lost["rows"])
                self.lost = {}
            if self.table_updates:
                cur.executemany("""UPDATE pos.table_status SET status=%s, check_id=%s, party_size=%s, seated_at=%s,
                                   expected_close=%s, updated_at=now() WHERE table_id=%s""",
                                [(*v, k) for k, v in self.table_updates.items()])
                self.table_updates = {}
            if self.shift_new:
                rows = [tuple(r.get(c) for c in self.SHIFT_COLS) for r in self.shift_new.values()]
                cur.executemany(f"""INSERT INTO pos.labor_shifts ({', '.join(self.SHIFT_COLS)}) VALUES ({', '.join(['%s'] * len(self.SHIFT_COLS))})
                                    ON CONFLICT (shift_id) DO UPDATE SET hours=EXCLUDED.hours, wage_cost=EXCLUDED.wage_cost,
                                    burden_cost=EXCLUDED.burden_cost, total_labor_cost=EXCLUDED.total_labor_cost,
                                    clock_in_ts=EXCLUDED.clock_in_ts, clock_out_ts=EXCLUDED.clock_out_ts, status=EXCLUDED.status""", rows)
                self.shift_new = {k: v for k, v in self.shift_new.items() if v["status"] != "completed"}
            if self.purch:
                cur.executemany("INSERT INTO pos.purchases VALUES (%s,%s,%s,%s,%s,%s)", self.purch)
                self.purch = []
            if self.waste:
                cur.executemany("INSERT INTO pos.waste_log (location_id, business_date, waste_cost, reason) VALUES (%s,%s,%s,%s)", self.waste)
                self.waste = []
            if clock is not None:
                cur.execute("""UPDATE pos.stream_status SET sim_clock=%s, last_tick_at=now(),
                               orders_written = orders_written + %s WHERE id=1""", (clock, self.orders_written))
                self.orders_written = 0
        con.commit()

    def seed_usage(self, clock: datetime):
        """Load the last 7 days of recipe-cost usage so the first invoices after a start are sized right."""
        d = clock.date()
        rows = self.con.execute("""
            SELECT o.location_id, o.business_date, m.category, sum(i.theoretical_cost)
            FROM pos.orders o JOIN pos.order_items i USING (check_id) JOIN pos.dim_menu_items m USING (item_id)
            WHERE o.business_date >= %s AND o.business_date < %s GROUP BY 1, 2, 3""", (d - timedelta(days=8), d)).fetchall()
        for lid, bd, cat, cost in rows:
            for pcat, share in G.COST_SPLIT[cat].items():
                self.usage[(lid, pcat, bd)] += float(cost) * share
        for lid, bd, n in self.con.execute("""SELECT location_id, business_date, count(*) FROM pos.orders
                WHERE channel <> 'Dine-in' AND business_date >= %s AND business_date < %s GROUP BY 1, 2""",
                (d - timedelta(days=8), d)).fetchall():
            self.usage[(lid, "Packaging", bd)] += n * 1.35

    def recover(self, clock: datetime):
        """Close anything left open by a previous run so the floor starts clean."""
        n = self.con.execute("""
            WITH o AS (
              SELECT o.check_id FROM pos.orders o WHERE o.status='open')
            UPDATE pos.orders SET status='abandoned', close_ts=open_ts WHERE check_id IN (SELECT check_id FROM o)""").rowcount
        self.con.execute("UPDATE pos.table_status SET status='free', check_id=NULL, party_size=NULL, seated_at=NULL, expected_close=NULL")
        self.con.execute("""UPDATE pos.labor_shifts SET status='completed',
                              clock_out_ts = coalesce(clock_out_ts, clock_in_ts + interval '6 hours'),
                              hours = coalesce(hours, 6), wage_cost = coalesce(wage_cost, 6*hourly_rate),
                              burden_cost = coalesce(burden_cost, 6*hourly_rate*0.12),
                              total_labor_cost = coalesce(total_labor_cost, 6*hourly_rate*1.12)
                            WHERE status <> 'completed' AND business_date < %s""", (clock.date(),))
        # today's schedule is rebuilt from scratch on start
        self.con.execute("DELETE FROM pos.labor_shifts WHERE business_date >= %s AND business_date > DATE '2026-08-31'", (clock.date(),))
        self.con.commit()
        if n:
            print(f"  closed {n} checks left open by the previous run")


def reset_stream(con):
    cut = "DATE '2026-09-01'"
    con.execute(f"DELETE FROM pos.order_items WHERE check_id IN (SELECT check_id FROM pos.orders WHERE business_date >= {cut})")
    for t, col in (("orders", "business_date"), ("lost_parties", "business_date"), ("labor_shifts", "business_date"),
                   ("purchases", "invoice_date"), ("waste_log", "business_date"), ("operating_expenses", "month")):
        con.execute(f"DELETE FROM pos.{t} WHERE {col} >= {cut}")
    con.execute("UPDATE pos.table_status SET status='free', check_id=NULL, party_size=NULL, seated_at=NULL, expected_close=NULL")
    con.execute(f"UPDATE pos.stream_status SET sim_clock = {cut}::timestamp, orders_written = 0 WHERE id = 1")
    con.commit()
    print("stream data cleared; restarting from 2026-09-01")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=1.0, help="simulated minutes per real minute after catch-up (1 = live)")
    ap.add_argument("--tick", type=float, default=5.0, help="seconds between writes in live mode")
    ap.add_argument("--catchup-only", action="store_true", help="fill the gap to now, then exit")
    ap.add_argument("--reset-stream", action="store_true", help="delete everything streamed after the history and start again from 1 Sep 2026")
    ap.add_argument("--run-seconds", type=float, default=0, help="stop after this many real seconds (0 = forever)")
    args = ap.parse_args()

    con = connect()
    if args.reset_stream:
        reset_stream(con)
    s = Streamer(con)
    clock = con.execute("SELECT sim_clock FROM pos.stream_status WHERE id=1").fetchone()[0]
    s.recover(clock)
    s.seed_usage(clock)
    target = now_local()
    print(f"catching up {clock:%Y-%m-%d %H:%M} -> {target:%Y-%m-%d %H:%M} (restaurant time)")
    t_start = time.time()
    last_print = clock.date()
    while clock < target - timedelta(seconds=args.tick):
        nxt = min(clock + timedelta(minutes=15), target)
        s.step(clock, nxt)
        clock = nxt
        if clock.date() != last_print:
            last_print = clock.date()
            print(f"  ... {clock:%Y-%m-%d}", end="\r", flush=True)
        target = now_local() if args.speed == 1 else target
    con.execute("UPDATE pos.stream_status SET mode=%s, speed=%s WHERE id=1",
                ("live" if args.speed == 1 else "demo", args.speed))
    con.commit()
    print(f"\ncaught up in {time.time() - t_start:.0f}s")
    if args.catchup_only:
        return

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    mode = "live" if args.speed == 1 else f"demo x{args.speed:g}"
    print(f"streaming ({mode}), writing every {args.tick:g}s. Ctrl+C to stop.")
    real0, sim0 = time.time(), clock
    while not stop["flag"]:
        time.sleep(args.tick)
        nxt = now_local() if args.speed == 1 else sim0 + timedelta(seconds=(time.time() - real0) * args.speed)
        if nxt <= clock:          # clock is ahead (e.g. after a demo run); wait for real time to catch up
            continue
        s.step(clock, nxt)
        clock = nxt
        opened = con.execute("SELECT count(*) FROM pos.table_status WHERE status='occupied'").fetchone()[0]
        print(f"  {clock:%a %H:%M:%S}  tables occupied: {opened:3d}", end="\r", flush=True)
        if args.run_seconds and time.time() - real0 > args.run_seconds:
            break
    print("\nstopped")


if __name__ == "__main__":
    main()
