---
title: Design an Amazon e-commerce backend
section: backend
order: 79
phase: "Phase 10 · System design case studies"
summary: Catalog and search, cart, inventory reservation without overselling, the checkout saga, payments, flash sales, order history and recommendations.
---

An online store is mostly a read system with one very sensitive write path. Browsing, search and product pages are 99% of traffic and tolerate slightly stale data. Checkout is a small fraction of traffic but touches money and finite stock, across services that cannot share a transaction. The design question is where you draw that line: cache and denormalize everything up to "Place order", then switch to idempotent, conditional, auditable writes.

## Requirements

### Clarifying questions

- First-party retail, marketplace sellers, or both? Both; sellers own their listings and stock.
- Physical goods only? Yes; digital goods skip inventory and fulfillment.
- Multiple warehouses? Yes; stock is per (SKU, warehouse).
- How strict is "no overselling"? Strict for limited stock (flash sales); for regular stock a rare oversell can become a backorder, but the system should not do it by design.

### Functional

- Browse categories, search with filters, view product pages with price and availability.
- Cart: add, update, remove; persists across devices; guest carts merge on login.
- Checkout: address, shipping option, payment; place order; get confirmation.
- Orders: status tracking, history, cancellation and returns.
- Flash sales: limited quantity at a set start time.
- Recommendations on product and home pages.

### Non-functional

| Property | Target |
|---|---|
| Users | 300M monthly, 60M daily |
| Product page / search latency | p99 under 200 ms / 300 ms |
| Checkout latency | p99 under 1 s excluding the payment provider |
| Availability | 99.99% for browse and cart; checkout may reject, never lose an order |
| Consistency | inventory and payment strongly consistent; catalog and search eventually (seconds) |
| Read/write | about 100:1 |

### Out of scope

Warehouse management, shipping carrier integration, seller onboarding, ads, fraud models.

## Capacity estimates

```text
product views  = 60M x 15 / 86,400          = ~10k/s avg, ~30k/s peak
searches       = 60M x 5 / 86,400           = ~3.5k/s avg, ~10k/s peak
cart writes    = 60M x 1 / 86,400           = ~700/s
orders         = 10M/day / 86,400           = ~115/s avg, ~350/s peak
sale day       = x10 orders                 = ~3.5k/s
flash sale     = 1M users in the 1st minute = ~17k/s on ONE item

catalog        = 500M SKUs x 5 KB           = 2.5 TB
images         = 500M x 5 x 300 KB          = ~750 TB (object store + CDN)
search index   = 500M x 2 KB x 3 copies     = ~3 TB
orders         = 10M x 3 KB                 = 30 GB/day, ~11 TB/year
hot cache      = top 10M products x 5 KB    = 50 GB in Redis
```

Two numbers drive the design. Browse traffic is high but cacheable, so it scales with replicas, caches and the CDN. The flash sale is the opposite: 17k writes per second against a single counter, which no single database row survives.

## API design

```http
GET /v1/search?q=running+shoes&brand=acme&size=42&sort=relevance&cursor=...
200 {"results": [{"productId": "p1", "title": "...", "priceCents": 4999,
                  "availability": "IN_STOCK"}],
     "facets": {"brand": [{"value": "acme", "count": 312}]},
     "nextCursor": "..."}

GET /v1/products/{productId}
GET /v1/cart
POST /v1/cart/items          {"skuId": "s1-42", "qty": 1}
PATCH /v1/cart/items/{skuId} {"qty": 2}

POST /v1/checkout                         Idempotency-Key: 4f0c...
{"cartVersion": 17, "addressId": "a9", "shippingOption": "standard"}
201 {"checkoutId": "chk_77", "totalCents": 10498,
     "reservedUntil": "2026-11-16T18:15:00Z", "lines": [...]}

POST /v1/orders                           Idempotency-Key: chk_77
{"checkoutId": "chk_77", "paymentMethodId": "pm_2"}
202 {"orderId": "o_1001", "status": "PENDING_PAYMENT"}

GET /v1/orders?cursor=...                 (newest first)
GET /v1/orders/{orderId}
POST /v1/orders/{orderId}/cancel
```

