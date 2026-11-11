---
title: DRF performance
section: python-django
order: 12
summary: Where DRF list endpoints spend time, how nested serializers cause N+1, prefetching in get_queryset, cheaper serializers, and pagination that scales.
---

A DRF list endpoint has two costs: the queries it runs and the Python it executes to turn rows into JSON. The first is usually N+1 hidden in nested serializers and `SerializerMethodField`. The second is the serializer machinery itself, which is noticeable at a few thousand objects per response. Both are fixable without leaving DRF.

## How it works

For `GET /api/orders/` on a `ModelViewSet`:

1. `get_queryset()` builds the queryset (lazy).
2. `filter_queryset()` applies filter backends.
3. `paginate_queryset()` slices it (and for page-number pagination runs `COUNT(*)`).
4. The page is evaluated: one query, plus prefetches.
5. `serializer.data` calls `to_representation()` on each instance. For every field: get the attribute (descriptors, possibly a query), run the field's `to_representation`, build an ordered dict.
6. The renderer converts to JSON.

Step 5 is where the hidden queries live: `source="customer.name"`, a nested `CustomerSerializer`, or a `SerializerMethodField` calling `obj.lines.count()` each fire per row unless the data was loaded in step 4.

## In practice

### Nested serializers need matching prefetches

```python
class LineItemSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="product.sku")

    class Meta:
        model = LineItem
        fields = ["id", "sku", "quantity", "unit_price"]

class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name")
    lines = LineItemSerializer(many=True)
    line_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "number", "status", "customer_name", "lines", "line_count"]

    def get_line_count(self, obj):
        return obj.lines.count()            # query per order
```

For 50 orders with 4 lines each: 1 + 50 (customer) + 50 (lines) + 200 (product) + 50 (count) = 351 queries.

Fix in the view, where the queryset is built:

```python
from django.db.models import Count, Prefetch

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer

    def get_queryset(self):
        return (
            Order.objects
            .filter(tenant=self.request.tenant)
            .select_related("customer")
            .prefetch_related(
                Prefetch("lines",
                         queryset=LineItem.objects.select_related("product"))
            )
            .annotate(line_count=Count("lines"))
            .order_by("-created_at", "-id")
        )
```

And make the serializer read the annotation:

```python
line_count = serializers.IntegerField(read_only=True)
```

Now: orders + `COUNT` for pagination + lines-with-products = 3 queries regardless of page size.

> **Rule of thumb:** the serializer declares what it reads; `get_queryset` must load exactly that. Keep them next to each other, or give the serializer a `setup_eager_loading(queryset)` classmethod that the view calls, so they change together.

### Avoid `len(obj.lines.all())` vs `.count()` confusion

With `lines` prefetched, `obj.lines.all()` and `len(obj.lines.all())` are free; `obj.lines.count()` uses the prefetch cache too. `obj.lines.filter(...)` is not free. Prefer annotations for counts so they don't depend on prefetch state.

### Lock it in with a test

```python
def test_order_list_queries(api_client, django_assert_num_queries, tenant):
    OrderFactory.create_batch(20, tenant=tenant, lines=3)
    # count + orders + lines; add any auth/tenant lookups your middleware does
    with django_assert_num_queries(3):
        resp = api_client.get("/api/orders/")
    assert resp.status_code == 200
```

### Serializer CPU cost

`ModelSerializer` is convenient but does per-field work for every object: field lookup, `source` traversal, validators setup, `OrderedDict` building. Rough orders of magnitude: tens of microseconds per field per object. A 1000-row response with 15 fields and a nested list can spend hundreds of milliseconds in Python after the SQL is done.

Options, cheapest effort first:

1. **Paginate smaller.** 50–100 objects per page is plenty for UIs.
2. **Read-only fields.** For list endpoints, a read-only serializer with plain `serializers.Serializer` and explicit fields skips model-field introspection.
3. **`values()` for hot read paths.** Skip model instances and serializers entirely:

```python
class OrderSummaryView(APIView):
    def get(self, request):
        rows = (Order.objects
                .filter(tenant=request.tenant, status="open")
                .values("id", "number", "total", "customer__name")[:200])
        return Response(list(rows))
```

4. **Faster JSON rendering.** `orjson` via a custom renderer helps with large payloads.
5. **Cache** whole responses or expensive sub-results (see [Caching in Django]({{ '/python-django/django-caching/' | relative_url }})).

Profile first: `django-silk` or `py-spy` show whether time is in SQL, serialization, or rendering.

### Pagination that scales

| Class | SQL | Good for | Problem |
|---|---|---|---|
| `PageNumberPagination` | `COUNT(*)` + `LIMIT/OFFSET` | Small tables, UIs needing page numbers | `COUNT(*)` and deep `OFFSET` get slow on millions of rows |
| `LimitOffsetPagination` | Same | Same | Same |
| `CursorPagination` | `WHERE (created_at, id) < (...) LIMIT n` | Feeds, large tables, infinite scroll | No total count, no jump to page N |

```python
class OrderCursorPagination(CursorPagination):
    page_size = 50
    ordering = ("-created_at", "-id")   # unique, indexed
```

Cursor pagination needs a stable, unique ordering backed by an index, e.g. `(tenant_id, created_at DESC, id DESC)`. For page-number pagination on large tenants, some APIs cap the count or estimate it from `pg_class.reltuples`.

### Writes

- Bulk endpoints: validate with `many=True`, then `bulk_create` in a custom `ListSerializer.create`, instead of one `save()` per item.
- Validators like `UniqueTogetherValidator` run a query per object. For bulk inputs, check uniqueness once with one `IN` query, and rely on a DB unique constraint for correctness.
- Return `201` with a minimal representation instead of re-serializing the whole nested object graph.

## Pitfalls

- **`SerializerMethodField` doing queries.** The most common hidden N+1. Every method field should read only prefetched or annotated data.
- **`source="a.b.c"` chains.** Each hop is a related lookup; each needs `select_related`.
- **Prefetch in the serializer's `to_representation`.** Too late, it runs per object.
- **Different serializers per action.** `get_serializer_class()` switching to a detail serializer for `retrieve` needs a matching `get_queryset` branch on `self.action`.
- **Permission classes that query.** `has_object_permission` hitting the DB per object in a list. Filter the queryset by tenant instead.
- **Browsable API in production timing.** It renders forms and can run extra queries. Benchmark with `?format=json` or disable it.
- **Unbounded `page_size_query_param`.** Set `max_page_size`, or a client asks for 100,000 rows.

## Checklist

- Every list view: `select_related` / `prefetch_related` / `annotate` matching the serializer's fields.
- No queries in `SerializerMethodField`.
- Query-count test per list endpoint with several rows.
- Page size 50–100; `max_page_size` set; `CursorPagination` for large or append-only data.
- `values()` + plain response for hot, read-only summary endpoints.
- Profile to separate SQL time from serialization time.

## Interview angle

**Why is my DRF list endpoint slow?** Usually N+1 from nested serializers or method fields; fix with `select_related`/`prefetch_related`/annotations in `get_queryset`. Then serializer CPU for large pages; paginate and simplify.

**Page number vs cursor pagination?** Page number needs `COUNT(*)` and `OFFSET`, both O(n) on large tables. Cursor uses an indexed keyset predicate, constant cost per page, but no totals or random access.

**Where do you put `select_related` for a ViewSet?** In `get_queryset()`, so every action and the filter/pagination steps build on it.
