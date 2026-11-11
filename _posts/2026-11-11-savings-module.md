---
date: 2026-11-11 15:00:00 +0530
title: "Realised or started? Building a savings tracker in Django and SQL"
summary: "How I built a procurement savings module: store inputs, spread savings across months in SQL, and make monthly spreadsheet re-uploads idempotent."
tags: [django, postgresql, data-modeling]
---

"How much did we save this quarter?" sounds like one question. When leadership on a procurement team asks it, they mean one of two different things, and the spreadsheets their teams kept could not tell those two apart.

I built the savings module at Gainfront to answer both. Gainfront makes supplier lifecycle management software for procurement teams: onboarding, risk, contracts, spend analytics, and supplier diversity and sustainability tracking. I owned savings from the first pull request through the dashboard, the upload rules, the move to our newer frontend, and the editable data table people use today. This post covers how it works and where it is still weak.

## What a savings initiative is

A procurement team renegotiates its packaging contract. Last year packaging cost 1,500; under the new terms they expect to spend 1,200. That is a savings initiative: a supplier, a period (say January to December), a baseline of what you would have spent, and the actual spend that comes in. Savings is baseline minus actual.

Projected savings is the promise: 300 over the year. Realised savings is the part of that promise that has actually happened inside a window of time, backed by recorded spend. If a report covers a quarter of the initiative's life, a quarter of the savings is realised in it. If no actual spend has been recorded, nothing is realised, however good the plan looks.

Leadership asked two questions:

1. **How much did we realise this quarter?** A January-to-December initiative realises a slice in every month it runs.
2. **How much did we start this quarter?** The initiative counts once, in full, in its start month. This one measures the pipeline: is the team signing new deals?

For the packaging deal in Q4, realised is 75 and started is 0. Both numbers are correct, and a dashboard that mixes them is misleading somebody.

Before the module, initiatives lived in spreadsheets: one per team, each with its own columns, its own idea of what "savings" meant, and a fresh copy every month.

## The constraints

- **Money has to add up.** A dashboard total must equal the sum of the rows in the export, to the cent.
- **Files are messy, and they come back.** Currency symbols, thousands separators, months spelled three ways. The same file returns every month with updated actuals.
- **The two questions must never blur.** Each screen answers exactly one of them.
- **Everything filters** by supplier, category, status, owner and date, and stays fast as the table grows.

## The shape of the system

```text
 client spreadsheet (any column layout)
        |
        v
 shared file loader: maps columns, stores parsed rows
        |
        v
 import task on our queue: dry run, then one transaction
        |   per row: skip / create / update actuals
        v
 initiatives table (inputs only: dates, baseline, actual)
        |
   +----+------------------------------+
   v                                   v
 "realised" endpoints             "started" endpoints
 (prorated by month overlap)      (whole value in start month)
   |                                   |
   +-----------------+-----------------+
                     v
   Vue pages: dashboard, report, data table, Excel export
```

The file loader and the task queue are shared pieces of our platform. I plugged savings into both instead of writing a new upload endpoint.

## Store inputs, derive outputs

The initiatives table stores what a person can type or a file can carry: dates, supplier, owner, category, status, baseline and actual spend. It has no savings column.

A stored total goes stale the moment someone edits an input and some code path forgets to recompute it. A derived value cannot go stale. The cost is computing it on every read, and PostgreSQL does that arithmetic inside the same `SELECT` that fetches the row.

I rejected two other designs:

- **One row per initiative per month.** Spreading at write time makes the monthly chart a plain `GROUP BY`. But every edit to dates or amounts rewrites a dozen rows, and safe re-uploads get much harder.
- **Deriving actual spend from transaction data.** My first version did this. Each initiative carried matching rules (by category, division or project), and a background task recomputed actual spend whenever new transactions arrived. In practice it tied savings to every spend load, and it asked users to configure matching for numbers they already had in their spreadsheets. I took it out. Actual spend is an input now, like everything else.

