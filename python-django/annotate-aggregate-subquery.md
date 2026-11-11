---
title: Annotations, aggregates and subqueries
section: python-django
order: 10
summary: annotate vs aggregate, how Django builds GROUP BY, Subquery/OuterRef and Exists, F and Q expressions, and the join-inflation traps that give wrong totals.
---

Dashboards, invoice summaries and "orders with their outstanding balance" lists are where Django code either pushes work into PostgreSQL or drags rows into Python. Expressions let you compute in SQL: one query instead of N, and no 100k-row loops in a worker. They also have one classic failure mode: totals that are silently wrong because two joins multiplied the rows.

## How it works

### aggregate vs annotate

- `aggregate()` is **terminal**. It collapses the queryset to one row and returns a dict.
- `annotate()` adds a computed column **per row** of the queryset and returns a queryset. If the expression is an aggregate, Django adds a `GROUP BY`.

```python
from django.db.models import Count, Sum, Avg, Q

Invoice.objects.filter(tenant_id=7).aggregate(
    total=Sum("amount", default=0),
    open_count=Count("id", filter=Q(status="open")),
)
# {'total': Decimal('48210.00'), 'open_count': 12}

Customer.objects.filter(tenant_id=7).annotate(
    invoice_count=Count("invoices"),
    billed=Sum("invoices__amount", default=0),
)
# each customer gets .invoice_count and .billed
```

`default=0` (Django 4.0+) wraps the aggregate in `COALESCE`, so empty sets return 0 instead of `None`. `filter=Q(...)` compiles to PostgreSQL's `FILTER (WHERE ...)`, giving several conditional counts in one pass.

### How GROUP BY is chosen

- `annotate()` with an aggregate on a model queryset groups by the model's primary key (PostgreSQL allows selecting other columns of that table because they are functionally dependent).
- `values(...)` **before** `annotate()` sets the grouping columns:

```python
(Order.objects.filter(tenant_id=7)
    .values("status")
    .annotate(n=Count("id"), revenue=Sum("total"))
    .order_by("status"))
# SELECT status, COUNT(id), SUM(total) ... GROUP BY status
```

- `values()` **after** `annotate()` just selects columns; grouping stays per object.
- Fields in `order_by()` are added to `GROUP BY` too. `Meta.ordering` no longer affects grouping (since Django 3.1), but an explicit `order_by("created_at")` will split your groups. Clear with `.order_by()` or order by the grouped fields.

### F and Q

- `F("field")` references a column in SQL. Use it for comparisons between columns and atomic updates.
- `Q(...)` builds boolean conditions combinable with `|`, `&`, `~`.

```python
from django.db.models import F, Q

Invoice.objects.filter(amount_paid__lt=F("amount"))           # partially paid
Order.objects.filter(Q(status="open") | Q(priority__gte=3), ~Q(tenant__suspended=True))
Product.objects.filter(pk=pid).update(stock=F("stock") - 1)    # no race
```

### Subquery and OuterRef

`Subquery` embeds a queryset as a scalar (or set) inside another query. `OuterRef` refers to a column of the outer query. Use it to pull "the latest X" or "a per-row aggregate over one relation" without a join.

```python
from django.db.models import OuterRef, Subquery

latest_payment = (
    Payment.objects
    .filter(invoice=OuterRef("pk"))
    .order_by("-paid_at")
    .values("paid_at")[:1]
)
invoices = Invoice.objects.filter(tenant_id=7).annotate(
    last_paid_at=Subquery(latest_payment)
)
```

The subquery must return one column (`values("x")`) and at most one row (`[:1]` or a grouping that guarantees it).

### Exists

```python
from django.db.models import Exists

has_overdue = Invoice.objects.filter(
    customer=OuterRef("pk"), status="open", due_date__lt=today
)
Customer.objects.filter(tenant_id=7).filter(Exists(has_overdue))
Customer.objects.annotate(at_risk=Exists(has_overdue))
```

