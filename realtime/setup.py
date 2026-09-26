"""One-time setup of the real-time stack, then (optionally) start it.

    python realtime/setup.py            # schema + 24 months of history + analytics layer + first evaluation
    python realtime/setup.py --start    # same, then run the streamer and evaluator until Ctrl+C

Needs PostgreSQL reachable at FORKCAST_DSN
(default postgresql://postgres:forkcast@localhost:5432/forkcast).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import connect, run_sql_file, DSN  # noqa: E402

PY = sys.executable


def step(msg):
    print(f"\n=== {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true", help="start streamer + evaluator after setup")
    ap.add_argument("--speed", type=float, default=1.0, help="streamer speed when starting (1 = real time)")
    args = ap.parse_args()

    print(f"database: {DSN.rsplit('@', 1)[-1]}")
    step("1/5 schema and 24 months of history")
    subprocess.run([PY, str(HERE / "load_history.py"), "--reset"], check=True)

    step("2/5 catch the stream up from 1 Sep 2026 to now")
    subprocess.run([PY, str(HERE / "streamer.py"), "--catchup-only"], check=True)

    step("3/5 analytics layer (views, facts) and full refresh")
    with connect() as con:
        t = time.time()
        run_sql_file(con, "10_analytics.sql")
        run_sql_file(con, "20_evaluation.sql")
        con.execute("SELECT analytics.refresh(DATE '2024-09-01')")
        con.commit()
        print(f"  built in {time.time() - t:.0f}s")

    step("4/5 first evaluation: forecast, KPIs, OKRs, pain points")
    subprocess.run([PY, str(HERE / "evaluator.py"), "--backfill", "28"], check=True)
    subprocess.run([PY, str(HERE / "evaluator.py"), "--once"], check=True)

    step("5/5 Power BI project")
    subprocess.run([PY, str(HERE.parent / "powerbi" / "build_pbip.py")], check=True)

    print("\nSetup complete. Open powerbi/ForkCast.pbip in Power BI Desktop (see powerbi/POWERBI_GUIDE.md).")
    if not args.start:
        print("Start the live feed with:  python realtime/setup.py --start   (or run streamer.py and evaluator.py yourself)")
        return
    run_live(args.speed)


def run_live(speed=1.0):
    print("\nstarting streamer and evaluator (Ctrl+C stops both)")
    procs = [subprocess.Popen([PY, str(HERE / "streamer.py"), "--speed", str(speed)]),
             subprocess.Popen([PY, str(HERE / "evaluator.py"), "--interval", "120"])]
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()


if __name__ == "__main__":
    main()
