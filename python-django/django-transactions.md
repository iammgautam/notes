---
title: Transactions in Django
section: python-django
order: 11
summary: Autocommit, atomic and savepoints, select_for_update and its options, on_commit for Celery tasks, ATOMIC_REQUESTS, and the bugs each one prevents or causes.
---

A multi-tenant billing system breaks in specific ways without transactions: an invoice marked paid with no payment row, a Celery task that runs before the row it needs is committed, two workers both allocating the last unit of stock. Django's transaction API is small, but each piece maps to a concrete PostgreSQL behavior you need to understand to use it safely.

## How it works

### Autocommit by default

Django opens connections in **autocommit** mode: every query is its own transaction and commits immediately. Two `save()` calls are two transactions; a crash between them leaves half the work done.

### atomic

`transaction.atomic()` opens a transaction (`BEGIN`) on entry and commits on normal exit. If an exception propagates out, it rolls back.

```python
from django.db import transaction

@transaction.atomic
def record_payment(invoice_id: int, amount: Decimal) -> Payment:
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    payment = Payment.objects.create(invoice=invoice, amount=amount)
    invoice.amount_paid = F("amount_paid") + amount
    invoice.save(update_fields=["amount_paid"])
    return payment
```

### Nesting means savepoints

An inner `atomic()` inside an outer one creates a **savepoint** (`SAVEPOINT s1`). If the inner block raises, Django rolls back to the savepoint and re-raises; the outer transaction can continue if you catch the exception **outside** the inner block.

```python
with transaction.atomic():
    order = Order.objects.create(tenant=tenant, number=number)
    for line in lines:
        try:
            with transaction.atomic():               # savepoint
                LineItem.objects.create(order=order, **line)
        except IntegrityError:
            log.warning("duplicate line skipped", extra={"order": order.pk})
```

Without the inner `atomic`, the `IntegrityError` leaves the whole transaction in an aborted state: PostgreSQL rejects every following statement with "current transaction is aborted", and Django raises `TransactionManagementError` on the next query.

Options:

- `savepoint=False` on an inner block skips the savepoint (cheaper; an error then marks the outer transaction for rollback).
- `durable=True` (3.2+) asserts the block is outermost. Use it on functions that must really commit when they return, like payment capture.

### select_for_update

Adds `FOR UPDATE` to the `SELECT`, locking the returned rows until the transaction ends. Other transactions that try to lock or update those rows wait.

```python
with transaction.atomic():
    job = (Job.objects
           .select_for_update(skip_locked=True)
           .filter(status="queued", tenant_id=tenant_id)
           .order_by("created_at")
           .first())
    if job:
        job.status = "running"
        job.save(update_fields=["status"])
```

| Option | SQL | Use |
|---|---|---|
| default | `FOR UPDATE` | Serialize changes to specific rows |
| `nowait=True` | `FOR UPDATE NOWAIT` | Fail fast with `DatabaseError` instead of waiting |
| `skip_locked=True` | `FOR UPDATE SKIP LOCKED` | Work queues: each worker grabs a different row |
| `of=("self",)` | `FOR UPDATE OF order` | Lock only the main table when joining with `select_related` |
| `no_key=True` | `FOR NO KEY UPDATE` | Weaker lock that doesn't block inserts of child rows referencing the row by FK |

It must run inside a transaction. The queryset is lazy, so the lock is taken when it's evaluated, not when you build it. Outside `atomic`, evaluating it raises `TransactionManagementError`.

### on_commit

Side effects outside the database (enqueuing Celery tasks, sending emails, publishing events) must not happen if the transaction rolls back, and must not happen **before** the data is visible to other connections.

```python
def create_invoice(order: Order) -> Invoice:
    with transaction.atomic():
        invoice = Invoice.objects.create(order=order, tenant_id=order.tenant_id)
        transaction.on_commit(lambda: render_invoice_pdf.delay(invoice.pk))
    return invoice
```

- If the transaction commits, the callback runs right after `COMMIT`.
- If it rolls back, the callback is discarded.
- Outside any transaction (autocommit), it runs immediately.
- In a savepoint that rolls back, its callbacks are discarded too.
- `robust=True` (4.2+) logs and continues if one callback raises, so later callbacks still run.

Celery 5.4+ adds `task.delay_on_commit(...)` for Django, which does the same thing.

### ATOMIC_REQUESTS

