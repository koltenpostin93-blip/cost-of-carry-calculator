import base64
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from history_archive import ARCHIVE_CUTOFF_YEAR, load_price_archive
from massive_api import (MassiveApiError, get_fed_funds_rate, get_futures_curve,
                         get_settlement_histories)

HERE = Path(__file__).parent

# Swap LOGO_FILE to the 50-year anniversary asset once it's dropped into assets/.
LOGO_FILE = "logo-50yr.png"
FAVICON_FILE = "jsa_favicon.png"
WATERMARK_FILE = "logo-50yr.png"
WATERMARK_OPACITY = 0.10


def asset(name: str) -> str:
    return str(HERE / "assets" / name)


def watermark_path() -> str | None:
    """Prefer the 50-year mark; fall back to the standard logo until it's dropped in."""
    for name in (WATERMARK_FILE, LOGO_FILE):
        candidate = asset(name)
        if Path(candidate).exists():
            return candidate
    return None


@st.cache_data(show_spinner=False)
def watermark_uri(path: str) -> str:
    """Logo as a base64 data URI — Plotly layout images can't read local paths."""
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


st.set_page_config(
    page_title="JSA - Cost of Carry & Seasonal Spreads",
    page_icon=asset(FAVICON_FILE),
    layout="wide",
)

COMMODITIES = [
    {
        "key": "corn",
        "label": "Corn",
        "sublabel": "CBOT · ZC",
        "product_code": "ZC",
        "default_storage": 0.00265,
        "storage_unit": "cents/bu/day",
        "multiplier": 100,
    },
    {
        "key": "soybeans",
        "label": "Soybeans",
        "sublabel": "CBOT · ZS",
        "product_code": "ZS",
        "default_storage": 0.00265,
        "storage_unit": "cents/bu/day",
        "multiplier": 100,
    },
    {
        "key": "soymeal",
        "label": "Soybean meal",
        "sublabel": "CBOT · ZM",
        "product_code": "ZM",
        "default_storage": 0.12,
        "storage_unit": "$/ton/day",
        "multiplier": 1,
    },
    {
        "key": "soyoil",
        "label": "Soybean oil",
        "sublabel": "CBOT · ZL",
        "product_code": "ZL",
        "default_storage": 0.005,
        "storage_unit": "cents/lb/day",
        "multiplier": 1,
    },
    {
        "key": "chi_wheat",
        "label": "Chicago wheat (SRW)",
        "sublabel": "CBOT · ZW",
        "product_code": "ZW",
        "default_storage": 0.00265,
        "storage_unit": "cents/bu/day",
        "multiplier": 100,
    },
    {
        "key": "kc_wheat",
        "label": "KC wheat (HRW)",
        "sublabel": "CBOT · KE",
        "product_code": "KE",
        "default_storage": 0.00165,
        "storage_unit": "cents/bu/day",
        "multiplier": 100,
    },
    {
        "key": "spring_wheat",
        "label": "Spring wheat (HRS)",
        "sublabel": "MGEX · HRS",
        "product_code": "HRS",
        "default_storage": 0.002667,
        "storage_unit": "cents/bu/day",
        "multiplier": 100,
    },
]

HISTORY_LOOKBACK_DAYS = 365

# Full-carry convention: fed funds + 2.25%.
FED_FUNDS_SPREAD_PCT = 2.25
# Static safety net for when the live ZQ read fails. The workbook's 6.14% assumed a
# 2.5% spread, so it is rebased here to keep the fallback on the same convention.
FALLBACK_ANNUAL_RATE_PCT = 5.89

MONTH_LETTERS = {
    "F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun",
    "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec",
}


def get_api_key() -> str:
    try:
        key = st.secrets.get("MASSIVE_API_KEY", "")
    except Exception:
        key = ""
    return key or os.environ.get("MASSIVE_API_KEY", "")


def friendly_contract(ticker: str, product_code: str) -> str:
    suffix = ticker[len(product_code):]
    if len(suffix) == 2 and suffix[0] in MONTH_LETTERS:
        return f"{MONTH_LETTERS[suffix[0]]} '2{suffix[1]}"
    return ticker


@st.cache_data(ttl="5m", show_spinner=False)
def load_curve(product_code: str, api_key: str, as_of: str, n_contracts: int) -> pd.DataFrame:
    return get_futures_curve(product_code, api_key, date.fromisoformat(as_of), n_contracts=n_contracts)


@st.cache_data(ttl="6h", show_spinner="Loading spread history…")
def load_history(tickers: tuple[str, ...], api_key: str, as_of: str) -> dict[str, pd.Series]:
    """Settlement history for a whole curve. Cached hard — daily bars change once a day."""
    return get_settlement_histories(list(tickers), api_key)


@st.cache_data(ttl="1h", show_spinner=False)
def load_fed_funds(api_key: str, as_of: str) -> dict:
    """Front-month ZQ implied fed funds rate. Cached — it moves in basis points."""
    return get_fed_funds_rate(api_key, date.fromisoformat(as_of))


def carry_bucket(pct: float) -> str:
    if pct >= 0.75:
        return "high"
    if pct >= 0.50:
        return "mid"
    return "low"


BUCKET_STYLE = {
    "high": "background-color:#C6EFCE;color:#006100;",
    "mid": "background-color:#FFEB9C;color:#9C5700;",
    "low": "background-color:#FFC7CE;color:#9C0006;",
}

GROUP_BAND = "background-color:#EAF7EA;"
GROUP_HEADER = "background-color:#CDEFCD;font-weight:600;border-top:1px solid #8FCB8F;"


def compute_carry_table(curve: pd.DataFrame, daily_storage_rate: float, annual_rate: float, multiplier: int,
                        history: dict[str, pd.Series] | None = None,
                        history_start: date | None = None) -> pd.DataFrame:
    """Full spread matrix: every near contract against every later (deferred) contract."""
    history = history or {}
    rows = []
    for i in range(len(curve)):
        near = curve.iloc[i]
        for j in range(i + 1, len(curve)):
            far = curve.iloc[j]
            days = (far["expiration"] - near["expiration"]).days
            if days <= 0:
                continue
            spread = near["price"] - far["price"]
            storage_full = days * daily_storage_rate * multiplier
            interest_full = near["price"] * annual_rate * days / 360
            full_carry = storage_full + interest_full
            if not full_carry or not storage_full or not interest_full:
                continue
            # historical range of this spread, over sessions where both legs traded
            near_hist = history.get(near["ticker"])
            far_hist = history.get(far["ticker"])
            low = high = low_date = high_date = sessions = None
            if near_hist is not None and far_hist is not None and len(near_hist) and len(far_hist):
                spread_hist = (near_hist - far_hist).dropna()
                if history_start is not None:
                    spread_hist = spread_hist[spread_hist.index >= history_start]
                if len(spread_hist):
                    sessions = len(spread_hist)
                    low, high = spread_hist.min(), spread_hist.max()
                    low_date, high_date = spread_hist.idxmin(), spread_hist.idxmax()

            rows.append(
                {
                    "near_idx": i,
                    "Near": near["ticker"],
                    "Far": far["ticker"],
                    "Near price": near["price"],
                    "Current": spread,
                    "Low": low,
                    "Low date": low_date,
                    "High": high,
                    "High date": high_date,
                    "Sessions": sessions,
                    "Full storage": storage_full,
                    "% full storage": spread / -storage_full,
                    "Monthly interest": interest_full / (days / 30),
                    "Full interest": interest_full,
                    "% full interest": -spread / interest_full,
                    "Full carry": full_carry,
                    "% full carry": spread / -full_carry,
                }
            )
    return pd.DataFrame(rows)


FULL_CARRY_COLUMNS = ["Full storage", "Full interest", "Full carry"]


def table_watermark_css() -> str:
    """The dataframe grid paints its cells on an opaque canvas, so a background image
    behind it never shows. The mark is overlaid instead, with pointer-events:none so
    sorting, scrolling and the toolbar still work."""
    wm = watermark_path()
    if not wm:
        return ""
    return f"""<style>
[class*="st-key-tablewrap_"] {{ position: relative; }}
[class*="st-key-tablewrap_"]::after {{
  content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 5;
  background-image: url('{watermark_uri(wm)}');
  background-repeat: no-repeat; background-position: center center;
  background-size: min(34%, 260px);
  opacity: {WATERMARK_OPACITY};
}}
</style>"""


def plotly_config(filename: str) -> dict:
    """One-click PNG straight from the chart toolbar. Client-side, so it costs nothing
    to render and works on Streamlit Cloud where kaleido is fragile."""
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "toImageButtonOptions": {"format": "png", "filename": filename,
                                 "height": 700, "width": 1400, "scale": 2},
    }


@st.cache_data(show_spinner=False, max_entries=32)
def figure_png(fig_json: str) -> bytes | None:
    """Server-side PNG for the explicit download button. Cached on the figure itself so
    a rerun that doesn't change the chart doesn't pay for it twice."""
    try:
        import plotly.io as pio
        return pio.from_json(fig_json).to_image(format="png", width=1400, height=700, scale=2)
    except Exception:
        return None