Checkout is split in two. `POST /checkout` reprices the cart and reserves stock, so the user sees a firm total and nothing sells out from under them while they type a card number. `POST /orders` runs the saga and returns 202 because payment may take seconds; the client polls or receives a push. Both are idempotent: a double click returns the same checkout or order.

## Data model

| Data | Store | Key / index | Why |
|---|---|---|---|
| Products, SKUs, attributes | Document store or PostgreSQL JSONB | product_id; sku_id | Attributes vary by category |
| Search index | OpenSearch / Elasticsearch | doc per product, facet fields | Full text, facets, ranking |
| Prices | PostgreSQL | (sku_id, region, valid_from) | Auditable, time-bounded promotions |
| Inventory | PostgreSQL, sharded by sku_id | (sku_id, warehouse_id) | Conditional updates, CHECK constraints |
| Reservations | Same shard as inventory | reservation_id; (status, expires_at) | Atomic with stock change |
| Carts | Key-value (DynamoDB-style) | user_id or guest cart_id | Single-key reads and writes, always writable |
| Orders, order_items | PostgreSQL, sharded by user_id | order_id; (user_id, created_at DESC) | Transactions; "my orders" is one shard |
| Order events / outbox | Same shard as orders | (order_id, seq) | Reliable publishing to Kafka |
| Recommendations | Key-value | sku_id, user_id | Precomputed lists |

Inventory and its reservations live on the same shard so a reservation and its stock change commit together. Orders are sharded by `user_id` because the dominant query is a user's own history; the order ID embeds the shard so lookups by ID route without a directory.

## High-level design

```text
 clients ---> CDN (pages, images, cached product fragments)
    |
    v
 +--------+   +----------+  +---------+   CDC   +-----------+
 | API gw |-->| catalog  |->| Kafka   |-------->| search    |
 | + BFF  |   | / PDP    |  | (events)|         | indexer   |
 +--------+   +----------+  +---------+         +-----------+
   |  |  |                    ^     |                 |
   |  |  +--> search ---------)-----)-------> OpenSearch
   |  +-----> cart (KV)       |     +--> fulfillment, email,
   |                          |          analytics, recs
   v                          | outbox
 +---------------+ reserve +-----------+
 | checkout /    |-------->| inventory |  PostgreSQL by sku
 | order saga    |         +-----------+
 | orchestrator  | pay     +-----------+
 |               |-------->| payments  |--> PSP (cards, wallets)
 +---------------+         +-----------+
        |
        v
   orders DB (by user_id)
```

### Catalog and search

Sellers and merchandisers write to the catalog service, the source of truth. Every change emits an event (via change data capture or an outbox) that the search indexer turns into an updated search document, so new listings appear in search within seconds. Search uses BM25 text relevance, then a learning-to-rank model on behavioral signals (clicks, purchases), with facets computed by index aggregations.

The product page is assembled by the BFF from parts with very different freshness:

| Part | Freshness | Cache |
|---|---|---|
| Title, images, description | Changes rarely | CDN and Redis, long TTL, purged on change |
| Price | Minutes | Redis, short TTL |
| Availability | Seconds | Coarse bucket ("in stock", "only 3 left"), 30 s TTL |
| Reviews summary, recommendations | Hours | Precomputed |

The page shows approximate availability; the real check happens at checkout. That one decision keeps the inventory database out of the browse path.

### Cart

A cart is a list of (sku_id, qty, price_seen) keyed by user, read and written as one item in a key-value store. Guest carts live under a cookie ID and merge into the user's cart at login (sum quantities, cap at stock limits). Adding to cart does not reserve stock and does not lock price; checkout reprices and warns if something changed.

Amazon's Dynamo paper (2007) used the cart as its motivating example: "add to cart" should succeed even during failures, with conflicting versions merged later, at the cost that a deleted item can occasionally reappear. Most designs today accept a single leader per cart instead, since one user rarely edits their cart from two places at once.

### Order history

`GET /orders` hits one shard: `WHERE user_id = ? ORDER BY created_at DESC, order_id DESC` with keyset [pagination]({{ '/backend/pagination/' | relative_url }}) on `(created_at, order_id)`. The order detail page joins order data with shipment tracking; build it as a read model updated from order and shipment events rather than joining across services at request time. Orders older than a couple of years move to cheaper storage but stay queryable for returns, invoices and tax.

### Recommendations

