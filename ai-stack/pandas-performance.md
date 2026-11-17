---
title: Pandas performance
section: ai-stack
order: 12
phase: "Phase 2 · Pandas"
summary: Vectorize instead of looping, cut memory with dtypes and Arrow, stream big files in chunks, profile, and know when to switch to Polars, DuckDB or Spark.
---

A pandas job that takes 40 minutes usually has one of three causes: a Python loop over rows, text columns stored as Python objects, or a dataset that should never have been loaded whole. After this note you can find which one with a profiler, fix it, and say when pandas is the wrong tool and which engine replaces it. In the target role this decides whether a tool call answers in 200 ms or times out, and whether a nightly GL job fits on the box.

## Prerequisites

[Groupby, merge and reshape]({{ '/ai-stack/pandas-groupby-merge-reshape/' | relative_url }}). Familiarity with the cost of N+1 queries in Django helps; the patterns rhyme.

## The mental model: who touches each value

Pandas is fast when compiled code (NumPy or Arrow) loops over a whole column. It is slow when Python touches each row, because every row becomes Python objects, attribute lookups and function calls.

```text
  per-row Python                         whole-column compiled code
  <-------------------------------------------------------------->
  iterrows    apply(axis=1)   itertuples /     np.where, .str,
  (Series     (Series per     zip over cols    groupby, merge,
  per row)    row, hidden)    (tuples)         Arrow compute
  slowest                                      fastest
```

Measured on a laptop with 50,000 invoice rows, computing tax with one branch per row (the runnable block below reproduces this):

| Approach | Time |
|---|---|
| `iterrows()` | ~580 ms |
| `apply(axis=1)` | ~160 ms |
| `zip` over columns | ~27 ms |
| `np.where` on columns | ~1 to 2 ms |

The gap grows with row count. At 5 million rows `iterrows` takes about a minute; the vectorized version stays well under a second.

## Measure before you fix

Use `time.perf_counter` or `timeit` for one operation (`%timeit` in a notebook), `cProfile` to find which function is slow, and `tracemalloc` or `memory_usage(deep=True)` for memory. Guessing is how people spend a day optimizing a read that takes 2% of the runtime.

```python
import cProfile
import io
import pstats
import time
import tracemalloc

import numpy as np
import pandas as pd

rng = np.random.default_rng(7)
N = 50_000
invoices = pd.DataFrame(
    {
        "amount": rng.uniform(10, 20_000, N).round(2),
        "category": rng.choice(["software", "travel", "consulting", "hardware"], N),
    }
)


def tax_iterrows(df):
    out = []
    for _, row in df.iterrows():                    # builds a Series per row
        rate = 0.05 if row["category"] == "travel" else 0.18
        out.append(row["amount"] * rate)
    return pd.Series(out, index=df.index)


def tax_apply(df):
    return df.apply(lambda r: r["amount"] * (0.05 if r["category"] == "travel" else 0.18), axis=1)


def tax_zip(df):
    return pd.Series(
        [a * (0.05 if c == "travel" else 0.18) for a, c in zip(df["amount"], df["category"])],
        index=df.index,
    )


def tax_vectorized(df):
    return df["amount"] * np.where(df["category"] == "travel", 0.05, 0.18)


def timed(fn, df):
    start = time.perf_counter()
    result = fn(df)
    return result, time.perf_counter() - start


if __name__ == "__main__":
    results = {fn.__name__: timed(fn, invoices) for fn in (tax_iterrows, tax_apply, tax_zip, tax_vectorized)}
    for name, (_, seconds) in results.items():
        print(f"{name:16s} {seconds * 1000:8.1f} ms")
    expected = results["tax_vectorized"][0]
    for name, (series, _) in results.items():
        pd.testing.assert_series_equal(series, expected, check_names=False)
    assert results["tax_vectorized"][1] * 10 < results["tax_iterrows"][1]

    # Which function dominates? Profile the slow path on a slice.
    profiler = cProfile.Profile()
    profiler.runcall(tax_apply, invoices.head(5_000))
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(5)
    print(stream.getvalue()[:800])

    # Peak memory of an operation
    tracemalloc.start()
    tax_vectorized(invoices)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 5_000_000                         # a few float arrays, not Python objects
```

