---
title: Idempotency
section: backend
order: 23
phase: "Phase 3 · API design"
summary: Make retries safe with idempotent methods, idempotency keys that store and replay responses, unique-constraint upserts, and deduplicating consumers.
---

A client sends `POST /payments`. The server charges the card and commits, then the response is lost to a load balancer timeout. The client cannot tell "never happened" from "happened, reply lost", and its only useful move is to retry. A retry is only safe if doing the operation twice has the same effect as doing it once. That property is idempotency. Every layer of a modern stack retries: browsers, HTTP client libraries, service meshes, Celery with `acks_late`, Kafka consumers after a rebalance. Any write path that is not idempotent will eventually double-charge, double-email or double-ship.

```text
client                       server                        database
  | POST /payments  -------->  |                                |
  | Idempotency-Key: k1        | claim k1, INSERT payment,      |
  |                            | store response, COMMIT ------> |
  |      x  response lost (reset, timeout, LB 504)              |
  |                            |                                |
  | POST /payments  -------->  | k1 completed, same body        |
  | Idempotency-Key: k1        | -> replay stored 201           |
  | <------- 201 (replayed)    | no second charge               |
```

## Idempotent methods

RFC 9110 defines two separate properties. A method is **safe** if the client does not request any state change (read-only semantics). It is **idempotent** if the intended effect on the server of N identical requests is the same as the effect of one.

| Method | Safe | Idempotent | Notes |
|---|---|---|---|
| GET, HEAD, OPTIONS | yes | yes | Safe implies idempotent |
| PUT | no | yes | Replace the whole resource with this representation |
| DELETE | no | yes | Second call may return 404; the state is the same |
| POST | no | no | Needs an idempotency key or natural dedupe |
| PATCH | no | not guaranteed | Depends on the patch document |

What this means in practice:

- **State, not response.** `DELETE /files/9` returning 204 and then 404 is idempotent: after either call the file is gone.
- **Incidental effects don't count.** Access logs, metrics and rate-limit counters change on every call; that's fine.
- **It is a promise your code must keep.** A `PUT /carts/42/items/7` that increments the quantity is not idempotent whatever the method says. Send absolute values (`{"quantity": 3}`), never deltas.
- **PATCH varies.** A JSON Merge Patch that sets fields behaves idempotently. A JSON Patch `add` to `/items/-` appends every time.
- **Infrastructure relies on it.** HTTP libraries and proxies retry idempotent methods on connection failures; urllib3's `Retry`, for example, only retries idempotent methods by default and leaves POST alone.

A cheap trick for creates: let the client choose the ID. `PUT /orders/0192f0c2-...` with a client-generated UUID (v7 for index locality) turns "create" into an idempotent upsert keyed by that ID. It suits mobile and offline clients well. You must validate the ID format and scope it by tenant.

## Idempotency keys

When the operation is a POST with no natural unique key, the client generates a unique key per logical operation and sends it in a header, conventionally `Idempotency-Key`. Stripe popularized the pattern and the IETF httpapi working group has a draft standardizing the header. The server records the key with the outcome and, on a repeat, returns the stored outcome instead of executing again.

The client rule is the one people get wrong: **one key per user intent**, generated before the first attempt and reused for every retry of that attempt. A fresh key per retry silently disables the whole mechanism.

Server flow:

1. Claim `(scope, key)` atomically by inserting a row marked `in_progress`.
2. If the row already exists: different fingerprint returns 422, still in progress returns 409, completed replays the stored response.
3. Run the handler. In the same database transaction, store the response and mark the key completed.
4. If the handler fails and the transaction rolls back, release the claim so a retry can run.

### Storage

- **Same database as the business data.** The business write and "key completed with this response" must commit atomically. With the key in Redis and the payment in Postgres, a crash between the two commits leaves a payment with no key record, and the retry charges again.
- Redis is fine as a fast in-flight guard in front, never as the only record for money-moving operations.
- **Columns:** scope, key, request fingerprint, status, owner token, lease expiry, response status, body and selected headers (`Location`), and created or expiry time. Unique constraint on `(scope, key)`.
- **Scope by tenant and API key or user, never globally.** Two clients that both send `1` as a key must not collide, and a global namespace would replay one tenant's response to another, which is a data leak.
- Cap the key length (255 is common) and validate its charset at the boundary.

