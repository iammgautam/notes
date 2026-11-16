---
title: Cache stampede and hot keys
section: backend
order: 32
phase: "Phase 4 · Caching"
summary: Why one expiring key can take down a database, and the fixes (single-flight, coalescing, XFetch, stale-while-revalidate, jitter, warming) plus hot keys.
---

A cache lets a database serve far more traffic than it could alone, which also means the database can no longer survive without it. A cache stampede (thundering herd, or dog-piling) is what happens when many requests miss the same key at once and all go to the database to rebuild it. Hot keys are the related failure where one key is so popular that the single cache node holding it saturates. Both hit the most popular data, so both hurt the most users.

## Cache stampede

### Why it happens

```text
 "home:feed" expires; 10k req/s; rebuild query takes 200 ms

 req 1   --miss--> SELECT ... (200 ms) ---------> SET home:feed
 req 2   --miss--> SELECT ... (200 ms) --------->
 req 3   --miss--> SELECT ... (200 ms) --------->
   ...      about 2,000 identical queries start in those 200 ms
 req N   --miss--> SELECT ...
          DB CPU saturates, the query now takes 2 s, so 20,000
          requests miss before anyone refills: a feedback loop
```

The arithmetic is `requests per second x rebuild time`: 10k req/s with a 200 ms rebuild means about 2,000 concurrent identical queries. The rebuild slows under that load, which widens the window, which lets more requests miss. A database sized for a 1% miss rate collapses, and because the cache never gets refilled, it does not recover on its own.

Triggers:

- A hot key's TTL expires.
- Many keys expire together because they were loaded together (after a deploy, a warm-up script, or a batch job) with the same TTL. This mass version is often called a cache avalanche.
- A cache node restarts or fails over and comes back empty.
- A deploy changes the key format or version, which is a full flush in disguise.
- Someone runs `FLUSHALL` or a CDN purge-all.

Related terms that interviewers mix in:

| Term | What happens | Main fixes |
|---|---|---|
| Stampede (breakdown) | one hot key expires; many concurrent rebuilds | single-flight, XFetch, stale-while-revalidate |
| Avalanche | many keys expire at once, or a node dies | jittered TTLs, warming, replicas |
| Penetration | requests for keys that exist nowhere miss every time | negative caching, Bloom filter of valid ids, validation |

## Fixes

### Locking and single-flight

Let one caller rebuild and make the rest wait for its result.

- **In process (single-flight):** a per-key in-flight map. The first caller runs the loader; concurrent callers for the same key block on the same future and receive the same result. Go's `singleflight` package is the canonical version; the runnable demo below implements it in Python. It deduplicates only within a process: 200 pods still issue up to 200 rebuilds.
- **Across processes (distributed lock):** `SET lock:<key> <token> NX PX 5000` in Redis. The winner rebuilds and releases the lock with a compare-and-delete script; losers either serve a stale copy (best), poll the cache briefly with backoff, or fail fast. The lock TTL must exceed the rebuild time, or a second rebuild starts while the first is still running. See [Distributed locking]({{ '/backend/distributed-locking/' | relative_url }}) for why such a lock is only advisory.

Locking caps database load at one rebuild per key, but losers wait. If they wait for an empty cache, latency spikes to the rebuild time; pair locking with stale values whenever you can.

### Request coalescing

Coalescing is single-flight implemented by an intermediary instead of your code. A reverse proxy or CDN holds concurrent requests for the same uncached object, sends one request to the origin, and answers all waiters with the response. nginx does this with `proxy_cache_lock`, Varnish coalesces by default, and CDNs collapse requests at the edge and the origin shield. Memcache leases, as publicly described by Facebook, do it inside the cache: the server issues one lease token per key at a time and tells other clients to wait briefly and retry.

### Probabilistic early expiration (XFetch)

Instead of everyone missing at the moment of expiry, let individual requests volunteer to recompute slightly early, with a probability that rises as expiry approaches. Vattani, Chierichetti and Lowenstein ("Optimal Probabilistic Cache Stampede Prevention", VLDB 2015) showed this rule is optimal under their model:

```text
 recompute early if:  now - delta * beta * ln(rand()) >= expiry

 delta  = how long the last recompute took (store it with the value)
 beta   = eagerness, 1.0 by default; > 1 recomputes earlier
 rand() = uniform in (0, 1], so -ln(rand()) is a positive random gap
```

