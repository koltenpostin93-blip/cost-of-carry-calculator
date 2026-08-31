# JSA — Cost of Carry & Seasonal Spreads

Streamlit app that prices CBOT and MGEX grain futures spreads against the full financial
cost of carry (storage + interest), replicating the JSA *Cost of Carry Sheet* workbook with
live market data.

## What it does

For every near/deferred contract pair on the curve it computes:

```
spread          = near price - far price
full storage    = days between expirations x daily storage rate   (x100 on cents/bu markets)
full interest   = near price x annual rate x days / 360
full carry      = full storage + full interest
% of full carry = spread / -(full storage + full interest)
```

A spread paying 100% of full carry covers storage and interest exactly. Colour coding
follows the workbook: green >= 75%, yellow 50-74%, red < 50%.

## Tabs

| Tab | Purpose |
| --- | --- |
| **Summary** | All seven markets stacked in the workbook's layout, two-digit contract labels, one interest rate driving every market. |
| **Spread Builder** | Free-form seasonal chart: pick market, both legs, measure, and overlay prior crop years on a shared calendar axis with an average line. |
| **Per-market tabs** | Full spread matrix with 12-month spread high/low and dates, plus history and seasonal charts. |

Markets: corn (ZC), soybeans (ZS), soybean meal (ZM), soybean oil (ZL), Chicago/SRW wheat
(ZW), KC/HRW wheat (KE), MGEX spring wheat (HRS).

## Interest rate

Defaults to the live front-month **CME 30-Day Federal Funds future (ZQ)** implied rate
(`100 - price`) plus a 2.50% spread, the workbook's convention for a commercial cost of
funds. Editable per market.

## Data source

[Massive](https://massive.com) futures REST API (`api.massive.com/futures/v1`):

- `/contracts` — outright tickers and settlement dates (combos filtered client-side)
- `/snapshot` — live prices
- `/aggs/{ticker}?resolution=1session` — daily settlement history

Daily bars begin **2021-09-02**, which caps seasonal overlays at roughly five crop years.
Massive carries no options on futures — only OPRA-listed equity/index options, which are
a separate entitlement.

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py --server.port 8512
```

Create `.streamlit/secrets.toml` from the example and add your key:

```toml
MASSIVE_API_KEY = "your-key-here"
```

## Deploying to Streamlit Cloud

Point the app at `app.py`, then add `MASSIVE_API_KEY` under **Settings -> Secrets**.
`.streamlit/secrets.toml` is gitignored and never leaves the machine.

`plotly` and `kaleido` are pinned deliberately: newer combinations have silently broken
PNG export on Streamlit Cloud. Charts also expose Plotly's built-in camera button, which
is client-side and works regardless.

## Notes

- First Notice Day is computed from the CME grain rule (last business day before the
  delivery month) — the API exposes no FND field. Weekend-aware, not holiday-aware.
- Spreads are calculated arithmetically and may deviate from quoted board spreads.
- Prior-year seasonal analogs are built by rolling the contract year back and aligning
  each year on its near leg's expiration.
