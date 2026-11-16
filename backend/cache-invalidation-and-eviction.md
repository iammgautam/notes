---
title: Cache invalidation, eviction and consistency
section: backend
order: 30
phase: "Phase 4 · Caching"
summary: How cached data leaves the cache (TTL, deletes, versioned keys, CDC), which eviction policy to pick, and the races that serve stale data and how to close them.
---

Data leaves a cache in two ways. **Invalidation** removes it because it is no longer correct. **Eviction** removes it because memory is full. Invalidation is a correctness problem: get it wrong and users see old prices or another tenant's settings. Eviction is an efficiency problem: get it wrong and your hit ratio collapses. Consistency is what you can promise about the gap between the cache and the database, and the honest answer for most caches is "bounded staleness", with races you must deliberately close.

## Cache invalidation

### TTL

Every entry gets a time to live; after it, the entry is gone and the next read reloads it. TTL needs no coordination with writers and bounds staleness from every source: missed deletes, races, bugs, writes from systems you forgot about. Choose it from business tolerance, not from performance: "a plan change may take 60 s to show" is a product decision.

- Short TTLs (seconds) fit volatile data and in-process L1 caches.
- Long TTLs (hours) fit reference data, paired with explicit invalidation.
- Add jitter (for example ±10%) so keys loaded together do not expire together.
- Sliding expiry (extend the TTL on each read) keeps hot entries forever, so it needs explicit invalidation too.

### Explicit delete

The writer deletes the affected keys after the database commit, as in cache-aside. It gives near-immediate freshness, but only if every code path that writes the data knows every key derived from it. That breaks down when a row feeds many keys (a product appears in category pages, search results, and recommendations) or when writes come from several services, admin scripts, or migrations. Keep a TTL even with explicit deletes.

### Versioned keys

Instead of deleting, change the key so old entries are never read again and age out by TTL or eviction.

- **Schema version:** `invoice:9001:v3`. Bump it when the cached shape changes, so a deploy never reads the old format. Django's `VERSION` cache setting does this globally.
- **Generation counter:** store `t:42:gen` and build keys as `t:42:g17:report:...`. One `INCR t:42:gen` invalidates every report for tenant 42 at once, which you cannot do with deletes without scanning. The cost is one extra lookup for the generation, which you can cache locally for a second or two.
- **Content-addressed keys:** for immutable derived data, key by a hash of the inputs, for example `emb:sha256(model + text)`. Nothing ever needs invalidating.

### Event-driven invalidation with CDC

Change data capture reads the database's own change log and turns every committed change into an event. For PostgreSQL, Debezium uses logical decoding (the `pgoutput` plugin and a replication slot) and publishes per-table events to Kafka keyed by primary key. An invalidation consumer maps each change to cache keys and deletes them.

```text
 any writer --> PostgreSQL --WAL--> Debezium --> Kafka --> invalidator
 (app, admin,                       (logical      (keyed     |
  migration)                         decoding)     by PK)     v
                                                         DEL keys
                                                         in Redis
```

Why it is attractive: it catches every writer, including ones that know nothing about the cache; events arrive in commit order per key; deletes are idempotent, so at-least-once delivery is fine. Costs: one more pipeline to run, invalidation lag from tens of milliseconds to seconds, and a replication slot that retains WAL on the primary if the consumer stops, which can fill the disk. Facebook publicly described the same idea in "Scaling Memcache at Facebook": daemons tail the MySQL commit log and broadcast deletes to every cache cluster. PostgreSQL `LISTEN`/`NOTIFY` is a lightweight alternative, but notifications sent while a listener is disconnected are lost.

| Method | Freshness | Catches all writers | Cost |
|---|---|---|---|
| TTL only | up to TTL | yes | none |
| Explicit delete | immediate, minus races | only instrumented paths | code in every writer |
| Versioned keys | immediate | only instrumented paths | orphaned keys use memory |
| CDC | pipeline lag | yes | Debezium, Kafka, operations |

## TTL and eviction policies

TTL and eviction answer different questions. TTL says "this entry may be wrong after N seconds". Eviction says "memory is full; which entry is least valuable?" A cache with long TTLs still needs a good eviction policy, and a cache that never fills never evicts.

### LRU, LFU, FIFO and random

| Policy | Evicts | Strength | Weakness |
|---|---|---|---|
| LRU | least recently used | adapts quickly; `O(1)` | one large scan flushes the hot set |
| LFU | least frequently used | keeps a stable hot set; scan resistant | slow to adapt; needs aging |
| FIFO | oldest inserted | no work on hits; lock-friendly | ignores popularity |
| Random | any entry | no metadata at all | can evict a hot key |
| CLOCK | first entry without a recent-use bit | LRU-like with cheap hits | approximate |

