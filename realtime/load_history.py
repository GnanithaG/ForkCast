"""Create the Postgres schema and bulk-load the 24-month history (Sep 2024 - Aug 2026).

The history comes from src/generate_data.py (run automatically if the parquet
files are missing). After this, start the live streamer: it catches up from
1 Sep 2026 to the current time and then keeps writing events as they happen.

Run:  python realtime/load_history.py [--reset]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import connect, run_sql_file, copy_df  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

POS_TABLES = ["order_items", "orders", "lost_parties", "labor_shifts", "purchases", "waste_log",
              "operating_expenses", "table_status", "stream_status", "dim_employees", "dim_tables",
              "dim_menu_items", "dim_locations"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate all schemas first")
    args = ap.parse_args()

    if not (RAW / "fact_checks.parquet").exists():
        print("history parquet not found -> generating it (about 30 s)")
        subprocess.run([sys.executable, "generate_data.py"], cwd=ROOT / "src", check=True)

    with connect() as con:
        if args.reset:
            con.execute("DROP SCHEMA IF EXISTS analytics CASCADE; DROP SCHEMA IF EXISTS pos CASCADE;")
        run_sql_file(con, "00_schema.sql")
        con.execute("TRUNCATE " + ", ".join(f"pos.{t}" for t in POS_TABLES) + " CASCADE")
        rd = lambda n: pd.read_parquet(RAW / f"{n}.parquet")

        t = time.time()
        copy_df(con, "pos.dim_locations", rd("dim_locations"))
        copy_df(con, "pos.dim_tables", rd("dim_tables"))
        copy_df(con, "pos.dim_menu_items", rd("dim_menu_items"))
        copy_df(con, "pos.dim_employees", rd("dim_employees"))

        ck = rd("fact_checks")
        ck["status"] = "closed"
        copy_df(con, "pos.orders", ck)
        print(f"  orders        {len(ck):>10,}")
        items = rd("fact_check_items")
        copy_df(con, "pos.order_items", items)
        print(f"  order_items   {len(items):>10,}")
        copy_df(con, "pos.lost_parties", rd("fact_lost_demand"))

        ls = rd("fact_labor_shifts")
        start = pd.to_datetime(ls.business_date) + pd.to_timedelta(ls.start_hour, unit="h")
        ls["clock_in_ts"] = start.dt.floor("min")
        ls["clock_out_ts"] = (start + pd.to_timedelta(ls.hours, unit="h")).dt.floor("min")
        ls["status"] = "completed"
        copy_df(con, "pos.labor_shifts", ls)
        print(f"  labor_shifts  {len(ls):>10,}")
        copy_df(con, "pos.purchases", rd("fact_purchases"))
        copy_df(con, "pos.waste_log", rd("fact_waste_log"))
        copy_df(con, "pos.operating_expenses", rd("fact_operating_expenses"))

        con.execute("""INSERT INTO pos.table_status (table_id, location_id, zone, seats)
                       SELECT table_id, location_id, zone, seats FROM pos.dim_tables""")
        con.execute("""INSERT INTO pos.stream_status (id, sim_clock, last_tick_at, mode, speed, orders_written)
                       VALUES (1, (SELECT max(business_date) + 1 FROM pos.orders)::timestamp, now(), 'history', 0, 0)""")
        con.commit()
        con.execute("ANALYZE")
        print(f"  history loaded in {time.time() - t:.0f}s; streamer will resume from 2026-09-01 00:00")


if __name__ == "__main__":
    main()