def export_row(frame: pd.DataFrame, filename: str, key: str, fig=None):
    """Copy-to-clipboard + CSV, plus PNG when a figure is supplied.

    PNG bytes are only rendered once the user asks, because to_image() costs about a
    second per chart and this app draws a lot of charts."""
    row = st.container(horizontal=True, vertical_alignment="center")
    with row:
        # st.code carries a native copy-to-clipboard control. A hand-rolled
        # <button onclick=...> cannot work here: contract labels contain apostrophes
        # ("Sep '26") which close the attribute, and Streamlit strips inline handlers.
        tsv = frame.to_csv(sep="	", index=False)
        with st.popover("Copy", width=90):
            st.caption("Tab-separated — use the copy icon, then paste into Excel.")
            st.code(tsv, language=None, height=260)
        st.download_button("CSV", frame.to_csv(index=False).encode(), f"{filename}.csv",
                           "text/csv", key=f"csv_{key}", width=90)
        if fig is not None:
            if st.button("PNG", key=f"png_btn_{key}", width=90,
                         help="Render this chart as a PNG for download."):
                st.session_state[f"png_ready_{key}"] = True
            if st.session_state.get(f"png_ready_{key}"):
                data = figure_png(fig.to_json())
                if data:
                    st.download_button("Save PNG", data, f"{filename}.png", "image/png",
                                       key=f"png_dl_{key}", width=120)
                else:
                    st.caption("PNG export unavailable — use the camera icon on the chart.")


def render_legend():
    chips = "".join(
        f'<span style="{BUCKET_STYLE[b]}padding:2px 10px;border-radius:4px;font-size:0.82rem;'
        f'white-space:nowrap;">{txt}</span>'
        for b, txt in (
            ("high", "&ge; 75% full carry"),
            ("mid", "50&ndash;74% partial"),
            ("low", "&lt; 50% thin"),
        )
    )
    st.markdown(
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:2px 0 6px;">'
        '<span style="font-size:0.82rem;color:#6b7280;">% of full carry</span>' + chips + "</div>",
        unsafe_allow_html=True,
    )


def render_commodity(commodity: dict, api_key: str, as_of: date, default_rate_pct: float):
    key = commodity["key"]
    with st.container(border=True):
        header = st.container(horizontal=True, vertical_alignment="center")
        with header:
            st.subheader(commodity["label"])
            st.caption(commodity["sublabel"])

        controls = st.container(horizontal=True, vertical_alignment="bottom")
        with controls:
            n_contracts = st.slider(
                "Contract months to load",
                min_value=3,
                max_value=10,
                value=6,
                key=f"months_{key}",
                width=230,
                help="Spread pairs grow quickly — n contracts = n×(n-1)/2 rows.",
            )
            storage_rate = st.number_input(
                f"Daily storage rate ({commodity['storage_unit']})",
                min_value=0.0,
                value=commodity["default_storage"],
                step=0.00005,
                format="%.5f",
                key=f"storage_{key}",
                width=230,
            )
            annual_rate_pct = st.number_input(
                "Annual interest rate (%)",
                min_value=0.0,
                max_value=25.0,
                value=default_rate_pct,
                step=0.01,
                key=f"rate_{key}",
                width=190,
                help=f"Defaults to live front-month fed funds + {FED_FUNDS_SPREAD_PCT:.2f}%. "
                "Edit to use your own cost of funds.",
            )
            show_full_carry = st.toggle(
                "Show full carry $",
                value=False,
                key=f"cols_{key}",
                help="Reveal the dollar components behind the percentages: "
                "full storage, full interest and full carry.",
            )

        annual_rate = annual_rate_pct / 100

        try:
            curve = load_curve(commodity["product_code"], api_key, as_of.isoformat(), n_contracts)
        except MassiveApiError as e:
            st.error(f"Couldn't load live quotes: {e}")
            return

        if curve.empty:
            st.warning("No live contracts returned for this market right now.")
            return

        try:
            history = load_history(tuple(curve["ticker"]), api_key, as_of.isoformat())
        except MassiveApiError as e:
            st.warning(f"Spread history unavailable: {e}")
            history = {}

        table = compute_carry_table(
            curve,
            storage_rate,
            annual_rate,
            commodity["multiplier"],
            history,
            history_start=as_of - timedelta(days=HISTORY_LOOKBACK_DAYS),
        )
        if table.empty:
            st.warning("Not enough contract months to build spreads.")
            return

        front_pair = table.iloc[0]
        best = table.loc[table["% full carry"].idxmax()]
        with st.container(horizontal=True):
            st.metric("Front month", friendly_contract(curve.iloc[0]["ticker"], commodity["product_code"]),
                      f"{curve.iloc[0]['price']:.2f}", delta_color="off", border=True)
            st.metric("Widest full-carry capture (any pair)", f"{best['% full carry']:.0%}",
                      f"{friendly_contract(best['Near'], commodity['product_code'])} / "
                      f"{friendly_contract(best['Far'], commodity['product_code'])}", delta_color="off", border=True)
            st.metric("Nearby spread", f"{front_pair['Current']:+.2f}",
                      f"{friendly_contract(front_pair['Near'], commodity['product_code'])} / "
                      f"{friendly_contract(front_pair['Far'], commodity['product_code'])}",
                      delta_color="off", border=True)

        display = table.copy()
        display["Near"] = display.apply(lambda r: friendly_contract(r["Near"], commodity["product_code"]), axis=1)
        display["Far"] = display.apply(lambda r: friendly_contract(r["Far"], commodity["product_code"]), axis=1)
        # blank repeated "Near"/"Near price" within a group so it reads like a merged header row
        is_group_start = display["near_idx"] != display["near_idx"].shift(1)
        display["Near price"] = [
            f"{v:.2f}" if start else "" for v, start in zip(display["Near price"], is_group_start)
        ]
        display.loc[~is_group_start, "Near"] = ""
        for col in ("Low date", "High date"):
            display[col] = [d.strftime("%b %d, %Y") if pd.notna(d) else "—" for d in display[col]]
        hidden = ["near_idx", "Sessions"]
        if not show_full_carry:
            hidden += FULL_CARRY_COLUMNS
        display = display.drop(columns=hidden)

        def zebra(row: pd.Series):
            group_start = row["Near"] != ""
            base = GROUP_HEADER if group_start else (GROUP_BAND if row.name % 2 == 0 else "")
            return [base] * len(row)

        def style_pct(col: pd.Series):
            return [BUCKET_STYLE[carry_bucket(v)] for v in col]

        number_formats = {
            "Current": "{:+.2f}",
            "Low": "{:+.2f}",
            "High": "{:+.2f}",
            "Sessions": "{:,.0f}",
            "Full storage": "{:.2f}",
            "% full storage": "{:.0%}",
            "Monthly interest": "{:.2f}",
            "Full interest": "{:.2f}",
            "% full interest": "{:.0%}",
            "Full carry": "{:.2f}",
            "% full carry": "{:.0%}",
        }
        styler = (
            display.style.apply(zebra, axis=1)
            .apply(style_pct, subset=["% full carry"])
            .format({k: v for k, v in number_formats.items() if k in display.columns}, na_rep="—")
        )

        render_legend()
        with st.container(key=f"tablewrap_{key}"):
            st.dataframe(styler, hide_index=True, width="stretch",
                         height=min(38 * (len(display) + 1) + 3, 620))
        export_row(display, f"carry_table_{key}", key=f"tbl_{key}")
        traded = table["Sessions"].dropna()
        depth = (
            f" Ranges are built from {int(traded.min()):,}–{int(traded.max()):,} sessions per spread"
            f" (days both legs traded)." if len(traded) else ""
        )
        st.caption(
            f"{len(curve)} contract months loaded → {len(table)} near/deferred spread pairs across the curve. "
            f"Low/High are the extremes of each spread over the trailing 12 months.{depth}"
        )

        render_charts(commodity, table, history, curve, api_key, as_of, storage_rate, annual_rate)


SEASONAL_YEARS_BACK = 4
# Corn/soybean calendar spreads can reach back through the CSV archive (contract years
# 2008+) rather than just Massive's live ~2021-09 window — other markets simply run out
# of data past ~5 years back and the loop already skips those gracefully.
DEEP_SEASONAL_YEARS_BACK = 18
SEASONAL_COLORS = ["#0693e3", "#e8833a", "#5aa469", "#b05fb0", "#9aa5b1"]
RANGE_CHOICES = {"1Y": 365, "2Y": 730, "All": None}
REF_STORAGE_COLOR = "#8d6e63"
REF_INTEREST_COLOR = "#7986cb"
REF_CARRY_COLOR = "#5aa469"
FND_COLOR = "#c62828"


def first_notice_day(expiration: date) -> date:
    """CME grain rule: First Notice Day is the last business day of the month
    preceding the delivery month. Weekend-aware only — exchange holidays are not
    applied, so a holiday-adjacent FND can land a day late."""
    first_of_delivery_month = pd.Timestamp(year=expiration.year, month=expiration.month, day=1)
    return (first_of_delivery_month - pd.offsets.BDay(1)).date()


def shift_ticker_year(ticker: str, product_code: str, delta: int) -> str | None:
    """ZCU6 -> ZCU5 at delta=-1. Massive quotes outrights with a single-digit year,
    and resolves that digit to the most recent matching contract."""
    suffix = ticker[len(product_code):]
    if len(suffix) != 2 or suffix[0] not in MONTH_LETTERS:
        return None
    month, year = suffix[0], int(suffix[1])
    shifted = year + delta
    if shifted < 0:
        return None
    return f"{product_code}{month}{shifted % 10}"


def month_letter_of(ticker: str, product_code: str) -> str | None:
    suffix = ticker[len(product_code):]
    return suffix[0] if len(suffix) == 2 and suffix[0] in MONTH_LETTERS else None


@st.cache_resource(show_spinner=False)
def price_archive() -> dict[tuple[str, str, int], pd.Series]:
    return load_price_archive()


