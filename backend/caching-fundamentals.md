---
title: Caching fundamentals
section: backend
order: 27
phase: "Phase 4 · Caching"
summary: Hit-ratio math, where caches live, what is worth caching, HTTP caching headers, and the limits of process-local caches like lru_cache.
---

A cache keeps a copy of data closer to the reader than its source, so most reads skip the slow or expensive path. In a typical Django and PostgreSQL service that means fewer queries per request, lower p50 latency, and a database sized for the miss traffic instead of the full read load. The price is that you now hold two copies of the truth, and every caching decision is a decision about how stale and how wrong you are willing to be, and for how long.

## Caching fundamentals

Caching works because access patterns are skewed. A small fraction of keys (the active tenants, the home page, the current pricing plan) receives most reads. Most real workloads look roughly Zipfian: the k-th most popular item gets traffic proportional to `1/k^s`. That skew is why a cache holding 1–10% of the data can serve well over half of the reads.

Application caches mostly exploit temporal locality (what was read now will be read again soon); CPU cache lines and database pages also exploit spatial locality.

### Hit ratio math

With hit ratio `h`, cache lookup time `t_cache` and origin time `t_origin`, a cache-aside read costs on average:

```text
E[latency] = t_cache + (1 - h) * t_origin
origin QPS = total QPS * (1 - h)
```

With `t_cache = 0.5 ms` and `t_origin = 20 ms`:

| Hit ratio | Mean latency | Origin load at 50k QPS |
|---|---|---|
| 0% (no cache) | 20 ms | 50,000 QPS |
| 90% | 2.5 ms | 5,000 QPS |
| 99% | 0.7 ms | 500 QPS |
| 99.9% | 0.52 ms | 50 QPS |

Read the miss ratio, not the hit ratio. Going from 90% to 99% looks like a 10% improvement but cuts origin load tenfold. The flip side is just as important: a cache at 99% that drops to 90% (a flush, a deploy that changes key formats, a node failure) sends ten times more traffic to a database that was sized for the old load. That is how a cache outage becomes a database outage.

Means also hide the tail. If 1% of requests miss and each request touches 20 cache keys, about 18% of requests (`1 - 0.99^20`) hit the origin at least once, so p99 latency tracks origin latency even at a "99% hit ratio".

### Where caches live

```text
 client          edge            your infrastructure          storage
+---------+    +-------+    +---------+    +---------+    +-----------+
| browser |--->|  CDN  |--->| reverse |--->|   app   |--->| Postgres  |
| HTTP    |    |  PoP  |    | proxy   |    | process |    | shared    |
| cache,  |    | cache |    | nginx / |    | L1 dict |    | buffers + |
| app DB  |    |       |    | Varnish |    |         |    | OS cache  |
+---------+    +-------+    +---------+    +----+----+    +-----------+
                                                |
                                           +----v-----+
                                           | Redis /  |
                                           | Memcached|
                                           +----------+
```

| Layer | Typical hit latency | Shared by | Who can invalidate |
|---|---|---|---|
| Browser / mobile HTTP cache | ~0 (no network) | one user | nobody, only expiry |
| CDN edge | ~10–50 ms from user | all users near a PoP | you, via purge API |
| Reverse proxy | ~1 ms inside the DC | all users | you |
| In-process (dict, `lru_cache`) | ~100 ns–1 µs | one worker process | that process only |
| Redis / Memcached | ~0.2–1 ms round trip | all app servers | you |
| DB buffer pool / page cache | inside the query | all queries | the database |

Each layer closer to the user saves more work on a hit and is harder to invalidate. That is the central trade-off of the whole topic.

### What to cache

Good candidates share four traits: read far more often than written, expensive to produce, reused by many requests, and tolerant of some staleness.

- Reference and configuration data: plans, feature flags, permission matrices, country lists.
- Expensive derived results: dashboard aggregates, search facets, rendered fragments, serialized API responses.
- Slow or paid external calls: a third-party API with rate limits, LLM embeddings keyed by a hash of the input text.
- Session data and token introspection results, with TTLs shorter than the token lifetime.

Poor candidates:

- Data that must be exact at the moment of a decision: account balances, inventory at checkout, idempotency records. Read those from the source of truth.
- Long-tail data with low reuse: each entry is written once and evicted before a second read.
- Write-heavy keys, where invalidation traffic exceeds read savings.
- Values that are cheaper to compute than to serialize, send and deserialize.