LRU is the default because recency predicts reuse for most workloads. Its weakness is scans: a nightly export or a crawler touching 100k one-off keys pushes out everything useful. LFU resists that, but a key that was hot last week keeps its high count unless counts decay. CLOCK is how operating system page caches and PostgreSQL's shared buffers (clock-sweep with a usage count) approximate LRU without moving list nodes on every hit. Recent research (S3-FIFO, SOSP 2023) shows that FIFO queues with a small probationary queue match or beat LRU on many production traces while being cheaper.

### TinyLFU and W-TinyLFU

TinyLFU separates **admission** from eviction. It keeps an approximate frequency for recently seen keys (including keys not in the cache) in a Count-Min sketch with small counters. When a new key arrives and the cache is full, it compares the newcomer's estimated frequency with the eviction victim's and admits the newcomer only if it is more popular. Periodically all counters are halved, so old popularity fades. One-hit wonders never displace useful entries, and the sketch costs a few bits per entry.

**W-TinyLFU**, used by Java's Caffeine, adds a small admission window (about 1% of capacity, LRU) so a burst of new keys can establish itself, followed by the TinyLFU filter, followed by a segmented LRU main area (a probation segment and a protected segment for keys hit again). Caffeine tunes the window size adaptively. The authors' published trace results show hit ratios close to the theoretical optimum across recency-biased and frequency-biased workloads. Go's Ristretto uses a similar TinyLFU admission policy.

### Redis maxmemory-policy

Redis does not keep a global LRU list. Each key stores a 24-bit field. Under LRU policies it is an access clock; on eviction Redis samples `maxmemory-samples` keys (default 5), keeps the best candidates in a small pool, and evicts the oldest. Under LFU policies the same bits hold a last-decay timestamp and an 8-bit logarithmic counter that increments with decreasing probability (`lfu-log-factor`) and decays every `lfu-decay-time` minutes. New keys start with a small nonzero count so they are not evicted immediately.

| If your Redis holds | Use |
|---|---|
| only cache data | `allkeys-lfu` (stable hot set) or `allkeys-lru` |
| cache data plus keys that must not be evicted | `volatile-lfu` or `volatile-lru`; give cache keys a TTL, others none |
| queues, locks, sessions only | `noeviction`, and alert on memory |

Check with `INFO stats` (`evicted_keys`, `keyspace_hits`, `keyspace_misses`) and `OBJECT FREQ key` under LFU. More on Redis memory in [Redis and Memcached]({{ '/backend/redis-and-memcached/' | relative_url }}).

## Cache consistency

### Stale reads

A cache is a replica with no replication protocol, so it serves stale data whenever an update has not reached it yet. Sources of staleness:

- The TTL window when you rely on expiry.
- The gap between the database commit and the delete.
- Filling the cache from a database read replica that has not applied the write yet: the delete happens on time, then a reader refills the key with the old row from the lagging replica, and it stays stale for the full TTL.
- Multiple layers (browser, CDN, L1, Redis), each with its own expiry.

When a user must see their own write (read-your-writes), route that user's reads for the next few seconds to the primary and bypass the cache, or use write-through for that data.

### The delete-after-write race

Even with "update the database, then delete the key", a slow reader can put an old value back:

```text
 time  reader A               writer B              cache   db
  t1   GET k -> miss                                 -      v1
  t2   SELECT -> v1                                  -      v1
  t3                          UPDATE -> v2           -      v2
  t4                          commit, DEL k          -      v2
  t5   SET k v1 (late)                               v1     v2
       the cache serves v1 until the TTL expires
```

It needs a reader whose read-then-set spans the writer's whole update-and-delete, which is rare but real under GC pauses, network delays, and slow serialization. Deleting before the update is worse: any read between the delete and the commit refills the old value, so that window is the entire transaction.

### Delayed double delete

Delete after the commit, then delete again after a delay longer than a typical read-then-fill (plus replica lag if caches fill from replicas), for example 500 ms to a few seconds. The second delete removes whatever a racing reader wrote. Run the second delete from a delayed job, never by sleeping in the request. It shrinks the window rather than closing it, and doubles delete traffic.

### Leases and versioned fills

**Leases** close the race. On a miss, the cache hands the reader a lease token. A delete invalidates outstanding tokens. A set succeeds only with a valid token, so the late `SET k v1` at t5 is rejected. Facebook described leases in its memcache paper; the same mechanism also limits stampedes, because the server hands out one token per key per few seconds and tells other readers to wait or take a stale value.

