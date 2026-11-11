---
title: Race conditions in web apps
section: os-concurrency
order: 5
summary: Spot check-then-act, lost updates and double submits in Django, and fix them with atomic UPDATEs, select_for_update, constraints and idempotency keys.
---

Web apps are concurrent even if your code has no threads: many gunicorn workers, many pods, Celery workers and retrying clients all hit the same rows. Race conditions here don't crash anything; they quietly oversell inventory, double-charge cards, create duplicate accounts, and lose edits. They pass every test because tests run one request at a time.

## How it works

A race exists when correctness depends on the **interleaving** of operations that read and write shared state. In web apps the shared state is the database (and sometimes Redis or an external API). Three shapes cover almost every bug.

### 1. Check-then-act

```python
if not Subscription.objects.filter(tenant=t, plan="pro").exists():   # check
    Subscription.objects.create(tenant=t, plan="pro")               # act
```

Two requests both run the check before either inserts. Both see "no row", both create. Same with "username available?", "coupon unused?", "seats left?".

### 2. Lost update (read-modify-write)

```python
account = Account.objects.get(pk=pk)       # balance = 100
account.balance -= 30                      # in Python
account.save()                             # writes 70
```

```text
T1: read 100 ---------------- write 70
T2:        read 100 ----------------- write 50
final: 50  (T1's debit is lost; should be 20)
```

`save()` also writes **every field**, so a concurrent change to an unrelated field gets overwritten too. Two users editing the same record in the Vue UI is the human-speed version of this.

### 3. Double submit and retries

A user double-clicks "Pay", a mobile client retries after a timeout, a webhook provider redelivers, Celery re-runs a task after a worker crash with `acks_late`. The server receives the same intent twice and acts twice.

### Why `transaction.atomic()` alone doesn't fix it

Postgres' default isolation is **Read Committed**. A transaction sees rows committed before each statement; it does not lock rows it merely reads. Wrapping check-then-act in `atomic()` makes it all-or-nothing, not exclusive. Two transactions can still both read "no row" and both insert.

## In practice

Pick the fix that matches the shape. Cheapest first.

### Atomic UPDATE with an F expression

Let the database do the read-modify-write in one statement; the row lock it takes serializes concurrent updates.

```python
from django.db.models import F

Account.objects.filter(pk=pk).update(balance=F("balance") - 30)
```

```sql
UPDATE accounts SET balance = balance - 30 WHERE id = 42;
```

Add the business rule to the `WHERE` so it is checked and applied atomically:

```python
updated = (Coupon.objects
           .filter(code=code, uses__lt=F("max_uses"))
           .update(uses=F("uses") + 1))
if updated == 0:
    raise CouponExhausted()
```

### Unique constraints for "only one"

The database is the only place that sees all concurrent inserts. Let it enforce uniqueness.

```python
class Subscription(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    plan = models.CharField(max_length=20)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"], condition=models.Q(active=True),
                name="one_active_subscription_per_tenant",
            )
        ]
```

```python
from django.db import IntegrityError, transaction

try:
    with transaction.atomic():
        Subscription.objects.create(tenant=t, plan="pro")
except IntegrityError:
    ...  # someone else won; load and return the existing one
```

`get_or_create` and `update_or_create` are only race-safe when a unique constraint backs the lookup fields; without one they can still create duplicates. With raw SQL, `INSERT ... ON CONFLICT DO NOTHING / DO UPDATE` does this in one statement.

### Pessimistic locking with `select_for_update`

When you must read, decide in Python, then write:

```python
with transaction.atomic():
    order = Order.objects.select_for_update().get(pk=pk, tenant=t)
    if order.status != "pending":
        raise InvalidState()
    order.status = "paid"
    order.save(update_fields=["status", "updated_at"])
```

- The second transaction blocks at `select_for_update` until the first commits, then reads the new state.
- Must be inside `atomic()` (Django raises otherwise).
- `nowait=True` fails immediately if locked; `skip_locked=True` skips locked rows, which makes Postgres a decent job queue: `select_for_update(skip_locked=True).filter(status="queued")[:10]`.
- `of=("self",)` to avoid locking joined rows from `select_related`.
- Lock in a consistent order when locking multiple rows (see [Deadlocks, livelocks and starvation]({{ '/os-concurrency/deadlocks/' | relative_url }})).
- Keep the transaction short. No HTTP calls while holding the lock.

### Optimistic locking with a version column

When conflicts are rare and you don't want to hold locks, especially across user think-time:

