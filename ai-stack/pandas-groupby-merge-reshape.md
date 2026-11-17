---
title: Groupby, merge and reshape
section: ai-stack
order: 11
phase: "Phase 2 · Pandas"
summary: Aggregate with groupby, join safely with validate and indicator, reconcile bank vs ledger, reshape with pivot and melt, and run rolling and month-end views.
---

Most finance questions are a groupby, a join or a reshape: spend by vendor, invoices without a matching PO, bank lines missing from the ledger, budget against actuals by month, closing balances at each month end. After this note you can write all of those in pandas, guard joins against silent row multiplication, and build a small bank reconciliation. These are also the functions you expose to an agent as tools in [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }}).

## Prerequisites

[Cleaning and transforming data with Pandas]({{ '/ai-stack/pandas-cleaning-and-transformation/' | relative_url }}): typed columns, money as integer minor units or `Decimal`, `assign` and `pipe`.

## Split-apply-combine

`groupby` splits rows by key, applies a function to each group, and combines the results. What you get back depends on which "apply" you use:

```text
               split by vendor          apply              combine
 rows  ---->  [V001: 3 rows]  ---->  agg: one row       -> 1 row/group
              [V002: 2 rows]         transform: same    -> same shape
              [V003: 1 row ]           shape as input      as input
                                     filter: keep or    -> subset of
                                       drop the group      rows
```

### Aggregation, transform and filter

```python
import pandas as pd

invoices = pd.DataFrame(
    {
        "invoice_id": [f"INV-{i}" for i in range(1, 9)],
        "vendor_id": ["V001", "V001", "V002", "V001", "V002", "V003", "V002", "V001"],
        "dept": ["IT", "IT", "Ops", "Ops", "Ops", "IT", "Ops", "IT"],
        "amount": [1200.0, 800.0, 450.0, 9500.0, 300.0, 75.0, 500.0, 1000.0],
        "invoice_date": pd.to_datetime(
            ["2026-01-03", "2026-01-15", "2026-01-20", "2026-02-02",
             "2026-02-10", "2026-02-11", "2026-02-25", "2026-03-01"]
        ),
    }
)

# Named aggregation: output column = (input column, function)
vendor_spend = (
    invoices.groupby("vendor_id", as_index=False)
    .agg(
        total=("amount", "sum"),
        invoices=("invoice_id", "count"),
        largest=("amount", "max"),
        last_invoice=("invoice_date", "max"),
    )
    .sort_values("total", ascending=False)
)

# transform: broadcast a group statistic back to every row
invoices["vendor_median"] = invoices.groupby("vendor_id")["amount"].transform("median")
invoices["share_of_dept"] = invoices["amount"] / invoices.groupby("dept")["amount"].transform("sum")
outliers = invoices[invoices["amount"] > 3 * invoices["vendor_median"]]

# filter: keep whole groups that pass a test
big_vendors = invoices.groupby("vendor_id").filter(lambda g: g["amount"].sum() > 5_000)

if __name__ == "__main__":
    print(vendor_spend)
    assert vendor_spend.iloc[0].to_dict()["vendor_id"] == "V001"
    assert vendor_spend.set_index("vendor_id").loc["V001", "total"] == 12500.0
    assert outliers["invoice_id"].tolist() == ["INV-4"]
    assert round(invoices.groupby("dept")["share_of_dept"].sum(), 9).eq(1).all()
    assert set(big_vendors["vendor_id"]) == {"V001"}
```

Notes that save debugging time:

- `as_index=False` returns the keys as columns, which is what you want before a merge or export.
- Rows with a missing key are dropped by default. Pass `dropna=False` to keep an "unknown vendor" group, which in finance is often the most interesting one.
- Grouping by a categorical uses `observed=True` by default in pandas 3, so unused categories do not appear.
- `groupby(...).apply(f)` is the slow escape hatch. In pandas 3 the grouping columns are no longer passed to `f`; select the columns you need or use `agg`/`transform`.

## Merging

`merge` is SQL `JOIN` for DataFrames. The `how` argument picks the type:

| `how` | Keeps | Typical finance use |
|---|---|---|
| `inner` | Keys in both | Invoices enriched with vendor master |
| `left` | All left rows | Keep every transaction even if the vendor is unknown |
| `right` | All right rows | Rarely needed; swap sides and use `left` |
| `outer` | All keys from both | Reconciliation: see both sides' leftovers |
| `cross` | Every pair | Generate account x month grids |