Expensive values (large `delta`) start refreshing earlier; cheap values refresh close to expiry. With high traffic, one request almost always refreshes the key before it expires, so nobody sees a miss and no lock is needed. With low traffic, it degrades to normal expiry, which is harmless because low traffic cannot stampede. Store `delta` and `expiry` alongside the value, for example as fields of a Redis hash.

### Stale-while-revalidate

Keep two expiries: a soft one after which the value is stale, and a hard one (Redis TTL) after which it is gone. Between them, serve the stale value immediately and trigger one background refresh guarded by a lock. Users never wait, the database sees one rebuild, and if the database is down you keep serving stale data until the hard TTL. This is the same idea as the HTTP `stale-while-revalidate` directive and nginx's `proxy_cache_use_stale updating`. Alert on refresh failures, or stale data quietly persists for the whole hard TTL.

### Jittered TTLs

Keys loaded together expire together. Multiply each TTL by a random factor, for example `ttl * random.uniform(0.9, 1.1)`, so expiries spread over a window instead of landing in the same second. It costs one line, and it is the main defense against avalanches after warm-ups and deploys.

### Cache warming

Fill the cache before traffic arrives instead of letting users fill it with misses:

- Before shifting traffic to a new region, cluster or cache node, preload the top N keys from access logs or a list of known-hot entities (active tenants, the home page, top products).
- Rate-limit warming so it does not become its own stampede, and jitter the TTLs it sets.
- When a new key version ships, warm the new keys before switching reads, or roll the change out to a few pods first.
- Facebook publicly described "cold cluster warmup": a new memcache cluster fills misses from a warm cluster's cache instead of the database until its hit ratio recovers.

| Fix | Prevents | Cost |
|---|---|---|
| Single-flight (in process) | duplicate rebuilds per process | waiting callers; per-process only |
| Distributed lock | duplicate rebuilds cluster-wide | lock round trips; waiting; lock TTL tuning |
| Request coalescing | duplicate origin fetches at proxy or CDN | proxy config; waiters |
| XFetch | expiry-time misses on hot keys | a few extra early rebuilds; store delta |
| Stale-while-revalidate | user-facing misses and waits | serving stale data; two TTLs |
| Jittered TTLs | synchronized mass expiry | none |
| Warming | cold-start misses | tooling; knowing what is hot |

## Hot keys and their mitigation

A hot key is a single key with far more traffic than any node can serve: a viral post, a celebrity profile, a global configuration blob, a flash-sale product. Sharding does not help, because all requests for one key go to one shard. Symptoms: one Redis node at 100% CPU or NIC bandwidth while the rest idle, timeouts only for requests that touch certain entities, and latency spikes that follow news or marketing campaigns.

### Detection

- Per-key counters in your cache client wrapper, sampled (for example 1 in 100 requests), exported as top-N metrics.
- `redis-cli --hotkeys`, which needs an LFU `maxmemory-policy` because it reads LFU counters.
- Per-node CPU, network and ops/s dashboards; one outlier node is the tell.
- `MONITOR` for a few seconds as a last resort; it slows the server down considerably.

### Read-hot keys

- **Local L1 cache.** Cache the value in process for 1–5 s. With 200 pods and a 2 s TTL, Redis sees about 100 reads per second for that key regardless of user traffic. This is the most effective fix.
- **Key replication.** Store N copies (`feed:home#0` to `feed:home#7`) that hash to different shards, read a random copy, and update or delete all copies on write. In Redis Cluster, do not put the copies under one hash tag, or they land on the same slot again.
- **Read replicas.** Spread reads for the hot shard across its replicas, accepting replication lag.
- **Move it outward.** If the value is public, serve it from the CDN with a short TTL.
- **Shrink it.** A 500 KB value at 20k req/s is 10 GB/s. Compress it, split it, or cache only the part callers need.

### Write-hot keys

A counter incremented on every like of a viral post serializes on one key (and on one row if it goes to PostgreSQL, where every update takes a row lock).

- **Sharded counters.** Increment a random one of N sub-keys (`likes:post:9:0` to `likes:post:9:15`) and sum them on read, caching the sum briefly.
- **Local aggregation.** Accumulate increments in process and flush with one `INCRBY` every second, which is write-behind from [Caching patterns]({{ '/backend/caching-patterns/' | relative_url }}).
- **Append, then roll up.** Insert like events and aggregate them asynchronously instead of updating a hot row.

