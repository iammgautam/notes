---
title: Pandas fundamentals
section: ai-stack
order: 9
phase: "Phase 2 · Pandas"
summary: Series, DataFrame and Index, pandas 3 dtypes and copy-on-write, loc/iloc selection, and loading finance data from CSV, JSON, SQL, Excel and Parquet.
---

After this note you can load a finance extract from CSV, JSON, SQL, Excel or Parquet into a DataFrame with the right dtypes, select exactly the rows and columns you want without tripping over copy-on-write, and size up an unfamiliar dataset in a few minutes. In the target role this is the first step of almost every task: a GL export, an invoice dump or a bank statement lands in pandas before it becomes a report, a tool result for an agent, or chunks in a RAG index.

All code targets **pandas 3.1** with pyarrow installed. Pandas 3 changed defaults that older tutorials still teach, so this note calls those out as it goes.

## Prerequisites

Comfortable Python (dicts, comprehensions, functions). The FastAPI phase, ending with [FastAPI testing and production]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}), is useful context but not required.

## What changed in pandas 3

| Area | pandas 2.x | pandas 3.x |
|---|---|---|
| Copy semantics | Views sometimes, `SettingWithCopyWarning` | Copy-on-write always; every derived object behaves as a copy |
| Chained assignment | Sometimes worked | Never works; emits `ChainedAssignmentError` |
| Text columns | `object` dtype | `str` dtype (Arrow-backed when pyarrow is installed) |
| Parsed datetimes | Always `datetime64[ns]` | Resolution inferred, usually `datetime64[us]` |
| Offsets | `"M"`, `"Q"`, `"H"` | `"ME"`, `"QE"`, `"h"` (old aliases raise) |
| Removed | `applymap`, `fillna(method=...)` | Use `DataFrame.map`, `ffill()`/`bfill()` |
| New | | `pd.col("amount")` expressions in `assign` and `loc` |

## The mental model: labeled columns

A `DataFrame` is a dict of columns that share one row index. Each column is a `Series`: a typed array plus that index. Operations run on whole columns in compiled code and **align on index labels**, not positions.

```text
              Index        Series      Series     Series
            (labels)      vendor_id    amount     status
           +----------+------------+----------+----------+
           | INV-1001 |   V001     |  1250.00 |  paid    |
           | INV-1002 |   V002     |   980.50 |  open    |
           | INV-1003 |   V001     |   310.25 |  open    |
           +----------+------------+----------+----------+
              one dtype per column, aligned by label
```

Alignment is the feature that surprises people. When two Series meet, pandas matches labels and fills the gaps with missing values:

```python
import pandas as pd

invoices = pd.DataFrame(
    {
        "invoice_id": ["INV-1001", "INV-1002", "INV-1003", "INV-1004"],
        "vendor_id": ["V001", "V002", "V001", "V003"],
        "amount": [1250.00, 980.50, 310.25, 4500.00],
        "status": ["paid", "open", "open", "disputed"],
    }
).set_index("invoice_id")

amount = invoices["amount"]  # Series: values + the same index
credits = pd.Series({"INV-1002": 80.50, "INV-1004": 500.00, "INV-9999": 10.00})

net = amount - credits                      # aligned on labels
net_filled = amount.sub(credits, fill_value=0)

if __name__ == "__main__":
    print(net)
    assert net["INV-1002"] == 900.0
    assert pd.isna(net["INV-1001"])          # no credit -> NaN, not 1250
    assert "INV-9999" in net.index           # result index is the union
    assert net_filled["INV-1001"] == 1250.0  # fill_value treats missing as 0
```

The index is not just a row number. Use a meaningful one (`invoice_id`, a date) when you look rows up by it or align on it, and `reset_index()` to turn it back into a column before exporting or merging on it.

## Dtypes

Every column has one dtype, and the dtype decides memory use, speed and how missing values behave. There are three families:

| Family | Examples | Missing value | Use for |
|---|---|---|---|
| NumPy | `int64`, `float64`, `bool`, `datetime64[us]` | `NaN` / `NaT` (no missing ints) | Dense numeric data |
| pandas extension (nullable) | `Int64`, `Float64`, `boolean`, `string`, `category` | `pd.NA` | Integers or flags with gaps |
| Arrow-backed | `int64[pyarrow]`, `string[pyarrow]`, `ArrowDtype(pa.decimal128(18, 2))` | `pd.NA` | Interop, decimals, memory |