Amazon published item-to-item collaborative filtering in 2003: for each item, precompute similar items from co-purchase and co-view data, offline. At request time, recommendations for a user are the neighbors of items they recently viewed or bought, ranked and filtered (in stock, not already bought). Lists are served from a key-value store keyed by `sku_id`, so a product page adds one lookup. If the service is slow, the widget falls back to category bestsellers or disappears; it never blocks the page.

## Deep dives

### Inventory reservation and overselling prevention

Overselling happens when two buyers read "1 left" and both decrement. The fix is to never read-then-write: make the check part of the write.

```sql
-- Illustrative: succeeds only if enough unreserved stock exists
UPDATE inventory
   SET reserved = reserved + :qty
 WHERE sku_id = :sku AND warehouse_id = :wh
   AND on_hand - reserved >= :qty;
-- rowcount 1 = reserved; 0 = not enough stock
```

The row lock serializes concurrent buyers on that row, and a `CHECK (reserved <= on_hand)` backs it up. Stock moves through three states:

- **Reserve** at checkout start with a TTL (10–15 minutes): `reserved += qty`, insert a reservation row.
- **Commit** when payment succeeds: `on_hand -= qty, reserved -= qty`, reservation `COMMITTED`.
- **Release** on payment failure, cancel, or TTL expiry (a sweeper): `reserved -= qty`, reservation `RELEASED`.

Each transition only moves a reservation out of `HELD`, so a duplicate "payment succeeded" event is a no-op. A runnable version on SQLite with 50 concurrent buyers for 10 units:

```python
import os
import sqlite3
import tempfile
import threading
import time

SCHEMA = """
CREATE TABLE inventory (
  sku TEXT PRIMARY KEY,
  on_hand INTEGER NOT NULL,
  reserved INTEGER NOT NULL DEFAULT 0,
  CHECK (reserved >= 0 AND reserved <= on_hand)
);
CREATE TABLE reservations (
  id TEXT PRIMARY KEY,                -- one row per checkout line: idempotent
  sku TEXT NOT NULL,
  qty INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL                -- HELD, COMMITTED, RELEASED
);
"""


def connect(path):
    return sqlite3.connect(path, timeout=30, isolation_level=None)


def reserve(conn, res_id, sku, qty, ttl_s=900, now=time.time):
    """Hold stock for a checkout. Retrying with the same res_id is safe."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM reservations WHERE id = ?",
                           (res_id,)).fetchone()
        if row:
            conn.execute("COMMIT")
            return row[0] == "HELD"
        cur = conn.execute(
            "UPDATE inventory SET reserved = reserved + ? "
            "WHERE sku = ? AND on_hand - reserved >= ?", (qty, sku, qty))
        if cur.rowcount != 1:                      # not enough stock left
            conn.execute("ROLLBACK")
            return False
        conn.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, 'HELD')",
                     (res_id, sku, qty, now() + ttl_s))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def settle(conn, res_id, outcome):
    """outcome COMMITTED (paid: stock ships) or RELEASED (failed/expired).
    Only a HELD reservation moves, so duplicate events are no-ops."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "UPDATE reservations SET status = ? WHERE id = ? AND status = 'HELD' "
        "RETURNING sku, qty", (outcome, res_id)).fetchone()
    if row:
        sku, qty = row
        shipped = qty if outcome == "COMMITTED" else 0
        conn.execute("UPDATE inventory SET reserved = reserved - ?, "
                     "on_hand = on_hand - ? WHERE sku = ?",
                     (qty, shipped, sku))
    conn.execute("COMMIT")
    return row is not None


def release_expired(conn, now=time.time):
    """Sweeper: abandoned checkouts give their stock back."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM reservations WHERE status = 'HELD' AND expires_at < ?",
        (now(),))]
    return sum(settle(conn, i, "RELEASED") for i in ids)


def available(conn, sku):
    return conn.execute(
        "SELECT on_hand - reserved FROM inventory WHERE sku = ?",
        (sku,)).fetchone()[0]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "shop.db")
        setup = connect(path)
        setup.executescript(SCHEMA)
        setup.execute("INSERT INTO inventory VALUES ('ps5', 10, 0)")

        wins = []
        def buyer(n):
            conn = connect(path)
            if reserve(conn, f"chk-{n}", "ps5", 1):
                wins.append(n)
            conn.close()

        threads = [threading.Thread(target=buyer, args=(n,))
                   for n in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wins) == 10 and available(setup, "ps5") == 0  # no oversell

        first, second = wins[0], wins[1]
        assert reserve(setup, f"chk-{first}", "ps5", 1)  # retry: still held
        assert settle(setup, f"chk-{first}", "COMMITTED")    # paid
        assert not settle(setup, f"chk-{first}", "COMMITTED")  # duplicate
        assert settle(setup, f"chk-{second}", "RELEASED")    # payment failed
        assert available(setup, "ps5") == 1

        later = lambda: time.time() + 3600                   # TTL passes
        assert release_expired(setup, now=later) == 8
        on_hand, reserved = setup.execute(
            "SELECT on_hand, reserved FROM inventory").fetchone()
        assert (on_hand, reserved) == (9, 0)
        setup.close()
    print("ok", sorted(wins))
```