What you cache also matters. Caching raw rows is easy to invalidate (one row, one key) but saves little. Caching the final serialized response saves the most work but depends on many rows, so invalidating it correctly is harder. Pick the highest level whose dependencies you can still enumerate.

Key design is part of correctness. A key must include every input that changes the output: tenant, user or role if the output is personalized, locale, API version, and a schema version for the cached shape, for example `t:42:invoice:9001:v3`. In a multi-tenant system, a key missing the tenant id is a data leak, not a performance bug. See [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}).

### Latency and cost wins

Take a read path at 30k QPS peak where one PostgreSQL replica serves about 5k of these queries per second. Without a cache you need six or more replicas. At a 95% hit ratio the database sees 1.5k QPS: one replica plus one for failover. A single Redis node handles on the order of 100k simple operations per second, so a small primary with a replica absorbs the rest, and RAM is far cheaper than database CPU.

The catch is that the cache is now load-bearing. You must plan for a cold cache (after restart or failover) and a degraded one, because the database can no longer survive the full load alone. [Cache stampede and hot keys]({{ '/backend/cache-stampede/' | relative_url }}) covers that failure mode.

## Client-side vs server-side caching

Client-side caches live on the user's device or in a shared cache between the user and you (CDN, corporate proxy). They save the network round trip entirely, but you control them only through response headers, and once a response is cached you cannot reach in and delete it. Server-side caches live in your infrastructure: you control size, key, and invalidation, but every request still crosses the network.

| | Client-side (browser, app, CDN) | Server-side (proxy, app, Redis) |
|---|---|---|
| Saves | network round trip, origin work | origin work only |
| Control | headers only | full |
| Invalidation | expiry, or change the URL; CDN purge | explicit delete, TTL, events |
| Scope | private (one user) or shared | shared across users |
| Risk | stale data you cannot recall | memory cost, consistency bugs |

### HTTP caching: Cache-Control

HTTP caching is defined in RFC 9111. `Cache-Control` is the main lever:

| Directive | Meaning |
|---|---|
| `max-age=N` | fresh for N seconds in any cache |
| `s-maxage=N` | overrides `max-age` for shared caches (CDN, proxy) |
| `private` | only the end user's cache may store it |
| `public` | shared caches may store it, even with `Authorization` |
| `no-cache` | may store, but must revalidate before every use |
| `no-store` | must not store at all (secrets, PII responses) |
| `must-revalidate` | once stale, never serve without revalidating |
| `immutable` | will not change while fresh; skip revalidation on reload |
| `stale-while-revalidate=N` | serve stale up to N s while refreshing in background |
| `stale-if-error=N` | serve stale up to N s if the origin errors |

`no-cache` does not mean "do not cache"; `no-store` does. If a response has no explicit freshness but has `Last-Modified`, caches may apply heuristic freshness (commonly 10% of the time since last modification), so API responses without `Cache-Control` can be cached in ways you did not intend.

### Validators: ETag and Last-Modified

Validators make revalidation cheap. The server sends `ETag: "abc123"` (an opaque version tag) or `Last-Modified: <date>`. When the cached copy is stale, the client sends `If-None-Match: "abc123"` or `If-Modified-Since: <date>`. If nothing changed, the server replies `304 Not Modified` with no body.

```http
GET /api/invoices/9001 HTTP/1.1
If-None-Match: "a91f3c"

HTTP/1.1 304 Not Modified
ETag: "a91f3c"
Cache-Control: private, no-cache
```

A 304 still costs a round trip and usually a cheap lookup on the server, but it saves serialization, bandwidth, and client parsing. `Last-Modified` has one-second resolution, so ETags are more precise. A weak ETag (`W/"abc"`) means "semantically equivalent", and nginx downgrades strong ETags to weak when it gzips a response on the fly. `Vary` tells caches which request headers select different representations; `Vary: Accept-Encoding` is normal, `Vary: User-Agent` fragments a shared cache into near-uselessness.

The standard recipe for web assets: fingerprinted file names (`app.3f9a1c.js`) with `Cache-Control: public, max-age=31536000, immutable`, and the HTML that references them with `no-cache` plus an ETag. You never invalidate an asset; you ship a new URL.

### Browser and mobile caches

Browsers keep an HTTP cache that obeys these headers, plus the programmable Cache API used by service workers for offline support. Mobile HTTP stacks do the same if enabled: OkHttp on Android has an HTTP cache that honors `Cache-Control`, and `URLCache` plays that role on iOS. Mobile apps also keep their own data caches (SQLite, Room, Core Data) for offline use, which you invalidate through app logic such as sync tokens or a version field in API responses. Old app versions live for years, so client-side caching rules effectively become part of your API contract.