def deep_year_key(product_code: str, month_letter: str, year: int) -> str:
    """A ticker-shaped key that also carries the full 4-digit year, so it can't collide
    the way Massive's own single-digit-year outrights (`ZCZ6`) do past ~9 years back."""
    return f"{product_code}{month_letter}_{year}"


@st.cache_data(ttl="6h", show_spinner="Loading seasonal history…")
def load_deep_seasonal_histories(near: str, far: str, product_code: str, api_key: str,
                                 years_back: int, near_expiry: date,
                                 far_expiry: date) -> tuple[dict[str, pd.Series], dict[str, date]]:
    """Prior-year analogs of a near/far pair, bridging Massive (contract years >= 2022)
    with the pre-2021 CSV archive (corn/soybeans only — see history_archive.py) so the
    lookback isn't capped at Massive's ~5-year live window. Returns dicts keyed by
    `deep_year_key`, drop-in compatible with `build_pair_series`.
    """
    near_letter = month_letter_of(near, product_code)
    far_letter = month_letter_of(far, product_code)
    if not near_letter or not far_letter:
        return {}, {}

    archive = price_archive()
    hist: dict[str, pd.Series] = {}
    expiries: dict[str, date] = {}
    live_tickers: dict[str, str] = {}  # massive ticker -> deep key

    for back in range(years_back + 1):
        for letter, base_expiry in ((near_letter, near_expiry), (far_letter, far_expiry)):
            year = base_expiry.year - back
            key = deep_year_key(product_code, letter, year)
            if key in expiries or key in live_tickers.values():
                continue
            if year >= ARCHIVE_CUTOFF_YEAR:
                live_tickers[f"{product_code}{letter}{year % 10}"] = key
            else:
                series = archive.get((product_code, letter, year))
                if series is not None and len(series):
                    hist[key] = series
                    expiries[key] = date(year, base_expiry.month, min(base_expiry.day, 28))

    if live_tickers:
        fetched = get_settlement_histories(list(live_tickers), api_key)
        for ticker, key in live_tickers.items():
            series = fetched.get(ticker)
            if series is not None and len(series):
                hist[key] = series
                letter, year = key.split("_")[0][len(product_code):], int(key.rsplit("_", 1)[1])
                base_expiry = near_expiry if letter == near_letter else far_expiry
                expiries[key] = date(year, base_expiry.month, min(base_expiry.day, 28))

    return hist, expiries


def carry_components(near_price: float, days: int, storage_rate: float,
                     annual_rate: float, multiplier: int) -> tuple[float, float]:
    """(full storage, full interest) in quote units for one near/far pair."""
    storage_full = days * storage_rate * multiplier
    interest_full = near_price * annual_rate * days / 360
    return storage_full, interest_full


def build_pair_series(hist: dict, near: str, far: str, mode: str,
                      storage_rate: float, annual_rate: float, multiplier: int,
                      expiries: dict | None = None):
    """Returns (series, near_expiry, far_expiry, storage_full, interest_full_latest).

    A still-trading contract's history ends today, not at its expiration, so the
    seasonal alignment uses the real expiry where we know it — otherwise the current
    year sits weeks out of step with the expired analogs."""
    blank = (None, None, None, None, None)
    near_h, far_h = hist.get(near), hist.get(far)
    if near_h is None or far_h is None or not len(near_h) or not len(far_h):
        return blank
    spread = (near_h - far_h).dropna()
    if not len(spread):
        return blank

    expiries = expiries or {}
    near_expiry = expiries.get(near, near_h.index.max())
    far_expiry = expiries.get(far, far_h.index.max())
    days = (far_expiry - near_expiry).days
    if days <= 0:
        return blank

    near_prices = near_h.reindex(spread.index)
    storage_full, interest_latest = carry_components(
        float(near_prices.iloc[-1]), days, storage_rate, annual_rate, multiplier
    )
    if mode == "nominal":
        return spread, near_expiry, far_expiry, storage_full, interest_latest

    interest_series = near_prices * annual_rate * days / 360
    pct = (spread / -(storage_full + interest_series))
    pct = pct.replace([float("inf"), float("-inf")], pd.NA).dropna()
    return pct, near_expiry, far_expiry, storage_full, interest_latest


def _add_vline(fig, x, text, color):
    """Draw a vertical marker without add_vline(), which averages its endpoints to
    place the annotation and so raises TypeError on datetime.date x-values."""
    x = pd.Timestamp(x) if isinstance(x, date) else x
    fig.add_shape(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1,
                  line=dict(color=color, dash="dash", width=1.5))
    fig.add_annotation(x=x, xref="x", y=1.0, yref="paper", text=text, showarrow=False,
                       yanchor="bottom", font=dict(size=10, color=color))


def _style_axes(fig, y_title, x_title, fmt):
    wm = watermark_path()
    if wm:
        fig.add_layout_image(dict(
            source=watermark_uri(wm), xref="paper", yref="paper",
            x=0.5, y=0.5, sizex=0.55, sizey=0.55,
            xanchor="center", yanchor="middle", sizing="contain",
            opacity=WATERMARK_OPACITY, layer="below",
        ))
    fig.update_layout(
        height=340, margin=dict(l=10, r=78, t=22, b=10),
        yaxis_title=y_title, xaxis_title=x_title,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
    )
    fig.update_yaxes(tickformat=fmt, gridcolor="#eceff1", zeroline=True, zerolinecolor="#cfd8dc")
    fig.update_xaxes(gridcolor="#eceff1")


def _reference_levels(mode, storage_full, interest_full):
    """The y-values where the spread would exactly cover storage, interest, and both.

    In a carry market the spread is negative, so nominally these sit below zero; as a
    share of full carry they are the storage and interest slices of 100%."""
    full_carry = storage_full + interest_full
    if not full_carry:
        return []
    if mode == "nominal":
        return [
            (-storage_full, "storage", REF_STORAGE_COLOR),
            (-interest_full, "interest", REF_INTEREST_COLOR),
            (-full_carry, "full carry", REF_CARRY_COLOR),
        ]
    return [
        (storage_full / full_carry, "storage", REF_STORAGE_COLOR),
        (interest_full / full_carry, "interest", REF_INTEREST_COLOR),
        (1.0, "full carry", REF_CARRY_COLOR),
    ]