The default text dtype in pandas 3 is `str`. It is backed by Arrow when pyarrow is installed and uses `NaN` for missing values, so it behaves like the old `object` columns in comparisons but is faster and stricter: you cannot put an `int` into a `str` column. The older opt-in `"string"` dtype still exists and uses `pd.NA`.

```python
import pandas as pd
import pyarrow as pa

qty = pd.Series([10, None, 3])                      # NaN forces float64
qty_nullable = pd.Series([10, None, 3], dtype="Int64")

names = pd.Series(["Acme Corp", None, "Globex"])    # pandas 3 default: str
names_na = names.astype("string")                   # opt-in, pd.NA semantics

money = pd.Series(["1250.00", "980.50"]).astype(
    pd.ArrowDtype(pa.decimal128(18, 2))             # exact decimal, Arrow-backed
)

if __name__ == "__main__":
    assert qty.dtype == "float64"
    assert str(qty_nullable.dtype) == "Int64" and qty_nullable.sum() == 13
    assert names.dtype == "str" and pd.isna(names.iloc[1])
    assert names_na.iloc[1] is pd.NA
    try:
        names.iloc[0] = 42                          # str columns reject ints
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")
    print(repr(money.sum()))                        # Decimal('2230.50')
```

> **Gotcha:** An integer column with one missing value silently becomes `float64`. Account numbers, quantities and IDs then print as `4000.0`, and large IDs lose precision. Read IDs as `str` and counts as `Int64`.

## Copy-on-write and chained assignment

Pandas 3 makes copy-on-write (CoW) permanent. The rule is simple: **any object you derive from another (a column, a filtered frame, a slice) behaves as an independent copy**. Pandas shares memory under the hood and copies only when one side is modified, so it stays cheap.

The consequence is that **chained assignment never works**. `df[mask]["col"] = value` is two operations: `df[mask]` produces a new object, and the assignment modifies that temporary, which is then thrown away. Pandas 2 sometimes modified the original and sometimes did not, which is what `SettingWithCopyWarning` was about. Pandas 3 removes the ambiguity: it never modifies the original and emits `ChainedAssignmentError` to tell you so.

```python
import warnings

import pandas as pd

gl = pd.DataFrame(
    {
        "account": ["4000", "5000", "5000"],
        "amount": [1200.0, -300.0, None],
        "approved": [True, False, False],
    }
)

# 1. Chained assignment: modifies a temporary, original unchanged
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    gl[gl["account"] == "5000"]["approved"] = True
assert not gl["approved"].all()
assert any(issubclass(w.category, pd.errors.ChainedAssignmentError) for w in caught)

# 2. One step on the original with .loc: works
gl.loc[gl["account"] == "5000", "approved"] = True

# 3. Same trap with inplace methods on a column; assign the result instead
gl["amount"] = gl["amount"].fillna(0.0)

# 4. A filtered frame is its own object; changing it never touches gl
expenses = gl[gl["amount"] < 0]
expenses["amount"] = expenses["amount"].abs()

if __name__ == "__main__":
    assert gl["approved"].all()
    assert gl["amount"].notna().all()
    assert (gl.loc[gl["amount"] < 0, "amount"] < 0).all()  # parent untouched
    assert (expenses["amount"] > 0).all()
```

Two habits follow. Write to the frame you own with a single `.loc[rows, cols] = value`. And stop sprinkling `.copy()` defensively; under CoW it only wastes memory.

## Selecting rows and columns

| Accessor | Selects by | Slice end | Example |
|---|---|---|---|
| `df["col"]`, `df[["a", "b"]]` | Column names | n/a | `invoices["amount"]` |
| `df.loc[rows, cols]` | Labels or boolean masks | Inclusive | `invoices.loc["INV-1001":"INV-1003", "amount"]` |
| `df.iloc[rows, cols]` | Integer positions | Exclusive | `invoices.iloc[:2, 2]` |
| `df.at[row, col]` | One label pair | n/a | Fast scalar get/set |

