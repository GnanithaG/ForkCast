"""Build the DuckDB warehouse: staging views + KPI marts from the SQL models in /sql.

Run:  python src/build_warehouse.py
"""
from datetime import date
import time

import duckdb

from config import RAW, WAREHOUSE, SQL_DIR, START, END

TTM_START = date(END.year - 1, END.month % 12 + 1, 1)   # trailing-twelve-month window start


def build():
    params = {"RAW": RAW.as_posix(), "START": START.isoformat(), "END": END.isoformat(),
              "TTM_START": TTM_START.isoformat()}
    WAREHOUSE.unlink(missing_ok=True)
    con = duckdb.connect(str(WAREHOUSE))
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        t = time.time()
        sql = sql_file.read_text()
        for k, v in params.items():
            sql = sql.replace("{" + k + "}", v)
        con.execute(sql)
        print(f"  ran {sql_file.name:<22} {time.time() - t:5.1f}s")
    tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'mart_%' ORDER BY 1").fetchall()
    for (t,) in tables:
        print(f"    {t:<28} {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>9,} rows")
    con.close()


if __name__ == "__main__":
    build()