def render_charts(commodity: dict, table: pd.DataFrame, history: dict, curve: pd.DataFrame,
                  api_key: str, as_of: date, storage_rate: float, annual_rate: float):
    code = commodity["product_code"]
    key = commodity["key"]
    expiries = dict(zip(curve["ticker"], curve["expiration"]))
    tickers = list(curve["ticker"])

    st.divider()
    st.markdown("##### Spread history & seasonality")

    picker = st.container(horizontal=True, vertical_alignment="bottom")
    with picker:
        near = st.selectbox(
            "Near leg", tickers[:-1], key=f"near_{key}_{len(tickers)}", width=150,
            format_func=lambda t: friendly_contract(t, code),
        )
        later = [t for t in tickers
                 if expiries[t] > expiries.get(near, curve["expiration"].min())]
        far = st.selectbox(
            "Far leg", later, key=f"far_{key}_{len(tickers)}_{near}", width=150,
            format_func=lambda t: friendly_contract(t, code),
        )
        mode_label = st.segmented_control(
            "Measure", ["Nominal", "% of full carry"], default="Nominal", key=f"mode_{key}",
        )
        range_label = st.segmented_control(
            "Range", list(RANGE_CHOICES), default="1Y", key=f"range_{key}",
        )
        has_archive = code in ("ZC", "ZS")
        max_years = DEEP_SEASONAL_YEARS_BACK if has_archive else SEASONAL_YEARS_BACK
        years_back = st.slider(
            "Prior years", 0, max_years, SEASONAL_YEARS_BACK,
            key=f"years_{key}", width=170,
            help="Prior-year analogs of the same spread, e.g. Sep/Dec 2025 beside Sep/Dec 2026."
            + (" Corn/soybeans reach back through the 2008+ archive." if has_archive else ""),
        )
        show_avg = st.toggle(
            "Average", value=True, key=f"avg_{key}",
            help="Mean across the overlaid years at each point in the season.",
        )

    if not far:
        st.info("Pick a far leg that expires after the near leg.")
        return

    mode = "nominal" if (mode_label or "Nominal") == "Nominal" else "carry"
    window_days = RANGE_CHOICES.get(range_label or "1Y", 365)
    unit = commodity["storage_unit"].split("/")[0]
    y_title = f"Spread ({unit})" if mode == "nominal" else "% of full carry"
    fmt = ".2f" if mode == "nominal" else ".0%"
    label = f"{friendly_contract(near, code)} / {friendly_contract(far, code)}"

    series, near_exp, far_exp, storage_full, interest_full = build_pair_series(
        history, near, far, mode, storage_rate, annual_rate, commodity["multiplier"], expiries
    )
    left, right = st.columns(2)

    # ---------------------------------------------------------------- history
    with left:
        st.caption(f"**{label}** — history")
        if series is None or not len(series):
            st.info("No overlapping settlement history for this pair.")
        else:
            shown = series
            if window_days:
                cutoff = as_of - timedelta(days=window_days)
                shown = series[series.index >= cutoff]
            if not len(shown):
                st.info(f"No sessions inside the {range_label} window.")
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=list(shown.index), y=list(shown.values), mode="lines", name=label,
                    line=dict(color=SEASONAL_COLORS[0], width=2),
                    hovertemplate="%{x|%b %d, %Y}<br>%{y:" + fmt + "}<extra></extra>",
                ))
                for y, text, color in _reference_levels(mode, storage_full, interest_full):
                    fig.add_hline(y=y, line_dash="dot", line_color=color, line_width=1.5,
                                  annotation_text=text, annotation_position="right",
                                  annotation_font=dict(size=10, color=color))
                for exp_date, leg in ((near_exp, "near"), (far_exp, "far")):
                    fnd = first_notice_day(exp_date)
                    if shown.index.min() <= fnd <= shown.index.max():
                        _add_vline(fig, fnd, f"{leg} FND", FND_COLOR)
                _style_axes(fig, y_title, None, fmt)
                fig.update_layout(showlegend=False)
                st.plotly_chart(fig, width="stretch", key=f"hist_{key}",
                                config=plotly_config(f"{key}_spread_history"))
                export_row(shown.rename("spread").reset_index().rename(columns={"index": "date"}),
                           f"{key}_spread_history", key=f"hist_{key}", fig=fig)
                st.caption(
                    f"{len(shown):,} sessions · {shown.index.min():%b %d, %Y} → "
                    f"{shown.index.max():%b %d, %Y} · near FND {first_notice_day(near_exp):%b %d, %Y}"
                )

    # --------------------------------------------------------------- seasonal
    with right:
        st.caption(f"**{label}** — seasonal, aligned on near-leg expiration")
        near_letter, far_letter = month_letter_of(near, code), month_letter_of(far, code)
        deep_hist, deep_expiries = load_deep_seasonal_histories(
            near, far, code, api_key, years_back, expiries[near], expiries[far]
        )
        fig = go.Figure()
        drawn = 0
        by_dte: dict[str, pd.Series] = {}
        for back in range(years_back + 1):
            n = deep_year_key(code, near_letter, expiries[near].year - back)
            f = deep_year_key(code, far_letter, expiries[far].year - back)
            s, n_exp, _f_exp, s_full, i_full = build_pair_series(
                deep_hist, n, f, mode, storage_rate, annual_rate, commodity["multiplier"], deep_expiries
            )
            if s is None or not len(s):
                continue
            days_out = [-(n_exp - d).days for d in s.index]
            keep = [i for i, d in enumerate(days_out) if window_days is None or d >= -window_days]
            if not keep:
                continue
            name = (f"{MONTH_LETTERS[near_letter]} {expiries[near].year - back} / "
                    f"{MONTH_LETTERS[far_letter]} {expiries[far].year - back}"
                    + (" (current)" if back == 0 else ""))
            fig.add_trace(go.Scatter(
                x=[days_out[i] for i in keep], y=[s.values[i] for i in keep], mode="lines", name=name,
                line=dict(color=SEASONAL_COLORS[back % len(SEASONAL_COLORS)],
                          width=3 if back == 0 else 1.5),
                opacity=1.0 if back == 0 else 0.7,
                hovertemplate=f"{name}<br>%{{x}}d to expiry<br>%{{y:{fmt}}}<extra></extra>",
            ))
            by_dte[name] = pd.Series([s.values[i] for i in keep],
                                     index=pd.Index([days_out[i] for i in keep], name="dte"))
            drawn += 1

        if not drawn:
            st.info("No prior-year analogs with overlapping history for this pair.")
        else:
            if show_avg and len(by_dte) > 1:
                # Each crop year trades on its own session dates, so averaging the raw
                # points would jump between "mean of five years" and "one lonely year".
                # Put every year on a common daily grid first, then only average where
                # most years are present.
                lo = min(min(s.index) for s in by_dte.values())
                grid = pd.RangeIndex(int(lo), 1)
                aligned = {}
                for nm, s_dte in by_dte.items():
                    clean = s_dte[~s_dte.index.duplicated(keep="last")].sort_index()
                    aligned[nm] = clean.reindex(grid).interpolate(limit_area="inside")
                frame = pd.DataFrame(aligned)
                required = max(2, (len(aligned) + 1) // 2)
                avg = frame.mean(axis=1, skipna=True)[frame.count(axis=1) >= required]
                if len(avg):
                    fig.add_trace(go.Scatter(
                        x=list(avg.index), y=list(avg.values), mode="lines",
                        name=f"Avg ({len(aligned)}yr)",
                        line=dict(color="#111111", width=2.2, dash="dot"),
                        hovertemplate=f"Avg<br>%{{x}}d to expiry<br>%{{y:{fmt}}}<extra></extra>",
                    ))
            for y, text, color in _reference_levels(mode, storage_full, interest_full):
                fig.add_hline(y=y, line_dash="dot", line_color=color, line_width=1.5,
                              annotation_text=text, annotation_position="right",
                              annotation_font=dict(size=10, color=color))
            fnd_x = -(near_exp - first_notice_day(near_exp)).days
            if window_days is None or fnd_x >= -window_days:
                _add_vline(fig, fnd_x, "FND", FND_COLOR)
            _style_axes(fig, y_title, "Calendar days to near-leg expiration", fmt)
            st.plotly_chart(fig, width="stretch", key=f"seas_{key}",
                            config=plotly_config(f"{key}_seasonal"))
            st.caption(
                f"{drawn} crop year{'s' if drawn != 1 else ''} overlaid · x = 0 is the near leg's "
                "expiration, so each year lines up at the same point in its life."
            )

    st.caption(
        f"Reference lines are the levels at which **{label}** would exactly cover full storage "
        f"({storage_full:.2f}), full interest ({interest_full:.2f}) and full carry "
        f"({storage_full + interest_full:.2f}) in {unit}. Storage is fixed across the pair's life; "
        "interest floats with the near leg's price, so its line is drawn at the current level. "
        "FND follows the CME grain rule — last business day before the delivery month "
        "(weekends only, not exchange holidays)."
    )


BUILDER_MAX_YEARS = 6
BUILDER_COLORS = ["#1f1f1f", "#0693e3", "#4a7c59", "#e8833a", "#8e44ad",
                  "#c0392b", "#16a085", "#7f8c8d"]


SUMMARY_CONTRACTS = 6
SUMMARY_COLUMNS = ["Spreads", "Far", "Current", "Monthly Interest", "Full Storage",
                   "% Full Storage", "Full Interest", "Full Carry", "% Full Carry"]
SECTION_BAR = ("background:#A9D08E;color:#1f3d1f;font-weight:700;text-align:center;"
               "padding:3px 0;border-radius:3px;letter-spacing:.04em;font-size:0.95rem;")


def sheet_ticker(ticker: str, product_code: str, expiration: date) -> str:
    """ZCU6 -> ZCU26, matching the workbook's two-digit contract labels."""
    suffix = ticker[len(product_code):]
    if len(suffix) != 2:
        return ticker
    return f"{product_code}{suffix[0]}{expiration.year % 100:02d}"


def summary_section(commodity: dict, api_key: str, as_of: date, annual_rate_pct: float):
    code = commodity["product_code"]
    storage_rate = commodity["default_storage"]
    annual_rate = annual_rate_pct / 100

    st.markdown(
        f"<div style='{SECTION_BAR}'>{commodity['sublabel'].split(' · ')[0].upper()} "
        f"{commodity['label'].upper()}</div>",
        unsafe_allow_html=True,
    )

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), SUMMARY_CONTRACTS)
    except MassiveApiError as e:
        st.error(f"{commodity['label']}: {e}")
        return
    if curve.empty:
        st.warning(f"{commodity['label']}: no live contracts.")
        return

    table = compute_carry_table(curve, storage_rate, annual_rate, commodity["multiplier"])
    if table.empty:
        st.warning(f"{commodity['label']}: not enough contract months.")
        return

    expiries = dict(zip(curve["ticker"], curve["expiration"]))
    left, right = st.columns([1, 4])
    with left:
        st.markdown(
            f"<div style='font-size:0.82rem;line-height:1.7;padding-top:6px;'>"
            f"Daily Storage Rate&nbsp;&nbsp;<b>{storage_rate:.5f}</b><br>"
            f"Annual Interest Rate&nbsp;&nbsp;<b>{annual_rate_pct:.2f}%</b><br>"
            f"<span style='color:#6b7280;'>(Fed Funds + {FED_FUNDS_SPREAD_PCT:.2f}%)</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    display = pd.DataFrame({
        "near_idx": table["near_idx"],
        "Spreads": [sheet_ticker(t, code, expiries[t]) for t in table["Near"]],
        "Far": [sheet_ticker(t, code, expiries[t]) for t in table["Far"]],
        "Current": table["Current"],
        "Monthly Interest": table["Monthly interest"],
        "Full Storage": table["Full storage"],
        "% Full Storage": table["% full storage"],
        "Full Interest": table["Full interest"],
        "Full Carry": table["Full carry"],
        "% Full Carry": table["% full carry"],
    })
    is_group_start = display["near_idx"] != display["near_idx"].shift(1)
    display.loc[~is_group_start, "Spreads"] = ""
    display["Monthly Interest"] = [
        f"{v:.2f}" if start else "" for v, start in zip(display["Monthly Interest"], is_group_start)
    ]
    display = display.drop(columns=["near_idx"])

    def zebra(row: pd.Series):
        base = GROUP_HEADER if row["Spreads"] != "" else (GROUP_BAND if row.name % 2 == 0 else "")
        return [base] * len(row)

    styler = (
        display.style.apply(zebra, axis=1)
        .apply(lambda col: [BUCKET_STYLE[carry_bucket(v)] for v in col], subset=["% Full Carry"])
        .format({
            "Current": "{:+.2f}", "Full Storage": "{:.2f}", "% Full Storage": "{:.0%}",
            "Full Interest": "{:.2f}", "Full Carry": "{:.2f}", "% Full Carry": "{:.0%}",
        }, na_rep="—")
    )
    with right:
        # keyed container so the shared tablewrap_ watermark CSS attaches here too
        with st.container(key=f"tablewrap_sum_{commodity['key']}"):
            st.dataframe(styler, hide_index=True, width="stretch",
                         height=min(35 * (len(display) + 1) + 3, 900))
    export_row(display, f"cost_of_carry_{commodity['key']}", key=f"sum_{commodity['key']}")