Two arguments turn a merge from hopeful into checked:

- **`validate=`** asserts the key relationship: `"one_to_one"`, `"one_to_many"`, `"many_to_one"`, `"many_to_many"`. A duplicated row in a vendor master turns a `many_to_one` join into row multiplication, which silently doubles spend. `validate="many_to_one"` raises `MergeError` instead.
- **`indicator=True`** adds a `_merge` column with `left_only`, `right_only` or `both`. Filtering on it gives anti-joins: invoices with no vendor record, POs never invoiced.

```python
import pandas as pd

invoices = pd.DataFrame(
    {
        "invoice_id": ["INV-1", "INV-2", "INV-3", "INV-4"],
        "vendor_id": ["V001", "V002", "V001", "V009"],
        "amount": [1200.0, 450.0, 800.0, 60.0],
    }
)
vendors = pd.DataFrame(
    {
        "vendor_id": ["V001", "V002", "V002", "V003"],      # V002 duplicated by an upsert bug
        "name": ["Acme Corp", "Globex", "Globex Ltd", "Initech"],
    }
)

# 1. Without validate: rows multiply and spend is overstated
naive = invoices.merge(vendors, on="vendor_id", how="left")

# 2. With validate: the bad master data is caught at the join
try:
    invoices.merge(vendors, on="vendor_id", how="left", validate="many_to_one")
    caught = False
except pd.errors.MergeError:
    caught = True

# 3. Fix the master, then use indicator to find invoices without a vendor
clean_vendors = vendors.drop_duplicates("vendor_id", keep="last")
joined = invoices.merge(
    clean_vendors, on="vendor_id", how="left", validate="many_to_one", indicator=True
)
orphans = joined.loc[joined["_merge"] == "left_only", "invoice_id"]

if __name__ == "__main__":
    assert len(naive) == 5 and naive["amount"].sum() == 2960.0   # 450 counted twice
    assert caught
    assert len(joined) == len(invoices) and joined["amount"].sum() == 2510.0
    assert orphans.tolist() == ["INV-4"]
```

> **Rule of thumb:** Every production merge gets `validate=`, and a row count assertion after it. A silent fan-out is the most common way a finance report ends up wrong without an error.

When key names differ, use `left_on`/`right_on`. When non-key columns collide, `suffixes=("_bank", "_gl")` makes the origin of each column obvious.

## Reconciliation: bank vs ledger

Reconciliation matches two records of the same money: the bank statement and the general ledger cash account. Real matching runs in passes, from strict to loose, and whatever is left is the reconciliation report.

```text
 bank lines            ledger lines
     |                      |
     +---- pass 1: exact ---+   same reference and amount
     |     (merge, 1:1)     |
     v                      v
 leftovers ---- pass 2: ----+   same amount, dates within 3 days
     |     merge_asof       |
     v                      v
 bank-only              ledger-only
 (fees, interest)       (cheques not yet cleared)
```

Match on integer paise, never on floats: `0.1 + 0.2 == 0.3` is `False`, and the same is true for join keys.