### Request fingerprint

Store a hash of method, path and the canonicalized body (sorted keys, no insignificant whitespace) with the key. The same key with a different fingerprint means a client bug: executing it is wrong, and replaying the old response would lie. Reject it with 422, which is what the IETF draft suggests. Leave volatile fields such as client timestamps and tracing headers out of the fingerprint, or genuine retries get rejected.

### Concurrent duplicates

Double-clicks and short client timeouts produce a second request while the first is still running. Check-then-insert races: both requests `SELECT`, both see nothing, both execute. The unique constraint is the lock. `INSERT ... ON CONFLICT DO NOTHING` lets exactly one request win the claim.

In Postgres, a second inserter of the same key blocks until the first transaction commits or aborts. Commit the claim in its own short transaction, not inside the long business transaction, or the duplicate hangs for the whole request. The loser returns `409 Conflict` with `Retry-After` (the draft's suggestion), or polls briefly for the result. It never executes.

A process that crashes after claiming leaves an `in_progress` row behind. Give claims a lease (`locked_until`) so another request can take over after it expires. Takeover is only safe when:

- the first attempt's database effects commit in the same transaction as the completion, so either both happened or neither did;
- the stale owner is fenced: completion updates `WHERE owner = <my token>` and rolls back if no row matches;
- external calls (payment provider, email API) receive the key or a key derived from it, so a takeover hits the provider's dedupe instead of charging twice.

For multi-step requests with several external calls, a well-known write-up of Stripe-style keys on Postgres adds "recovery points": the key row records the last completed step, and a retry resumes from there.

### TTL

Keys are kept for a bounded window that must outlive the longest client retry horizon. Stripe documents that keys may be pruned once they are at least 24 hours old. Mobile clients with offline queues can retry days later, so set the TTL from your real clients, not a default. After expiry the same key executes as new; document that. Purge with batched deletes on an indexed `created_at`, or partition by day and drop old partitions, which avoids delete and vacuum churn (see [partitioning]({{ '/postgresql/partitioning/' | relative_url }})).

### Replaying the stored response

- Store status, body and the headers that matter, and replay them unchanged. Mark replays with a header such as `Idempotent-Replayed: true` (Stripe's name) to make debugging easy.
- **Store definite outcomes:** success, and deterministic business failures such as 402 card declined or 409 out of stock. The operation had a result; replaying it is correct.
- **Do not store** validation failures that happened before execution, or failures where the transaction rolled back. Release the key so a corrected or repeated request can run. Stripe takes a stricter line and replays even a 500 once execution started, on the grounds that a partial external effect should not be blindly re-run. Pick a policy per endpoint and document it.
- A replay returns the original answer even if the resource changed since (refunded, cancelled). That's correct: it answers that request.

### Idempotency store (runnable)

SQLite stands in for Postgres here; the claim, fencing token, lease takeover, TTL and replay logic carry over unchanged.

```python
import hashlib
import json
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE idempotency_keys (
    scope        TEXT NOT NULL,      -- tenant or API key: never global
    key          TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    owner        TEXT NOT NULL,      -- fencing token for the current claim
    status       TEXT NOT NULL,      -- in_progress | completed
    locked_until REAL NOT NULL,
    expires_at   REAL NOT NULL,
    resp_status  INTEGER,
    resp_body    TEXT,
    PRIMARY KEY (scope, key)
);
CREATE TABLE payments (id INTEGER PRIMARY KEY, scope TEXT, amount INTEGER);
"""


def fingerprint(method, path, body):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method} {path} {canonical}".encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, db, clock, ttl=24 * 3600, lease=30):
        self.db, self.clock, self.ttl, self.lease = db, clock, ttl, lease

    def claim(self, scope, key, fp):
        """Return (owner, None) if we own the key, else (None, response)."""
        now, owner = self.clock(), uuid.uuid4().hex
        with self.db:
            # Expired keys and abandoned claims (lease ran out) are free again.
            self.db.execute(
                "DELETE FROM idempotency_keys WHERE scope=? AND key=? AND "
                "(expires_at<=? OR (status='in_progress' AND locked_until<=?))",
                (scope, key, now, now))
            won = self.db.execute(
                "INSERT INTO idempotency_keys (scope, key, fingerprint, owner,"
                " status, locked_until, expires_at) VALUES (?,?,?,?,"
                " 'in_progress',?,?) ON CONFLICT DO NOTHING",
                (scope, key, fp, owner, now + self.lease, now + self.ttl),
            ).rowcount == 1
            if won:
                return owner, None
            old_fp, status, code, body = self.db.execute(
                "SELECT fingerprint, status, resp_status, resp_body"
                " FROM idempotency_keys WHERE scope=? AND key=?",
                (scope, key)).fetchone()
        if old_fp != fp:
            return None, (422, {"code": "idempotency_key_reused"}, {})
        if status == "in_progress":
            return None, (409, {"code": "request_in_progress"},
                          {"Retry-After": "1"})
        return None, (code, json.loads(body), {"Idempotent-Replayed": "true"})

    def complete(self, scope, key, owner, status, body):
        n = self.db.execute(
            "UPDATE idempotency_keys SET status='completed', resp_status=?,"
            " resp_body=? WHERE scope=? AND key=? AND owner=?",
            (status, json.dumps(body), scope, key, owner)).rowcount
        if n != 1:  # our lease expired and another request took over
            raise RuntimeError("lost idempotency claim")

    def release(self, scope, key, owner):
        with self.db:
            self.db.execute("DELETE FROM idempotency_keys WHERE scope=? AND"
                            " key=? AND owner=?", (scope, key, owner))

    def handle(self, scope, key, method, path, body, handler):
        owner, stored = self.claim(scope, key, fingerprint(method, path, body))
        if stored:
            return stored
        try:
            with self.db:  # business write and stored response commit together
                status, resp = handler(self.db, scope, body)
                self.complete(scope, key, owner, status, resp)
        except Exception:
            self.release(scope, key, owner)  # rolled back: let a retry run
            raise
        return status, resp, {}


def create_payment(db, scope, body):
    cur = db.execute("INSERT INTO payments (scope, amount) VALUES (?, ?)",
                     (scope, body["amount"]))
    return 201, {"payment_id": cur.lastrowid, "amount": body["amount"]}


if __name__ == "__main__":
    now = [1000.0]
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    store = IdempotencyStore(db, clock=lambda: now[0])
    count = lambda: db.execute("SELECT count(*) FROM payments").fetchone()[0]
    post = lambda key, body, h=create_payment: store.handle(
        "tenant-1", key, "POST", "/payments", body, h)

    first = post("k1", {"amount": 500})
    assert first == (201, {"payment_id": 1, "amount": 500}, {})
    replay = post("k1", {"amount": 500})                 # client retry
    assert replay[:2] == first[:2]
    assert replay[2] == {"Idempotent-Replayed": "true"} and count() == 1
    assert post("k1", {"amount": 999})[0] == 422         # same key, new body

    fp = fingerprint("POST", "/payments", {"amount": 7})
    owner_a, _ = store.claim("tenant-1", "k2", fp)       # A is in flight
    assert post("k2", {"amount": 7})[0] == 409           # concurrent duplicate

    def crashes(db, scope, body):
        create_payment(db, scope, body)
        raise TimeoutError("died after the insert")
    try:
        post("k3", {"amount": 50}, crashes)
    except TimeoutError:
        pass
    assert count() == 1                                  # insert rolled back
    assert post("k3", {"amount": 50})[0] == 201 and count() == 2

    now[0] += 31                                         # A's lease expires
    assert post("k2", {"amount": 7})[0] == 201           # B takes over
    try:
        with db:
            store.complete("tenant-1", "k2", owner_a, 201, {})
        raise AssertionError("stale owner must be fenced off")
    except RuntimeError:
        pass

    now[0] += 24 * 3600                                  # TTL passed
    assert post("k1", {"amount": 500})[2] == {} and count() == 4
    print("ok")
```

## Natural idempotency via unique constraints and upserts

Often the domain already has a key that makes duplicates impossible: one charge per invoice, one row per provider event, one membership per (user, team). Then you don't need a key table at all. A database constraint is race-free and protects every code path, including admin scripts, Celery retries and backfills, not just the HTTP endpoint.

```sql
-- One charge per invoice, however many times the job runs
INSERT INTO charges (invoice_id, amount_cents, provider_ref)
VALUES ($1, $2, $3)
ON CONFLICT (invoice_id) DO NOTHING
RETURNING id;            -- no row back: it already existed; SELECT it

-- Upsert with absolute values is idempotent; count = count + 1 is not
INSERT INTO inventory (sku, warehouse_id, on_hand, version)
VALUES ($1, $2, $3, $4)
ON CONFLICT (sku, warehouse_id) DO UPDATE
SET on_hand = EXCLUDED.on_hand, version = EXCLUDED.version
WHERE inventory.version < EXCLUDED.version;   -- ignore stale duplicates

-- A state transition as a guarded update
UPDATE orders SET status = 'paid', paid_at = now()
WHERE id = $1 AND status = 'pending';  -- 0 rows: already paid, or not payable
```

In Django, `get_or_create` and `update_or_create` are only race-safe when a unique constraint covers the lookup fields. With the constraint, `get_or_create` catches the `IntegrityError` and re-fetches; without it, two concurrent calls create two rows.

| Approach | Good for | Cost |
|---|---|---|
| Idempotency-Key table | Arbitrary POSTs, public APIs | Extra table, TTL sweep, replay logic |
| Unique key + `ON CONFLICT` | "One X per Y" business rules | Needs a real business key |
| Client-generated ID + PUT | Creates from mobile or offline clients | Clients must generate IDs |
| Guarded state transition | pending to paid, open to closed | You must model states |
| Version or sequence check | Reordered or replicated updates | A version per entity |

## Idempotent consumers

Brokers deliver at least once. Kafka redelivers after a rebalance or a crash before the offset commit, SQS after the visibility timeout, Celery with `acks_late` when a worker dies mid-task. "Exactly-once processing" end to end is at-least-once delivery plus an idempotent consumer. Kafka transactions give exactly-once only for read-process-write loops that stay inside Kafka; a write to Postgres or a call to an email API needs your own dedupe. See [delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}).

Three patterns, often combined:

1. **Inbox table.** Insert `(consumer, message_id)` under a unique constraint in the same transaction as the side effect, and ack or commit the offset only after that transaction commits. A duplicate hits the constraint and is skipped.
2. **Naturally idempotent handler.** Upsert by entity ID with absolute values, as above.
3. **Version check.** Apply an event only if `event.version > entity.version`. This handles duplicates and reordering at once.

Dedupe on a producer-assigned event ID carried in the payload or a header, not on the broker offset: a producer retry can write the same event twice at two different offsets. Keep inbox rows at least as long as a message can be redelivered, including consumer-group resets. When the side effect leaves your database (sending an email), the dedupe row and the effect cannot be atomic; pass the event ID to the provider as its idempotency key, or accept a rare duplicate. On the producer side, pair this with a transactional outbox (see [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }})).