def render_summary(api_key: str, as_of: date, default_rate_pct: float):
    head = st.container(horizontal=True, vertical_alignment="center")
    with head:
        st.markdown("### Cost of Carry")
        st.markdown(
            f"<div style='text-align:right;font-size:0.82rem;padding-top:10px;'>"
            f"<b>Date</b>&nbsp;&nbsp;{as_of:%m/%d/%Y}</div>",
            unsafe_allow_html=True,
        )
    st.caption("Spreads are calculated arithmetically and could deviate from board quotes.")
    render_legend()

    rate_pct = st.number_input(
        "Annual interest rate applied to every market (%)", min_value=0.0, max_value=25.0,
        value=default_rate_pct, step=0.01, key="summary_rate", width=330,
        help=f"Defaults to live front-month fed funds + {FED_FUNDS_SPREAD_PCT:.2f}%.",
    )

    for commodity in COMMODITIES:
        summary_section(commodity, api_key, as_of, rate_pct)
        st.write("")


MATRIX_METRICS = ["Market Carry", "Cost of Carry", "% Full Carry"]
MATRIX_META = ["Symbol", "Month", "Price", ""]
GROUP_TOP = "border-top:2px solid #8FCB8F;"


def tick_price(price: float, multiplier: int) -> str:
    """Grain board notation: 515.00 -> 515-0, 552.75 -> 552-6 (eighths of a cent).
    Meal ($/ton) and oil (cents/lb) aren't quoted that way, so they stay decimal."""
    if multiplier != 100:
        return f"{price:.2f}"
    whole = int(price)
    eighths = int(round((price - whole) * 8))
    if eighths >= 8:
        whole, eighths = whole + 1, 0
    return f"{whole}-{eighths}"


def build_matrix(curve: pd.DataFrame, code: str, storage_rate: float,
                 annual_rate: float, multiplier: int):
    """Near contracts down the side, deferred across the top; three rows per near month.

    Market Carry is far - near, i.e. carry quoted positive, which is the opposite sign
    to the spread column in the other tables."""
    legs = list(curve.itertuples(index=False))
    labels = [sheet_ticker(r.ticker, code, r.expiration) for r in legs]
    rows = []
    for i, near in enumerate(legs):
        for metric in MATRIX_METRICS:
            row = {
                "Symbol": labels[i] if metric == "Cost of Carry" else "",
                "Month": f"{near.expiration:%b %y}" if metric == "Cost of Carry" else "",
                "Price": tick_price(near.price, multiplier) if metric == "Cost of Carry" else "",
                "": metric,
            }
            for j, far in enumerate(legs):
                col = labels[j]
                if j <= i:
                    row[col] = None
                    continue
                days = (far.expiration - near.expiration).days
                if days <= 0:
                    row[col] = None
                    continue
                market_carry = far.price - near.price
                storage_full = days * storage_rate * multiplier
                interest_full = near.price * annual_rate * days / 360
                cost_of_carry = storage_full + interest_full
                if metric == "Market Carry":
                    row[col] = market_carry
                elif metric == "Cost of Carry":
                    row[col] = cost_of_carry
                else:
                    row[col] = market_carry / cost_of_carry if cost_of_carry else None
            rows.append(row)
    return pd.DataFrame(rows), labels


# ── Soybean board crush ───────────────────────────────────────────────────────
# One 60 lb bushel yields roughly 44 lb meal and 11 lb oil:
#   meal  $/ton  x 44/2000 = x 0.022
#   oil   c/lb   x 11 / 100 = x 0.11   (cents -> dollars)
#   beans c/bu   / 100                 (cents -> dollars)
# Massive lists no tradeable crush product — its SOM "ZM:ZL:ZS Soy Crush" combo
# returns no prices — so the margin is built from the three legs.
CRUSH_CODE = "CS"
CRUSH_MEAL_FACTOR = 44 / 2000
CRUSH_OIL_FACTOR = 11 / 100
# Soybeans trade F H K N Q U X; meal and oil trade F H K N Q U V Z. Same letter
# where both exist, and the November bean pairs with December product — the
# standard November crush.
CRUSH_MONTH_MAP = {"F": "F", "H": "H", "K": "K", "N": "N", "Q": "Q", "U": "U", "X": "Z"}


def crush_value(bean_cents: float, meal_usd: float, oil_cents: float) -> float:
    """Board crush in $/bu."""
    return CRUSH_MEAL_FACTOR * meal_usd + CRUSH_OIL_FACTOR * oil_cents - bean_cents / 100


def crush_legs(bean_ticker: str) -> tuple[str, str] | None:
    """ZSX6 -> (ZMZ6, ZLZ6). None when the bean month has no product counterpart."""
    suffix = bean_ticker[2:]
    if len(suffix) != 2:
        return None
    product_month = CRUSH_MONTH_MAP.get(suffix[0])
    if not product_month:
        return None
    return f"ZM{product_month}{suffix[1]}", f"ZL{product_month}{suffix[1]}"