```python
import pandas as pd

bank = pd.DataFrame(
    {
        "bank_id": ["B1", "B2", "B3", "B4", "B5"],
        "date": pd.to_datetime(["2026-01-05", "2026-01-07", "2026-01-09", "2026-01-12", "2026-01-31"]),
        "amount_paise": [118_000, -25_000, 50_000, -50_000, -1_500],
        "ref": ["UTR123", "CHQ-881", "NEFT-77", "IMPS-9", "BANK FEE"],
    }
)
ledger = pd.DataFrame(
    {
        "je_id": ["JE-1", "JE-2", "JE-3", "JE-4", "JE-5"],
        "date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-11", "2026-01-10", "2026-01-30"]),
        "amount_paise": [118_000, -25_000, 50_000, -50_000, -72_000],
        "ref": ["UTR123", "CHQ-881", "INV-3", "PAY-4", "CHQ-882"],
    }
)


def reconcile(bank: pd.DataFrame, ledger: pd.DataFrame, days: int = 3):
    # Pass 1: exact reference + amount, one-to-one or fail loudly
    exact = bank.merge(
        ledger, on=["ref", "amount_paise"], suffixes=("_bank", "_gl"), validate="one_to_one"
    ).assign(match="exact")

    # Pass 2: same amount, nearest date within tolerance
    rest_bank = bank[~bank["bank_id"].isin(exact["bank_id"])].sort_values("date")
    rest_gl = (
        ledger[~ledger["je_id"].isin(exact["je_id"])]
        .assign(gl_date=lambda d: d["date"])
        .sort_values("date")
    )
    near = pd.merge_asof(
        rest_bank, rest_gl, on="date", by="amount_paise",
        tolerance=pd.Timedelta(days=days), direction="nearest", suffixes=("_bank", "_gl"),
    ).dropna(subset=["je_id"])
    near = near[~near["je_id"].duplicated()].assign(match="amount+date")  # one GL line, one bank line

    matched = pd.concat(
        [exact[["bank_id", "je_id", "amount_paise", "match"]],
         near[["bank_id", "je_id", "amount_paise", "match"]]],
        ignore_index=True,
    )
    bank_only = bank[~bank["bank_id"].isin(matched["bank_id"])]
    ledger_only = ledger[~ledger["je_id"].isin(matched["je_id"])]
    return matched, bank_only, ledger_only


matched, bank_only, ledger_only = reconcile(bank, ledger)

if __name__ == "__main__":
    print(matched)
    assert matched.set_index("bank_id")["je_id"].to_dict() == {
        "B1": "JE-1", "B2": "JE-2", "B3": "JE-3", "B4": "JE-4"
    }
    assert bank_only["ref"].tolist() == ["BANK FEE"]          # book it in the GL
    assert ledger_only["ref"].tolist() == ["CHQ-882"]         # outstanding cheque
    # The bank-vs-GL difference is fully explained by the unmatched items
    diff = bank["amount_paise"].sum() - ledger["amount_paise"].sum()
    assert diff == bank_only["amount_paise"].sum() - ledger_only["amount_paise"].sum()
```

`merge_asof` requires both sides sorted on the `on` key, matches each left row to at most one right row, but can reuse a right row for several left rows. The `duplicated()` line enforces one-to-one; in a real system you would also flag those collisions for review rather than silently picking the first.

## Concat and reshape

`concat` stacks frames with the same columns, such as monthly extracts. `keys=` records where each block came from.

Reshaping moves data between **long** (one row per observation: account, month, amount) and **wide** (one row per account, one column per month). Finance users think in wide spreadsheets; pandas and LLM tools work best on long data.

| Function | Direction | Use |
|---|---|---|
| `pivot_table` | long to wide, with aggregation | Account x month report |
| `pivot` | long to wide, no duplicates allowed | Already-aggregated data |
| `melt` | wide to long | Budget spreadsheets with month columns |
| `stack` / `unstack` | column level to row level and back | MultiIndex frames |

```python
import pandas as pd

jan = pd.DataFrame({"account": ["5000", "5100", "5000"], "amount": [1200.0, 300.0, 450.0]})
feb = pd.DataFrame({"account": ["5000", "5100"], "amount": [900.0, 650.0]})
actuals = pd.concat([jan, feb], keys=["2026-01", "2026-02"], names=["month", None]).reset_index(level="month")

# Long -> wide report with totals
report = actuals.pivot_table(
    index="account", columns="month", values="amount", aggfunc="sum",
    fill_value=0, margins=True, margins_name="Total",
)

# The budget arrives wide from a spreadsheet; melt it to long
budget_wide = pd.DataFrame({"account": ["5000", "5100"], "2026-01": [1500.0, 400.0], "2026-02": [1000.0, 500.0]})
budget = budget_wide.melt(id_vars="account", var_name="month", value_name="budget")

variance = (
    actuals.groupby(["account", "month"], as_index=False)["amount"].sum()
    .merge(budget, on=["account", "month"], how="outer", validate="one_to_one")
    .fillna({"amount": 0.0, "budget": 0.0})
    .assign(variance=lambda d: d["amount"] - d["budget"])
)

# stack / unstack: move the month level between columns and rows
by_month = report.drop(index="Total", columns="Total")
long_again = by_month.stack()                      # Series indexed by (account, month)

if __name__ == "__main__":
    print(report)
    assert report.loc["5000", "2026-01"] == 1650.0 and report.loc["Total", "Total"] == 3500.0
    over = variance[variance["variance"] > 0]
    assert over[["account", "month"]].values.tolist() == [["5000", "2026-01"], ["5100", "2026-02"]]
    assert long_again[("5100", "2026-02")] == 650.0
    assert long_again.unstack().equals(by_month)
```

