---
title: Cleaning and transforming data with Pandas
section: ai-stack
order: 10
phase: "Phase 2 · Pandas"
summary: Missing values, type fixes, exact money handling, strings, dates and time zones, dedup, method chaining and vectorized rules on messy finance data.
---

Raw finance extracts are messy: amounts with currency symbols and parentheses for negatives, vendor names typed five ways, timestamps in UTC that belong to a different month in local time, the same invoice loaded twice. After this note you can turn that into a clean, typed, deduplicated frame with a readable pipeline, and you can explain why money needs more care than `float64`. In the target role this is the step that decides whether an LLM answer about "January spend" is right: the model only sees what your cleaning produced.

## Prerequisites

[Pandas fundamentals]({{ '/ai-stack/pandas-fundamentals/' | relative_url }}): dtypes, `loc`, copy-on-write, and reading files.

## The shape of a cleaning pipeline

```text
 raw extract
     |
     v
 normalize columns ---> parse types ---> handle missing values
                                              |
                                              v
 validate <--- derive fields <--- dedupe <--- normalize text and dates
     |
     v
 clean frame (typed, one row per business event)
```

Each box is a small function from DataFrame to DataFrame. You chain them with `pipe`, test them one by one, and log what each one dropped or changed.

## Missing values

Pandas has several missing markers: `NaN` for floats and the default `str` dtype, `NaT` for datetimes, and `pd.NA` for nullable and Arrow dtypes. `isna()` detects all of them, so use it instead of `== None` or `== np.nan` (which is always `False`).

The finance rule: **a missing amount is not zero**. Filling it with 0 makes totals look complete when they are not. Fill only where the business has a real default, and route the rest to review.

```python
import numpy as np
import pandas as pd

invoices = pd.DataFrame(
    {
        "invoice_id": ["INV-1", "INV-2", "INV-3", "INV-4", "INV-5"],
        "vendor_id": ["V001", "V002", None, "V001", "V003"],
        "amount": [1200.0, np.nan, 310.0, 95.5, 640.0],
        "tax": [216.0, 0.0, np.nan, 17.19, np.nan],
        "cost_center": ["CC10", None, "CC20", None, "CC30"],
    }
)

cleaned = (
    invoices
    .dropna(subset=["vendor_id"])                        # unpostable without a vendor
    .fillna({"tax": 0.0, "cost_center": "UNASSIGNED"})   # explicit per-column defaults
)
needs_review = cleaned[cleaned["amount"].isna()]         # do not invent money

# Time series gaps: carry the last known FX rate forward over weekends
fx = pd.DataFrame(
    {"usd_inr": [86.10, np.nan, np.nan, 86.45]},
    index=pd.date_range("2026-01-30", periods=4, freq="D"),
)
fx["usd_inr"] = fx["usd_inr"].ffill()

if __name__ == "__main__":
    print(invoices.isna().sum())
    assert "INV-3" not in cleaned["invoice_id"].values
    assert needs_review["invoice_id"].tolist() == ["INV-2"]
    assert cleaned["cost_center"].notna().all()
    assert fx["usd_inr"].tolist() == [86.10, 86.10, 86.10, 86.45]
```

`fillna(method="ffill")` was removed in pandas 3; use `ffill()` and `bfill()`. When the gaps belong to groups (one FX series per currency), use `df.groupby("currency")["rate"].ffill()` so a value never leaks from one currency into another.

## Parsing types

Amounts from spreadsheets and bank portals arrive as text: `"$1,234.50"`, `"(500.00)"` for a negative, blanks, and the occasional `"1.2k"`. Normalize the text, then convert with `errors="coerce"` and count what failed, so bad values become visible instead of crashing the job or disappearing.

```python
import pandas as pd

raw = pd.DataFrame(
    {
        "amount": ["$1,234.50", "(500.00)", "1.2k", "", " 75 "],
        "txn_date": ["31/01/2026", "01/02/2026", "02/02/2026", "bad", "03/02/2026"],
    }
)

text = (
    raw["amount"]
    .str.strip()
    .str.replace(r"[$,]", "", regex=True)
    .str.replace(r"^\((.*)\)$", r"-\1", regex=True)      # (500.00) -> -500.00
)
amount = pd.to_numeric(text, errors="coerce")
unparsed = raw.loc[amount.isna() & text.ne(""), "amount"]

txn_date = pd.to_datetime(raw["txn_date"], format="%d/%m/%Y", errors="coerce")

if __name__ == "__main__":
    assert amount.tolist()[:2] == [1234.5, -500.0] and amount.iloc[4] == 75.0
    assert unparsed.tolist() == ["1.2k"]                 # report these, do not drop silently
    assert txn_date.iloc[1] == pd.Timestamp("2026-02-01")  # day first, not Jan 2
    assert txn_date.isna().sum() == 1
```