@st.cache_data(ttl="5m", show_spinner=False)
def load_crush_curve(api_key: str, as_of: str, n_contracts: int) -> pd.DataFrame:
    """Crush margin per bean contract month, with each leg's price alongside."""
    beans = load_curve("ZS", api_key, as_of, n_contracts)
    meal = load_curve("ZM", api_key, as_of, n_contracts + 4).set_index("ticker")
    oil = load_curve("ZL", api_key, as_of, n_contracts + 4).set_index("ticker")

    rows = []
    for bean in beans.itertuples(index=False):
        legs = crush_legs(bean.ticker)
        if not legs:
            continue
        meal_t, oil_t = legs
        if meal_t not in meal.index or oil_t not in oil.index:
            continue
        meal_price = float(meal.loc[meal_t, "price"])
        oil_price = float(oil.loc[oil_t, "price"])
        rows.append({
            "ticker": f"{CRUSH_CODE}{bean.ticker[2:]}",
            "expiration": bean.expiration,
            "price": crush_value(bean.price, meal_price, oil_price),
            "Beans": bean.ticker, "Bean price": bean.price,
            "Meal": meal_t, "Meal price": meal_price,
            "Oil": oil_t, "Oil price": oil_price,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl="6h", show_spinner="Loading crush history…")
def load_crush_history(bean_tickers: tuple[str, ...], api_key: str, as_of: str) -> dict[str, pd.Series]:
    """Crush margin history keyed by synthetic CS ticker, on sessions where all
    three legs settled."""
    wanted: list[str] = []
    mapping: dict[str, tuple[str, str, str]] = {}
    for bean in bean_tickers:
        legs = crush_legs(bean)
        if not legs:
            continue
        meal_t, oil_t = legs
        mapping[f"{CRUSH_CODE}{bean[2:]}"] = (bean, meal_t, oil_t)
        wanted += [bean, meal_t, oil_t]

    raw = get_settlement_histories(sorted(set(wanted)), api_key)
    out: dict[str, pd.Series] = {}
    for label, (bean, meal_t, oil_t) in mapping.items():
        b, m, o = raw.get(bean), raw.get(meal_t), raw.get(oil_t)
        if b is None or m is None or o is None or not len(b) or not len(m) or not len(o):
            continue
        frame = pd.DataFrame({"b": b, "m": m, "o": o}).dropna()
        if frame.empty:
            continue
        out[label] = (CRUSH_MEAL_FACTOR * frame["m"]
                      + CRUSH_OIL_FACTOR * frame["o"]
                      - frame["b"] / 100)
    return out


def render_crush(api_key: str, as_of: date):
    st.markdown("##### Soybean crush")
    st.caption(
        r"Board crush margin per bushel: **0.022 × meal (\$/ton) + 0.11 × oil (¢/lb) − beans (\$/bu)**, "
        "on the assumption of 44 lb of meal and 11 lb of oil from a 60 lb bushel. The November bean "
        "is paired with December meal and oil, the standard November crush. Massive lists no tradeable "
        "crush contract, so this is built from the three legs rather than quoted off the board."
    )

    controls = st.container(horizontal=True, vertical_alignment="bottom")
    with controls:
        n_contracts = st.slider("Contract months", 3, 10, 6, key="crush_months", width=210)
        range_label = st.segmented_control("Range", list(RANGE_CHOICES), default="1Y", key="crush_range")
        years_back = st.slider("Prior crop years", 0, SEASONAL_YEARS_BACK, 3,
                               key="crush_years", width=190)

    try:
        curve = load_crush_curve(api_key, as_of.isoformat(), n_contracts)
    except MassiveApiError as e:
        st.error(f"Couldn't build the crush curve: {e}")
        return
    if curve.empty:
        st.warning("No crush months could be built — a leg is missing from the live curve.")
        return

    window_days = RANGE_CHOICES.get(range_label or "1Y", 365)

    with st.container(horizontal=True):
        front = curve.iloc[0]
        best = curve.loc[curve["price"].idxmax()]
        st.metric("Front crush", friendly_contract(front["ticker"], CRUSH_CODE),
                  f"${front['price']:.3f}/bu", delta_color="off", border=True)
        st.metric("Widest crush on the board", f"${best['price']:.3f}/bu",
                  friendly_contract(best["ticker"], CRUSH_CODE), delta_color="off", border=True)
        st.metric("Board spread, front to back",
                  f"{curve['price'].max() - curve['price'].min():.3f}",
                  f"{len(curve)} months", delta_color="off", border=True)

    display = pd.DataFrame({
        "Crush": [friendly_contract(t, CRUSH_CODE) for t in curve["ticker"]],
        "Crush $/bu": curve["price"],
        "Beans": curve["Beans"], "Beans ¢/bu": curve["Bean price"],
        "Meal": curve["Meal"], "Meal $/ton": curve["Meal price"],
        "Oil": curve["Oil"], "Oil ¢/lb": curve["Oil price"],
    })
    styler = display.style.format({
        "Crush $/bu": "{:.3f}", "Beans ¢/bu": "{:.2f}",
        "Meal $/ton": "{:.2f}", "Oil ¢/lb": "{:.2f}",
    }).apply(lambda r: [GROUP_BAND if r.name % 2 == 0 else ""] * len(r), axis=1)
    with st.container(key="tablewrap_crush"):
        st.dataframe(styler, hide_index=True, width="stretch",
                     height=min(38 * (len(display) + 1) + 3, 480))
    export_row(display, "soybean_crush_curve", key="crush_curve")

    # ── history & seasonality for one crush month ────────────────────────────
    pick = st.selectbox("Crush month", list(curve["ticker"]),
                        key=f"crush_pick_{n_contracts}", width=190,
                        format_func=lambda t: friendly_contract(t, CRUSH_CODE))
    bean_for = dict(zip(curve["ticker"], curve["Beans"]))
    if pick not in bean_for:
        pick = curve.iloc[0]["ticker"]
    anchor_expiry = dict(zip(curve["ticker"], curve["expiration"]))[pick]

    bean_tickers = [bean_for[pick]]
    for back in range(1, years_back + 1):
        older = shift_ticker_year(bean_for[pick], "ZS", -back)
        if older:
            bean_tickers.append(older)
    hist = load_crush_history(tuple(bean_tickers), api_key, as_of.isoformat())

    left, right = st.columns(2)
    with left:
        st.caption(f"**{friendly_contract(pick, CRUSH_CODE)} crush** — history")
        series = hist.get(pick)
        if series is None or not len(series):
            st.info("No overlapping settlement history across the three legs.")
        else:
            shown = series[series.index >= as_of - timedelta(days=window_days)] if window_days else series
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=list(shown.index), y=list(shown.values), mode="lines",
                                     line=dict(color=SEASONAL_COLORS[0], width=2), name="crush",
                                     hovertemplate="%{x|%b %d, %Y}<br>$%{y:.3f}<extra></extra>"))
            _style_axes(fig, "Crush ($/bu)", None, ".2f")
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, width="stretch", key="crush_hist",
                            config=plotly_config("soybean_crush_history"))
            st.caption(f"{len(shown):,} sessions · {shown.index.min():%b %d, %Y} → {shown.index.max():%b %d, %Y}")

    with right:
        st.caption(f"**{friendly_contract(pick, CRUSH_CODE)} crush** — seasonal")
        fig = go.Figure()
        drawn = 0
        for back, bean in enumerate(bean_tickers):
            label = f"{CRUSH_CODE}{bean[2:]}"
            s = hist.get(label)
            if s is None or not len(s):
                continue
            expiry = s.index.max() if back else anchor_expiry
            dte = [-(expiry - d).days for d in s.index]
            keep = [i for i, d in enumerate(dte) if window_days is None or d >= -window_days]
            if not keep:
                continue
            fig.add_trace(go.Scatter(
                x=[anchor_expiry + timedelta(days=dte[i]) for i in keep],
                y=[s.values[i] for i in keep], mode="lines",
                name=label + (" (current)" if back == 0 else ""),
                line=dict(color=SEASONAL_COLORS[back % len(SEASONAL_COLORS)],
                          width=3 if back == 0 else 1.5),
                opacity=1.0 if back == 0 else 0.75,
                hovertemplate=f"{label}<br>$%{{y:.3f}}<extra></extra>"))
            drawn += 1
        if not drawn:
            st.info("No prior-year crush history available.")
        else:
            _style_axes(fig, "Crush ($/bu)", None, ".2f")
            fig.update_xaxes(tickformat="%b", dtick="M1")
            st.plotly_chart(fig, width="stretch", key="crush_seasonal",
                            config=plotly_config("soybean_crush_seasonal"))
            st.caption(f"{drawn} crop year{'s' if drawn != 1 else ''} overlaid, aligned on the bean leg's expiration.")


def render_matrix(api_key: str, as_of: date, default_rate_pct: float):
    st.markdown("##### Spread matrix")
    st.caption(
        "Every near month against every deferred month at once. **Market Carry** is the "
        "deferred less the near — carry quoted positive, the opposite sign to the spread "
        "columns elsewhere in this app. **Cost of Carry** is full storage plus full interest "
        "over the same span, and **% Full Carry** is the first divided by the second."
    )

    controls = st.container(horizontal=True, vertical_alignment="bottom")
    with controls:
        names = [c["label"] for c in COMMODITIES]
        pick = st.selectbox("Commodity", names, key="mx_commodity", width=210)
        commodity = COMMODITIES[names.index(pick)]
        code = commodity["product_code"]
        n_load = st.slider("Contract months", 3, 12, 9, key="mx_months", width=200)
        storage_rate = st.number_input(
            f"Daily storage ({commodity['storage_unit']})", min_value=0.0,
            value=commodity["default_storage"], step=0.00005, format="%.5f",
            key="mx_storage", width=220)
        annual_rate_pct = st.number_input("Annual interest rate (%)", min_value=0.0, max_value=25.0,
                                          value=default_rate_pct, step=0.01, key="mx_rate", width=190)

    try:
        curve = load_curve(code, api_key, as_of.isoformat(), n_load)
    except MassiveApiError as e:
        st.error(f"Couldn't load {pick} quotes: {e}")
        return
    if curve.empty or len(curve) < 2:
        st.warning("Not enough live contracts to build a matrix.")
        return

    frame, labels = build_matrix(curve, code, storage_rate, annual_rate_pct / 100,
                                 commodity["multiplier"])

    # Cells are rendered to strings here rather than via Styler.format: the percent rows
    # and the value rows need different formats within the same column, and a second
    # format(subset=...) call drops the na_rep, leaving literal "None" in the empty half.
    def cell(value, metric: str) -> str:
        if value is None or pd.isna(value):
            return ""
        return f"{value:+.2%}" if metric == "% Full Carry" else f"{value:.2f}"

    display = frame.copy()
    for col in labels:
        display[col] = [cell(v, m) for v, m in zip(frame[col], frame[""])]

    def style_row(row: pd.Series):
        source = frame.iloc[row.name]
        metric = source[""]
        # same banding as the summary and per-market tables: a green header row
        # carrying the contract, a light band under it, and the shared carry buckets
        if metric == "Market Carry":
            base = GROUP_TOP + GROUP_BAND
        elif metric == "Cost of Carry":
            base = GROUP_HEADER
        else:
            base = GROUP_BAND
        out = []
        for col in row.index:
            value = source[col] if col in labels else None
            if value is None or pd.isna(value) or metric != "% Full Carry":
                out.append(base)
            else:
                out.append(base + BUCKET_STYLE[carry_bucket(value)])
        return out

    styler = display.style.apply(style_row, axis=1)

    render_legend()
    with st.container(key="tablewrap_matrix"):
        st.dataframe(styler, hide_index=True, width="stretch",
                     height=min(35 * (len(display) + 1) + 3, 900))
    export_row(display, f"spread_matrix_{commodity['key']}", key="matrix")


MIN_BUILDER_LEGS = 2
MAX_BUILDER_LEGS = 6
BUILDER_CURVE_MONTHS = 10


def _default_leg_weight(i: int) -> float:
    """+1, -1, +1, -1, … — a plain calendar spread by default; edit for flies/ratios."""
    return 1.0 if i % 2 == 0 else -1.0


def render_leg_pickers(api_key: str, as_of: date) -> list[dict] | None:
    """The leg rows shared by every mode: market, contract, weight — plus the
    +/- Add leg controls that let the spread grow past two legs."""
    if "builder_legs" not in st.session_state:
        st.session_state.builder_legs = MIN_BUILDER_LEGS

    ctrl = st.container(horizontal=True, vertical_alignment="center")
    with ctrl:
        if st.button("+ Add leg", key="b_add_leg",
                     disabled=st.session_state.builder_legs >= MAX_BUILDER_LEGS):
            st.session_state.builder_legs += 1
        if st.button("− Remove leg", key="b_remove_leg",
                     disabled=st.session_state.builder_legs <= MIN_BUILDER_LEGS):
            st.session_state.builder_legs -= 1
        st.caption(f"{st.session_state.builder_legs} leg(s) — each can be its own market "
                   "and contract month, so a butterfly, condor, or cross-commodity spread "
                   "all build the same way.")

    labels = [c["label"] for c in COMMODITIES]
    legs = []
    for i in range(st.session_state.builder_legs):
        row = st.container(horizontal=True, vertical_alignment="bottom")
        with row:
            commodity_label = st.selectbox(
                f"Leg {i + 1} market", labels, key=f"b_leg{i}_commodity", width=190,
            )
            commodity = COMMODITIES[labels.index(commodity_label)]
            code = commodity["product_code"]
            try:
                curve = load_curve(code, api_key, as_of.isoformat(), BUILDER_CURVE_MONTHS)
            except MassiveApiError as e:
                st.error(f"Leg {i + 1}: couldn't load {commodity_label} quotes: {e}")
                return None
            if curve.empty:
                st.warning(f"Leg {i + 1}: no live contracts for {commodity_label}.")
                return None
            tickers = list(curve["ticker"])
            contract = st.selectbox(
                # keyed on the commodity too — otherwise switching Leg i's market can
                # leave this widget holding a ticker from the old commodity's curve,
                # which isn't in the new options list at all
                f"Leg {i + 1} contract", tickers, key=f"b_leg{i}_contract_{code}", width=150,
                index=min(i, len(tickers) - 1),
                format_func=lambda t, code=code: friendly_contract(t, code),
            )
            weight = st.number_input(
                f"Leg {i + 1} weight", value=_default_leg_weight(i), step=1.0,
                key=f"b_leg{i}_weight", width=130,
                help="Positive = long, negative = short. Use non-±1 weights for ratio "
                "spreads (e.g. a crush-style combo) or fly wings.",
            )
        matches = curve.loc[curve["ticker"] == contract]
        row_match = matches.iloc[0] if len(matches) else curve.iloc[0]
        legs.append({
            "commodity": commodity, "product_code": code, "ticker": contract,
            "weight": float(weight), "price": float(row_match["price"]),
            "expiration": row_match["expiration"],
        })
    return legs