`EXISTS` stops at the first matching row and never duplicates outer rows, unlike filtering across a reverse FK (`filter(invoices__status="open")`), which joins and can return duplicates that need `.distinct()`.

## In practice

### The join-inflation trap

```python
# WRONG: totals multiplied
Customer.objects.annotate(
    billed=Sum("invoices__amount"),
    paid=Sum("payments__amount"),
)
```

Two reverse-FK joins produce `invoices × payments` rows per customer before grouping. A customer with 3 invoices and 4 payments has every invoice amount counted 4 times. `Count(..., distinct=True)` fixes counts but not sums.

Fix with one subquery per relation:

```python
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

def sum_subquery(model, field, fk="customer"):
    return Coalesce(
        Subquery(
            model.objects
            .filter(**{fk: OuterRef("pk")})
            .order_by()
            .values(fk)
            .annotate(s=Sum(field))
            .values("s"),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        Value(0),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

Customer.objects.filter(tenant_id=7).annotate(
    billed=sum_subquery(Invoice, "amount"),
    paid=sum_subquery(Payment, "amount"),
).annotate(balance=F("billed") - F("paid"))
```

The `.order_by()` clears default ordering so the inner `GROUP BY` stays on the FK; `.values(fk).annotate(...)` groups; the last `.values("s")` returns one column. Each subquery runs per customer, and an index on `invoice(customer_id)` keeps it cheap.

### Conditional expressions

```python
from django.db.models import Case, When, Value, CharField

Invoice.objects.annotate(
    bucket=Case(
        When(due_date__gte=today, then=Value("current")),
        When(due_date__gte=today - timedelta(days=30), then=Value("1-30")),
        default=Value("30+"),
        output_field=CharField(),
    )
).values("bucket").annotate(total=Sum("amount")).order_by("bucket")
```

An aging report in one query.

### alias for filter-only expressions

`alias()` (Django 3.2+) defines an expression usable in `filter`/`order_by` without selecting it:

```python
Customer.objects.alias(n=Count("orders")).filter(n__gte=10)
```

## Pitfalls

- **Wrong sums across multiple to-many joins.** Symptom: totals that are exact multiples of the right value. Use subqueries per relation.
- **Unexpected `GROUP BY` columns.** `order_by` fields and `values()` placement change grouping. Print `qs.query` to check.
- **Mixed types.** `F("amount") * F("quantity")` across `DecimalField` and `IntegerField` may need `output_field=` or `ExpressionWrapper`.
- **Integer division.** `F("paid") / F("total")` on integer columns truncates in PostgreSQL. Cast with `Cast(..., FloatField())` or use decimals.
- **`Subquery` returning more than one row.** PostgreSQL raises "more than one row returned by a subquery used as an expression". Add `[:1]` or proper grouping.
- **Correlated subqueries on large outer sets.** Each runs per outer row. Fine with an index and a paginated outer query; slow over 1M rows. Consider a single grouped query joined in Python, or a rollup table.
- **`distinct()` with ordering.** `distinct()` on a queryset ordered by a related field adds that field to `SELECT DISTINCT`, producing "duplicates".

## Checklist

- Totals: `aggregate()`. Per-row numbers: `annotate()`. Grouped reports: `values().annotate()`.
- Never sum across two to-many relations in one `annotate`.
- `default=0` or `Coalesce` for nullable aggregates.
- `Exists` over `filter(reverse__...)` + `distinct()`.
- Clear ordering (`.order_by()`) in grouped and subquery querysets.
- Check `str(qs.query)` and `EXPLAIN ANALYZE` for anything non-trivial.

## Interview angle

**annotate vs aggregate?** `aggregate` returns a dict summarizing the whole queryset; `annotate` adds a value per row (grouped per object or per `values()` fields) and returns a queryset.

**How do you get "latest payment date per invoice" in one query?** `Subquery` on payments filtered by `OuterRef("pk")`, ordered by date descending, `.values("paid_at")[:1]`.

**Why might `Sum` be wrong when annotating two relations?** The joins form a cross product per parent row, so each value is repeated. Use a subquery per relation.