### Server-side caches

On the server you choose the layer: a reverse proxy cache (nginx `proxy_cache`, Varnish) for whole responses, an application cache (Redis, Memcached) for objects and fragments, or database-level structures such as materialized views. These are covered in [Redis and Memcached]({{ '/backend/redis-and-memcached/' | relative_url }}) and [Caching patterns]({{ '/backend/caching-patterns/' | relative_url }}).

## In-memory caching

A process-local cache is a data structure inside your application process: a dict, `functools.lru_cache`, `cachetools.TTLCache`, or Django's `LocMemCache`. A hit is a hash lookup with no network, no serialization, and no extra failure mode. It is the fastest cache you can have and the hardest to keep consistent.

### functools.lru_cache

`functools.lru_cache(maxsize=128, typed=False)` memoizes a function by its arguments, evicting least recently used entries past `maxsize`. `functools.cache` (Python 3.9+) is the unbounded version. Useful details:

- `f.cache_info()` returns hits, misses, maxsize and current size; export it as a metric.
- `f.cache_clear()` is the only invalidation, and it clears everything.
- Arguments must be hashable, and the cache returns the same object every time. Mutating a returned dict mutates the cached value for every caller.
- There is no TTL. Data cached at startup stays until eviction or restart.
- The cache stays internally consistent under threads, but two threads missing the same key at once may both call the function.
- On a method, `self` becomes part of the key, so the cache keeps every instance alive until evicted. Use `functools.cached_property` for per-instance values instead.

### Multi-process issues

Production Python runs many processes: Gunicorn or uWSGI workers per pod, many pods, plus Celery workers. Each process has its own in-memory cache, which causes four problems:

1. **Duplicated memory.** 20 pods with 4 workers is 80 copies of the cache.
2. **Lower hit ratio.** Each copy warms independently, so a key misses up to 80 times before it is warm everywhere.
3. **Invisible invalidation.** Deleting a key in the process that handled the write leaves 79 stale copies.
4. **Inconsistent reads.** Consecutive requests from one user land on different workers and see different versions.

Use process-local caches for data that is immutable or tolerates seconds of staleness: config, feature flags with a 10–30 s TTL, compiled templates, and as a small L1 in front of Redis. If L1 entries must be dropped promptly, broadcast invalidations over Redis pub/sub or use Redis client-side caching; see [Distributed caching and CDNs]({{ '/backend/distributed-caching-and-cdn/' | relative_url }}). Loading data before fork (`gunicorn --preload`) shares pages copy-on-write, but CPython reference counting writes to object headers, so pages gradually get copied; `gc.freeze()` reduces this but does not eliminate it.

## In practice

The runnable demo below checks the hit-ratio formula, measures an LRU cache's hit ratio on a Zipf workload at 1% and 10% of the key space, and implements a conditional GET with ETags.

```python
import hashlib
import random
from collections import OrderedDict
from itertools import accumulate


def effective_latency_ms(hit_ratio, t_cache=0.5, t_origin=20.0):
    # Every request pays the cache lookup; misses also pay the origin.
    return t_cache + (1 - hit_ratio) * t_origin


def lru_hit_ratio(capacity, requests):
    cache, hits = OrderedDict(), 0
    for key in requests:
        if key in cache:
            hits += 1
            cache.move_to_end(key)
        else:
            cache[key] = True
            if len(cache) > capacity:
                cache.popitem(last=False)
    return hits / len(requests)


def zipf_requests(n_keys, n_requests, s=1.0, seed=7):
    rng = random.Random(seed)
    cum = list(accumulate(1 / (k + 1) ** s for k in range(n_keys)))
    return rng.choices(range(n_keys), cum_weights=cum, k=n_requests)


def etag_for(body):
    return '"' + hashlib.sha256(body).hexdigest()[:16] + '"'


def conditional_get(body, if_none_match=None):
    etag = etag_for(body)
    if if_none_match:
        # The header may list several tags; comparison is weak (ignore W/).
        sent = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
        if etag in sent or "*" in sent:
            return 304, {"ETag": etag}, b""
    return 200, {"ETag": etag, "Cache-Control": "private, no-cache"}, body


if __name__ == "__main__":
    assert round(effective_latency_ms(0.90), 2) == 2.5
    assert round(effective_latency_ms(0.99), 2) == 0.7

    reqs = zipf_requests(n_keys=10_000, n_requests=200_000)
    small, large = lru_hit_ratio(100, reqs), lru_hit_ratio(1_000, reqs)
    print(f"LRU hit ratio, 1% of keys: {small:.2f}, 10% of keys: {large:.2f}")
    assert 0.3 < small < large < 0.9  # skew makes a small cache useful

    status, headers, _ = conditional_get(b'{"plan": "pro"}')
    assert status == 200
    status, _, body = conditional_get(b'{"plan": "pro"}', headers["ETag"])
    assert (status, body) == (304, b"")
    assert conditional_get(b'{"plan": "team"}', headers["ETag"])[0] == 200
```