def render_combo_history(legs: list[dict], unit_label: str, as_of: date, api_key: str):
    hist = load_history(tuple(sorted({l["ticker"] for l in legs})), api_key, as_of.isoformat())
    frame = pd.DataFrame({f"leg{i}": hist.get(l["ticker"]) for i, l in enumerate(legs)}).dropna()
    if frame.empty:
        st.info("No overlapping settlement history across all selected legs.")
        return

    combo = sum(l["weight"] * frame[f"leg{i}"] for i, l in enumerate(legs))
    range_label = st.segmented_control("Range", list(RANGE_CHOICES), default="1Y", key="b_combo_range")
    window_days = RANGE_CHOICES.get(range_label or "1Y", 365)
    shown = combo[combo.index >= as_of - timedelta(days=window_days)] if window_days else combo
    if not len(shown):
        st.info(f"No sessions inside the {range_label} window.")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(shown.index), y=list(shown.values), mode="lines", name="combo",
        line=dict(color=BUILDER_COLORS[0], width=2),
        hovertemplate="%{x|%b %d, %Y}<br>%{y:.3f}<extra></extra>",
    ))
    _style_axes(fig, f"Combo value ({unit_label})", None, ".2f")
    fig.update_layout(showlegend=False, height=360)
    st.plotly_chart(fig, width="stretch", key="builder_combo_hist",
                    config=plotly_config("combo_spread_history"))
    export_row(shown.rename("value").reset_index().rename(columns={"index": "date"}),
               "combo_spread_history", key="builder_combo")
    st.caption(f"{len(shown):,} sessions · {shown.index.min():%b %d, %Y} → {shown.index.max():%b %d, %Y}")


def render_seasonal_pair(commodity: dict, near: str, far: str, api_key: str, as_of: date,
                         default_rate_pct: float):
    """The original two-leg, single-market seasonal overlay — unchanged, just re-entered
    once the builder confirms it has exactly one market and two legs."""
    code = commodity["product_code"]

    has_archive = code in ("ZC", "ZS")
    max_years = DEEP_SEASONAL_YEARS_BACK if has_archive else BUILDER_MAX_YEARS

    row3 = st.container(horizontal=True, vertical_alignment="bottom")
    with row3:
        mode_label = st.segmented_control("Measure", ["Nominal", "% of full carry"],
                                          default="Nominal", key="b_mode")
        years_back = st.slider("Prior crop years", 1, max_years, min(4, max_years), key="b_years", width=190,
                               help="Corn/soybeans reach back through the 2008+ archive." if has_archive else None)
        show_avg = st.toggle("Average", value=True, key="b_avg",
                             help="Mean across the overlaid years at each point in the season.")
        storage_rate = st.number_input(
            f"Daily storage ({commodity['storage_unit']})", min_value=0.0,
            value=commodity["default_storage"], step=0.00005, format="%.5f",
            key="b_storage", width=210)
        annual_rate_pct = st.number_input("Annual interest rate (%)", min_value=0.0, max_value=25.0,
                                          value=default_rate_pct, step=0.01, key="b_rate", width=180)
        window_label = st.segmented_control("Season length", ["6M", "1Y", "18M"],
                                            default="1Y", key="b_window")

    annual_rate = annual_rate_pct / 100
    mode = "nominal" if (mode_label or "Nominal") == "Nominal" else "carry"
    window_days = {"6M": 183, "1Y": 365, "18M": 548}.get(window_label or "1Y", 365)
    unit = commodity["storage_unit"].split("/")[0]
    y_title = f"Spread ({unit})" if mode == "nominal" else "% of full carry"
    fmt = ".2f" if mode == "nominal" else ".0%"
    pair_label = f"{friendly_contract(near, code)} / {friendly_contract(far, code)}"

    curve = load_curve(code, api_key, as_of.isoformat(), BUILDER_CURVE_MONTHS)
    expiries = dict(zip(curve["ticker"], curve["expiration"]))
    near_letter, far_letter = month_letter_of(near, code), month_letter_of(far, code)
    hist, deep_expiries = load_deep_seasonal_histories(
        near, far, code, api_key, years_back, expiries[near], expiries[far]
    )
    anchor_expiry = expiries[near]

    fig = go.Figure()
    by_dte: dict[str, pd.Series] = {}
    storage_full = interest_full = None
    skipped: list[str] = []

    for back in range(years_back + 1):
        n = deep_year_key(code, near_letter, expiries[near].year - back)
        f = deep_year_key(code, far_letter, expiries[far].year - back)
        series, near_exp, _far_exp, s_full, i_full = build_pair_series(
            hist, n, f, mode, storage_rate, annual_rate, commodity["multiplier"], deep_expiries
        )
        display = (f"{MONTH_LETTERS[near_letter]} {expiries[near].year - back} / "
                  f"{MONTH_LETTERS[far_letter]} {expiries[far].year - back}")
        if series is None or not len(series):
            skipped.append(display)
            continue
        if back == 0:
            storage_full, interest_full = s_full, i_full

        dte = [-(near_exp - d).days for d in series.index]
        keep = [i for i, d in enumerate(dte) if d >= -window_days]
        if not keep:
            skipped.append(display)
            continue

        # shift every year onto the current contract's calendar so months line up
        xs = [anchor_expiry + timedelta(days=dte[i]) for i in keep]
        ys = [series.values[i] for i in keep]
        name = display
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=name + (" (current)" if back == 0 else ""),
            line=dict(color=BUILDER_COLORS[back % len(BUILDER_COLORS)],
                      width=3.5 if back == 0 else 1.6),
            opacity=1.0 if back == 0 else 0.8,
            hovertemplate=f"{name}<br>%{{y:{fmt}}}<extra></extra>",
        ))
        by_dte[name] = pd.Series([series.values[i] for i in keep],
                                 index=pd.Index([dte[i] for i in keep], name="dte"))

    if not by_dte:
        st.warning("No overlapping settlement history for this pair in any year.")
        return

    if show_avg and len(by_dte) > 1:
        # Each crop year trades on its own session dates, so averaging the raw points
        # would jump between "mean of five years" and "one lonely year". Put every year
        # on a common daily grid first, then only average where most years are present.
        grid = pd.RangeIndex(-window_days, 1)
        aligned = {}
        for name, s_dte in by_dte.items():
            clean = s_dte[~s_dte.index.duplicated(keep="last")].sort_index()
            aligned[name] = clean.reindex(grid).interpolate(limit_area="inside")
        frame = pd.DataFrame(aligned)
        required = max(2, (len(aligned) + 1) // 2)
        avg = frame.mean(axis=1, skipna=True)[frame.count(axis=1) >= required]
        if len(avg):
            fig.add_trace(go.Scatter(
                x=[anchor_expiry + timedelta(days=int(d)) for d in avg.index],
                y=list(avg.values), mode="lines", name=f"Avg ({len(aligned)}yr)",
                line=dict(color="#111111", width=2.2, dash="dot"),
                hovertemplate=f"Avg<br>%{{y:{fmt}}}<extra></extra>",
            ))

    if storage_full is not None:
        for y, text, color in _reference_levels(mode, storage_full, interest_full):
            fig.add_hline(y=y, line_dash="dot", line_color=color, line_width=1.5,
                          annotation_text=text, annotation_position="right",
                          annotation_font=dict(size=10, color=color))
    fnd = first_notice_day(anchor_expiry)
    if fnd >= anchor_expiry - timedelta(days=window_days):
        _add_vline(fig, fnd, "FND", FND_COLOR)

    wm = watermark_path()
    if wm:
        fig.add_layout_image(dict(
            source=watermark_uri(wm), xref="paper", yref="paper", x=0.5, y=0.5,
            sizex=0.45, sizey=0.45, xanchor="center", yanchor="middle",
            sizing="contain", opacity=WATERMARK_OPACITY, layer="below",
        ))
    fig.update_layout(
        height=560, margin=dict(l=10, r=80, t=30, b=10),
        yaxis_title=y_title, xaxis_title=None,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=11)),
        title=dict(text=f"{commodity['label']} — {pair_label} seasonal spread",
                   x=0.5, xanchor="center", font=dict(size=16)),
    )
    fig.update_yaxes(tickformat=fmt, gridcolor="#eceff1", zeroline=True, zerolinecolor="#cfd8dc")
    fig.update_xaxes(gridcolor="#eceff1", tickformat="%b", dtick="M1")
    st.plotly_chart(fig, width="stretch", key="builder_chart",
                    config=plotly_config(f"{code}_{near}_{far}_seasonal"))
    export_row(pd.DataFrame(by_dte).sort_index().reset_index(),
               f"{code}_{near}_{far}_seasonal", key="builder", fig=fig)

    note = (
        f"{len(by_dte)} crop year{'s' if len(by_dte) != 1 else ''} overlaid · x-axis is the "
        f"current contract's calendar, with each prior year shifted so its near leg expires "
        f"at the same point."
    )
    if skipped:
        note += f" No usable history for {', '.join(skipped)} — the feed's daily bars start 2021-09-02."
    st.caption(note)


