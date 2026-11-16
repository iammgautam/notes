---
title: Design a URL shortener (Bitly)
section: backend
order: 73
phase: "Phase 10 · System design case studies"
summary: "ID generation with base62 counters, Snowflake IDs and hashes, 301 vs 302 redirects, a click analytics pipeline, caching, aliases, expiry and abuse."
---

A URL shortener looks trivial: store a mapping, return a redirect. The interview is about what happens around that mapping at scale: generating billions of short, unique, non-guessable codes without coordination bottlenecks, serving redirects at six-figure QPS with single-digit-millisecond latency, capturing analytics without slowing the redirect, and keeping the service from becoming a phishing delivery network. It follows the shape from [the design framework]({{ '/backend/design-framework/' | relative_url }}).

## Requirements

### Clarifying questions

- Do users get analytics (clicks by day, country, device, referrer)? Yes, near real time is fine.
- Custom aliases (`sho.rt/spring-sale`)? Yes, for signed-in users.
- Do links expire? Optional per-link expiry; anonymous links default to a long expiry.
- Can a link's destination be edited after creation? Yes for paid accounts; this matters for the redirect type.
- Same long URL shortened twice: same code? Per owner, yes; across owners, no.

### Functional

1. Create a short link for a long URL, optionally with a custom alias and expiry.
2. Redirect `GET /{code}` to the long URL.
3. View click analytics per link.
4. Disable or delete a link; report abuse.

### Non-functional

| Property | Target |
|---|---|
| New links | 50M/day |
| Redirects | 5B/day (read:write = 100:1) |
| Redirect latency | p99 < 20 ms server-side, excluding network |
| Availability | 99.99% for redirects, 99.9% for creation |
| Durability | A created link is never lost |
| Analytics freshness | Minutes; approximate is acceptable |
| Code length | 7 characters for generated codes |
| Security | Codes not trivially enumerable; malicious URLs blocked |

### Out of scope

Billing, team management, QR code rendering, branded domains (mentioned only where they change the design).

## Capacity estimates

**QPS.** 50M links/day ÷ 86,400 s ≈ 580 writes/s, about 1,700/s at a 3× peak. Redirects are 100× that: 5B/day ≈ 58,000/s average and about 175,000/s at peak. ID generation at 1,700/s is trivial for any scheme; the interesting constraints are code length and predictability, not throughput.

**Key space.** Over 5 years: 50M × 365 × 5 ≈ 91B links. Base62 with 6 characters gives 62^6 ≈ 56.8B codes, which isn't enough. Seven characters give 62^7 ≈ 3.52 trillion, so after five years only about 2.6% of the space is used.

**Storage.** Per link: 7-byte code, a long URL averaging 100 bytes but allowed up to 2 KB, owner ID, timestamps, status, plus index and storage-engine overhead, call it 500 bytes. 50M × 500 B = 25 GB/day, about 9 TB/year, about 46 TB over 5 years, about 137 TB with 3× replication. That is too much for one database primary but small for a partitioned key-value store.

**Bandwidth.** A redirect response is mostly headers, around 500 bytes. 58,000/s × 500 B ≈ 29 MB/s ≈ 230 Mbps average, under 1 Gbps at peak. Bandwidth isn't a constraint.

**Cache.** Click traffic is extremely skewed toward recent and viral links. Assume 500M distinct links are clicked per day and cache the hottest 20%: 100M entries × ~250 bytes (code, URL, expiry, Redis overhead) ≈ 25 GB. A small Redis cluster holds it with room to spare.

**Analytics.** 5B click events/day × ~200 bytes ≈ 1 TB/day raw, roughly 100–200 GB/day after columnar compression.

## API design

```http
POST /api/v1/links
Authorization: Bearer <token>
Idempotency-Key: 5f1c2a7e-0d7b-4a51-9f3e-2b8c1d0e6a44
Content-Type: application/json

{
  "long_url": "https://example.com/catalog/shoes?utm_source=mail",
  "custom_alias": "spring-sale",
  "expires_at": "2027-01-01T00:00:00Z"
}
```