## Spreading savings across months

Savings are tracked at month granularity. An initiative covers an inclusive range of months, a report window covers another, and the realised share is the overlap.

```text
             2025                                            2026
             Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec Jan Feb Mar
initiative   |----------------- 12 months ------------------|
window                                           |---- Oct to Mar ------|
overlap                                          |--- 3 ----|

baseline 1,500   actual 1,200   projected savings 300
realised in window = 300 x 3 / 12 = 75
started in window  = 0  (all 300 counted in Jan 2025)
```

The rules:

- **Length** is the inclusive month count: January to December is 12, March to March is 1.
- **Overlap** is floored at zero. Otherwise an initiative entirely outside the window has a negative overlap and quietly subtracts from the total.
- **Realised** is `(baseline - actual) * overlap / length`.
- **Zero actuals means zero savings.** With no actual spend recorded, baseline minus actual is the whole baseline, and every freshly entered plan would show up as if the full 1,500 had been saved. I pinned this with tests on every screen that shows a savings number, because it is the easiest rule to lose in a refactor.

## Realised savings as a QuerySet

The whole calculation is a chain of annotations, so it runs in the database. A simplified version with neutral names:

```python
MONEY = DecimalField(max_digits=30, decimal_places=10)


def month_index(field):
    # 2025-03 -> 2025 * 12 + 3. Subtracting two indexes counts months.
    return ExtractYear(field) * 12 + ExtractMonth(field)


def with_realised(initiatives, window_start, window_end):
    first, last = month_index("starts_on"), month_index("ends_on")
    lo = Greatest(first, Value(window_start.year * 12 + window_start.month))
    hi = Least(last, Value(window_end.year * 12 + window_end.month))
    saved = F("baseline") - F("actual")
    return initiatives.annotate(
        length=last - first + 1,
        overlap=Greatest(hi - lo + 1, Value(0)),
    ).annotate(
        realised=Case(
            When(actual=0, then=Value(Decimal("0"))),
            default=saved * F("overlap") / F("length"),
            output_field=MONEY,
        )
    )


in_window = Q(starts_on__lte=window_end, ends_on__gte=window_start)
totals = with_realised(
    SavingsInitiative.objects.filter(in_window, company=company),
    window_start,
    window_end,
).aggregate(realised=Sum("realised"))
```

The `in_window` filter is the standard test for two overlapping ranges, and it lives in the filter layer once.

> **Gotcha:** order of operations matters. `overlap` and `length` are integers, and PostgreSQL divides integers as integers, so `overlap / length` on its own is `3 / 12 = 0` and a whole quarter of savings disappears. Multiply the money first, let `numeric` carry the division, and round once at the API edge.

Why SQL and not a Python loop? The result is still a QuerySet, so the same expression feeds the paginated report, the dashboard totals and the export, with the user's filters applied by the database. A loop would need every row in memory and would be a second copy of the rule, free to drift.

## The "started" view is a GROUP BY

Each initiative counts in full in its start month, so the monthly chart is one grouped query. Python only fills months with no rows, so the chart has no gaps:

```python
saved = Case(
    When(actual=0, then=Value(Decimal("0"))),
    default=F("baseline") - F("actual"),
    output_field=MONEY,
)
rows = (
    initiatives.filter(starts_on__range=(window_start, window_end))
    .annotate(y=ExtractYear("starts_on"), m=ExtractMonth("starts_on"))
    .values("y", "m")
    .annotate(total=Sum(saved))
)
by_month = {(r["y"], r["m"]): r["total"] for r in rows}
# every (year, month) in the window, zero where nothing started
chart = [by_month.get(ym, Decimal("0")) for ym in window_months]
```

I gave each question its own family of endpoints instead of one endpoint with a `mode` flag. With a flag, a screen can easily fetch one meaning while its title says the other. With separate endpoints the meaning is in the URL.