The same SQL runs on PostgreSQL (`RETURNING` included); there you would use the default `READ COMMITTED` level, since the conditional `UPDATE` re-checks the predicate after acquiring the row lock. With several warehouses, reserve from the warehouse chosen by a sourcing step (nearest with stock), falling back to the next one when the update matches zero rows.

### Checkout and order saga

Placing an order spans inventory, orders and payments, each with its own database. A [saga]({{ '/backend/distributed-transactions/' | relative_url }}) with an orchestrator that persists its state after every step:

| Step | Action | Compensation |
|---|---|---|
| 1 | Reprice cart, validate address and limits | none |
| 2 | Reserve inventory (done at checkout, TTL 15 min) | Release reservation |
| 3 | Create order `PENDING_PAYMENT` with `checkout_id` unique | Mark `CANCELED` |
| 4 | Authorize payment, idempotency key = order_id | Void the authorization |
| 5 | Commit reservation, order `CONFIRMED` (pivot) | none: forward only after this |
| 6 | Publish `OrderPlaced` via outbox: fulfillment, email | Retried until delivered |

Steps before the pivot can be undone; steps after it must be retried until they succeed. The rules that make it work:

- **Idempotency everywhere.** The order is keyed by `checkout_id` (unique constraint), the authorization by `order_id`, reservations by line ID. A crashed orchestrator resumes from its last persisted step and repeats calls safely. See [idempotency]({{ '/backend/idempotency/' | relative_url }}).
- **Unknown is not failure.** If the payment call times out, the authorization may have succeeded. Query the provider with the same idempotency key before compensating. The reservation TTL must exceed this resolution window, or stock is released for an order that later turns out to be paid.
- **Outbox for events.** The order row and its `OrderPlaced` event commit in one local transaction; a relay publishes to Kafka. No "saved the order but lost the event".

### Payments

For physical goods the common pattern is authorize at order time and capture at shipment, so the customer is not charged for items that never ship; split shipments become partial captures, and cancellations before shipment void the hold. The payment service keeps its own ledger of intents, authorizations, captures and refunds; provider webhooks update it; a daily reconciliation against the provider's settlement report catches anything the webhooks missed. Card data never touches the order system: the client tokenizes with the provider and sends a payment method ID. The details belong in [design a payment system]({{ '/backend/design-payment-system/' | relative_url }}).

### Flash sales

1M people want 1,000 units at 12:00:00. The database row for that SKU would see 17k conditional updates a second, each waiting for the previous commit; row-lock serialization caps that at a small fraction of the load. Layer defenses so most requests never reach it:

1. **Static edge.** The sale page is static, served from the CDN, with a countdown driven by server time. Nothing dynamic loads until the start.
2. **Waiting room.** At the start, users enter a virtual queue and get signed admission tokens at a controlled rate (say 2,000/s). Only token holders can call checkout. Rate limiting, bot checks and a per-account limit apply at the queue, not deep in the stack.
3. **Fast gate in Redis.** Preload stock into Redis and decrement atomically in a Lua script that also enforces one unit per user:

```lua
-- Illustrative. KEYS[1] = sale:{s42}:stock, KEYS[2] = sale:{s42}:buyers
-- ARGV[1] = user id, ARGV[2] = qty
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return -1 end
local left = tonumber(redis.call('GET', KEYS[1]) or '0')
if left < tonumber(ARGV[2]) then return 0 end
redis.call('DECRBY', KEYS[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[1])
return 1
```