Always pass `format=` for dates. Without it, `01/02/2026` is ambiguous, and the parser guesses from the first value. `convert_dtypes()` is the other useful tool: it moves a whole frame to nullable dtypes (`Int64`, `Float64`, `string`, `boolean`) in one call.

## Money: Decimal vs float

Binary floats cannot represent most decimal fractions exactly. `0.1 + 0.2` is `0.30000000000000004`, and `2.675` is stored as `2.67499999...`. Summing millions of floats drifts by fractions of a cent, and rounding disagrees depending on who does it:

| Value | Python `round(x, 2)` | pandas `.round(2)` | `Decimal(str)` half-up |
|---|---|---|---|
| `2.675` | 2.67 | 2.68 | 2.68 |
| `1.005` | 1.0 | 1.0 | 1.01 |
| `0.125` | 0.12 | 0.12 | 0.13 |

Pandas `.round()` uses round-half-to-even (banker's rounding) for floats, `Decimal` objects and Arrow decimals alike. Most invoicing and tax policies expect half-up. If a reconciliation is off by one paisa on some rows, this is usually why.

Three workable representations:

| Representation | Exact | Vectorized | Use when |
|---|---|---|---|
| Integer minor units (`Int64` paise or cents) | Yes | Yes | Ledgers, reconciliation, sums |
| `Decimal` in an `object` column | Yes | No (Python loop) | Small frames, tax calculations |
| Arrow `decimal128(18, 2)` | Yes | Yes | Large frames, Parquet round trips |

```python
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

CENT = Decimal("0.01")

lines = pd.DataFrame({"net": ["2.675", "1.005", "0.125"], "gst_rate": ["0.18", "0.18", "0.05"]})

# 1. Decimal objects: parse from strings, round with an explicit policy
net = lines["net"].map(Decimal)
gst = (net * lines["gst_rate"].map(Decimal)).map(lambda d: d.quantize(CENT, ROUND_HALF_UP))
net_rounded = net.map(lambda d: d.quantize(CENT, ROUND_HALF_UP))

# 2. Arrow decimal: vectorized; choose the rounding mode via pyarrow.compute
dec = lines["net"].astype(pd.ArrowDtype(pa.decimal128(18, 3)))
half_even = dec.round(2)                                     # pandas default
half_up = pd.Series(pc.round(pa.array(dec), ndigits=2, round_mode="half_up"), dtype=dec.dtype)

# 3. Integer minor units: allocate a total without losing a paisa
def allocate(total_paise: int, weights: list[float]) -> list[int]:
    """Split a total by weights; leftover paise go to the largest remainders."""
    raw = [total_paise * w / sum(weights) for w in weights]
    shares = [int(x) for x in raw]
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i] - shares[i], reverse=True)
    for i in by_remainder[: total_paise - sum(shares)]:
        shares[i] += 1
    return shares

if __name__ == "__main__":
    assert 0.1 + 0.2 != 0.3
    assert pd.Series([2.675, 1.005]).round(2).tolist() == [2.68, 1.0]
    assert net_rounded.tolist() == [Decimal("2.68"), Decimal("1.01"), Decimal("0.13")]
    assert gst.tolist() == [Decimal("0.48"), Decimal("0.18"), Decimal("0.01")]
    assert half_even.tolist() == [Decimal("2.680"), Decimal("1.000"), Decimal("0.120")]
    assert half_up.tolist() == [Decimal("2.680"), Decimal("1.010"), Decimal("0.130")]
    rent = allocate(100_000, [1, 1, 1])                       # Rs 1,000.00 over 3 cost centers
    assert sum(rent) == 100_000 and sorted(rent) == [33_333, 33_333, 33_334]
```

> **Rule of thumb:** Parse money from strings straight into `Decimal` or integer minor units. Converting through `float` first bakes the error in before you can round it away.

## String operations

The `.str` accessor runs vectorized string methods over a column. With the pandas 3 `str` dtype these run on Arrow and return `str` or `bool` columns; missing values stay missing, and boolean methods like `contains` return `False` for them.

```python
import pandas as pd

vendors = pd.DataFrame(
    {
        "vendor_name": ["  Acme Corp Pvt. Ltd.", "ACME CORP PVT LTD", "Globex, Inc.", None],
        "memo": ["Payment INV-0042 Jan", "inv-0042 duplicate?", "Retainer", "Refund INV-0107"],
    }
)

SUFFIXES = r"\b(pvt|private|ltd|limited|inc|llc|corp)\b"
vendors["name_key"] = (
    vendors["vendor_name"]
    .str.lower()
    .str.replace(r"[^\w\s]", " ", regex=True)    # drop punctuation
    .str.replace(SUFFIXES, " ", regex=True)      # drop legal suffixes
    .str.split()
    .str.join(" ")                               # collapse whitespace
)
vendors["invoice_ref"] = vendors["memo"].str.upper().str.extract(r"(INV-\d{4})", expand=False)
vendors["mentions_refund"] = vendors["memo"].str.contains("refund", case=False)

if __name__ == "__main__":
    assert vendors["name_key"].iloc[0] == vendors["name_key"].iloc[1] == "acme"
    assert vendors["invoice_ref"].tolist()[:2] == ["INV-0042", "INV-0042"]
    assert pd.isna(vendors["invoice_ref"].iloc[2])
    assert vendors["mentions_refund"].tolist() == [False, False, False, True]
```

Normalized keys like `name_key` are what you join and dedupe on. Keep the original column for display and audit.

## Dates and time zones

Store timestamps in UTC, convert to the entity's local zone for anything period-based. A card transaction at `2026-01-31 20:00 UTC` is on 1 February in India, so it belongs to the February close for an Indian entity. Indian fiscal years run April to March, which `Q-MAR` periods model directly.

```python
import pandas as pd

txns = pd.DataFrame(
    {
        "txn_id": ["T1", "T2", "T3"],
        "ts": ["2026-01-31T17:00:00Z", "2026-01-31T20:00:00Z", "2026-04-01T04:00:00Z"],
        "due": ["2026-01-15", "2026-02-20", "2026-05-01"],
    }
)

txns["ts"] = pd.to_datetime(txns["ts"], utc=True)                  # datetime64[us, UTC]
local = txns["ts"].dt.tz_convert("Asia/Kolkata")
txns["period"] = local.dt.tz_localize(None).dt.to_period("M")     # booking month, IST
txns["fiscal_q"] = local.dt.tz_localize(None).dt.to_period("Q-MAR")
txns["days_past_due"] = (pd.Timestamp("2026-02-28") - pd.to_datetime(txns["due"])).dt.days

if __name__ == "__main__":
    assert txns["period"].astype(str).tolist() == ["2026-01", "2026-02", "2026-04"]
    assert str(txns["fiscal_q"].iloc[0]) == "2026Q4"               # FY ending Mar 2026
    assert str(txns["fiscal_q"].iloc[2]) == "2027Q1"               # new fiscal year
    assert txns["days_past_due"].tolist() == [44, 8, -62]
```

Pandas 3 infers datetime resolution, so parsed strings are usually `datetime64[us]` rather than `[ns]`. That only matters if you compare raw `int64` values or dtypes across systems. Drop the time zone (`tz_localize(None)`) only after converting to the zone you want, otherwise you keep UTC wall-clock times.

## Duplicates

Two kinds matter in finance. **Exact duplicates** come from loading the same file twice. **Business-key duplicates** are the same invoice entered twice with small differences, which is how duplicate payments happen.

```python
import pandas as pd

ap = pd.DataFrame(
    {
        "row_id": [1, 2, 3, 4, 5],
        "vendor_id": ["V001", "V001", "V002", "V001", "V002"],
        "invoice_no": ["INV-0042", "inv 0042", "G-17", "INV-0043", "G-17"],
        "amount": [1180.00, 1180.00, 500.00, 90.00, 500.00],
    }
)

ap["invoice_key"] = ap["invoice_no"].str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
key = ["vendor_id", "invoice_key", "amount"]

suspects = ap[ap.duplicated(subset=key, keep=False)].sort_values(key)   # every copy
deduped = ap.drop_duplicates(subset=key, keep="first")

if __name__ == "__main__":
    assert suspects["row_id"].tolist() == [1, 2, 3, 5]
    assert deduped["row_id"].tolist() == [1, 3, 4]
```

Use `keep=False` to review all copies, `keep="first"` to remove them once you trust the key. Never dedupe on `amount` alone: two genuine invoices for the same amount are common.

## Method chaining, assign and pipe

Chaining keeps a transformation readable top to bottom and avoids half-modified intermediate frames. `assign` adds columns and returns a new frame; `pipe` lets you insert your own functions into the chain. Pandas 3 adds `pd.col("name")`, an expression that refers to a column of the frame being built, so you no longer need `lambda df: df["name"]` in `assign`.

This is also where **vectorized rules** replace `apply`. `df.apply(f, axis=1)` calls a Python function once per row; `np.select`, `Series.map`, `where` and `pd.cut` do the same logic over whole columns in compiled code. The [performance note]({{ '/ai-stack/pandas-performance/' | relative_url }}) measures the difference; expect 10 to 100 times.

Categoricals fit naturally here. An ordered categorical for aging buckets sorts in business order (not alphabetically) and stores each label once. Since pandas 3, `groupby` on a categorical defaults to `observed=True`, so empty buckets are left out unless you pass `observed=False`.

```python
import numpy as np
import pandas as pd

raw = pd.DataFrame(
    {
        " Invoice No ": ["INV-1", "INV-2", "INV-3", "INV-4", "INV-4"],
        "Vendor": ["V001", "V002", "V001", "V003", "V003"],
        "Amount": ["1,200.00", "85.50", "(40.00)", "9,800.00", "9,800.00"],
        "Due Date": ["2026-01-10", "2026-02-25", "2026-01-20", "2025-11-15", "2025-11-15"],
    }
)

AS_OF = pd.Timestamp("2026-02-28")
AGING = pd.CategoricalDtype(["current", "1-30", "31-90", "90+"], ordered=True)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns=lambda c: c.strip().lower().replace(" ", "_"))


def parse_amount(s: pd.Series) -> pd.Series:
    s = s.str.replace(",", "").str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(s, errors="raise")


def clean_invoices(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.pipe(normalize_columns)
        .drop_duplicates()
        .assign(
            amount=lambda d: parse_amount(d["amount"]),
            due_date=lambda d: pd.to_datetime(d["due_date"], format="%Y-%m-%d"),
            vendor=pd.col("vendor").str.upper(),
        )
        .assign(
            days_late=lambda d: (AS_OF - d["due_date"]).dt.days.clip(lower=0),
            kind=lambda d: np.select(
                [d["amount"] < 0, d["amount"] >= 5000], ["credit_note", "large"], "standard"
            ),
        )
        .assign(
            aging=lambda d: pd.cut(d["days_late"], [-1, 0, 30, 90, np.inf], labels=AGING.categories)
            .astype(AGING)
        )
        .sort_values(["aging", "amount"], ascending=[False, False])
        .reset_index(drop=True)
    )


clean = clean_invoices(raw)

# The row-wise equivalent of `kind`, for comparison: correct but a Python loop
slow_kind = clean.apply(
    lambda r: "credit_note" if r["amount"] < 0 else ("large" if r["amount"] >= 5000 else "standard"),
    axis=1,
)

if __name__ == "__main__":
    print(clean)
    assert len(clean) == 4 and clean["invoice_no"].iloc[0] == "INV-4"
    assert clean["aging"].iloc[0] == "90+" and clean["aging"].dtype == AGING
    assert (slow_kind == clean["kind"]).all()
    by_bucket = clean.groupby("aging")["amount"].sum()            # observed=True: pandas 3 default
    assert by_bucket.index.tolist() == ["1-30", "31-90", "90+"]
    all_buckets = clean.groupby("aging", observed=False)["amount"].sum()
    assert all_buckets.index.tolist() == ["current", "1-30", "31-90", "90+"]
    assert raw.shape == (5, 4)                       # the input was never modified
```

Each `assign` sees the columns created by the previous one, which is why the chain is split into three. Under copy-on-write the chain does not copy data at every step; pandas copies only the columns it actually changes.

## Coming from Django

| Django | pandas cleaning |
|---|---|
| `Form.clean_<field>()` | One `parse_*` function per column, used in `assign` |
| `full_clean()` and model validators | A validation step at the end of the chain (pandera, covered in [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }})) |
| `DecimalField(max_digits=18, decimal_places=2)` | `Decimal`, integer paise, or `ArrowDtype(pa.decimal128(18, 2))` |
| `USE_TZ = True`, store UTC, render local | Same rule: `utc=True` on parse, `tz_convert` for periods |
| `unique_together` | `duplicated(subset=[...])` check |
| `TextChoices` | Ordered `CategoricalDtype` |