## In practice

The first runnable demo starts 50 threads that miss the same key at the same moment. Without single-flight every thread runs the loader; with it, one does:

```python
import threading
import time


class _Call:
    def __init__(self):
        self.done, self.value, self.error = threading.Event(), None, None


class SingleFlight:
    def __init__(self):
        self._lock, self._calls = threading.Lock(), {}

    def do(self, key, fn):
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = self._calls[key] = _Call()
        if leader:
            try:
                call.value = fn()
            except Exception as exc:
                call.error = exc
            finally:
                with self._lock:
                    del self._calls[key]
                call.done.set()
        else:
            call.done.wait()
        if call.error:
            raise call.error
        return call.value


def stampede(use_single_flight, n_threads=50):
    cache, loads, flight = {}, [], SingleFlight()
    start = threading.Barrier(n_threads)

    def loader():
        loads.append(1)
        time.sleep(0.05)  # the slow database query
        return "feed-v2"

    def load():
        if "feed" not in cache:  # re-check: a leader may have just filled it
            cache["feed"] = loader()
        return cache["feed"]

    def request():
        start.wait()
        if "feed" in cache:
            return
        flight.do("feed", load) if use_single_flight else load()

    threads = [threading.Thread(target=request) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(loads)


if __name__ == "__main__":
    naive, guarded = stampede(False), stampede(True)
    print(f"database loads: naive {naive}, single-flight {guarded}")
    assert naive > 10 and guarded == 1
```

The second demo simulates 10 minutes of traffic at 1,000 req/s on one key with a 60 s TTL and a 200 ms rebuild, with no locks. Plain expiry produces a burst of concurrent rebuilds and user-visible misses at every expiry; XFetch refreshes slightly early, about once per cycle, with no misses:

```python
import math
import random


def simulate(policy, seconds=600, rps=1000, ttl=60.0, delta=0.2,
             beta=1.0, seed=11):
    rng = random.Random(seed)
    expiry, ready_at = ttl, None  # value cached at t=0; refresh in flight?
    rebuilds = misses = 0
    for i in range(seconds * rps):
        now = i / rps
        if ready_at is not None and now >= ready_at:
            expiry, ready_at = ready_at + ttl, None  # refreshed value lands
        expired = now >= expiry
        early = (policy == "xfetch" and not expired and
                 now - delta * beta * math.log(1.0 - rng.random()) >= expiry)
        if expired or early:
            rebuilds += 1  # no lock: every volunteer hits the database
            misses += expired
            if ready_at is None:
                ready_at = now + delta
    return rebuilds, misses


if __name__ == "__main__":
    plain_rebuilds, plain_misses = simulate("plain")
    x_rebuilds, x_misses = simulate("xfetch")
    print(f"plain TTL: {plain_rebuilds} rebuilds, {plain_misses} misses")
    print(f"XFetch:    {x_rebuilds} rebuilds, {x_misses} misses")
    assert plain_misses > 1000 and x_misses == 0
    assert x_rebuilds * 20 < plain_rebuilds
```

Stale-while-revalidate with a Redis lock, a soft expiry stored in the value and a jittered hard TTL (illustrative; `release_lock` is the compare-and-delete script from [Redis and Memcached]({{ '/backend/redis-and-memcached/' | relative_url }})):

```python
import json, random, time, uuid
from concurrent.futures import ThreadPoolExecutor

refresher = ThreadPoolExecutor(max_workers=4)


def _fill(key, loader, fresh_s, keep_s):
    value = loader()
    entry = {"value": value, "fresh_until": time.time() + fresh_s}
    r.set(key, json.dumps(entry), ex=int(keep_s * random.uniform(0.9, 1.1)))
    return value


def _locked_fill(key, loader, fresh_s, keep_s):
    token = uuid.uuid4().hex
    if not r.set(f"lock:{key}", token, nx=True, px=5000):
        return None  # someone else is rebuilding
    try:
        return _fill(key, loader, fresh_s, keep_s)
    finally:
        release_lock(keys=[f"lock:{key}"], args=[token])


def get_swr(key, loader, fresh_s=60, keep_s=3600):
    raw = r.get(key)
    if raw is not None:
        entry = json.loads(raw)
        if time.time() >= entry["fresh_until"]:  # stale: refresh in background
            refresher.submit(_locked_fill, key, loader, fresh_s, keep_s)
        return entry["value"]
    for _ in range(40):  # cold miss: one caller rebuilds, others wait
        value = _locked_fill(key, loader, fresh_s, keep_s)
        if value is not None:
            return value
        time.sleep(0.05)
        raw = r.get(key)
        if raw is not None:
            return json.loads(raw)["value"]
    raise TimeoutError(key)  # shed load instead of stampeding the database
```

