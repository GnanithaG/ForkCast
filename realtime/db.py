"""Database connection helpers shared by the loader, streamer and evaluator.

Connection string comes from FORKCAST_DSN, e.g.
    postgresql://postgres:forkcast@localhost:5432/forkcast
"""
import io
import os
from pathlib import Path

import psycopg

DSN = os.environ.get("FORKCAST_DSN", "postgresql://postgres:forkcast@localhost:5432/forkcast")
SQL_DIR = Path(__file__).resolve().parent / "sql"
BUSINESS_TZ = "America/New_York"


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=autocommit)


def run_sql_file(con, name: str):
    con.execute((SQL_DIR / name).read_text())


def copy_df(con, table: str, df, columns=None, chunk: int = 250_000):
    """Fast bulk insert of a DataFrame using COPY ... FROM STDIN (CSV)."""
    cols = list(columns or df.columns)
    col_sql = ", ".join(cols)
    with con.cursor() as cur:
        for start in range(0, len(df), chunk):
            buf = io.StringIO()
            df.iloc[start:start + chunk][cols].to_csv(buf, index=False, header=False, na_rep="")
            with cur.copy(f"COPY {table} ({col_sql}) FROM STDIN WITH (FORMAT csv, NULL '')") as cp:
                cp.write(buf.getvalue())