## Common mistakes

- **`fillna(0)` on amounts**: hides missing data in totals. Fill only with a real business default; send the rest to review.
- **`pd.to_datetime` without `format=`**: day and month swap on ambiguous dates. Pass the format and `errors="coerce"`, then count the `NaT`s.
- **Money through float**: `float("2.675")` is already wrong. Parse to `Decimal` or integer paise, and round with an explicit mode.
- **Assuming `.round(2)` is half-up**: it is half-to-even. Use `Decimal.quantize(..., ROUND_HALF_UP)` or `pyarrow.compute.round(..., round_mode="half_up")`.
- **Period from UTC timestamps**: month-end transactions land in the wrong month. Convert to local time first.
- **`apply(axis=1)` for simple rules**: use `np.select`, `map` or `where`.
- **Deduping on too few columns**: deleting genuine invoices that share an amount. Build a normalized business key.

## Practice tasks

1. Write `parse_amount` that handles `$`, `₹`, commas, parentheses negatives and trailing minus signs (`500.00-`). Done: a list of 10 tricky inputs maps to expected values, and unparseable ones are returned separately.
2. Given UTC timestamps for card transactions, assign each one to an IST booking month and an Indian fiscal quarter. Done: transactions in the last 5.5 hours of a UTC month land in the next month.
3. Allocate a shared cloud bill of Rs 1,00,000.01 across five cost centers by headcount. Done: shares sum exactly to the bill and no share differs from its exact value by more than one paisa.
4. Build a duplicate-payment detector: normalized vendor name and invoice number, same amount, within 30 days. Done: it flags planted duplicates and no false positives on a 1,000-row synthetic set.
5. Rewrite a cleaning script you have that uses intermediate variables and `inplace=True` into one `clean_*` function with `pipe` and `assign`. Done: same output, asserted with `pd.testing.assert_frame_equal`.