def render_builder(api_key: str, as_of: date, default_rate_pct: float):
    """Spread builder: 2+ legs, each its own market and contract month.

    Two legs in one market keep the full seasonal, prior-crop-year overlay below.
    Anything else — three-plus legs, or legs from different markets — falls back to
    a net-value readout plus the combo's own price history, since seasonal year-rolling
    and % of full carry are only well-defined within a single calendar-spread market.
    """
    st.markdown("##### Build a spread")
    st.caption(
        "Start with a calendar spread, then use **+ Add leg** to build a butterfly, condor, "
        "or a cross-commodity spread — each leg is its own market, contract month, and "
        "weight. Exactly two legs in the same market keeps the seasonal, prior-year view; "
        "anything broader shows the combo's live price history instead."
    )

    legs = render_leg_pickers(api_key, as_of)
    if legs is None:
        return

    same_commodity = len({l["product_code"] for l in legs}) == 1
    same_unit = len({l["commodity"]["storage_unit"] for l in legs}) == 1
    unit_label = legs[0]["commodity"]["storage_unit"].split("/")[0] if same_unit else "mixed units"
    net_actual = sum(l["weight"] * l["price"] for l in legs)

    legs_df = pd.DataFrame({
        "Leg": [f"Leg {i + 1}" for i in range(len(legs))],
        "Market": [l["commodity"]["label"] for l in legs],
        "Contract": [friendly_contract(l["ticker"], l["product_code"]) for l in legs],
        "Weight": [l["weight"] for l in legs],
        "Price": [l["price"] for l in legs],
        "Contribution": [l["weight"] * l["price"] for l in legs],
    })
    st.dataframe(
        legs_df.style.format({"Weight": "{:+.3f}", "Price": "{:.3f}", "Contribution": "{:+.3f}"}),
        hide_index=True, width="stretch",
    )

    pct_full = None
    if same_commodity and len(legs) >= 2:
        commodity = legs[0]["commodity"]
        rate_row = st.container(horizontal=True, vertical_alignment="bottom")
        with rate_row:
            storage_rate = st.number_input(
                f"Daily storage ({commodity['storage_unit']})", min_value=0.0,
                value=commodity["default_storage"], step=0.00005, format="%.5f",
                key="b_combo_storage", width=210,
            )
            annual_rate_pct = st.number_input(
                "Annual interest rate (%)", min_value=0.0, max_value=25.0,
                value=default_rate_pct, step=0.01, key="b_combo_rate", width=180,
            )
        annual_rate = annual_rate_pct / 100
        base = min(legs, key=lambda l: l["expiration"])
        theo_net = 0.0
        for l in legs:
            if l is base:
                theo_price = base["price"]
            else:
                days = (l["expiration"] - base["expiration"]).days
                storage_full, interest_full = carry_components(
                    base["price"], days, storage_rate, annual_rate, commodity["multiplier"]
                )
                theo_price = base["price"] + storage_full + interest_full
            theo_net += l["weight"] * theo_price
        pct_full = net_actual / theo_net if theo_net else None

    metrics = st.container(horizontal=True)
    with metrics:
        st.metric("Net spread value", f"{net_actual:+.3f} {unit_label}", delta_color="off", border=True)
        if pct_full is not None:
            st.metric("% of full carry", f"{pct_full:.0%}", delta_color="off", border=True)

    if not same_unit:
        st.info(
            "These legs are priced in different units, so the net value above is a raw "
            "weighted sum, not a ready-made dollar spread. Set weights to do the unit "
            "conversion yourself — see the Crush tab's 0.022 (meal) / 0.11 (oil) factors "
            "for a worked example."
        )

    st.divider()
    st.markdown("##### Combo price history")
    render_combo_history(legs, unit_label, as_of, api_key)

    if same_commodity and len(legs) == 2:
        near, far = sorted(legs, key=lambda l: l["expiration"])
        st.divider()
        st.markdown("##### Seasonal, prior-crop-year view")
        st.caption(
            "Prior crop years are reconstructed by rolling the contract year back "
            "(Nov/Jan 2026 → Nov/Jan 2025 → …), each shifted so its near leg expires on "
            "the same calendar point, which is what lets the lines sit on one seasonal axis."
        )
        render_seasonal_pair(near["commodity"], near["ticker"], far["ticker"], api_key, as_of,
                             default_rate_pct)
    else:
        st.caption(
            "Seasonal year overlay and % of full carry need exactly two legs in the same "
            "market — add/remove legs or switch every leg to one market to bring it back."
        )


def main():
    col_logo, col_title = st.columns([1, 6], vertical_alignment="center")
    with col_logo:
        st.image(asset(LOGO_FILE), width=150)
    with col_title:
        st.title("Cost of Carry & Seasonal Spreads")
    st.caption(
        "Live CBOT & MGEX grain futures curves priced against full financial cost of carry "
        "(storage + interest), every near month against every deferred month. "
        "Spreads are calculated arithmetically and may deviate from board quotes. "
        f"Data as of {datetime.now():%b %d, %Y %I:%M %p} · quotes delayed per Massive API."
    )

    api_key = get_api_key()
    if not api_key:
        st.error(
            "No MASSIVE_API_KEY found. Add it to `.streamlit/secrets.toml` "
            '(`MASSIVE_API_KEY = "..."`) or as an environment variable.'
        )
        st.stop()

    css = table_watermark_css()
    if css:
        st.markdown(css, unsafe_allow_html=True)

    as_of = date.today()

    try:
        ff = load_fed_funds(api_key, as_of.isoformat())
        default_rate_pct = round(ff["rate_pct"] + FED_FUNDS_SPREAD_PCT, 2)
        st.caption(
            f"Interest rate defaults to fed funds **{ff['rate_pct']:.3f}%** "
            f"(front-month {ff['ticker']} @ {ff['price']:.4f}, settles {ff['expiration']:%b %d, %Y}) "
            f"+ {FED_FUNDS_SPREAD_PCT:.2f}% = **{default_rate_pct:.2f}%** — editable per market below."
        )
        source_line = (
            f"Front-month **{ff['ticker']}** last traded **{ff['price']:.4f}**, "
            f"so the implied rate is `100 − {ff['price']:.4f}` = **{ff['rate_pct']:.3f}%**."
        )
    except MassiveApiError as e:
        default_rate_pct = FALLBACK_ANNUAL_RATE_PCT
        st.caption(
            f"Live fed funds unavailable ({e}); interest rate falls back to "
            f"{FALLBACK_ANNUAL_RATE_PCT:.2f}%."
        )
        source_line = (
            f"Live fed funds could not be read, so the rate falls back to a static "
            f"**{FALLBACK_ANNUAL_RATE_PCT:.2f}%**."
        )

    with st.expander("How the interest rate is derived, and how carry is calculated"):
        st.markdown(
            f"""
**1 · Fed funds, from the futures board**

The rate is read live from the CME **30-Day Federal Funds future (`ZQ`)**. These settle
against the average daily *effective* fed funds rate over the contract month and are
quoted as `100 − rate`, so the front month is the market's read on the current rate.
{source_line}

**2 · The carry spread**

The reference workbook's convention is **fed funds + {FED_FUNDS_SPREAD_PCT:.2f}%**, approximating a
commercial cost of funds rather than the risk-free rate. That sum seeds the
*Annual interest rate* box on every market, and each tab can be overridden
independently with your own cost of funds.

**3 · Interest cost of holding the near contract**

For each near/deferred pair, interest accrues on the near contract's price over the
days between the two expirations, on a **360-day** basis:

```
full interest = near price × annual rate × days ÷ 360
```

**4 · Full carry, and what the percentage means**

Storage is added to interest to give the total cost of carrying the grain:

```
full storage    = days × daily storage rate     (×100 on the cents/bu markets)
full carry      = full storage + full interest
% of full carry = spread ÷ −(full storage + full interest)
```

A spread paying **100%** of full carry covers storage and interest exactly — the market
is paying you to hold grain. Below that, carrying costs more than the board returns.
Because the near leg is the more expensive one in a carry market the spread is negative,
hence the sign flip in the denominator.
"""
        )

    tabs = st.tabs(["Summary", "Spread Builder", "Spread Matrix", "Crush"]
                   + [c["label"] for c in COMMODITIES])
    with tabs[0]:
        render_summary(api_key, as_of, default_rate_pct)
    with tabs[1]:
        render_builder(api_key, as_of, default_rate_pct)
    with tabs[2]:
        render_matrix(api_key, as_of, default_rate_pct)
    with tabs[3]:
        render_crush(api_key, as_of)
    for tab, commodity in zip(tabs[4:], COMMODITIES):
        with tab:
            render_commodity(commodity, api_key, as_of, default_rate_pct)


if __name__ == "__main__":
    main()