## Rolling and expanding windows

Windows compute a statistic over a moving slice of rows. Time-based windows (`"7D"`, `"30D"`) use a datetime index or `on=` column and handle irregular dates, which integer windows do not.

```python
import pandas as pd

payments = pd.DataFrame(
    {
        "vendor_id": ["V001"] * 6 + ["V002"] * 3,
        "paid_on": pd.to_datetime(
            ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-20", "2026-02-01", "2026-02-03",
             "2026-01-03", "2026-01-04", "2026-01-30"]
        ),
        "amount": [100.0, 120.0, 90.0, 110.0, 950.0, 105.0, 40.0, 60.0, 50.0],
    }
).sort_values(["vendor_id", "paid_on"], ignore_index=True)

g = payments.groupby("vendor_id")["amount"]
payments["prior_median"] = g.transform(lambda s: s.shift().expanding().median())  # excludes current row
payments["spike"] = payments["amount"] > 3 * payments["prior_median"]
payments["running_total"] = g.cumsum()

spend_7d = payments.groupby("vendor_id").rolling("7D", on="paid_on")["amount"].sum()

if __name__ == "__main__":
    assert payments.loc[payments["spike"], "amount"].tolist() == [950.0]
    assert spend_7d.loc[("V001", pd.Timestamp("2026-01-06"))] == 310.0   # Jan 2 + 5 + 6
    assert payments.groupby("vendor_id")["running_total"].last().to_dict() == {"V001": 1475.0, "V002": 150.0}
```

The `shift()` before `expanding()` matters: without it the current payment is part of its own baseline, which hides spikes.

## Resampling and month-end close

`resample` is a groupby on time buckets. In pandas 3 the aliases are `"ME"` (month end), `"QE"` (quarter end), `"YE"`, `"BME"` (business month end), `"W-FRI"`, `"D"` and `"h"`; the old `"M"` raises. `pd.Grouper(key=..., freq="ME")` does the same inside a normal `groupby`.

A compact month-end close over GL entries: check every journal balances, compute each account's monthly movement, and roll it into closing balances. Empty months are filled with zero so every account has a balance at every month end.

```python
import pandas as pd

gl = pd.DataFrame(
    {
        "entry_id": ["JE-1", "JE-1", "JE-2", "JE-2", "JE-3", "JE-3", "JE-4", "JE-4"],
        "posted_on": pd.to_datetime(
            ["2026-01-10", "2026-01-10", "2026-01-28", "2026-01-28",
             "2026-03-05", "2026-03-05", "2026-03-31", "2026-03-31"]
        ),
        "account": ["1100", "4000", "5000", "2000", "1100", "4000", "5000", "1100"],
        "amount_paise": [500_000, -500_000, 120_000, -120_000, 250_000, -250_000, 80_000, -80_000],
    }
)

# 1. Every journal entry must balance (debits positive, credits negative)
unbalanced = gl.groupby("entry_id")["amount_paise"].sum().loc[lambda s: s != 0]

# 2. Monthly movement per account, months as columns, gaps as zero
movement = (
    gl.set_index("posted_on")
    .groupby("account")["amount_paise"]
    .resample("ME")
    .sum()
    .unstack("posted_on", fill_value=0)
)

# 3. Closing balance = running sum of movements across months
closing = movement.cumsum(axis=1)

if __name__ == "__main__":
    print(closing)
    assert unbalanced.empty
    feb = pd.Timestamp("2026-02-28")
    assert feb in closing.columns                     # quiet month still has a column
    assert (closing.sum() == 0).all()                 # trial balance nets to zero each month
    assert closing.loc["1100", pd.Timestamp("2026-03-31")] == 670_000
    assert closing.loc["4000", feb] == -500_000
```

## Coming from Django