```json
{
  "code": "spring-sale",
  "short_url": "https://sho.rt/spring-sale",
  "long_url": "https://example.com/catalog/shoes?utm_source=mail",
  "expires_at": "2027-01-01T00:00:00Z",
  "created_at": "2026-11-16T10:00:00Z"
}
```

Returns `201 Created`; `409 Conflict` if the alias is taken; `422` for an invalid or blocked URL; `429` when rate-limited. The [idempotency]({{ '/backend/idempotency/' | relative_url }}) key makes client retries safe.

| Endpoint | Purpose |
|---|---|
| `GET /{code}` | Redirect: `302` with `Location`, `404` unknown, `410` expired or disabled |
| `GET /api/v1/links/{code}` | Link metadata |
| `PATCH /api/v1/links/{code}` | Change destination or expiry (purges caches) |
| `DELETE /api/v1/links/{code}` | Soft-delete |
| `GET /api/v1/links?cursor=...` | Caller's links, newest first |
| `GET /api/v1/links/{code}/stats?from=&to=&group_by=day,country` | Analytics |

```json
{
  "code": "spring-sale",
  "total_clicks": 182344,
  "unique_visitors_estimate": 120511,
  "series": [
    {"date": "2026-11-15", "clicks": 10423, "top_country": "US"}
  ]
}
```

## Data model

The hot access pattern is a single point lookup by code at up to 175,000/s across about 91B rows, with no joins. That shape fits a partitioned key-value or wide-column store (DynamoDB, Cassandra, ScyllaDB). Conditional writes ("put if not exists") enforce uniqueness of codes and aliases without any locking. PostgreSQL would also work at lower scale, hash-partitioned on code across a dozen or so primaries, but you would be building by hand what the KV store gives you ([SQL vs NoSQL]({{ '/backend/sql-vs-nosql/' | relative_url }})).

**links** (partition key `code`)

| Field | Type | Notes |
|---|---|---|
| `code` | string | Partition key; generated or custom |
| `long_url` | string | Up to 2 KB, normalized |
| `owner_id` | int64 | Null for anonymous |
| `created_at` | timestamp | |
| `expires_at` | timestamp | Nullable; also mirrored to the store's TTL attribute |
| `status` | enum | `active`, `disabled`, `deleted` |
| `is_custom` | bool | |

Secondary index `(owner_id, created_at)` serves "my links" with cursor pagination. An optional dedupe table keyed by `(owner_id, sha256(long_url))` returns an existing code when the same user shortens the same URL again. Global dedupe across owners is a trap: two users would share analytics, expiry and edit rights.

Accounts, API keys and billing live in PostgreSQL. Click events go to a columnar store; ClickHouse is a good fit for "count by day and country for one code". Illustrative schema:

```sql
CREATE TABLE clicks (
    code        String,
    ts          DateTime,
    country     LowCardinality(String),
    device      LowCardinality(String),
    referrer    String,
    visitor_id  UInt64,
    is_bot      UInt8
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (code, ts);
```

## High-level design

```text
      browser                   API client
         |                          |
   +-----v-----+                    |
   | CDN edge  |                    |
   +-----+-----+                    |
         | miss                     |
   +-----v--------------------------v----+
   |             Load balancer           |
   +-----+--------------------------+----+
         | GET /{code}              | /api/v1/*
   +-----v--------+           +-----v-----+  lease  +-----------+
   | Redirect svc |           | Link API  |-------->| ID ranges |
   | + local LRU  |           +-----+-----+  blocks | (etcd/DB) |
   +-+----+-----+-+                 | put-if-absent +-----------+
     |    |     |                   v
     |    |     |  miss   +----------------------+  link.created
     |    |     +-------->| KV store: links      |--> safety scanner
     |    v               | (DynamoDB/Cassandra) |
     | +-------+          +----------------------+
     | | Redis |
     | +-------+
     | click events (async)
     v
 +-------+    +-----------+    +--------------------+
 | Kafka |--->| stream    |--->| ClickHouse, S3 raw |
 +-------+    | processor |    +--------------------+
              +-----------+
```

