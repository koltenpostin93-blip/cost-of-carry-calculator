"""Create JSA.COST_OF_CARRY and load the futures history archive into it.

Re-runnable: the table is TRUNCATEd before each load, so running this twice leaves
exactly one copy of the data. Reads credentials from the repo's .env.

    python snowflake/01_migrate_archive.py            # create + load
    python snowflake/01_migrate_archive.py --verify   # count/spot-check only
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
load_dotenv(REPO / ".env")

CSV_PATH = REPO / "data" / "futures_history_archive.csv"
SCHEMA = "COST_OF_CARRY"
TABLE = "FUTURES_HISTORY_ARCHIVE"

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
    PRODUCT_CODE VARCHAR(8)  NOT NULL,   -- ZC, ZS
    MONTH        VARCHAR(1)  NOT NULL,   -- contract month letter
    YEAR         NUMBER(4,0) NOT NULL,   -- contract year, e.g. 2014
    DATE         DATE        NOT NULL,   -- trading date
    PRICE        FLOAT       NOT NULL    -- settlement, quote units (cents/bu)
);
"""


def connect():
    import snowflake.connector

    missing = [k for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
    )


def verify(cur, expected: int | None = None) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE}")
    n = cur.fetchone()[0]
    print(f"  rows in Snowflake: {n:,}")
    if expected is not None:
        print(f"  rows in CSV      : {expected:,}")
        print(f"  {'MATCH' if n == expected else 'MISMATCH'}")
    cur.execute(
        f"SELECT PRODUCT_CODE, COUNT(*), MIN(YEAR), MAX(YEAR), MIN(DATE), MAX(DATE) "
        f"FROM {SCHEMA}.{TABLE} GROUP BY PRODUCT_CODE ORDER BY PRODUCT_CODE"
    )
    for code, count, y0, y1, d0, d1 in cur.fetchall():
        print(f"  {code}: {count:,} rows | contract years {y0}-{y1} | dates {d0} -> {d1}")
    return n


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"Archive CSV not found: {CSV_PATH}")
    csv_rows = len(pd.read_csv(CSV_PATH))

    conn = connect()
    cur = conn.cursor()
    try:
        if "--verify" in sys.argv:
            cur.execute(f"USE SCHEMA {os.environ.get('SNOWFLAKE_DATABASE', 'JSA')}.{SCHEMA}")
            verify(cur, csv_rows)
            return

        print(f"creating {SCHEMA}.{TABLE} ...")
        for stmt in filter(None, (s.strip() for s in DDL.split(";"))):
            cur.execute(stmt)
        # PUT to a table stage (@%TABLE) resolves against the session's current
        # schema, so set it explicitly rather than relying on a qualified name.
        cur.execute(f"USE SCHEMA {os.environ.get('SNOWFLAKE_DATABASE', 'JSA')}.{SCHEMA}")

        # The repo path contains spaces, which PUT does not handle — stage via a temp dir.
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "archive.csv"
            shutil.copy(CSV_PATH, staged)
            print("truncating and loading ...")
            cur.execute(f"TRUNCATE TABLE {SCHEMA}.{TABLE}")
            cur.execute(f"PUT 'file://{staged.as_posix()}' @%{TABLE} OVERWRITE = TRUE")
            cur.execute(
                # MATCH_BY_COLUMN_NAME on CSV requires PARSE_HEADER, which is
                # mutually exclusive with SKIP_HEADER — use PARSE_HEADER so the
                # load is keyed on the CSV's own header row, not column order.
                f"COPY INTO {SCHEMA}.{TABLE} FROM @%{TABLE} "
                "FILE_FORMAT = (TYPE = CSV PARSE_HEADER = TRUE FIELD_OPTIONALLY_ENCLOSED_BY = '\"') "
                "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
                "PURGE = TRUE"
            )
        conn.commit()
        print("verifying:")
        verify(cur, csv_rows)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