When you truly must loop (calling an external API per row, complex stateful logic), loop over `zip` of the columns you need or `itertuples(index=False)`, never `iterrows`. Better still, batch the external calls.

### Vectorized replacements

| Row-wise pattern | Vectorized replacement |
|---|---|
| `if/else` per row | `np.where(cond, a, b)` |
| `if/elif/elif` | `np.select([c1, c2], [v1, v2], default)` |
| Lookup in a dict | `s.map(mapping)` |
| Numeric ranges to labels | `pd.cut(s, bins, labels=...)` |
| String parsing | `.str.extract`, `.str.replace`, `.str.split(expand=True)` |
| Lookup in another table | `merge` |
| "Previous row for this vendor" | `groupby(...).shift()` |
| Running totals | `cumsum`, `groupby(...).cumsum()` |

## Memory and dtypes

Memory is usually the first wall: pandas needs the data plus room for intermediate copies, so plan for 2 to 5 times the frame size during heavy operations. Three levers:

1. **Text as `str` (Arrow) instead of `object`.** Pandas 3 does this by default. Code or files from pandas 2 that force `object` keep paying the old cost.
2. **Categoricals for low-cardinality text.** A `category` column stores each distinct string once plus small integer codes.
3. **Smaller numeric types.** `int32` or `float32` where the range allows, via `pd.to_numeric(s, downcast="integer")`. Never downcast money to `float32`: it has about 7 significant digits.

```python
import numpy as np
import pandas as pd

rng = np.random.default_rng(1)
N = 200_000
txns = pd.DataFrame(
    {
        "txn_id": np.arange(N, dtype="int64"),
        "vendor_id": rng.choice([f"V{i:04d}" for i in range(800)], N),
        "currency": rng.choice(["INR", "USD", "EUR"], N, p=[0.8, 0.15, 0.05]),
        "status": rng.choice(["posted", "pending", "reversed"], N),
        "amount_paise": rng.integers(1_000, 50_000_000, N),
    }
)


def mb(df: pd.DataFrame) -> float:
    return df.memory_usage(deep=True).sum() / 1e6


as_object = txns.astype({c: object for c in ["vendor_id", "currency", "status"]})  # pandas 2 style
optimized = txns.astype({"vendor_id": "category", "currency": "category", "status": "category"}).assign(
    txn_id=lambda d: pd.to_numeric(d["txn_id"], downcast="integer")
)

if __name__ == "__main__":
    sizes = {"object": mb(as_object), "str (pandas 3 default)": mb(txns), "category + downcast": mb(optimized)}
    for name, size in sizes.items():
        print(f"{name:24s} {size:6.1f} MB")
    assert sizes["str (pandas 3 default)"] < sizes["object"] / 2
    assert sizes["category + downcast"] < sizes["str (pandas 3 default)"] / 2
    assert optimized["txn_id"].dtype == "int32"
    # groupby on categories gives the same answer
    assert (
        txns.groupby("currency")["amount_paise"].sum().to_dict()
        == optimized.groupby("currency")["amount_paise"].sum().to_dict()
    )
```

Categoricals are not free. With high cardinality (invoice numbers, free-text memos) they use more memory than `str`. Concatenating frames whose categories differ falls back to a non-categorical dtype, and new values need `cat.add_categories` first. Use them for status, currency, account type and similar columns.

## The Arrow backend and Parquet

Pandas can store any column in Arrow memory. The `str` default already does for text; `dtype_backend="pyarrow"` does it for every column at read time, and `engine="pyarrow"` uses Arrow's multithreaded CSV parser.

Parquet is where Arrow pays off most. It is columnar, so reading 3 of 40 columns reads only those 3, and `filters=` pushes row predicates down so row groups that cannot match are skipped.