## In practice

### Postgres claim and sweep (illustrative)

```sql
CREATE TABLE idempotency_key (
  tenant_id     bigint      NOT NULL,
  key           text        NOT NULL CHECK (length(key) <= 255),
  fingerprint   bytea       NOT NULL,
  owner         uuid        NOT NULL,
  status        text        NOT NULL DEFAULT 'in_progress',
  locked_until  timestamptz NOT NULL,
  response_code smallint,
  response_body jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);
CREATE INDEX ON idempotency_key (created_at);

-- Claim in its own short transaction; RETURNING tells you if you won
INSERT INTO idempotency_key (tenant_id, key, fingerprint, owner, locked_until)
VALUES ($1, $2, $3, $4, now() + interval '30 seconds')
ON CONFLICT (tenant_id, key) DO NOTHING
RETURNING owner;

-- Sweep in batches from a periodic job
DELETE FROM idempotency_key
WHERE ctid IN (SELECT ctid FROM idempotency_key
               WHERE created_at < now() - interval '24 hours' LIMIT 5000);
```

### Deduplicating consumer in Django (illustrative)

```python
from django.db import IntegrityError, transaction

def handle_invoice_paid(event):
    with transaction.atomic():
        try:
            with transaction.atomic():  # savepoint keeps the outer txn usable
                ProcessedEvent.objects.create(
                    consumer="billing", event_id=event["event_id"])
        except IntegrityError:
            return  # duplicate delivery, already applied
        Invoice.objects.filter(
            pk=event["invoice_id"], status="open"
        ).update(status="paid")
    # ack the message or commit the offset only after this returns
```