In Django, the same ideas map to built-in tools. This is illustrative (Django 4.0+ for the built-in Redis backend):

```python
# settings.py
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://cache:6379/1",
        "TIMEOUT": 300,
        "KEY_PREFIX": "api",
        "VERSION": 3,  # bump to orphan every key after a shape change
    },
    "local": {  # per-process L1; never rely on it for invalidation
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "TIMEOUT": 30,
    },
}

# views.py
from django.views.decorators.cache import cache_control
from django.views.decorators.http import condition


def invoice_etag(request, invoice_id):
    inv = Invoice.objects.only("id", "updated_at").get(
        pk=invoice_id, tenant=request.tenant
    )
    return f"{inv.id}-{int(inv.updated_at.timestamp())}"


@cache_control(private=True, no_cache=True)
@condition(etag_func=invoice_etag)
def invoice_detail(request, invoice_id):
    ...  # only runs when the client's ETag is missing or stale
```

The ETag function runs a cheap indexed lookup; the expensive serialization runs only on a mismatch. Use the same idea for list endpoints with `max(updated_at)` and a count.

## Pitfalls

- **No `Cache-Control` on API responses.** Heuristic freshness lets browsers and proxies cache them. Set `private, no-cache` or `no-store` explicitly on authenticated endpoints.
- **Shared caching of personalized responses.** A `public` or `s-maxage` header on a response that depends on cookies serves one user's data to another. Default authenticated responses to `private`.
- **Missing tenant or user in the key.** The same leak at the application layer. Build keys in one helper that requires the tenant id.
- **Caching mutable objects in process.** A caller appends to a cached list and every later caller sees the change. Cache immutable values (tuples, frozen dataclasses) or copy on read.
- **No hit-ratio metric.** You cannot tell whether a cache helps. Export hits, misses and evictions per cache and alert on sudden drops.
- **Sizing the database for the cached load.** When the cache empties, the database takes the full load. Know your cold-cache capacity or have a plan to shed load.

## Interview questions

**Q: How do you decide whether something is worth caching?**
Check the read/write ratio, the cost of a miss, how many requests reuse each entry, and how much staleness the business tolerates. High reuse, expensive misses and tolerable staleness make a good candidate; exact-at-decision data such as balances does not.

**Q: Your hit ratio went from 99% to 95%. Why does that matter?**
The miss ratio went from 1% to 5%, so origin load grew fivefold. If the database was sized for 1% misses it may saturate, and latency for every miss rises with it.

**Q: What is the difference between `no-cache` and `no-store`?**
`no-cache` lets a cache store the response but requires revalidation (ETag or Last-Modified) before each use. `no-store` forbids storing it at all.

**Q: How do ETags save work if the client still makes a request?**
A matching `If-None-Match` lets the server return `304` with no body, skipping serialization and transfer. Generate the ETag from a cheap version signal (`updated_at`, a version column) so the check itself is cheap.

**Q: How do you cache static assets "forever" and still deploy changes?**
Put a content hash in the file name and serve it with `max-age=31536000, immutable`. The HTML is served with `no-cache`, so a deploy changes the references and clients fetch the new URLs.

**Q: Why not use `lru_cache` for everything in a Django app?**
Each worker process has its own copy, so memory is duplicated, warm-up is repeated per process, and invalidation reaches only one process. There is also no TTL. It fits immutable or short-lived data, not shared mutable state.

**Q: Where would you put caches for a read-heavy public product page?**
A CDN with a short `s-maxage` and `stale-while-revalidate` for the page, Redis for the product and price objects behind it, and possibly a small in-process L1 for configuration. Price and stock at checkout come from the database.

## Further reading

- RFC 9111, HTTP Caching (IETF, 2022)
- RFC 5861, HTTP Cache-Control Extensions for Stale Content
- MDN Web Docs, "HTTP caching"
- Python documentation, `functools.lru_cache` and `functools.cache`
- Django documentation, "Django's cache framework" and "Conditional View Processing"