```python
import tempfile
import timeit
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(3)
N = 300_000
gl = pd.DataFrame(
    {
        "entry_id": [f"JE-{i:07d}" for i in range(N)],
        "account": rng.choice(["1100", "2000", "4000", "5000", "5100"], N),
        "amount": rng.normal(0, 25_000, N).round(2),
        "memo": rng.choice(["accrual", "payment", "reversal", "fx revaluation"], N),
    }
)

with tempfile.TemporaryDirectory() as tmp:
    csv_path, pq_path = Path(tmp) / "gl.csv", Path(tmp) / "gl.parquet"
    gl.to_csv(csv_path, index=False)
    gl.to_parquet(pq_path, index=False, row_group_size=50_000)

    readers = {
        "csv default": lambda: pd.read_csv(csv_path, dtype={"account": "str"}),
        "csv pyarrow": lambda: pd.read_csv(csv_path, engine="pyarrow", dtype_backend="pyarrow",
                                           dtype={"account": "string[pyarrow]"}),
        "parquet 2 cols": lambda: pd.read_parquet(pq_path, columns=["account", "amount"]),
    }
    # best of 3, so one-off import and cache costs do not skew the result
    timings = {k: min(timeit.repeat(fn, number=1, repeat=3)) for k, fn in readers.items()}

    big = pd.read_parquet(pq_path, columns=["entry_id", "amount"], filters=[("amount", ">", 75_000)])
    arrow_frame = pd.read_csv(csv_path, engine="pyarrow", dtype_backend="pyarrow")

if __name__ == "__main__":
    print({k: f"{v * 1000:.0f} ms" for k, v in timings.items()})
    assert timings["parquet 2 cols"] < timings["csv default"]
    assert (big["amount"] > 75_000).all() and len(big) == (gl["amount"] > 75_000).sum()
    assert str(arrow_frame["amount"].dtype) == "double[pyarrow]"
```

Arrow dtypes also make handing data to Polars, DuckDB or Spark cheap, since they all speak Arrow. The trade-off: a few NumPy-only functions convert Arrow columns back first, so check a hot path's dtypes if it is unexpectedly slow.

## Chunked reading

When a file does not fit in memory, stream it with `chunksize=` and aggregate each chunk, then combine the partial results. This is map-reduce on one machine. Combine only things that compose: sums and counts compose, means and medians do not. Compute a mean as total sum over total count.

```python
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(11)
N = 120_000
statement = pd.DataFrame(
    {
        "vendor_id": rng.choice([f"V{i:03d}" for i in range(50)], N),
        "amount": rng.uniform(1, 10_000, N).round(2),
    }
)


def vendor_totals_chunked(path: Path, chunksize: int = 25_000) -> pd.DataFrame:
    partials = []
    with pd.read_csv(path, usecols=["vendor_id", "amount"], chunksize=chunksize) as reader:
        for chunk in reader:
            partials.append(chunk.groupby("vendor_id")["amount"].agg(["sum", "count"]))
    combined = pd.concat(partials).groupby(level="vendor_id").sum()
    return combined.assign(mean=combined["sum"] / combined["count"])


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "statement.csv"
    statement.to_csv(path, index=False)
    chunked = vendor_totals_chunked(path)

if __name__ == "__main__":
    full = statement.groupby("vendor_id")["amount"].agg(["sum", "count", "mean"])
    pd.testing.assert_frame_equal(chunked, full, check_exact=False, rtol=1e-9)
    assert chunked["count"].sum() == N
```

The Django analogy is `QuerySet.iterator(chunk_size=...)`: constant memory, one pass. If you need joins or sorts across the whole file, chunking gets awkward fast, and that is the signal to use DuckDB or Polars instead.

## query, eval and copy-on-write

`df.query("amount > @limit and status == 'open'")` and `df.eval("net = amount - tax")` are mostly about readability: column names without repeating `df[...]`, local variables with `@`, and backticks for names with spaces. With the optional `numexpr` package installed they can also save memory and time on large frames, because the whole expression is evaluated in one pass without full-size temporaries. Do not pass user or LLM text into `query`: it is an expression language, and that is an injection surface.

Copy-on-write helps performance without any code. Methods like `rename`, `reset_index`, `set_index` and column selection return new objects that share memory until something writes, so long `assign`/`pipe` chains no longer copy the whole frame at every step. The old habit of calling `.copy()` "to be safe" now only costs memory.