Boolean masks are Series of `True`/`False` aligned to the index. Combine them with `&`, `|` and `~`, and wrap each comparison in parentheses because those operators bind tighter than `==`.

```python
import pandas as pd

invoices = pd.DataFrame(
    {
        "invoice_id": ["INV-1001", "INV-1002", "INV-1003", "INV-1004"],
        "vendor_id": ["V001", "V002", "V001", "V003"],
        "amount": [1250.00, 980.50, 310.25, 4500.00],
        "status": ["paid", "open", "open", "disputed"],
        "due_date": pd.to_datetime(["2026-01-15", "2026-02-01", "2026-02-10", "2026-01-31"]),
    }
).set_index("invoice_id")

row = invoices.loc["INV-1002"]                              # Series
window = invoices.loc["INV-1001":"INV-1003", ["vendor_id", "amount"]]
first_two_status = invoices.iloc[:2, 2]                    # position 2 = status

as_of = pd.Timestamp("2026-02-05")
overdue = (invoices["status"] != "paid") & (invoices["due_date"] < as_of)
watchlist = invoices["vendor_id"].isin(["V001", "V003"]) & invoices["amount"].between(300, 5000)

# Write through .loc on the original frame
invoices.loc[overdue, "status"] = "overdue"

if __name__ == "__main__":
    assert row["amount"] == 980.50
    assert len(window) == 3                                 # label slice includes the end
    assert first_two_status.tolist() == ["paid", "open"]
    assert invoices.index[overdue].tolist() == ["INV-1002", "INV-1004"]
    assert invoices.loc[watchlist].index.tolist() == ["INV-1001", "INV-1003", "INV-1004"]
    assert invoices.at["INV-1004", "status"] == "overdue"
```

## Reading data

Finance data arrives in every format. The parsing options matter more than the function name: decide dtypes at read time so account codes keep their leading zeros and amounts become numbers.

### CSV, JSON, SQL and Parquet

```python
import io
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

CSV = """txn_id,posted_on,account,vendor_code,amount,memo
T-001,2026-01-03,4000,00042,"1,250.00",Consulting Jan
T-002,2026-01-05,5000,00107,-310.25,
T-003,2026-01-05,5000,00042,N/A,Travel reimbursement
"""

txns = pd.read_csv(
    io.StringIO(CSV),
    dtype={"account": "str", "vendor_code": "str"},  # keep leading zeros
    parse_dates=["posted_on"],
    thousands=",",
    na_values=["N/A"],
)

# Nested JSON from an AP system: flatten objects, explode line items
payload = [
    {"id": "INV-1", "vendor": {"id": "V001", "name": "Acme"}, "total": 100.0,
     "lines": [{"gl": "5000", "amt": 60.0}, {"gl": "5100", "amt": 40.0}]},
    {"id": "INV-2", "vendor": {"id": "V002", "name": "Globex"}, "total": 75.0,
     "lines": [{"gl": "5000", "amt": 75.0}]},
]
headers = pd.json_normalize(payload).drop(columns="lines")   # vendor.id, vendor.name
lines = pd.json_normalize(payload, record_path="lines", meta=["id", ["vendor", "id"]])
from_text = pd.read_json(io.StringIO('[{"id": "INV-3", "total": 12.5}]'))

# SQL: sqlite3 works directly; use a SQLAlchemy engine for Postgres
con = sqlite3.connect(":memory:")
con.executescript("""
    CREATE TABLE gl_entries (entry_id TEXT, account TEXT, posted_on TEXT, amount REAL);
    INSERT INTO gl_entries VALUES
        ('JE-1', '4000', '2026-01-31', -5000.0),
        ('JE-1', '1100', '2026-01-31',  5000.0),
        ('JE-2', '5000', '2026-02-02',   420.0),
        ('JE-2', '2000', '2026-02-02',  -420.0);
""")
jan = pd.read_sql(
    "SELECT * FROM gl_entries WHERE posted_on < ? ORDER BY entry_id",
    con,
    params=("2026-02-01",),
    parse_dates=["posted_on"],
)
by_account = pd.read_sql(  # aggregate in the database, ship less data
    "SELECT account, SUM(amount) AS net FROM gl_entries GROUP BY account", con
)

# Parquet keeps dtypes and lets you read only the columns you need
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "txns.parquet"
    txns.to_parquet(path, index=False)
    back = pd.read_parquet(path)
    slim = pd.read_parquet(path, columns=["txn_id", "amount"])

if __name__ == "__main__":
    assert txns["vendor_code"].tolist() == ["00042", "00107", "00042"]
    assert txns["amount"].iloc[0] == 1250.0 and pd.isna(txns["amount"].iloc[2])
    assert str(txns["posted_on"].dtype).startswith("datetime64")
    assert lines.shape == (3, 4) and "vendor.name" in headers.columns
    assert from_text.loc[0, "total"] == 12.5
    assert len(jan) == 2 and jan["posted_on"].dt.month.eq(1).all()
    assert by_account["net"].sum() == 0
    pd.testing.assert_frame_equal(back, txns)
    assert list(slim.columns) == ["txn_id", "amount"]
```