### Create flow

1. Link API authenticates the caller, checks [rate limits]({{ '/backend/rate-limiting/' | relative_url }}), validates and normalizes the URL, and checks it against a blocklist.
2. If an alias is given, it is validated; otherwise the server takes the next ID from its leased block and encodes it.
3. A conditional put writes the row; on conflict, an alias returns `409` and a generated code retries.
4. A `link.created` event triggers asynchronous safety scanning; the API returns `201`.

### Redirect flow

1. The CDN edge serves the redirect if it has a cached copy (only for links where you accept edge caching).
2. The redirect service checks an in-process LRU, then Redis, then the KV store, and repopulates the caches on the way back.
3. It checks `status` and `expires_at`, returns `302` or `410`, and appends a click event to an in-memory buffer that is flushed to Kafka asynchronously.

## Deep dives

### ID generation: counter, Snowflake, hash

| Approach | Code length | Coordination | Guessable | Same URL → same code | Write-time check |
|---|---|---|---|---|---|
| Counter + base62, leased ranges | 7 | Lease per block | Yes, unless scrambled | No | None |
| Snowflake-style 64-bit ID | 11 | Worker ID assignment | Partly (time-ordered) | No | None |
| Hash of URL, truncated | 7 | None | No | Yes | Every write |
| Random code | 7 | None | No | No | Every write (cheap) |
| Pre-generated key service | 7 | Key service | No | No | At generation |

**Counter + base62.** A central sequence (a PostgreSQL sequence, etcd or ZooKeeper) hands each app server a block of, say, 100,000 IDs. The server encodes each ID in base62 locally, so the central counter sees one request per 100,000 links. A crashed server wastes the rest of its block, which is fine in a 3.5-trillion space. Raw counters are sequential, so anyone can enumerate links by incrementing. Applying a bijection (multiply by a constant coprime to 62^7, modulo 62^7) breaks visible ordering without any chance of collision. That is obfuscation, not security: two consecutive codes reveal the step. If enumeration is a real threat, use a keyed permutation such as a small Feistel network.

**Snowflake-style IDs.** 41 bits of milliseconds since a custom epoch (about 69 years), 10 bits of worker ID (1,024 workers), 12 bits of sequence (4,096 IDs per millisecond per worker). No central coordination at runtime, and the IDs sort by time. But a 63-bit number needs 11 base62 characters, which defeats the purpose of a short link. Snowflake IDs fit better as internal row and event IDs. The operational traps are assigning worker IDs uniquely (lease them from etcd or derive them from a StatefulSet ordinal) and clocks moving backwards after NTP adjustments.

**Hash + collision handling.** Take SHA-256 of the normalized URL, reduce it to 7 base62 characters, and insert with put-if-absent. On a collision with a different URL, append a salt and retry. Hashing gives natural dedupe but costs a read or conditional write on every insert. Collisions also grow with the table: once 91B codes exist, a new code has about a 2.6% chance of hitting one, which means more retries, not failures.

**Choice.** Leased counter ranges with a keyed permutation give 7-character codes, no write-time collision checks, and no hot coordinator. Random codes with a conditional put are an equally defensible, simpler alternative. Snowflake IDs are used for click event IDs.

A runnable demo of base62, the scrambled counter, Snowflake IDs and hash-with-retry:

```python
import hashlib
import threading
import time

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
INDEX = {ch: i for i, ch in enumerate(ALPHABET)}
SPACE = BASE ** 7                       # 3.52 trillion 7-char codes
MULT = 2_176_477_521_739                # ~0.618 * SPACE, coprime with it
MULT_INV = pow(MULT, -1, SPACE)


def encode(n, width=0):
    if n < 0:
        raise ValueError("negative id")
    out = []
    while True:
        n, r = divmod(n, BASE)
        out.append(ALPHABET[r])
        if n == 0:
            break
    return "".join(reversed(out)).rjust(width, ALPHABET[0])


def decode(code):
    n = 0
    for ch in code:
        n = n * BASE + INDEX[ch]
    return n


def scramble(counter_id):   # bijection; obfuscation, not a keyed cipher
    return encode(counter_id * MULT % SPACE, 7)


def unscramble(code):
    return decode(code) * MULT_INV % SPACE


class Snowflake:
    EPOCH_MS = 1_704_067_200_000        # 2024-01-01T00:00:00Z

    def __init__(self, worker_id, clock=lambda: time.time_ns() // 1_000_000):
        if not 0 <= worker_id < 1024:
            raise ValueError("worker_id must fit in 10 bits")
        self.worker_id, self.clock = worker_id, clock
        self.last_ms, self.seq = -1, 0
        self.lock = threading.Lock()

    def next_id(self):
        with self.lock:
            now = self.clock()
            if now < self.last_ms:
                raise RuntimeError("clock moved backwards")
            if now == self.last_ms:
                self.seq = (self.seq + 1) & 0xFFF
                if self.seq == 0:       # 4,096 used this ms: wait
                    while now <= self.last_ms:
                        now = self.clock()
            else:
                self.seq = 0
            self.last_ms = now
            return (((now - self.EPOCH_MS) << 22)
                    | (self.worker_id << 12) | self.seq)


def parse_snowflake(sid):
    return (sid >> 22) + Snowflake.EPOCH_MS, (sid >> 12) & 0x3FF, sid & 0xFFF


def hash_code(url, table, length=7):
    salt = 0
    while True:
        digest = hashlib.sha256(f"{url}#{salt}".encode()).digest()
        n = int.from_bytes(digest[:8], "big") % BASE ** length
        code = encode(n, length)
        existing = table.get(code)
        if existing is None or existing == url:   # put-if-absent
            table[code] = url
            return code
        salt += 1                                 # collision: retry


if __name__ == "__main__":
    assert encode(0) == "0" and encode(61) == "z" and encode(62) == "10"
    assert decode("zzzzzzz") == SPACE - 1
    for n in (1, 125, 10**9, SPACE - 1):
        assert decode(encode(n)) == n

    codes = [scramble(i) for i in range(1, 20_001)]
    assert len(set(codes)) == 20_000 and all(len(c) == 7 for c in codes)
    assert [unscramble(c) for c in codes[:5]] == [1, 2, 3, 4, 5]
    print("scrambled:", codes[:3])

    gen = Snowflake(worker_id=42)
    ids = [gen.next_id() for _ in range(50_000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert parse_snowflake(ids[0])[1] == 42
    assert len(encode(ids[-1])) <= 11
    fixed = lambda: 1_800_000_000_000          # same ms on both workers
    a, b = Snowflake(1, fixed), Snowflake(2, fixed)
    assert len({a.next_id(), a.next_id(), b.next_id()}) == 3

    table = {}
    small = [hash_code(f"https://ex.com/{i}", table, length=2)
             for i in range(2_000)]                 # 3,844 slots: collisions
    assert len(set(small)) == 2_000
    assert hash_code("https://ex.com/7", table, 2) == small[7]   # dedupe
    print("snowflake:", ids[0], "->", encode(ids[0]), "ok")
```

### Redirects: 301 vs 302

| Status | Meaning | Client caching | Analytics | Destination editable |
|---|---|---|---|---|
| `301` | Moved permanently | Browsers may cache indefinitely unless `Cache-Control` limits it | Repeat clicks invisible | Not for clients that cached it |
| `302` | Found (temporary) | Not cached by default | Every click seen | Yes |
| `307` / `308` | Like 302 / 301, method preserved | Same as counterparts | Same | Same |

A `301` reduces load because browsers stop asking, but you lose repeat-click analytics and can never reliably change or disable the link for users who already cached it, including after it is found to be phishing. A `302` costs one request per click and keeps you in control. Default to `302` with `Cache-Control: private, max-age=0`. Offer `301` with a short `max-age` only to customers who ask for permanent semantics. For `GET` redirects, 307 and 308 add nothing.