Redis has no leases, but you can get the same guarantee with versions. The writer replaces the entry with a short-lived tombstone carrying the new row version instead of deleting it, and every fill carries the version it read; a Lua script rejects fills older than what is stored (illustrative):

```lua
-- KEYS[1] cache key; ARGV[1] version; ARGV[2] payload ('' = tombstone); ARGV[3] ttl
local cur = redis.call('HGET', KEYS[1], 'ver')
if cur and tonumber(cur) >= tonumber(ARGV[1]) then
  return 0  -- stale fill or out-of-order write: ignore
end
redis.call('HSET', KEYS[1], 'ver', ARGV[1], 'data', ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
```

Readers treat an empty `data` as a miss and fill with the version from the row (an `updated_at` or integer `version` column). This also orders concurrent write-through updates.

| Approach | Remaining stale window | Cost |
|---|---|---|
| TTL only | up to TTL | none |
| Delete after commit | rare race, then TTL | one delete per write |
| Plus delayed double delete | rarer, bounded by the delay | delayed job, extra deletes |
| Leases or versioned fills | closed for the fill race | cache support or Lua, version column |
| CDC invalidation | pipeline lag | Debezium, Kafka |
| Do not cache | none | database load |

## In practice

The runnable demo implements LRU and an `O(1)` LFU, then runs a workload where a hot set is interrupted by one-off scans. LFU keeps the hot set; LRU loses it after every scan. For the interview-style LRU design, see [LRU cache]({{ '/lld/lru-cache/' | relative_url }}).

```python
import random
from collections import OrderedDict, defaultdict


class LRUCache:
    def __init__(self, capacity):
        self.capacity, self.data = capacity, OrderedDict()

    def get(self, key):
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key]

    def put(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        if len(self.data) > self.capacity:
            self.data.popitem(last=False)


class LFUCache:
    """Buckets of keys per frequency; LRU order breaks ties in a bucket."""

    def __init__(self, capacity):
        self.capacity, self.values, self.freq = capacity, {}, {}
        self.buckets, self.min_freq = defaultdict(OrderedDict), 0

    def _bump(self, key):
        f = self.freq[key]
        del self.buckets[f][key]
        if not self.buckets[f]:
            del self.buckets[f]
            if self.min_freq == f:
                self.min_freq = f + 1
        self.freq[key] = f + 1
        self.buckets[f + 1][key] = None

    def get(self, key):
        if key not in self.values:
            return None
        self._bump(key)
        return self.values[key]

    def put(self, key, value):
        if key in self.values:
            self.values[key] = value
            self._bump(key)
            return
        if len(self.values) >= self.capacity:
            victim, _ = self.buckets[self.min_freq].popitem(last=False)
            if not self.buckets[self.min_freq]:
                del self.buckets[self.min_freq]
            del self.values[victim], self.freq[victim]
        self.values[key], self.freq[key] = value, 1
        self.buckets[1][key] = None
        self.min_freq = 1


def hit_ratio(cache, requests):
    hits = 0
    for key in requests:
        if cache.get(key) is not None:
            hits += 1
        else:
            cache.put(key, True)
    return hits / len(requests)


def hot_set_with_scans(seed=3):
    rng, reqs, scan = random.Random(seed), [], 0
    for _ in range(50):
        reqs += [f"hot:{rng.randrange(80)}" for _ in range(400)]
        reqs += [f"scan:{scan + i}" for i in range(200)]  # one-off export
        scan += 200
    return reqs


if __name__ == "__main__":
    lru = LRUCache(2)
    lru.put("a", 1); lru.put("b", 2); lru.get("a"); lru.put("c", 3)
    assert lru.get("b") is None and lru.get("a") == 1  # b was least recent

    lfu = LFUCache(2)
    lfu.put("a", 1); lfu.get("a"); lfu.put("b", 2); lfu.put("c", 3)
    assert lfu.get("b") is None and lfu.get("a") == 1  # b was least frequent

    reqs = hot_set_with_scans()
    r_lru = hit_ratio(LRUCache(100), reqs)
    r_lfu = hit_ratio(LFUCache(100), reqs)
    print(f"hit ratio with scans: LRU {r_lru:.2f}, LFU {r_lfu:.2f}")
    assert r_lfu > r_lru + 0.05
```

The second demo replays the delete-after-write race step by step and shows a lease rejecting the late fill:

```python
class LeaseCache:
    """Memcache-style leases: a miss issues a token; a delete revokes it."""

    def __init__(self):
        self.data, self.leases, self.next_token = {}, {}, 0

    def get(self, key):
        if key in self.data:
            return self.data[key], None
        self.next_token += 1
        self.leases[key] = self.next_token
        return None, self.next_token

    def set(self, key, value, token, use_leases=True):
        if use_leases and self.leases.get(key) != token:
            return False  # lease revoked by a delete: drop the stale fill
        self.data[key] = value
        self.leases.pop(key, None)
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.leases.pop(key, None)


def race(use_leases):
    db, cache = {"price:1": 100}, LeaseCache()
    _, token = cache.get("price:1")      # t1 reader A misses
    old = db["price:1"]                  # t2 reader A reads v1
    db["price:1"] = 120                  # t3 writer B updates
    cache.delete("price:1")              # t4 writer B deletes
    cache.set("price:1", old, token, use_leases)  # t5 late fill
    value, token = cache.get("price:1")
    if value is None:                    # next reader refills correctly
        cache.set("price:1", db["price:1"], token)
        value, _ = cache.get("price:1")
    return value


if __name__ == "__main__":
    assert race(use_leases=False) == 100  # stale until the TTL
    assert race(use_leases=True) == 120   # late fill rejected
    print("lease closes the delete-after-write race")
```

Delete after commit plus a delayed second delete in Django and Celery (illustrative):

```python
@app.task
def delete_keys(keys):
    cache.delete_many(keys)


def on_product_saved(product):
    keys = [f"product:{product.pk}:v3", f"t:{product.tenant_id}:catalog:v3"]

    def invalidate():
        cache.delete_many(keys)  # right after commit
        delete_keys.apply_async((keys,), countdown=2)  # and again, later

    transaction.on_commit(invalidate)
```

## Pitfalls

- **Deleting before the database write.** Readers refill the old value for the whole transaction. Delete after commit.
- **Filling from a lagging replica.** The delete was correct, the refill was stale. Fill from the primary for recently written keys, or delay a second delete past the replica lag.
- **Unbounded keys derived from one row.** A product change must invalidate 40 list caches. Prefer generation counters or shorter TTLs for derived lists.
- **No TTL as a backstop.** Every invalidation bug becomes permanent. Always set one.
- **Abandoned CDC replication slot.** The invalidator stops, the slot holds WAL, and the primary runs out of disk. Alert on slot lag (`pg_replication_slots`).
- **LRU with batch jobs sharing the cache.** A nightly export evicts the daytime hot set and the morning starts cold. Use LFU, a separate cache, or skip caching for scans.
- **Wrong Redis policy.** `volatile-lru` with no TTLs behaves like `noeviction`; `allkeys-lru` in an instance that also holds locks or queues evicts them.

## Interview questions

**Q: What is the difference between invalidation and eviction?**
Invalidation removes data because it is wrong (a write happened); eviction removes data because memory is full. TTL is invalidation by time; LRU and LFU are eviction policies.

**Q: How do you invalidate all cached reports for one tenant?**
Use a generation counter in the key (`t:42:g17:report:...`) and increment it. Old keys are never read again and expire on their own. Deleting would need a scan of the keyspace.

**Q: Describe a race that leaves stale data even with delete-after-write.**
A reader misses and reads v1; a writer updates to v2 and deletes the key; the reader then sets v1. The cache serves v1 until the TTL. Fix it with leases, versioned fills with tombstones, or reduce it with a delayed second delete.

**Q: Why use CDC for invalidation?**
It invalidates on every committed change regardless of which service, script or migration wrote it, in commit order per key, and retries safely because deletes are idempotent. The cost is pipeline lag and operating Debezium and Kafka.

**Q: LRU or LFU?**
LRU adapts quickly and suits recency-driven workloads; LFU keeps a stable hot set and resists scans but needs aging. W-TinyLFU gets most of both by admitting new keys only when they are more popular than the victim.

**Q: How does Redis implement LRU without a linked list?**
It stores a small access clock per key and, on eviction, samples a few keys and evicts the oldest from a candidate pool. It is approximate but close to true LRU with sample sizes of 5–10.

**Q: How would you guarantee a user sees their own update?**
Invalidate after commit and route that user's reads to the primary for a short period, or update the cache synchronously for that data (write-through with versions).

## Further reading

- Einziger, Friedman and Manes, "TinyLFU: A Highly Efficient Cache Admission Policy", ACM Transactions on Storage, 2017
- Nishtala et al., "Scaling Memcache at Facebook", NSDI 2013
- Redis documentation, "Key eviction"
- Debezium documentation, PostgreSQL connector
- Yang et al., "FIFO Queues are All You Need for Cache Eviction", SOSP 2023