The same protections at the reverse proxy, as nginx configuration for a micro-cached page (illustrative):

```nginx
proxy_cache_path /var/cache/nginx keys_zone=pages:50m max_size=5g inactive=1h;

location /catalog/ {
    proxy_cache pages;
    proxy_cache_valid 200 10s;
    proxy_cache_lock on;              # one request fills a missing entry
    proxy_cache_lock_timeout 5s;
    proxy_cache_use_stale updating error timeout http_500 http_502 http_503;
    proxy_cache_background_update on; # refresh expired entries in background
    proxy_pass http://django;
}
```

## Pitfalls

- **Lock without a TTL.** The holder crashes and the key is never rebuilt. Always set `PX`, sized above the slowest rebuild.
- **Lock TTL shorter than the rebuild.** A second rebuild starts while the first runs, and the first one's release deletes the second one's lock unless you compare tokens.
- **Losers hammering Redis.** Hundreds of clients polling a lock key in a tight loop create a new hot key. Back off, bound the wait, prefer serving stale.
- **Single-flight only in process.** It dedupes per pod, not per cluster. Combine with a distributed lock, XFetch, or proxy coalescing.
- **Same TTL for keys loaded together.** Warm-ups and deploys create synchronized expiry. Jitter every TTL.
- **Negative cache entries that live too long.** A newly created tenant gets 404s for minutes. Keep negative TTLs short and delete them on create.
- **Hot key copies under one hash tag.** All copies land on the same Redis Cluster slot and node.
- **Stale-while-revalidate hiding a broken refresh.** Users see stale data for the whole hard TTL while refreshes fail silently. Alert on refresh errors and data age.

## Interview questions

**Q: What is a cache stampede and why can it take down a database that handles normal load fine?**
Many concurrent requests miss the same key and all run the same expensive query. The database was sized for the miss rate, not the full load; the burst slows queries, which widens the miss window, which adds more queries, a positive feedback loop that does not recover on its own.

**Q: Locking or probabilistic early expiration?**
Locking guarantees one rebuild per key but makes others wait or serve stale, and adds lock round trips. XFetch needs no coordination and refreshes before expiry so nobody waits, at the cost of occasional duplicate rebuilds. For very hot keys, combine XFetch or stale-while-revalidate with a lock on the refresh.

**Q: Explain the XFetch condition.**
Recompute if `now - delta * beta * ln(rand()) >= expiry`. The random term is an exponentially distributed gap scaled by recompute time, so the probability of refreshing rises sharply as expiry approaches and expensive values refresh earlier.

**Q: How do you prevent many keys from expiring at the same time?**
Jitter TTLs, warm the cache gradually rather than all at once, and avoid deploys that change every key version at once.

**Q: How do you handle a hot key in Redis?**
Detect it with sampled client metrics or `redis-cli --hotkeys`, then add a short-TTL in-process cache, replicate the key across shards with suffixes and read a random copy, or read from replicas. For write-hot counters, shard the counter or aggregate locally and flush with `INCRBY`.

**Q: What is cache penetration and how is it different?**
Requests for keys that do not exist anywhere miss every time, so the cache never helps; it is often abuse. Cache a short-lived "not found" marker, validate ids early, or check a Bloom filter of valid ids before the database.

**Q: How would you bring up a new region without melting its database?**
Warm the regional cache from access logs or a list of hot entities at a limited rate, with jittered TTLs, or fill misses from another region's warm cache, then shift traffic gradually with weighted routing.

## Further reading

- Vattani, Chierichetti and Lowenstein, "Optimal Probabilistic Cache Stampede Prevention", PVLDB 8(8), 2015
- Nishtala et al., "Scaling Memcache at Facebook", NSDI 2013 (leases, cold cluster warmup)
- nginx documentation, `ngx_http_proxy_module` (`proxy_cache_lock`, `proxy_cache_use_stale`)
- Go documentation, `golang.org/x/sync/singleflight`
- RFC 5861, HTTP Cache-Control Extensions for Stale Content