```http
HTTP/1.1 302 Found
Location: https://example.com/catalog/shoes?utm_source=mail
Cache-Control: private, max-age=0
```

Link-preview bots (chat apps and social sites unfurling links) fetch the URL too. Classify them by user agent and mark them `is_bot` rather than counting them as clicks.

### Analytics pipeline

The redirect must never wait on analytics. The redirect service appends a compact event (event ID, code, timestamp, IP, user agent, referrer) to an in-memory buffer and a background thread batch-produces to Kafka every few milliseconds. If Kafka is unreachable, the buffer spills to local disk; if the disk fills, events are dropped and counted in a metric. The redirect always wins.

Partition Kafka by event ID, not by code, so one viral link can't create a hot partition ([Kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }})). A stream processor (Flink, Kafka Streams, or plain consumers):

1. Enriches each event: GeoIP to country, user-agent parsing to device, bot classification.
2. Deduplicates by event ID within a window, since delivery is at-least-once ([delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }})).
3. Writes raw events to ClickHouse in large batches (thousands of rows per insert) and to S3 as Parquet for reprocessing.
4. Maintains per-minute rollups per code, and HyperLogLog sketches for unique visitors, since exact distinct counts per link per day are expensive and approximate is acceptable.

The stats API reads rollups for long ranges and raw rows only for recent, fine-grained queries.

### Caching

Link popularity follows a steep power law, so caching is the main scaling lever ([caching patterns]({{ '/backend/caching-patterns/' | relative_url }})):

- **In-process LRU** in each redirect pod for the top few thousand codes, with a TTL of a few seconds. This absorbs a viral link without touching Redis.
- **Redis cache-aside** with a TTL of about a day, capped at `expires_at - now`. Expect a hit ratio well above 90%.
- **Negative caching:** cache "not found" for 60 seconds so scanners probing random codes don't reach the KV store.
- **CDN edge caching** of redirects is optional. It cuts latency worldwide but hides clicks from the origin, so analytics would have to come from CDN logs.
- **Stampede protection:** when a hot entry expires, coalesce concurrent misses so one request refills it ([cache stampede]({{ '/backend/cache-stampede/' | relative_url }})).

Invalidation matters for safety, not just freshness: when a link is disabled for abuse or its destination changes, delete the Redis key, broadcast an invalidation to pods (or keep the in-process TTL short enough), and purge the CDN.

### Custom aliases

Validate against `[A-Za-z0-9_-]{4,32}`, reject a reserved list (`api`, `admin`, `login`, `help`, `static`), and screen for profanity and obvious trademark abuse. Aliases and generated codes share one namespace, so both paths use the same put-if-absent write. A generated code that lands on an existing alias simply retries.

### Expiry

Check `expires_at` on every read and return `410 Gone`. Store-level TTLs are cleanup, not enforcement: DynamoDB deletes expired items lazily, sometimes long after expiry, and Cassandra hides expired cells but reclaims space only at compaction. Checking in the read path also lets you return `410` instead of a bare `404`. Never recycle expired codes: old copies of the short link live in emails and PDFs, and a recycled code would silently send those readers somewhere new.

### Abuse prevention

Shorteners are a favorite way to hide phishing and malware URLs.

- Check the destination at creation against a blocklist and a reputation service such as Google Safe Browsing, synchronously if it is fast, asynchronously otherwise.
- Re-scan periodically and when a link's click rate spikes; the content behind a destination can change after creation.
- Show an interstitial warning for suspicious but unconfirmed destinations.
- Rate-limit creation per account, API key and IP; require a CAPTCHA for anonymous creation.
- Refuse destinations that are other shorteners, to avoid chains and loops.
- Rate-limit `404` responses per IP to slow enumeration.
- Provide a report-abuse endpoint that disables a link and purges every cache layer within seconds.

## Scaling and bottlenecks