```python
DATABASES = {
    "default": {..., "ATOMIC_REQUESTS": True},
}
```

Wraps every view in `atomic()`. Simple and safe for consistency, but:

- The transaction spans the whole view, including slow external API calls, holding locks and a snapshot that long.
- Middleware and template rendering of `TemplateResponse` run outside it; streaming responses run after commit.
- Opt out per view with `@transaction.non_atomic_requests`.

Most teams leave it off and use explicit `atomic()` in service functions so the transaction boundary is visible in code.

### Isolation levels

PostgreSQL defaults to **READ COMMITTED**: each statement sees data committed before it started. Two transactions can both read "stock = 1" and both decrement. Fix with `select_for_update`, an atomic `F()` update with a condition, a unique constraint, or `REPEATABLE READ`/`SERIALIZABLE` with retry on serialization failure.

```python
updated = (Product.objects
           .filter(pk=pid, stock__gte=qty)
           .update(stock=F("stock") - qty))
if not updated:
    raise OutOfStock(pid)
```

That conditional update is atomic and needs no explicit lock.

## In practice

### Enqueue tasks only after commit

```python
# Bug: worker may run before COMMIT and get DoesNotExist,
# or run for an order that was rolled back.
with transaction.atomic():
    order = Order.objects.create(...)
    send_order_confirmation.delay(order.pk)

# Fix
with transaction.atomic():
    order = Order.objects.create(...)
    transaction.on_commit(lambda: send_order_confirmation.delay(order.pk))
```

Symptom of the bug: intermittent `DoesNotExist` in tasks that disappears when you retry. Note the lambda captures `order`, not `order.pk` at definition; bind the value if the variable is reused in a loop: `on_commit(partial(task.delay, order.pk))`.

### Keep transactions short

Don't call HTTP APIs, LLMs or S3 inside `atomic()`. A transaction that stays open for 30 seconds holds row locks (blocking other writers) and an old snapshot (blocking vacuum). In PostgreSQL this shows up as `idle in transaction` in `pg_stat_activity`. Set `idle_in_transaction_session_timeout` as a safety net.

### Lock ordering

Two transactions locking invoices 1 then 2, and 2 then 1, deadlock. PostgreSQL detects it after `deadlock_timeout` (1 s) and kills one with `deadlock detected`. Always lock in a consistent order:

```python
Invoice.objects.select_for_update().filter(pk__in=ids).order_by("pk")
```

## Pitfalls

- **Catching DB errors inside `atomic` without a savepoint.** The transaction is poisoned; the next query fails.
- **`select_for_update` outside a transaction or never evaluated.** No lock, or an error. Evaluate it (`list()`, `.get()`, `.first()`) inside `atomic`.
- **`select_for_update` with nullable `select_related` joins.** PostgreSQL rejects `FOR UPDATE` on the nullable side of an outer join. Use `of=("self",)`.
- **Tasks enqueued inside transactions.** Use `on_commit`.
- **Tests and on_commit.** `TestCase` wraps each test in a transaction that never commits, so callbacks never run. Use `self.captureOnCommitCallbacks(execute=True)` or pytest-django's `django_capture_on_commit_callbacks`.
- **PgBouncer transaction pooling** is fine with transactions but breaks session-level state (advisory locks with `pg_advisory_lock`, `SET` without `LOCAL`, server-side cursors outside a transaction).

## Checklist

- Wrap multi-write operations in `atomic()` in the service layer.
- `on_commit` for every task, email or event triggered by a write.
- `select_for_update` (or conditional `F()` updates) for read-modify-write on shared rows.
- `skip_locked=True` for DB-backed job queues.
- No network calls inside transactions; lock rows in a consistent order.
- Nested `atomic()` around statements whose errors you intend to catch.

## Interview angle

**What happens when you nest `atomic`?** The outer block is the real transaction; inner blocks are savepoints that roll back independently if their exception is caught outside them.

**Why use `on_commit` for Celery tasks?** So the task runs only if the data committed, and only after it's visible to the worker's connection.

**How do you prevent two workers from processing the same job?** `select_for_update(skip_locked=True)` inside a transaction, then mark it running, or a conditional `UPDATE ... WHERE status='queued'` and check the row count.

**`select_for_update` vs `F()` update?** `F()` handles single-statement arithmetic atomically. `select_for_update` is for read-decide-write logic that needs the current row values in Python.