Notes on each reader:

- **`read_csv`**: always pass `dtype=` for identifier columns. `usecols=` cuts memory, `na_values=` handles "N/A" and "-", `thousands=` handles `1,250.00`. You can add `dtype_backend="pyarrow"` to get Arrow dtypes for every column.
- **`read_json`**: pass a file path or a `StringIO`; literal JSON strings are no longer accepted. For nested payloads, `json_normalize` with `record_path` and `meta` is the tool.
- **`read_sql`**: pandas accepts a SQLAlchemy connectable, an ADBC connection or a `sqlite3` connection. Always use `params=`, never string formatting (the SQL injection rules from Django still apply). Push filters and aggregates into SQL when the database is the source.
- **`read_parquet`**: typed, compressed and columnar. Prefer it for anything you save and reload.

### Excel (illustrative)

`read_excel` needs an engine such as `openpyxl` installed. Finance workbooks have title rows, multiple sheets and merged headers, so the options do the work:

```python
# illustrative: requires `pip install openpyxl` and a real workbook
import pandas as pd

tb = pd.read_excel(
    "trial_balance_2026_01.xlsx",
    sheet_name="TB",            # or sheet_name=None for a dict of all sheets
    skiprows=4,                 # skip the report title block
    usecols="A:D",
    dtype={"Account": "str"},
)
```

## Inspecting a new dataset

Before transforming anything, answer: how big, which types, how many gaps, which values. This helper gives a one-table profile you can paste into a ticket or a prompt:

```python
import pandas as pd


def profile(df: pd.DataFrame) -> pd.DataFrame:
    """One row per column: dtype, nulls, distinct values and an example."""
    return pd.DataFrame(
        {
            "dtype": df.dtypes.astype(str),
            "nulls": df.isna().sum(),
            "null_pct": (df.isna().mean() * 100).round(1),
            "distinct": df.nunique(),
            "example": [df[c].dropna().iloc[0] if df[c].notna().any() else None for c in df],
        }
    )


vendors = pd.DataFrame(
    {
        "vendor_id": ["V001", "V002", "V003", "V004"],
        "name": ["Acme Corp", "Globex", None, "Initech"],
        "country": ["IN", "IN", "US", "IN"],
        "payment_terms_days": pd.array([30, 45, None, 30], dtype="Int64"),
    }
)

report = profile(vendors)

if __name__ == "__main__":
    print(vendors.shape)
    vendors.info(memory_usage="deep")
    print(vendors.describe(include="all").T)
    print(vendors["country"].value_counts(dropna=False))
    print(report)
    assert report.loc["name", "nulls"] == 1
    assert report.loc["country", "distinct"] == 2
    assert report.loc["payment_terms_days", "dtype"] == "Int64"
```

The quick checklist: `shape`, `info(memory_usage="deep")`, `head()` and `sample(5)`, `describe(include="all")`, `isna().sum()`, `value_counts(dropna=False)` on every categorical-looking column, and `duplicated(subset=key).sum()` on what should be a unique key.

## Coming from Django

