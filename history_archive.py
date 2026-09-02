"""Pre-2021 corn/soybean settlement history, ETL'd from the JSA "Futures History.xlsx"
workbook (see scripts/build_history_archive.py) to bridge the gap before Massive's daily
bars start (2021-09-02). Massive covers everything from contract year 2022 on; this file
covers 2008-2021 for corn (ZC) and soybeans (ZS) only — no wheat, meal, or oil.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ARCHIVE_PATH = Path(__file__).parent / "data" / "futures_history_archive.csv"

# Contract years at or after this are treated as "live" (fetched from Massive via its
# ordinary single-digit-year ticker); years before it come from the CSV archive instead.
# Kept well clear of Massive's real 2021-09-02 start so a contract's whole trading life
# (which can begin over a year before expiry) is drawn from one source, not stitched.
ARCHIVE_CUTOFF_YEAR = 2022


def load_price_archive() -> dict[tuple[str, str, int], pd.Series]:
    """{(product_code, month_letter, year): Series(date -> price)}. Empty dict if the
    archive CSV hasn't been generated (see scripts/build_history_archive.py)."""
    if not ARCHIVE_PATH.exists():
        return {}
    df = pd.read_csv(ARCHIVE_PATH, parse_dates=["date"])
    out: dict[tuple[str, str, int], pd.Series] = {}
    for (code, month, year), group in df.groupby(["product_code", "month", "year"]):
        out[(code, month, int(year))] = pd.Series(
            group["price"].to_numpy(), index=group["date"].dt.date
        ).sort_index()
    return out