```python
import pandas as pd

open_items = pd.DataFrame(
    {"invoice_id": ["A", "B", "C"], "amount": [500.0, 12_000.0, 7_500.0],
     "tax": [90.0, 2_160.0, 1_350.0], "Due In Days": [5, 40, 12]}
)
limit = 5_000
urgent = open_items.query("amount > @limit and `Due In Days` < 30")
with_net = open_items.eval("net = amount - tax")

if __name__ == "__main__":
    assert urgent["invoice_id"].tolist() == ["C"]
    assert with_net["net"].tolist() == [410.0, 9_840.0, 6_150.0]
    assert "net" not in open_items.columns                     # eval returned a new frame
```

## When to switch engines

Pandas is single-threaded for most operations and eager: it needs the data (and its intermediates) in RAM. That covers more than people expect, but not everything.

| Situation | Reach for | Why |
|---|---|---|
| Fits in RAM with headroom, interactive work | pandas | Ecosystem, familiarity, every library accepts it |
| Tens of GB on one machine, heavy groupby/joins | Polars | Multithreaded, lazy query optimizer, streaming |
| SQL over Parquet/CSV files, out-of-core joins | DuckDB | Embedded analytic SQL engine, spills to disk |
| Data already in a lakehouse, hundreds of GB+ | PySpark (Databricks) | Distributed; data never leaves the cluster |

All of them interoperate through Arrow, so a common pattern is: heavy lifting in DuckDB, Polars or Spark, then hand a small result to pandas for the last mile (formatting, plots, prompts).

```python
# illustrative: Polars lazy query (pip install polars)
import polars as pl

top_vendors = (
    pl.scan_parquet("gl_entries/*.parquet")            # nothing is read yet
    .filter(pl.col("posted_on") >= pl.date(2026, 1, 1))
    .group_by("vendor_id")
    .agg(pl.col("amount").sum().alias("total"))
    .sort("total", descending=True)
    .head(20)
    .collect()                                         # optimizer prunes columns, pushes filters
    .to_pandas()
)
```

```python
# illustrative: DuckDB over Parquet files and an in-memory DataFrame (pip install duckdb)
import duckdb

top_vendors = duckdb.sql("""
    SELECT g.vendor_id, v.name, SUM(g.amount) AS total
    FROM 'gl_entries/*.parquet' AS g
    JOIN vendors_df AS v USING (vendor_id)      -- a pandas DataFrame in local scope
    WHERE g.posted_on >= DATE '2026-01-01'
    GROUP BY ALL ORDER BY total DESC LIMIT 20
""").df()
```

## Databricks context

On Databricks the data lives in Delta tables governed by Unity Catalog (`catalog.schema.table`), and a Spark cluster does the work. Pandas still shows up in three places, each with its own rule:

- **`toPandas()`** collects the whole Spark DataFrame to the driver. Filter and aggregate in Spark first; a 200 GB `toPandas()` is an out-of-memory error, not a slow query. Arrow-based conversion (`spark.sql.execution.arrow.pyspark.enabled`) makes the transfer much faster.
- **Pandas API on Spark** (`import pyspark.pandas as ps`) runs pandas-like syntax distributed. Good for porting, but not every pandas behavior matches.
- **`applyInPandas` and pandas UDFs** run your plain pandas function on each group or batch, in parallel across the cluster. This is how a per-entity reconciliation written in pandas scales out.

```python
# illustrative: Databricks notebook, `spark` is provided
import pandas as pd
from pyspark.sql import functions as F

gl = spark.read.table("finance.gl.entries")

monthly = (
    gl.where(F.col("posted_on") >= "2026-01-01")
    .groupBy("entity_id", "account", F.date_trunc("month", "posted_on").alias("month"))
    .agg(F.sum("amount").alias("movement"))
)
small = monthly.toPandas()                      # thousands of rows, safe to collect


def reconcile_entity(pdf: pd.DataFrame) -> pd.DataFrame:
    """Plain pandas: runs once per entity on a worker."""
    ...


results = spark.read.table("finance.bank.lines").groupBy("entity_id").applyInPandas(
    reconcile_entity, schema="entity_id string, bank_id string, je_id string, match string"
)
```

> **Gotcha:** The pandas version on a Databricks runtime may lag the one on your laptop. Code written for pandas 3 (copy-on-write, `str` dtype, `"ME"` aliases) can behave differently on a pandas 2.x runtime. Check `pd.__version__` on the cluster and pin it in your job's environment.

## Coming from Django

