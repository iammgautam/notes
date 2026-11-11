---
title: How a QuerySet runs
section: python-django
order: 8
summary: Laziness, what triggers SQL, the result cache, how chaining clones, and how to count and read the queries your code actually sends.
---

Most Django performance problems are not slow SQL; they are SQL you didn't know you were running. A `QuerySet` looks like a list but is a query builder that executes at moments that aren't obvious from the code. Knowing exactly when it hits the database, and when it reuses results, is the foundation for fixing N+1s and keeping per-request query counts in single digits.

## How it works

### A QuerySet is a description, not data

```python
qs = Order.objects.filter(tenant_id=7)          # no SQL
qs = qs.exclude(status="cancelled")             # no SQL
qs = qs.select_related("customer").order_by("-created_at")  # no SQL
```

Internally a `QuerySet` holds:

- `query`: a `sql.Query` object (the tree of filters, joins, ordering, limits).
- `_result_cache`: `None` until evaluated, then a list of results.
- `_prefetch_related_lookups`: what to prefetch after the main query.
- `_iterable_class`: how rows become objects (`ModelIterable`, `ValuesIterable`, `ValuesListIterable`, `FlatValuesListIterable`).

### Chaining clones

Every method that returns a queryset (`filter`, `exclude`, `annotate`, `order_by`, `select_related`, `only`...) calls `_chain()`, which copies the query and returns a **new** queryset. The original is untouched. That makes it safe to build a base queryset once and derive variants from it:

```python
base = Invoice.objects.filter(tenant_id=tenant_id)
overdue = base.filter(status="open", due_date__lt=today)
paid = base.filter(status="paid")
```

It also means a clone has an empty result cache, even if the original was evaluated.

### What triggers evaluation

| Operation | SQL sent | Fills result cache? |
|---|---|---|
| `for o in qs` | Full query | Yes |
| `list(qs)`, `len(qs)` | Full query | Yes |
| `bool(qs)`, `if qs:` | Full query | Yes |
| `qs[5]` | `LIMIT 1 OFFSET 5` | No |
| `qs[:10]` | None (returns new lazy qs with `LIMIT 10`) | - |
| `qs[::2]` (step) | Full query | Yes |
| `repr(qs)` (shell, logs) | `LIMIT 21` | No |
| `pickle.dumps(qs)` | Full query | Yes |
| `x in qs` | Full query | Yes |
| `qs.count()` | `SELECT COUNT(*)` (or uses cache if filled) | No |
| `qs.exists()` | `SELECT 1 ... LIMIT 1` (or uses cache if filled) | No |
| `qs.first()`, `qs.get()` | Limited query | No |
| `qs.update()`, `qs.delete()` | Write query | No |

### From query to objects

On evaluation:

1. `query.get_compiler(using=db)` produces a `SQLCompiler`.
2. `compiler.as_sql()` renders SQL and a params list. Values are always passed as parameters, never interpolated.
3. The cursor executes; rows are fetched in chunks (`fetchmany`, 100 rows at a time by default).
4. The iterable class turns each row into an instance (`Model.from_db`), attaching `select_related` objects from the same row.
5. If there are prefetch lookups, `prefetch_related_objects` runs one extra query per lookup and stitches results in Python.
6. The list is stored in `_result_cache`.

### The result cache

Once evaluated, iterating the same queryset object again uses the cache:

```python
orders = Order.objects.filter(tenant_id=7)
for o in orders: ...          # 1 query
for o in orders: ...          # 0 queries, cache
orders.filter(status="open")  # new clone, will query again
Order.objects.filter(tenant_id=7)  # different object, will query again
```

`len(orders)` after iterating is free. `orders.count()` after iterating is also free (it checks the cache). Before iterating, `count()` is a separate `COUNT(*)` query.

## In practice

### See the SQL