| Django ORM | pandas | Note |
|---|---|---|
| `Invoice.objects.filter(status="open")` | `df[df["status"] == "open"]` or `df.query("status == 'open'")` | Eager, in memory |
| `.values("id", "amount")` | `df[["id", "amount"]]` | |
| `.annotate(net=F("amount") - F("credit"))` | `df.assign(net=df["amount"] - df["credit"])` | |
| `.aggregate(Sum("amount"))` | `df["amount"].sum()` | |
| `.order_by("-amount")` | `df.sort_values("amount", ascending=False)` | |
| `select_related("vendor")` | `df.merge(vendors, on="vendor_id")` | Covered in the merge note |
| Model field types | Column dtypes | Set at read time, not migration time |

The biggest shift: a QuerySet is lazy and the database does the work, while a DataFrame is eager and fully in memory. If the data lives in Postgres, filter and aggregate in SQL and load the result.

## Common mistakes

- **Chained assignment** (`df[mask]["col"] = x`, `df["col"].fillna(0, inplace=True)`): does nothing in pandas 3. Use `df.loc[mask, "col"] = x` and `df["col"] = df["col"].fillna(0)`.
- **IDs read as numbers**: vendor code `00042` becomes `42`. Pass `dtype={"vendor_code": "str"}`.
- **Comparing with `and`/`or`**: raises "truth value of a Series is ambiguous". Use `&`, `|`, `~` with parentheses.
- **Mixing `loc` and `iloc` semantics**: `loc` slices include the end label, `iloc` slices exclude the end position.
- **Checking for `object` dtype on text**: in pandas 3, text is `str`. Use `pd.api.types.is_string_dtype(s)` instead of `s.dtype == object`.
- **Loading a full table to filter it in pandas**: push `WHERE` and `GROUP BY` into `read_sql`.

## Practice tasks

1. Build a 10-row `invoices` frame with an `invoice_id` index. Select all open invoices over 1,000 using `loc` and a mask. Done: the mask is a named variable and the result has the expected IDs.
2. Write a CSV string with a vendor code that has leading zeros, an amount with thousands separators, and an "N/A" amount. Load it so all three parse correctly. Done: asserts on dtypes and values pass.
3. Create a SQLite table of GL entries, then use `read_sql` with `params=` to fetch one month and a second query that aggregates by account. Done: account totals match a pandas `groupby` on the raw rows.
4. Reproduce the chained assignment trap, capture the `ChainedAssignmentError` with `warnings.catch_warnings`, then fix it. Done: the original frame is updated only by the fixed version.
5. Run `profile()` on a real CSV you have (a bank statement export works). Write down three issues you would fix in the cleaning step.

## Interview questions

**What is the difference between a Series, a DataFrame and an Index?**
A Series is a typed 1-D array with an index. A DataFrame is a collection of Series sharing one index. The Index holds row labels and drives alignment, lookup and joins.

**What does copy-on-write change in pandas 3?**
Every derived object behaves as a copy, so modifying it never changes its parent. Memory is shared until a write happens. Chained assignment no longer works, and `SettingWithCopyWarning` is gone.

**Why does `df[df.x > 0]["y"] = 1` not update `df`?**
The first indexing step returns a new object; the assignment modifies that temporary. Use `df.loc[df.x > 0, "y"] = 1`.

**Why did my integer column become float?**
NumPy `int64` cannot hold missing values, so pandas used `float64` with `NaN`. Use the nullable `Int64` dtype or fill the gaps.

**What is the default string dtype in pandas 3?**
`str`, a `StringDtype` backed by Arrow when pyarrow is installed, with `NaN` as the missing value. It rejects non-string values and is faster and smaller than `object`.

**`loc` vs `iloc`?**
`loc` uses labels and boolean masks, with inclusive slices. `iloc` uses integer positions, with exclusive slices.

**How do you load data from Postgres into pandas safely?**
`pd.read_sql` with a SQLAlchemy engine, parameters passed via `params=`, filters and aggregation done in SQL, and dtypes or `parse_dates` set explicitly.

**Why prefer Parquet over CSV for intermediate data?**
It stores dtypes, compresses well, and supports reading only selected columns, so reloads are faster and lossless.

## Further reading

- pandas user guide: "10 minutes to pandas"
- pandas user guide: "Copy-on-Write (CoW)"
- pandas "Migration guide for the new string data type"
- pandas "What's new in 3.0.0"
- pandas user guide: "Indexing and selecting data"
- pandas user guide: "IO tools (text, CSV, HDF5, ...)"
- pandas user guide: "PyArrow Functionality"
