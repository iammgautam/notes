---
title: Finding and fixing N+1 queries
section: python-django
order: 9
summary: Why N+1 happens, select_related vs prefetch_related vs Prefetch, only/defer, and how to detect and lock in query counts with tooling and tests.
---

N+1 is the most common reason a Django endpoint goes from 80 ms to 8 seconds as a tenant grows. One query fetches N orders, then each order triggers another query for its customer, its lines, its invoice. Locally with 10 rows it's invisible; in production with 500 rows it's 500+ queries per request, each a network round trip. Fixing it systematically routinely takes an endpoint from hundreds of queries down to 5–10 and cuts SQL time by an order of magnitude.

## How it works

Related-object access goes through descriptors. `order.customer` checks the instance's field cache; on a miss it runs `SELECT ... FROM customer WHERE id = %s`. `order.lines.all()` builds a new queryset filtered by `order_id`. Inside a loop, that's one query per row.

```python
orders = Order.objects.filter(tenant_id=7)[:200]     # 1 query
for o in orders:
    print(o.customer.name)                           # +200
    for line in o.lines.all():                       # +200
        print(line.product.sku)                      # +N lines
```

Django offers two tools that fill those caches up front:

| | `select_related` | `prefetch_related` |
|---|---|---|
| Relations | ForeignKey, OneToOne (forward), reverse OneToOne | Any: reverse FK, ManyToMany, GenericFK, also FK |
| SQL | One query with `JOIN` | One extra query per lookup, `WHERE fk IN (...)` |
| Join done by | PostgreSQL | Python |
| Cost risk | Wide rows, duplicated parent data, bad plans on many joins | Huge `IN` lists, memory |

### select_related

Adds `LEFT OUTER JOIN` (or `INNER JOIN` for non-null FKs) and builds the related objects from the same row.

```python
Order.objects.select_related("customer", "customer__account_manager")
```

### prefetch_related

Runs the main query, collects the PKs, then runs `SELECT ... FROM line_item WHERE order_id IN (1, 2, ...)` and attaches results to each order's prefetch cache.

```python
Order.objects.prefetch_related("lines", "lines__product")
```

That's 3 queries total regardless of N: orders, lines, products.

### Prefetch objects

`Prefetch` lets you control the inner queryset and store it under a custom attribute:

```python
from django.db.models import Prefetch

open_lines = LineItem.objects.filter(cancelled=False).select_related("product")

orders = (
    Order.objects
    .filter(tenant_id=7)
    .select_related("customer")
    .prefetch_related(
        Prefetch("lines", queryset=open_lines, to_attr="open_lines")
    )
)
for o in orders:
    for line in o.open_lines:          # plain list, no query
        line.product.sku               # joined in the prefetch query
```

`to_attr` stores a list, which is faster than going through the related manager and makes it obvious in code that the data is pre-filtered.

## In practice

### The trap: filtering after prefetch

```python
orders = Order.objects.prefetch_related("lines")
for o in orders:
    o.lines.filter(cancelled=False)     # new query per order, prefetch ignored
    o.lines.all()                       # uses prefetch cache
```

Any method that changes the query (`filter`, `exclude`, `order_by`, `count()` on a filtered set) bypasses the prefetch. Push the filter into a `Prefetch` queryset instead.

### only and defer

Wide tables (JSONB payloads, text bodies) make every row expensive. Trim columns:

```python
Invoice.objects.only("id", "number", "total", "customer__name") \
    .select_related("customer")

Invoice.objects.defer("raw_payload", "pdf_html")
```

> **Gotcha:** accessing a deferred field later triggers one query **per instance**. `only()` is a new N+1 waiting to happen if a template or serializer reads another field. Pair it with a query-count test.

### Counts and sums: annotate instead of looping

```python
from django.db.models import Count, Sum

Order.objects.annotate(
    line_count=Count("lines"),
    total_qty=Sum("lines__quantity"),
)
```

One query with `GROUP BY` instead of `o.lines.count()` per order. See [Annotations, aggregates and subqueries]({{ '/python-django/annotate-aggregate-subquery/' | relative_url }}) for the double-counting trap when you annotate across two relations.

### Detecting N+1

**django-debug-toolbar** (development): the SQL panel shows query count, duplicated queries ("12 similar queries") and the stack trace for each. Look for the same `SELECT ... WHERE id = %s` repeated.

**Logging all SQL** (development):

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django.db.backends": {"handlers": ["console"], "level": "DEBUG"},
    },
}
```

Only emits when `DEBUG=True`.

**Tests** (the part that keeps it fixed):

```python
from django.test import TestCase

class OrderListQueries(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = TenantFactory()
        OrderFactory.create_batch(30, tenant=cls.tenant, lines=3)

    def test_order_list_constant_queries(self):
        self.client.force_login(UserFactory(tenant=self.tenant))
        with self.assertNumQueries(6):
            resp = self.client.get("/api/orders/")
        assert resp.status_code == 200
```

Create enough rows (more than 1) that an N+1 would change the count. With pytest-django the equivalent fixtures are `django_assert_num_queries` and `django_assert_max_num_queries`.

**Production**: APM tools (Sentry Performance, Datadog, New Relic) flag "N+1 query" spans automatically by detecting repeated similar queries in one transaction. Also watch queries-per-request as a metric.

**Libraries**: `nplusone` and `django-zeal` can raise on lazy loads in tests.

### A fix, step by step

1. Reproduce with realistic data volume, count queries.
2. Read the repeated SQL in the toolbar; identify the relation.
3. FK/one-to-one: add to `select_related`. Reverse FK/M2M: `prefetch_related` or `Prefetch`.
4. Per-row aggregates: `annotate`.
5. Re-count; lock in with `assertNumQueries`.
6. Run `EXPLAIN ANALYZE` on the remaining queries; the `IN (...)` and join queries need indexes on the FK columns (Django creates them for `ForeignKey` by default).

## Pitfalls

- **select_related on a to-many path.** Not allowed; Django raises `FieldError`. Use prefetch.
- **select_related chains of 6+ joins.** One query, but wide rows and harder plans. Sometimes splitting into a prefetch is faster. Measure.
- **Prefetching too much.** `prefetch_related("lines")` on 5000 orders with 50 lines each loads 250k objects. Paginate first, prefetch the page.
- **Model methods and properties.** `def total(self): return sum(l.amount for l in self.lines.all())` looks innocent and is an N+1 in every list view. Make it use annotations or require prefetching.
- **Signals and `save()` in loops.** A `post_save` handler that queries runs per row in a loop of saves. Use `bulk_create`/`bulk_update` and handle side effects in batch.
- **Tests with one row.** N+1 with N=1 is invisible to `assertNumQueries`.

## Checklist

- Every list endpoint has a query-count test with N > 1.
- FK/one-to-one: `select_related`. Reverse/M2M: `prefetch_related` with `Prefetch` when filtered.
- Never `filter()` a prefetched relation in a loop.
- Aggregates per row via `annotate`, not Python loops.
- Paginate before prefetching.
- `only()`/`defer()` only with a test guarding later attribute access.

## Interview angle

**What is N+1 and how do you fix it?** One query for a list plus one per item for a relation. Fix with `select_related` (JOIN) for single-valued relations and `prefetch_related` (second query with `IN`) for multi-valued ones, then guard with `assertNumQueries`.

**Why not always use select_related?** It only works for single-valued relations, and many joins create wide, duplicated rows and complex plans.

**How do you find N+1 in production?** APM traces showing repeated identical queries per request, queries-per-request metrics, and slow endpoints with high DB time but fast individual queries.