```python
print(qs.query)            # approximate SQL, params inlined for display only
print(qs.explain(analyze=True, buffers=True))   # PostgreSQL EXPLAIN

from django.db import connection, reset_queries
reset_queries()
run_report(tenant_id=7)
print(len(connection.queries))     # requires DEBUG=True
```

`str(qs.query)` is not exactly what's sent (quoting differs). Use it for reading, not for copy-pasting into production.

For tests and anywhere without `DEBUG`, use `CaptureQueriesContext`:

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

with CaptureQueriesContext(connection) as ctx:
    build_invoice_summary(tenant_id=7)
for q in ctx.captured_queries:
    print(q["time"], q["sql"][:120])
```

### Evaluate once, deliberately

```python
# Bad: 3 queries
qs = Order.objects.filter(tenant_id=t, status="open")
if qs.exists():
    total = qs.count()
    for o in qs: ...

# Good: 1 query, everything from the cache
orders = list(Order.objects.filter(tenant_id=t, status="open"))
if orders:
    total = len(orders)
    for o in orders: ...
```

Rule: if you will iterate anyway, evaluate once and use the list. If you only need yes/no, use `exists()`. If you only need the number, use `count()`.

### Pick the cheapest shape

```python
Order.objects.filter(tenant_id=t).values_list("id", flat=True)     # ints
Order.objects.filter(tenant_id=t).values("status", "total")        # dicts
Order.objects.filter(tenant_id=t).only("id", "number")             # partial models
```

Model instantiation costs on the order of tens of microseconds per row: negligible for 50 rows, seconds for 100k. `values_list` skips it.

### Writes that don't load rows

```python
from django.db.models import F

Invoice.objects.filter(tenant_id=t, due_date__lt=today, status="open") \
    .update(status="overdue")                       # one UPDATE

Product.objects.filter(pk=pid).update(stock=F("stock") - qty)   # race-safe
```

`update()` skips `save()`, the `pre_save`/`post_save` signals and `auto_now`. `delete()` still collects related objects for cascades and delete signals in Python, which can mean many queries for wide graphs.

## Pitfalls

- **Re-filtering an evaluated queryset.** `orders.filter(...)` after iterating `orders` runs a new query. If you already have the list, filter in Python.
- **`if qs:` followed by iteration on a different clone.** Two queries, not one.
- **Slicing then filtering.** `qs[:10].filter(...)` raises "Cannot filter a query once a slice has been taken."
- **Related managers.** `order.lines.all()` creates a new queryset each call; without prefetching, each call queries.
- **`repr` in logs.** Logging a queryset with `%r` evaluates it (up to 21 rows). Logging in a loop multiplies queries.
- **Default ordering.** `Meta.ordering` adds an `ORDER BY` to every query, including ones that don't need it. On large tables that can force a sort. Clear it with `.order_by()` where order doesn't matter.
- **Memory with huge result caches.** Iterating a 1M-row queryset keeps all instances in the cache. Use `.iterator()` (see [Generators and iterators]({{ '/python-django/generators-and-iterators/' | relative_url }})).

## Checklist

- Build querysets freely; know the line where they evaluate.
- Evaluate once; reuse the list.
- `exists()` for checks, `count()` for numbers, `values_list` for IDs.
- Watch query counts with `CaptureQueriesContext` or `assertNumQueries`.
- Use `update()` / `F()` for set-based writes.

## Interview angle

**When does a QuerySet hit the database?** On iteration, `len`, `bool`, `list`, step slicing, indexing, pickling, `repr`, `in`, and terminal methods like `get`, `count`, `exists`, `update`. Building and chaining are free.

**What is the result cache?** After evaluation a queryset stores its rows in `_result_cache`; re-iterating the same object reuses them. Any chained method returns a clone with an empty cache.

**`count()` vs `len()`?** `count()` runs `COUNT(*)` unless already evaluated; `len()` evaluates and loads everything. Use `len()` only if you need the rows anyway.
