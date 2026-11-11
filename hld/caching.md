---
title: Caching
section: hld
order: 5
summary: Cache patterns, eviction, invalidation, and how to handle stampedes and hot keys without serving garbage.
---

## What it is

A cache stores the result of expensive work (DB query, API call, render) in faster storage, usually memory, so repeat reads skip the work.

Layers: browser → CDN → API gateway → app in-process (LRU) → distributed cache (Redis/Memcached) → DB buffer cache.

## When to use

- Read-heavy data with a skewed access pattern (a small hot set).
- Expensive computations (aggregations, rendered pages, LLM responses for identical prompts).
- You can tolerate some staleness, or you can invalidate precisely.
- Not for: write-heavy data read once, or data that must be strongly consistent on every read.

## Patterns

| Pattern | Read path | Write path | Notes |
|---|---|---|---|
| Cache-aside (lazy) | App reads cache; on miss reads DB, fills cache | App writes DB, deletes cache key | Default choice. Cache only holds what is read |
| Read-through | Cache library loads from DB on miss | Same as cache-aside | Logic lives in cache layer |
| Write-through | Read cache | Write cache and DB synchronously | Always fresh; slower writes, caches cold data |
| Write-back (write-behind) | Read cache | Write cache, flush to DB async | Fast writes; data loss if cache dies |
| Write-around | Read-through/aside | Write DB only | Avoids polluting cache with write-once data |

```python
def get_user(user_id):
    key = f"user:v2:{user_id}"
    if (hit := redis.get(key)) is not None:
        return json.loads(hit)
    user = db.fetch_user(user_id)
    redis.set(key, json.dumps(user), ex=300 + random.randint(0, 60))  # TTL jitter
    return user

def update_user(user_id, data):
    db.update_user(user_id, data)
    redis.delete(f"user:v2:{user_id}")  # delete, don't set
```

> **Rule of thumb:** On write, **delete** the key rather than setting it. Two concurrent writers that each `SET` can leave the older value in cache.

## Eviction

| Policy | Evicts | Use |
|---|---|---|
| LRU | Least recently used | General default |
| LFU | Least frequently used | Stable popularity, resists scans |
| TTL | Expired keys | Bound staleness |
| FIFO / random | Oldest / random | Cheap, rarely best |

Redis: set `maxmemory` and `maxmemory-policy` (`allkeys-lru`, `allkeys-lfu`, `volatile-ttl`). Without it Redis errors on writes when full (`noeviction`).

## Invalidation

- **TTL only:** simple, bounded staleness. Good default.
- **Explicit delete on write:** fresher, but every write path must remember it.
- **Event-driven:** consume DB change events (CDC / outbox) and delete keys. Catches writes from all services.
- **Versioned keys:** `user:v2:42` or include `updated_at` in the key; bump the version to invalidate a whole class.
- Race to know: reader misses, reads old row, writer updates DB and deletes key, reader sets the old value. Short TTLs bound the damage; leases (Memcache-style) prevent it.

## Cache stampede (thundering herd)

A hot key expires, and thousands of requests miss at once and hammer the DB.

Fixes:
- **Request coalescing / lock:** first miss takes a lock (`SET lock:k 1 NX EX 5`), others wait briefly or serve stale.
- **Stale-while-revalidate:** store a soft expiry; serve stale and refresh in the background.
- **Probabilistic early refresh:** refresh slightly before expiry with probability that rises near the TTL.
- **TTL jitter:** avoid many keys expiring together.

## Hot keys

One key gets a huge share of traffic (celebrity profile, viral post) and saturates a single Redis shard.

- In-process L1 cache with a short TTL (1–5 s) in front of Redis.
- Replicate the key: `post:42#0..#N`, read a random suffix.
- Read from Redis replicas.
- Detect with `redis-cli --hotkeys` (LFU policy) or client-side sampling.

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Longer TTL | Higher hit rate | More staleness |
| In-process cache | Zero network hop | Per-instance inconsistency, memory per pod |
| Write-back | Write throughput | Durability risk |
| Caching negatives (404s) | Protects DB from missing-key floods | Must invalidate on create |

## Gotchas

- **Cache penetration:** requests for keys that never exist. Cache the miss briefly or use a Bloom filter.
- **Multi-tenant:** always include tenant id in the key. A missing prefix is a data leak.
- Cold start after a cache flush can take the DB down. Warm gradually.
- Serialization cost of big objects can exceed the DB query time.
- Measure hit rate; below ~80% on a hot path, question the design.

> **Interview tip:** Say which pattern (cache-aside), what key, what TTL, how you invalidate, and what happens on a stampede. That is the full answer.