## The dashboard queries

The summary cards are a couple of aggregates: realised totals over the annotated QuerySet, raw baseline and actual totals, and spend under management for the same suppliers. The breakdowns (category, status, type, top suppliers) are each one `GROUP BY` with `ORDER BY total DESC LIMIT n`. Any of them can count initiatives instead of summing value, which swaps `SUM` for `COUNT`.

The monthly realised chart is the awkward one. Proration depends on the month, so there is no column to group by. My first version looped over the months in Python and ran the proration aggregate once per month: 12 queries for a one-year chart, 24 for two. Correct and simple, but its cost grew with the window.

A teammate later folded it into one statement with conditional aggregation, while moving the module onto our shared base layer. Each month becomes a filtered `SUM` over the same scan. Roughly:

```sql
SELECT
  SUM(per_month) FILTER (WHERE starts_on <= DATE '2025-10-31'
                           AND ends_on   >= DATE '2025-10-01') AS oct,
  SUM(per_month) FILTER (WHERE starts_on <= DATE '2025-11-30'
                           AND ends_on   >= DATE '2025-11-01') AS nov
FROM (
  SELECT starts_on, ends_on,
         CASE WHEN actual = 0 THEN 0
              ELSE (baseline - actual) / length_months END AS per_month
  FROM savings_initiatives
  WHERE company_id = 42
) AS s;
```

In Django that is `Sum(expression, filter=Q(...))` once per month inside a single `.aggregate()`: one scan, one round trip, one column per month.

The Excel export reuses the overlap rule. The frontend describes each period column with a short code (a year, quarter or month, plus a metric), and the backend parses each code into a month range. A client asking for "Q1 savings" next to "March actuals" needs no backend change.

## Upload rules: skip, fail or update

The import is a task on our queue that reads a file the shared loader has already parsed. The rules inside it are mine:

1. **Dry run first.** Our queue runs every task in dry mode before queueing it. My dry mode checks that the file exists and finished parsing, and returns its row count, so a bad file id fails in the request that submitted it, not later on a worker.
2. **One transaction per file.** A file loads completely or not at all.
3. **Every row gets a decision.**

```text
 row --> required fields? ---no---> SKIP  (line n: missing supplier)
           | yes
           v
         any amount? -------no---> SKIP  (line n: no amounts)
           | yes
           v
         status allowed? ---no---> SKIP  (line n: status 'Draft')
           | yes
           v
         parse amounts --garbage--> FAIL FILE (roll back everything)
           | ok
           v
         key already loaded? --no--> CREATE
           | yes
           v
         actual changed? ---yes---> UPDATE actual only (old -> new)
           | no
           v
         UNCHANGED
```

Skip what the user expects to be left out (only active and completed initiatives load). Fail when loading a row would mean guessing what a number is.

The parsers are tolerant about formatting, because real files contain `$1,200`, `€ 99.50` and `£ 3,000`, and rejecting those pushes cleanup back onto the user. Instead of a list of currency symbols, I strip anything Unicode classifies as one:

```python
import unicodedata
from decimal import Decimal, InvalidOperation


def parse_amount(raw) -> Decimal:
    """'$1,200' -> 1200, '€ 99.50' -> 99.50, '' -> 0."""
    text = "".join(
        ch for ch in str(raw or "")
        if ch != "," and unicodedata.category(ch) != "Sc"  # Sc: currency
    ).strip()
    if text in ("", "-"):
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not an amount: {raw!r}") from None
```

Months arrive as `7`, `July` or `jul`, and all three parse. The file gives a start month, a start year and a length in months; I derive the end date as the last day of the final month.

Some teams record actual spend and a savings figure but no baseline. The table has no savings column, so I solve for the input: baseline = actual + savings, and the derived savings match the file exactly. A row with only a baseline keeps actual at zero, so it shows no savings until real spend arrives.

