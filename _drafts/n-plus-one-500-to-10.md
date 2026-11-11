---
title: "From 500 queries per request to 10"
summary: "Finding N+1 queries in Django and fixing them with select_related, prefetch_related, Prefetch and only(), then locking it in with assertNumQueries."
tags: [django, postgresql, performance]
---

At MarsDevs I cut the number of database queries on some code paths from 500+ down to roughly 5–10. Nothing exotic was involved. It was the N+1 problem, fixed with `select_related`, `prefetch_related` and fetching only the fields that were actually needed.

N+1 is the most common performance bug in Django apps, and it hides well, because each individual query is fast. This post is how I find it, how I fix it, and how I stop it from coming back.

## The problem

The Django ORM is lazy. Accessing a foreign key or a reverse relation on a model instance quietly runs a query if that data is not already loaded. In a loop, that turns one query for a list into one query for the list plus one per row, per relation.

> **TODO (author):** describe the kind of endpoint or page where this showed up (a list view, a report, an export), in generic terms, and how it was noticed.

The cost is rarely the database's CPU. It is round trips. Five hundred queries at a millisecond or two each, plus network latency and ORM object construction for each one, adds up to a slow page that looks perfectly healthy in any single-query view of the database.

## A simplified example

These models are illustrative, not from a real codebase:

```python
class Customer(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField()
    notes = models.TextField(blank=True)

class Product(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()

class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20)

class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
```

And a view that looks innocent:

```python
def order_list(request):
    orders = Order.objects.filter(status="open")[:100]
    data = []
    for order in orders:                        # 1 query
        data.append({
            "id": order.id,
            "customer": order.customer.name,    # +1 per order
            "items": [
                {"product": item.product.name,  # +1 per item
                 "qty": item.quantity}
                for item in order.items.all()   # +1 per order
            ],
        })
    return JsonResponse({"orders": data})
```

For 100 orders with 3 items each that is 1 + 100 + 100 + 300 = **501 queries**. Nothing in this code looks like a database call except the first line.

DRF serializers make it even easier to hide: a nested serializer or a `SerializerMethodField` that touches a relation runs per object, and the view code never mentions it.

## Finding it

You cannot fix what you are not counting. The tools I reach for:

- **django-debug-toolbar** in development. The SQL panel shows the query count and highlights duplicated and similar queries. A list page with hundreds of near-identical `SELECT ... WHERE id = %s` lines is the N+1 signature.
- **`CaptureQueriesContext`** when you want numbers in a shell or test:

    ```python
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        client.get("/orders/")
    print(len(ctx.captured_queries))
    for q in ctx.captured_queries[:5]:
        print(q["sql"])
    ```

- **Logging `django.db.backends`** at `DEBUG` locally, which prints every query. Noisy, but nothing hides.
- **APM or tracing in production**, where a request span with hundreds of child DB spans is hard to miss.

> **TODO (author):** say which of these actually surfaced the problem for you, and the before query count on a specific (anonymized) endpoint.

## Fixing it

### select_related for forward foreign keys

`select_related` follows `ForeignKey` and `OneToOneField` with a SQL `JOIN`, so the related object arrives in the same row:

```python
orders = Order.objects.filter(status="open").select_related("customer")
```

That removes the 100 customer queries. Use it for single-valued relations; joining a many-valued relation would multiply rows.

### prefetch_related for reverse and many-to-many relations

`prefetch_related` runs one extra query per relation using `WHERE ... IN (...)` and stitches the results together in Python:

```python
orders = (
    Order.objects.filter(status="open")
    .select_related("customer")
    .prefetch_related("items__product")
)
```

Now `order.items.all()` and `item.product` come from the prefetch cache. The query count no longer depends on how many orders there are.

### Prefetch objects for control

The string form prefetches everything with default querysets. A `Prefetch` object lets you shape the inner query: filter it, order it, join inside it, and trim its columns.

