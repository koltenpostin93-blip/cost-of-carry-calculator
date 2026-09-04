"""Snowflake backend for the pre-2021 futures history archive.

Deliberately thin: this app's only persistent data is one immutable reference table,
read once per session and cached, so there is no need for the cursor/dict shims the
other JSA apps carry. A single query returns a DataFrame and the column names are
lowercased to match what the CSV path produces.

Enabled by USE_SNOWFLAKE=1. When it is off — or when Snowflake is unreachable — the
committed CSV is used instead, so the app never hard-fails on a database outage.
"""
from __future__ import annotations

import os

import pandas as pd

SCHEMA = "COST_OF_CARRY"
TABLE = "FUTURES_HISTORY_ARCHIVE"

_TRUE = {"1", "true", "yes", "on"}


def use_snowflake() -> bool:
    return os.environ.get("USE_SNOWFLAKE", "").strip().lower() in _TRUE


def connect():
    """Explicit-credentials connection. No Snowpark/active-session path — this app is
    hosted on Streamlit Community Cloud, never Streamlit-in-Snowflake."""
    import snowflake.connector

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", SCHEMA),
    )


def read_archive() -> pd.DataFrame:
    """The whole archive as product_code / month / year / date / price.

    Snowflake returns UPPERCASE column names and native date objects; both are
    normalised here so callers cannot tell which backend served the rows.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT PRODUCT_CODE, MONTH, YEAR, DATE, PRICE "
                f"FROM {SCHEMA}.{TABLE} ORDER BY PRODUCT_CODE, YEAR, MONTH, DATE"
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()

    frame = pd.DataFrame(rows, columns=["product_code", "month", "year", "date", "price"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["year"] = frame["year"].astype(int)
    frame["price"] = frame["price"].astype(float)
    return frame