```python
updated = (Document.objects
           .filter(pk=pk, version=client_version)
           .update(body=new_body, version=F("version") + 1))
if updated == 0:
    raise Conflict("document was modified, reload and retry")   # HTTP 409/412
```

Expose the version as an `ETag` and require `If-Match` on `PATCH`, so the Vue client sends the version it loaded.

### Idempotency keys for double submits

The client generates a key per intent (UUID) and sends it with the request; the server stores the result and replays it on repeats.

```http
POST /v1/payments
Idempotency-Key: 7f9c2b1e-4a5d-4c8e-9b1f-2e6d0a3c8f11
```

```python
from hashlib import sha256

class IdempotencyRecord(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    key = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    status_code = models.IntegerField(null=True)
    response_body = models.JSONField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "key"], name="uniq_idem_key")]

def create_payment(request):
    key = request.headers["Idempotency-Key"]
    req_hash = sha256(request.body).hexdigest()
    try:
        with transaction.atomic():
            rec = IdempotencyRecord.objects.create(
                tenant=request.tenant, key=key, request_hash=req_hash)
    except IntegrityError:
        rec = IdempotencyRecord.objects.get(tenant=request.tenant, key=key)
        if rec.request_hash != req_hash:
            return Response(status=422)          # same key, different payload
        if rec.status_code is None:
            return Response(status=409)          # first attempt still in flight
        return Response(rec.response_body, status=rec.status_code)

    result = charge_and_record(request)          # the real work
    rec.status_code, rec.response_body = 201, result
    rec.save(update_fields=["status_code", "response_body"])
    return Response(result, status=201)
```

Also pass an idempotency key **downstream** (Stripe and most payment APIs accept one), expire records after 24–72 h, and on the frontend disable the button while a request is in flight. The button alone is not a fix.

### Idempotent consumers

Celery tasks and webhook handlers must tolerate duplicates: record processed event ids with a unique constraint, or make the effect naturally idempotent (`UPDATE ... SET status = 'sent' WHERE status = 'pending'`). Enqueue tasks with `transaction.on_commit` so a worker never runs before the row it needs is visible.

### Stronger isolation

`SERIALIZABLE` makes Postgres detect dangerous interleavings and abort one transaction with a serialization failure (SQLSTATE `40001`). It's correct by construction but requires retry logic everywhere it's used. Usually reserved for a few critical flows.

## Pitfalls

- **Read-modify-write in Python** with `obj.save()`. Use `F()` or `update_fields`.
- **Relying on `exists()` checks** for uniqueness. Use constraints.
- **`select_for_update` outside a transaction** or in autocommit code paths.
- **Locks held across slow I/O**: payment API calls inside a locked transaction stall every request for that row.
- **Caches as truth**: incrementing a counter in Redis and the DB separately; they drift. Pick one source of truth.
- **Testing**: races don't show up in unit tests. Reproduce with threads hitting the endpoint concurrently, or with two `psql` sessions stepping through the statements by hand.

```python
from concurrent.futures import ThreadPoolExecutor

def test_coupon_cannot_be_overused(live_server):
    url = f"{live_server.url}/v1/redeem"
    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda _: requests.post(url, json={"code": "X"}).status_code, range(20)))
    assert codes.count(200) == 5   # max_uses = 5
```

## Checklist

- Counters and balances: `UPDATE ... SET x = x + n` with the rule in `WHERE`.
- "Only one" rules: unique (or partial unique) constraints, catch `IntegrityError`.
- Read-decide-write: `select_for_update` in a short transaction, consistent lock order.
- User edits: version column or ETag, `If-Match`, 409/412 on conflict.
- Creating side effects (payments, emails): idempotency keys stored with a unique constraint.
- Background work: idempotent tasks, `on_commit` enqueueing.

## Interview angle

- **"Two users buy the last item at the same time. How do you prevent overselling?"** A conditional atomic update: `UPDATE inventory SET qty = qty - 1 WHERE sku = ? AND qty >= 1`, check affected rows. Or `select_for_update` on the row inside a transaction. Add a `CHECK (qty >= 0)` constraint as a backstop.
- **"Optimistic vs pessimistic locking?"** Pessimistic locks the row up front and makes others wait; good under high contention and short transactions. Optimistic checks a version at write time and fails on conflict; good for low contention and long user think-times.
- **"How do you make a payment endpoint safe to retry?"** Client-supplied idempotency key, stored with a unique constraint alongside the response, replayed on duplicates, and propagated to the payment provider.