```python
from django.db.models import Prefetch

items_qs = (
    OrderItem.objects
    .select_related("product")
    .only("id", "order_id", "quantity", "product__id", "product__name")
)

orders = (
    Order.objects.filter(status="open")
    .select_related("customer")
    .only("id", "status", "customer__id", "customer__name")
    .prefetch_related(Prefetch("items", queryset=items_qs))
)
```

Three queries total: orders joined to customers, items joined to products, and that is it. (The exact count depends on how you split the joins, but it is constant.)

`to_attr` is worth knowing too: `Prefetch("items", queryset=..., to_attr="open_items")` stores the result as a plain list on the instance, which is handy when you prefetch a filtered subset and do not want it confused with the full relation.

### only() and values() to fetch less

The other half of the CV line is "fetching only needed fields". A list endpoint that renders a name and a status does not need a `TextField` of notes or a large JSON blob on every row.

- `only(...)` / `defer(...)` keep model instances but load fewer columns.
- `values(...)` / `values_list(...)` skip model instances entirely, which is cheaper still when you are just building JSON.

> **Gotcha:** `only()` can make things worse. Touch a deferred field on each instance and Django runs one query per row to load it, which is a brand new N+1. Only defer what the code path truly never reads.

### Aggregate in the database

A related pattern: `len(order.items.all())` or a Python `sum` over a relation, just to show a count or total. `annotate` does it in one query:

```python
from django.db.models import Count, Sum

orders = Order.objects.annotate(
    item_count=Count("items"),
    total_qty=Sum("items__quantity"),
)
```

> **Gotcha:** combining several `Count`/`Sum` annotations across different multi-valued relations multiplies join rows and inflates the numbers. Use `Count(..., distinct=True)` or subqueries when you aggregate over more than one relation.

## Common ways a prefetch silently stops working

- **Filtering the related manager:** `order.items.filter(quantity__gt=1)` ignores the prefetch cache and queries again. Filter in the `Prefetch` queryset instead.
- **Model properties and methods** that query relations, called from a template or serializer.
- **Serializer changes:** someone adds a nested field, the view's queryset is not updated, and the count jumps.
- **Slicing or re-querying after prefetch:** a new queryset starts with an empty cache.

## Guarding with assertNumQueries

The fix is easy to undo by accident, so the last step is a test that fails when the query count grows. The trick is to create enough rows that an N+1 would be obvious, and assert a number that does not depend on N:

```python
from django.test import TestCase

class OrderListQueryCountTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_orders(count=20, items_per_order=3)  # your factory

    def test_query_count_is_constant(self):
        with self.assertNumQueries(3):
            response = self.client.get("/orders/")
        self.assertEqual(response.status_code, 200)
```

With pytest-django the same idea is the `django_assert_num_queries` fixture. If authentication, sessions or middleware add their own queries, include them in the number or log in with a setup that keeps them stable.

A stronger version runs the endpoint with 5 rows and with 50 and asserts the counts are equal. That catches N+1 without hard-coding a number that breaks every time someone adds a legitimate query.

> **TODO (author):** mention whether you added query-count tests on this project, and if so what the typical asserted count was.

## Results

On the code paths I worked on, queries dropped from **500+ to roughly 5–10**, by eliminating N+1 patterns with `select_related` and `prefetch_related` and fetching only the fields that were needed.

> **TODO (author):** if you measured response time before/after for the same endpoints, add it here. Do not reuse numbers from a different change.

## What I would do differently

- **Add query-count tests the day an endpoint ships,** not after it gets slow. They cost a few lines and catch most regressions in review.
- **Put the queryset next to the serializer.** When a serializer declares which relations it needs (a classmethod that returns the optimized queryset, for example), the two cannot drift apart.
- **Look at query count in code review** for any change that adds a relation to an API response.

The general lesson: in an ORM-heavy codebase, count queries before you profile anything else. A single slow query shows up in the database's slow log. Five hundred fast ones show up only as a slow page.