What made monthly re-uploads safe is an upsert on a natural key. Clients send the same file every month with new actuals, and (supplier, start, end, baseline) identifies an initiative across uploads:

```python
key = {"supplier_id": supplier_id, "starts_on": starts_on,
       "ends_on": ends_on, "baseline": baseline}
existing = SavingsInitiative.objects.filter(**key).first()

if existing is None:
    SavingsInitiative.objects.create(**key, **details, actual=actual)
    report.created(line_no)
elif has_actual and existing.actual != actual:
    report.updated(line_no, old=existing.actual, new=actual)
    existing.actual = actual
    existing.save(update_fields=["actual", "updated_at"])
```

A new key creates a row. A known key updates only actual spend, and only when it changed. Uploading the same file twice is a no-op, and edits someone made in the app to an owner or description survive next month's upload.

The task's result is a per-row report, stored with the task, so the user sees what happened to each line:

```json
{
  "loaded": 412,
  "skipped": [
    {"line": 18, "reason": "missing fields: supplier"},
    {"line": 77, "reason": "status 'Draft' not allowed"}
  ],
  "updated": [
    {"line": 203, "old_actual": "8000.00", "new_actual": "9150.00"}
  ]
}
```

## Performance considerations

- **Query counts don't grow with the data.** Every dashboard widget runs a fixed, small number of queries, whatever the number of initiatives.
- **Streaming exports.** The Excel export reads rows in chunks with `queryset.iterator(chunk_size=...)`, so memory stays flat.
- **No speculative indexes.** Savings tables are small next to transaction tables, and the overlap filter scans one company's initiatives. I would add an index when a query plan asks for one.
- **The import is per-row.** A few queries per row inside one transaction is fine at today's file sizes, and it is the first thing I would change if they grew.

## Moving to the newer frontend

The module started in our older Vue app, with REST endpoints from day one. When we standardised on a newer Vue 3 and TypeScript app, I moved savings over. Because the logic already lived behind the API, the backend barely noticed.

Then I built the data table. People came from spreadsheets and wanted to fix a number where they saw it, not open a form to change one field. The table works like a sheet: click a cell, edit, press Enter to save, and focus returns to the cell so keyboard users keep moving. Status and type cells open a picker of the client's configured choices. View modes show the same list read-only or as the quick editor, with a per-client default. Inline saves go through the same API and serializer as the form, so validation never forks.

## Results

- **Two clearly named answers.** Realised and started live on separate endpoints and screens, and the zero-actuals rule applies to every savings number either shows.
- **Fixed query counts.** The monthly realised chart went from one query per month in my version to one after my teammate's change; every other widget was fixed from the start.
- **Idempotent re-uploads.** The same file twice creates nothing new; a monthly file with fresh actuals updates only actuals.
- **26 backend tests** pin the rules: zero actuals on each screen, the upload back-solve, window boundaries, start-date grouping, and a supplier user getting a 403.

I don't have latency numbers worth publishing; the tables are small. The win is that the numbers are right and their cost doesn't grow with the window.

## What I'd do differently

- **Let PostgreSQL own the natural key from day one.** A unique constraint enforces it in one place and turns the per-row upsert loop into a single batched `INSERT ... ON CONFLICT DO UPDATE`.
- **Point at the line, not just the value.** When a file is rejected, the message should name the row and the column, because that is what the user goes back to fix in Excel.
- **Refuse to guess.** Spreadsheet input is full of ambiguity: blank months, accounting brackets for negatives, currency symbols. Every guess a parser makes should show up as a warning in the report, and a guess that could flip a sign should be a rejection.
- **Treat history as history.** Savings rows record what happened. Deleting the supplier they point at should be blocked, not cascade, and that choice belongs in the first migration, not a later one.
- **Treat linear proration as a model, not the truth.** Real savings are lumpy; a contract can front-load volume into one quarter. If clients need that, the honest model is per-month actuals, which brings back the row-per-month design I rejected.