| Django ORM / SQL | pandas |
|---|---|
| `.values("vendor").annotate(total=Sum("amount"))` | `groupby("vendor", as_index=False).agg(total=("amount", "sum"))` |
| `Subquery(...)` / `OuterRef` per row | `groupby().transform()` |
| `.filter(...).annotate(n=Count(...)).filter(n__gte=3)` (HAVING) | `groupby().filter(lambda g: len(g) >= 3)` |
| `select_related` / SQL `JOIN` | `merge(..., validate="many_to_one")` |
| `LEFT JOIN ... WHERE right.id IS NULL` | `merge(..., indicator=True)` then `left_only` |
| `Window(Sum(...), order_by=...)` | `cumsum()`, `rolling()`, `expanding()` |
| `TruncMonth("posted_on")` | `resample("ME")` or `pd.Grouper(freq="ME")` |

If the data is already in Postgres and large, do these in SQL and pull the result. Pandas wins when you combine sources (a bank CSV with a GL query) or need logic awkward in SQL, like nearest-date matching.

## Common mistakes

- **Merging without `validate=`**: duplicate keys multiply rows and inflate totals. Validate and assert row counts.
- **Float join keys**: amounts that print the same do not compare equal. Join on integer minor units or rounded `Decimal` strings.
- **Forgetting `dropna=False` in groupby**: rows with a missing vendor vanish from the totals.
- **`apply` where `agg` or `transform` fits**: slower and harder to read.
- **Integer windows on irregular dates**: `rolling(7)` is seven rows, not seven days. Use `"7D"` with `on=`.
- **Old offset aliases**: `resample("M")` raises in pandas 3. Use `"ME"`.
- **`merge_asof` on unsorted data**: it raises. Sort both sides by the `on` key.

## Practice tasks

1. From 1,000 synthetic invoices, produce a vendor table with total, count, median and last invoice date using named aggregation. Done: matches a hand-written loop on a sample of vendors.
2. Flag invoices more than three times their vendor's prior median using `transform`, with no row looking at itself. Done: planted spikes are flagged; the first invoice for a vendor never is.
3. Extend `reconcile` with a third pass that matches one bank line to two ledger lines that sum to it (split payments) within the same day. Done: tests for a split, a non-split, and an ambiguous case that goes to review.
4. Build a budget vs actual report: melt a wide budget, aggregate actuals by account and month, outer merge with `validate`, and pivot back to wide with a variance column per month. Done: totals match both sources.
5. Produce closing balances per account per month for a year of GL entries, including months with no activity, and assert the trial balance is zero at every month end.

## Interview questions

**`agg` vs `transform` vs `filter`?**
`agg` returns one row per group. `transform` returns a result aligned to the original rows, for group-relative features. `filter` keeps or drops entire groups.

**How do you stop a merge from silently duplicating rows?**
Pass `validate="many_to_one"` (or the right relationship), then assert the row count. Fix the duplicated keys at the source rather than with `drop_duplicates` after the fact.

**What does `indicator=True` do?**
It adds a `_merge` column saying whether each row came from left, right or both. Filtering on it gives anti-joins for exception reports.

**How would you reconcile a bank statement against the ledger in pandas?**
Normalize amounts to integer minor units, match exact reference and amount with a one-to-one merge, then match leftovers by amount and nearest date within a tolerance using `merge_asof`, enforce one-to-one, and report both sides' leftovers.

**`pivot` vs `pivot_table`?**
`pivot` only reshapes and fails on duplicate index/column pairs. `pivot_table` aggregates duplicates and supports `fill_value` and `margins`.

**What is the difference between `rolling(7)` and `rolling("7D")`?**
The first is seven rows; the second is a seven-day time window that respects gaps between dates.

**How do you compute month-end closing balances?**
Group by account, resample monthly with `"ME"` and sum the movement, fill empty months with zero, then take a cumulative sum across months.

**What changed for `groupby.apply` in pandas 3?**
The grouping columns are excluded from the frame passed to the function, so code that read them inside `apply` must select them differently.

## Further reading

- pandas user guide: "Group by: split-apply-combine"
- pandas user guide: "Merge, join, concatenate and compare"
- pandas API reference: `pandas.merge_asof`
- pandas user guide: "Reshaping and pivot tables"
- pandas user guide: "Windowing operations"
- pandas user guide: "Time series / date functionality" (resampling and offset aliases)