**At 10× (1.75M redirects/s peak).** Redirect pods scale horizontally; they are stateless. Redis becomes the first pressure point: add shards ([redis and memcached]({{ '/backend/redis-and-memcached/' | relative_url }})), and lean harder on the in-process LRU. The KV store splits partitions automatically, but a single viral code is one partition, which only caching fixes. Kafka needs more partitions and ClickHouse more shards; the analytics pipeline is usually where cost grows fastest.

**At 100×.** Single-region serving no longer meets the latency target worldwide. Run the read path active-active in several regions with a replicated KV store (DynamoDB global tables, Cassandra multi-datacenter), GeoDNS or anycast in front. Keep link creation either homed in one region or give each region its own ID range, so codes never collide without cross-region coordination. Storage grows with writes, not reads; links not clicked in a year can move to a cheaper tier.

**Hot keys.** A link shared by a celebrity can produce tens of thousands of clicks per second. The in-process LRU and CDN absorb it; Redis read replicas help when they don't.

## Reliability and failure modes

| Failure | Behavior |
|---|---|
| Redis down | Fall back to the KV store behind a concurrency limit; in-process LRU keeps the hottest links working |
| KV store partition slow | Serve from caches; time out quickly; return `503` rather than queueing |
| Kafka down | Buffer to local disk, then drop and alert; redirects unaffected |
| ID lease service down | Servers prefetch the next block at 80% use, so they run for hours without it |
| Bad deploy | Canary with synthetic checks against known codes; alert on `5xx` and latency |
| Region loss | DNS failover to a healthy region with replicated link data |
| Accidental deletion | Soft delete plus point-in-time backups |

Monitor redirect latency percentiles, cache hit ratio, `404` rate per IP, safety-scan backlog, and consumer lag on the analytics topic.

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Code generation | Leased counter + keyed permutation | Yes | 7 chars, no write-time checks |
| | Hash of URL | No | Collision checks; dedupe better done per owner |
| | Snowflake as public code | No | 11 chars; used for event IDs instead |
| Redirect | `302` default | Yes | Analytics, editable, revocable |
| | `301` | Opt-in | Permanent semantics for those who want it |
| Link store | KV / wide-column | Yes | Point lookups, 91B rows, conditional writes |
| | Sharded PostgreSQL | No | Works, but manual sharding for no relational benefit |
| Analytics | Async via Kafka | Yes | Redirect latency isolated from analytics |
| | Synchronous counter update | No | Adds a write to every redirect; hot rows |
| Unique visitors | HyperLogLog | Yes | Fixed memory, about 1% error |

## Interview follow-ups

**How do you guarantee uniqueness across many app servers?**
Each server leases a disjoint block of IDs from a central counter, so generated IDs never overlap. The KV store's conditional put is the final guard for aliases and for any random or hashed scheme.

**Why not just hash the long URL?**
Truncating a hash to 7 characters makes collisions routine at billions of rows, so every write needs a check-and-retry. It also forces identical codes for identical URLs, which is wrong across owners.

**How do you stop someone from enumerating all links?**
Non-sequential codes (keyed permutation or random), rate limits on `404`s per IP, negative caching, and longer codes for links marked private.

**301 or 302?**
302 by default: every click reaches you for analytics, and you can change or revoke the destination. A 301 can be cached by browsers indefinitely, which loses analytics and makes disabling a phishing link ineffective for anyone who cached it.

**How do you count unique visitors per link?**
A HyperLogLog sketch per link per day, merged for longer ranges. Exact distinct counts need memory proportional to visitors.

**The analytics pipeline is down for an hour. What happens?**
Redirects continue. Events buffer locally and in Kafka (which retains days of data), then consumers catch up. Deduplication by event ID makes replays safe.

**How would you support branded domains (`go.acme.com/x`)?**
Make the partition key `(domain, code)` so each domain has its own namespace, terminate TLS per domain with automated certificates, and route by `Host` header.

**How do you handle a link whose destination turns malicious after creation?**
Periodic and spike-triggered re-scans, a disable flag checked on every redirect, and an invalidation path that clears in-process caches, Redis, and the CDN within seconds.