## Pitfalls

- **New key per retry.** Generated inside the retry loop instead of outside it. Symptom: duplicates despite "having idempotency". Fix: generate per user intent and persist it with the client's pending operation.
- **Key store separate from the data.** Redis key, Postgres write, crash in between, double execution. Keep them in one transaction.
- **Global key namespace.** Cross-tenant collisions and leaked responses. Scope by tenant and credential.
- **No lease on claims.** One crash leaves the key `in_progress` forever and every retry gets 409. Add `locked_until` and a fencing token.
- **TTL shorter than the retry horizon.** An offline client retries after three days and executes again. Measure how long your clients actually retry.
- **Volatile fields in the fingerprint.** A client timestamp in the body makes every retry a 422.
- **Not propagating the key downstream.** Your endpoint is idempotent, the call to the payment provider is not.
- **Idempotent but stale.** A delayed duplicate of an old update overwrites a newer one. Use versions, not just dedupe.
- **`get_or_create` without a unique constraint.** Works in tests, creates duplicates under concurrency.

## Interview questions

**What's the difference between safe and idempotent?**
Safe means no state change is requested (GET, HEAD). Idempotent means repeating the request leaves the same state as doing it once (PUT, DELETE too). Every safe method is idempotent; not every idempotent method is safe.

**DELETE returns 404 the second time. Is it still idempotent?**
Yes. Idempotency is about the server state after the requests, not identical responses. After one or ten deletes, the resource is gone.