| Django habit | pandas equivalent |
|---|---|
| Avoid N+1 queries | Avoid per-row Python (`iterrows`, `apply(axis=1)`) |
| `.only("id", "amount")` | `usecols=` / `columns=` at read time |
| `QuerySet.iterator(chunk_size=2000)` | `read_csv(chunksize=...)` |
| Aggregate in the DB with `annotate` | Aggregate in SQL before `read_sql`, or in DuckDB/Spark before `toPandas()` |
| Django Debug Toolbar / `EXPLAIN` | `cProfile`, `%timeit`, `memory_usage(deep=True)` |

## Common mistakes

- **Optimizing without profiling**: measure first; the slow part is often a single `apply` or a CSV read.
- **`iterrows` for anything over a few thousand rows**: vectorize, or at least loop over `zip` of columns.
- **Forcing `object` dtype on text**: undoes the pandas 3 `str` default and triples memory.
- **Categoricals on unique IDs**: bigger and slower than `str`.
- **Chunked mean of means**: wrong unless chunks are equal size. Combine sums and counts.
- **`toPandas()` on a big Spark DataFrame**: aggregate in Spark first.
- **Defensive `.copy()` everywhere**: pointless under copy-on-write.
- **Keeping CSV as the intermediate format**: re-parsing text every run. Write Parquet.

## Practice tasks

1. Take any `apply(axis=1)` from your code (or the tax example) with three branches and rewrite it with `np.select`. Done: `assert_series_equal` passes and timing improves at least 20x on 100k rows.
2. Load a 1M-row synthetic transaction CSV three ways (default, `engine="pyarrow"`, Parquet with 3 columns). Done: a table of time and `memory_usage(deep=True)` for each.
3. Write a chunked reader that computes per-vendor sum, count, mean and max over a file larger than you want to load. Done: matches a full in-memory result on a smaller file.
4. Profile a slow notebook cell with `cProfile`, identify the top cumulative function, and fix it. Done: before/after numbers recorded.
5. Rewrite one aggregation as a DuckDB SQL query over Parquet and one as a Polars lazy query (illustrative if not installed). Done: you can explain why each would scale past RAM where pandas would not.

## Interview questions

**Why is `iterrows` slow?**
It builds a new Series for every row, so each row costs Python object creation, type checks and dictionary lookups. Vectorized operations run one compiled loop over the column.

**`apply` vs vectorization?**
`apply(axis=1)` is still a Python loop per row. Vectorization uses column operations (`np.where`, `np.select`, `.str`, `merge`) that run in compiled code, typically 10 to 100 times faster.

**How do you reduce a DataFrame's memory?**
Measure with `memory_usage(deep=True)`, use `str`/Arrow text instead of `object`, categoricals for low-cardinality text, downcast integers, read only needed columns, and keep money as integers or decimals rather than shrinking floats.

**How do you process a CSV larger than RAM with pandas?**
`read_csv(chunksize=...)`, aggregate each chunk into composable partials (sums, counts), then combine. For joins or sorts across the whole file, use DuckDB or Polars.

**What does the Arrow backend give you?**
Compact strings, real missing values for every type, exact decimals, faster Parquet and CSV IO, and zero-copy interchange with Polars, DuckDB and Spark.

**How does copy-on-write affect performance?**
Derived objects share memory until written, so method chains avoid copying the whole frame at each step, and defensive `.copy()` calls are no longer needed.

**When would you move from pandas to Polars, DuckDB or Spark?**
When data or intermediates no longer fit in RAM, when single-threaded groupbys are too slow (Polars), when SQL over files is the natural fit (DuckDB), or when data already lives in a lakehouse at hundreds of GB and above (Spark).

**What is the risk of `toPandas()` on Databricks?**
It collects the full dataset to the driver node; on large data it fails with an out-of-memory error. Reduce in Spark first.

## Further reading

- pandas user guide: "Enhancing performance"
- pandas user guide: "Scaling to large datasets"
- pandas user guide: "PyArrow Functionality"
- Python docs: `cProfile`/`profile` and `tracemalloc`
- Polars user guide: "Lazy API"
- DuckDB docs: "Python API" and "Parquet"
- Databricks docs: "Convert between PySpark and pandas DataFrames" and "pandas function APIs"