## Interview questions

**Why not use float for money?**
Most decimal fractions have no exact binary representation, so sums drift and rounding at half-cents is unpredictable. Use `Decimal`, integer minor units, or an Arrow decimal type, and round with an explicit mode.

**What rounding does pandas `.round()` use?**
Round half to even. For half-up, use `Decimal.quantize` with `ROUND_HALF_UP` or `pyarrow.compute.round` with `round_mode="half_up"`.

**How do you handle missing values in a ledger amount column?**
Count them, never fill them with zero by default, route the rows to review, and only fill fields that have a documented default (tax 0 for exempt lines, "UNASSIGNED" cost center).

**`NaN`, `None`, `pd.NA` and `NaT`: how do you test for missing?**
Always `isna()` / `notna()`. `NaN != NaN`, so equality checks fail.

**Why is `apply(axis=1)` slow and what replaces it?**
It calls a Python function per row with a Series built for each row. Use vectorized column operations: arithmetic, `np.select`, `np.where`, `map`, `.str` and `.dt` methods, `pd.cut`.

**What does `pipe` give you over intermediate variables?**
A readable top-to-bottom pipeline of testable functions, with no partially modified frames left around.

**How do you choose the accounting period of a timestamp?**
Store in UTC, convert to the legal entity's time zone, then take the period (`to_period("M")` or a fiscal frequency like `Q-MAR`).

**When would you use a categorical dtype?**
Low-cardinality text such as status, currency or aging bucket: it saves memory, speeds up groupby, and ordered categoricals sort in business order.

## Further reading

- pandas user guide: "Working with missing data"
- pandas user guide: "Working with text data"
- pandas user guide: "Time series / date functionality" (time zone handling)
- pandas user guide: "Categorical data"
- Python docs: `decimal` module
- PyArrow docs: "Compute functions" (`round` and rounding modes)