**Design idempotency for a payments API.**
Client sends an `Idempotency-Key` per payment intent. Server claims `(tenant, key)` with an insert under a unique constraint, stores a fingerprint, runs the charge, and stores the response in the same transaction as the payment row. Same key and body replays; different body returns 422; in flight returns 409. Keys expire after at least 24 hours, and the key is passed to the downstream processor.

**Two requests with the same key arrive at the same time. What happens?**
Both try to insert the claim; the unique constraint lets one win. The other gets `409` with `Retry-After`, or waits for the stored result. Neither a `SELECT` check nor an application lock is needed.

**Why not keep idempotency keys in Redis?**
The key record and the business write must commit atomically. Across two stores there is always a crash window that causes a double execution. Redis can sit in front as an optimization only.

**How do you get exactly-once processing from Kafka into Postgres?**
You don't get exactly-once delivery. You get at-least-once delivery plus an idempotent consumer: write an inbox row keyed by the producer's event ID in the same transaction as the effect, and commit the offset after the transaction.

**When do you not need an idempotency key?**
When the domain has a natural unique key (one charge per invoice, a provider's event ID), when the client can choose the resource ID and use PUT, or when the change is a guarded state transition.

## Further reading

- RFC 9110, *HTTP Semantics*, section 9.2 "Common Method Properties" (safe and idempotent methods).
- IETF httpapi working group draft, "The Idempotency-Key HTTP Header Field".
- Stripe API reference, "Idempotent requests".
- Brandur Leach, "Implementing Stripe-like Idempotency Keys in Postgres".
- *Designing Data-Intensive Applications*, Martin Kleppmann, chapter 11 "Stream Processing" (idempotence and exactly-once).