The `{s42}` hash tag keeps both keys in one Redis Cluster slot. After the 1,000th success, every request gets a "sold out" answer in under a millisecond.

4. **Async order creation.** Winners are queued; workers create orders and database reservations at a rate the database handles. The database stays the source of truth; if a payment fails, the unit goes back to both the database and the Redis counter.
5. **Split hot rows.** If you skip Redis, spread stock over N bucket rows (`sku_id, bucket`) and reserve from a random bucket, trying others when one is empty. Contention drops by N.

## Scaling and bottlenecks

| Scale | What breaks first | Fix |
|---|---|---|
| 10× browse | Catalog DB reads | Higher cache hit rate, read replicas, CDN for product fragments |
| 10× search | OpenSearch query load | More replicas; cache popular queries for seconds |
| 10× orders | Inventory hot SKUs | Bucketed stock rows, Redis gate, waiting room for known spikes |
| 10× orders | Orchestrator throughput | Partition saga workers by order_id; keep steps small |
| 100× | Order DB storage and writes | More shards by user_id; archive old orders to columnar storage |
| 100× | Kafka consumers lag (search, email) | More partitions; isolate slow consumers in their own groups |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Search cluster down | No search results | Degrade to category browse; cached popular queries |
| Recommendations slow | Widget missing | Timeout plus fallback to bestsellers |
| Payment provider down | Orders cannot be paid | Second provider; or accept the order as `PENDING_PAYMENT` and retry, with reservation TTL extended |
| Orchestrator crash mid-saga | Order stuck | Saga state persisted per step; resume on restart; watchdog for stale `PENDING` orders |
| Sweeper stops | Stock locked by abandoned checkouts | Alert on oldest `HELD` reservation age; availability looks low, not wrong |
| Kafka outage | Events delayed | Outbox rows accumulate and drain when it recovers; nothing lost |

The guiding rule: browse paths fail open (stale or degraded content), money and stock paths fail closed (reject and let the user retry), and nothing accepted is ever lost.

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| When to reserve stock | At add-to-cart | No | Carts are abandoned; stock locked for nothing |
| When to reserve stock | At checkout start, with TTL | Yes | Firm total while paying; abandoned holds expire |
| Oversell prevention | Read then write in app | No | Race between read and write |
| Oversell prevention | Conditional `UPDATE` + CHECK | Yes | The database enforces it atomically |
| Cross-service order | 2PC across services | No | Blocking, poor availability, PSP cannot join |
| Cross-service order | Orchestrated saga with outbox | Yes | Explicit compensations, resumable |
| Availability on product page | Exact, from inventory DB | No | Puts the write-hot DB on the read path |
| Availability on product page | Cached coarse bucket | Yes | Cheap; exact check at checkout |
| Flash sale gate | Database only | No | Hot-row serialization |
| Flash sale gate | Waiting room + Redis counter + async orders | Yes | Rejects losers in the cache layer |

## Interview follow-ups

**Why not reserve inventory when an item is added to the cart?** Most carts are abandoned. Reserving there locks stock for hours and lets a single user hoard. Reserve at checkout with a short TTL instead.

**The payment call timed out. Do you release the stock?** No. Query the provider by idempotency key first. Only release on a definite failure or after the authorization window has passed.

**How do you keep the search index in sync with the catalog?** Change data capture or an outbox from the catalog into Kafka, consumed by an indexer with idempotent upserts by product ID and version. A periodic full reindex into a new index with an alias swap repairs drift.

**Two warehouses have the item. Which one do you reserve from?** A sourcing step picks by delivery promise and cost, usually the nearest with stock. If its conditional update fails, try the next.

**How do you prevent one user from buying all 1,000 units?** Per-account limits enforced at the gate (the Redis buyers set) and in the database (unique `(sale_id, user_id)`), plus bot defenses in the waiting room.

**How does order history stay fast for a customer with 5,000 orders?** Sharded by user_id with an index on `(user_id, created_at DESC, order_id DESC)` and keyset pagination; old orders summarized on the list page and loaded in full only on demand.

**Prices changed while the user was in checkout. What do they pay?** The price locked in the checkout session, within its expiry. After expiry, checkout reprices and asks the user to confirm.
