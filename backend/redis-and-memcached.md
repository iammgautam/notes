---
title: Redis and Memcached
section: backend
order: 28
phase: "Phase 4 · Caching"
summary: Redis internals that matter in design reviews (structures, persistence, replication, Sentinel, Cluster, Lua, streams) and how Memcached differs.
---

Redis and Memcached are the two default answers to "put a cache in front of the database". Both keep data in RAM and answer in well under a millisecond on a local network. They differ in almost everything else: Memcached is a deliberately simple, multi-threaded, shard-on-the-client byte store; Redis is a single-threaded data-structure server with persistence, replication, clustering and scripting. Knowing the mechanics underneath (what blocks the event loop, what survives a restart, what happens on failover) is what separates "we use Redis" from a design you can defend.

## Redis

Redis executes commands on one main thread with an event loop. Every command is atomic with respect to every other command, there are no locks, and data structures stay simple. Since Redis 6, optional I/O threads handle socket reads, writes and parsing, and background threads handle `UNLINK` frees and AOF fsync, but command execution is still serial. The consequence: one slow command blocks every client. `KEYS *`, `SMEMBERS` on a million-element set, `DEL` on a huge hash, or a long Lua script all stall the whole instance. Use `SCAN`, `SSCAN`, `HSCAN` and `UNLINK` instead.

### Data structures

| Type | Typical use | Key operations and cost |
|---|---|---|
| String | cached JSON blobs, counters, locks | `GET`/`SET` `O(1)`, `INCR`, `SET NX PX` |
| Hash | object fields, per-tenant settings | `HGET`/`HSET` `O(1)`, `HGETALL` `O(n)` |
| List | simple queues, recent-items lists | `LPUSH`/`RPOP` `O(1)`, `BLMOVE` |
| Set | tags, unique visitors per day | `SADD`, `SISMEMBER` `O(1)`, `SINTER` |
| Sorted set | leaderboards, rate-limit windows, delay queues | `ZADD`, `ZRANGE` `O(log n + m)` |
| Stream | durable event log with consumer groups | `XADD`, `XREADGROUP`, `XACK` |
| Bitmap | feature flags per user id, daily activity | `SETBIT`, `BITCOUNT` |
| HyperLogLog | approximate distinct counts in 12 KB | `PFADD`, `PFCOUNT` (~0.81% error) |
| Geo | nearby search | `GEOADD`, `GEOSEARCH` (sorted set underneath) |

Small hashes, sets and sorted sets use compact encodings (listpacks, intsets) and switch to real hash tables or skiplists past configurable thresholds. That is why a million small hashes use far less memory than a million JSON strings with the same fields. As of Redis 8 (2025), JSON, time series and probabilistic structures that used to ship as Redis Stack modules are part of the main distribution.

### Persistence: RDB and AOF

**RDB** snapshots: `BGSAVE` forks the process and the child writes a compact point-in-time file while the parent keeps serving. Copy-on-write means memory use grows with the write rate during the snapshot, up to double in the worst case. Restarts are fast, but you lose everything since the last snapshot (minutes).

**AOF** (append-only file) logs every write command. `appendfsync` controls durability:

| Setting | Durability | Cost |
|---|---|---|
| `always` | fsync per write; lose at most one write batch | much slower writes |
| `everysec` (default) | lose up to ~1 s of writes | small |
| `no` | OS decides, often ~30 s | smallest |

The AOF is compacted by a background rewrite. Since Redis 7 it is stored as multiple files (a base plus incremental files tracked by a manifest), and with `aof-use-rdb-preamble` the base is an RDB snapshot, which gives fast loading with AOF's durability. For a pure cache, turn both off: persistence adds fork pauses and disk I/O for data you can rebuild from the database.

Forking a large instance takes time proportional to its page tables (tens of milliseconds per GB on some virtualized hosts), during which Redis does not serve. Disable transparent huge pages, as Redis warns at startup, and keep memory headroom for copy-on-write.

### Replication

Replication is asynchronous. A replica connects, sends `PSYNC` with the primary's replication ID and its offset, and receives either a partial resync from the primary's backlog buffer (`repl-backlog-size`) or a full resync: an RDB snapshot followed by the command stream. Replicas are read-only by default.

Two consequences matter for design:

- **Reads from replicas are stale** by the replication lag, usually milliseconds, sometimes seconds under load.
- **Acknowledged writes can be lost on failover.** The primary acknowledges before replicas have the write. `WAIT numreplicas timeout` blocks until N replicas acknowledge, which narrows the window but does not make Redis strongly consistent. `min-replicas-to-write` and `min-replicas-max-lag` make a primary stop accepting writes when it cannot reach replicas, which bounds how much an isolated primary can accept and later lose.

### Sentinel

Sentinel provides automatic failover for a single primary with replicas (no sharding). Several Sentinel processes monitor the primary. One Sentinel that cannot reach it marks it subjectively down; when a quorum agrees, it is objectively down, and the Sentinels elect a leader (a majority must vote) that promotes the best replica (priority, replication offset) and repoints the others. Clients ask Sentinel for the current primary address. Run at least three Sentinels in separate failure domains so a majority survives a single failure.

### Cluster and hash slots

Redis Cluster shards the keyspace into 16,384 hash slots. Each key maps to `CRC16(key) mod 16384`, each primary owns a range of slots, and each primary has replicas. Nodes gossip over a cluster bus; when a primary fails, its replicas ask the other primaries for votes and a majority promotes one.

```text
          client: caches the slot map, follows MOVED / ASK
              |                  |                  |
      slots 0-5460      slots 5461-10922   slots 10923-16383
        +----------+      +----------+      +----------+
        | primary A|      | primary B|      | primary C|
        +----+-----+      +----+-----+      +----+-----+
             | async           | async           | async
        +----v-----+      +----v-----+      +----v-----+
        | replica  |      | replica  |      | replica  |
        +----------+      +----------+      +----------+
        slot = CRC16(key, or the {hash tag} part) mod 16384
```

Mechanics you will be asked about:

- **Redirects.** A node that does not own a key's slot replies `MOVED <slot> <host:port>`; smart clients update their slot map. During resharding a migrating slot answers `ASK`, a one-time redirect for keys already moved.
- **Multi-key operations** (`MGET`, `MULTI`, Lua with several keys, `SUNIONSTORE`) work only when all keys share a slot; otherwise you get `CROSSSLOT`. **Hash tags** fix this: only the substring inside the first `{...}` is hashed, so `{tenant:42}:invoices` and `{tenant:42}:customers` land on the same slot. Overusing one tag recreates a hot shard.
- **Only database 0**, no `SELECT`.
- By default (`cluster-require-full-coverage yes`) the cluster rejects queries if any slot has no live owner.

### Lua scripts and functions

`EVAL` runs a Lua script atomically on the server: no other command runs in between. Use it for read-modify-write logic that must not race: compare-and-delete for lock release, token-bucket rate limiting, "set only if newer version". Load once and call by hash with `EVALSHA`, or, since Redis 7, register named **functions** (`FUNCTION LOAD`, `FCALL`) that persist and replicate with the data. Pass every key in `KEYS` rather than building names inside the script, so Cluster can route it and check slots. A script blocks the server while it runs; past `busy-reply-threshold` (5 s by default) other clients get `BUSY` errors, and a script that has already written cannot be killed without shutting down.

### Pipelines and transactions

A **pipeline** sends many commands without waiting for each reply, turning N round trips into one. It is a network optimization, not atomicity: other clients' commands can interleave. `MULTI`/`EXEC` queues commands and runs them as one uninterrupted batch, but there is no rollback: if one command fails at runtime, the others still apply. `WATCH` adds optimistic concurrency: `EXEC` aborts if a watched key changed since `WATCH`. In practice, Lua replaced most `WATCH` loops.

### Pub/sub vs streams

| | Pub/sub | Streams |
|---|---|---|
| Delivery | at-most-once, fire-and-forget | at-least-once with acknowledgments |
| Storage | none; offline subscribers miss messages | log kept in memory, trimmed by `MAXLEN` or `MINID` |
| Consumers | every subscriber gets every message | consumer groups split work; per-group offsets |
| Slow consumer | disconnected at output buffer limit | messages wait in the stream |
| Recovery | none | pending entries list, `XPENDING`, `XAUTOCLAIM` |
| Fits | cache invalidation broadcasts, live notifications | job queues, event fan-out, audit trails |

Classic pub/sub in Cluster broadcasts every message to every node; Redis 7 added sharded pub/sub (`SPUBLISH`, `SSUBSCRIBE`) that routes by channel slot. For durable messaging at scale, see [Kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}).

### Memory policies

`maxmemory` caps data memory, and `maxmemory-policy` decides what happens at the cap:

| Policy | Evicts | Use for |
|---|---|---|
| `noeviction` (default) | nothing; writes fail with `OOM` | queues, locks, data you cannot lose |
| `allkeys-lru` | approximately least recently used, any key | general cache |
| `allkeys-lfu` | approximately least frequently used | cache with stable hot set |
| `allkeys-random` | random key | uniform access |
| `volatile-lru` / `volatile-lfu` / `volatile-random` | same, only keys with a TTL | mixed cache and persistent data |
| `volatile-ttl` | keys with the nearest expiry | short-lived data first |

LRU and LFU are approximated by sampling (`maxmemory-samples`, default 5) instead of keeping a global list. LFU uses a small logarithmic counter per key that decays over time (`lfu-log-factor`, `lfu-decay-time`). Under a `volatile-*` policy with no expiring keys, Redis behaves like `noeviction`. Expired keys are removed lazily on access and by an active background cycle that samples keys with TTLs. Details and runnable LRU/LFU code are in [Cache invalidation, eviction and consistency]({{ '/backend/cache-invalidation-and-eviction/' | relative_url }}).

Licensing changed in 2024: Redis moved from BSD to source-available licenses, the Linux Foundation forked the last BSD release as Valkey, and Redis 8 (2025) added AGPLv3 as an option. Managed offerings such as ElastiCache and Memorystore support Valkey, which is protocol-compatible.

## Memcached

Memcached stores opaque byte values under keys of up to 250 bytes, with a default maximum value of 1 MB. It has no persistence, no replication, no server-side clustering and no data types beyond counters. That simplicity is the point: it does one job and scales across cores.

### Slab allocator

Memcached avoids `malloc` fragmentation by carving memory into 1 MB pages and assigning each page to a **slab class**. Each class holds fixed-size chunks, and chunk sizes grow by a factor (`-f`, default 1.25): with defaults, classes start around 96, 120, 152, 192 bytes and so on. An item goes into the smallest chunk that fits, so a 130-byte item occupies a 152-byte chunk and wastes 22 bytes. That is internal fragmentation: up to about 20% of each chunk with the default factor.

Eviction is per slab class: a full class evicts its own least recently used items even if other classes have free memory. Modern versions use a segmented LRU (hot, warm, cold) maintained by a background thread. When the size mix of values changes, memory stays assigned to the wrong classes (slab calcification); slab rebalancing (`slab_reassign`, `slab_automove`, on by default in modern releases) moves pages between classes over time.

### Multi-threading

Memcached runs a configurable number of worker threads (`-t`, default 4), each with its own event loop, over a shared hash table protected by fine-grained item locks. One instance can saturate many cores and a fast NIC; Redis gets the same parallelism only by running several instances or a Cluster.

### Consistent hashing on clients

Memcached servers do not know about each other. The **client** picks the server for each key, using consistent hashing so that adding or removing a server remaps only about `1/N` of keys instead of nearly all of them (as `hash(key) % N` would). libmemcached implements the ketama ring; Python's pymemcache `HashClient` uses rendezvous hashing. A lost server simply becomes misses for its share of keys. At very large scale, a proxy such as mcrouter does routing, replication of pools and failover, as publicly described in Facebook's "Scaling Memcache at Facebook" (NSDI 2013), which also introduced leases against stale sets and stampedes. See [Consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}).

Memcached supports compare-and-set (`gets` returns a CAS token, `cas` writes only if it is unchanged), and the newer meta protocol (`mg`, `ms`, `md`) adds flags for stale-while-revalidate and "you won the right to recompute" semantics.

## Redis vs Memcached

| | Redis | Memcached |
|---|---|---|
| Data model | strings, hashes, sets, sorted sets, streams, more | opaque bytes, counters |
| Threads | one command thread, plus I/O threads | multi-threaded |
| Persistence | RDB, AOF, hybrid | none |
| Replication and HA | async replicas, Sentinel, Cluster | none built in; client or proxy |
| Sharding | Cluster hash slots, or client-side | client-side consistent hashing |
| Max value | 512 MB string | 1 MB default, configurable |
| Eviction | 8 policies, sampled LRU/LFU | per-slab-class segmented LRU |
| Atomic logic | Lua, functions, `MULTI` | CAS, `incr`/`decr` |
| Choose when | you need structures, atomic ops, durability or messaging | pure look-aside cache of blobs at high throughput per box |

Redis usually wins because one system covers caching, rate limiting, locks and light queues; Memcached still wins on multi-core throughput for a plain object cache.

## In practice

Illustrative redis-py usage (API as of redis-py 5.x):

```python
import redis

r = redis.Redis(host="cache", port=6379, decode_responses=True,
                socket_timeout=0.05, socket_connect_timeout=0.05)

# Pipeline: one round trip for many reads (not atomic).
pipe = r.pipeline(transaction=False)
for pid in product_ids:
    pipe.hgetall(f"t:42:product:{pid}")
products = pipe.execute()

# Leaderboard on a sorted set.
r.zincrby("t:42:leaderboard:2026-11", 5, "user:17")
top10 = r.zrange("t:42:leaderboard:2026-11", 0, 9, desc=True, withscores=True)

# Atomic compare-and-delete: release a lock only if we still own it.
release = r.register_script("""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""")
release(keys=["lock:invoice:9001"], args=[token])

# Stream with a consumer group: at-least-once processing.
r.xadd("events:orders", {"order_id": "9001"}, maxlen=1_000_000, approximate=True)
for _, messages in r.xreadgroup("billing", "worker-1", {"events:orders": ">"},
                                count=10, block=5000):
    for msg_id, fields in messages:
        handle(fields)
        r.xack("events:orders", "billing", msg_id)
```

The short socket timeouts matter: a cache that hangs for 30 s is worse than a cache that is down, because your request threads pile up behind it.

Two illustrative `redis.conf` profiles:

```text
# Pure cache: rebuildable from the database
maxmemory 6gb
maxmemory-policy allkeys-lfu
save ""
appendonly no

# Durable store: sessions, queues, locks
maxmemory 6gb
maxmemory-policy noeviction
appendonly yes
appendfsync everysec
min-replicas-to-write 1
min-replicas-max-lag 10
```

The runnable demo computes Redis Cluster hash slots (including hash tag rules) and Memcached-style slab classes:

```python
def crc16_xmodem(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def key_slot(key):
    k = key.encode()
    start = k.find(b"{")
    if start != -1:
        end = k.find(b"}", start + 1)
        if end > start + 1:  # non-empty tag: hash only the tag
            k = k[start + 1:end]
    return crc16_xmodem(k) % 16384


def slab_classes(smallest=96, factor=1.25, page=1024 * 1024):
    sizes, size = [], smallest
    while size <= page // 2:
        sizes.append(size)
        size = int(size * factor)
        size += -size % 8  # align to 8 bytes
    return sizes


def chunk_for(item_size, sizes):
    return next(s for s in sizes if s >= item_size)


if __name__ == "__main__":
    assert crc16_xmodem(b"123456789") == 0x31C3  # CRC16 reference value
    assert key_slot("foo") == 12182
    tagged = {key_slot(k) for k in ("{tenant:42}:invoices",
                                    "{tenant:42}:customers", "tenant:42")}
    assert len(tagged) == 1  # hash tags co-locate keys
    assert key_slot("{}x") == crc16_xmodem(b"{}x") % 16384  # empty tag
    assert key_slot("a{}{b}") == crc16_xmodem(b"a{}{b}") % 16384

    sizes = slab_classes()
    assert sizes[:6] == [96, 120, 152, 192, 240, 304]
    assert chunk_for(130, sizes) - 130 == 22  # internal fragmentation
    print("slots ok; first slab classes:", sizes[:8])
```

## Pitfalls

- **Blocking commands in production.** `KEYS`, `FLUSHALL` without `ASYNC`, `HGETALL` on huge hashes, `DEL` of big keys. Find them with `SLOWLOG GET` and `redis-cli --bigkeys`; replace with `SCAN` family and `UNLINK`.
- **Cache without `maxmemory`, or with `noeviction`.** The instance fills up and every write fails with `OOM command not allowed`. Caches need an eviction policy.
- **`maxmemory` equal to RAM.** Fork copy-on-write, fragmentation, replication buffers and client buffers need headroom. Plan for 60–75% of RAM when persistence is on, and watch `mem_fragmentation_ratio`.
- **Treating Redis as durable by default.** Async replication plus failover can drop acknowledged writes. Keep money, orders and idempotency records in PostgreSQL.
- **Pub/sub as a queue.** Messages vanish when a subscriber reconnects. Use streams or a real broker.
- **`CROSSSLOT` after migrating to Cluster.** Multi-key commands and scripts that worked on one node fail. Design hash tags before migrating.
- **Hot or big keys.** One key per node takes the load of the whole feature. See [Cache stampede and hot keys]({{ '/backend/cache-stampede/' | relative_url }}).
- **Memcached value limit and slab calcification.** Values over 1 MB are rejected, and a wrapper that ignores set errors turns that into a permanent miss. A shifted value-size mix raises evictions while memory looks free.

## Interview questions

**Q: Redis executes commands on a single thread. Why is it fast?**
Everything is in memory, operations are mostly `O(1)` or `O(log n)`, there is no locking or context switching between commands, and the event loop multiplexes thousands of connections. The bottleneck is usually network round trips, which pipelining addresses.

**Q: RDB or AOF?**
RDB gives compact snapshots and fast restarts but loses minutes of writes. AOF with `everysec` loses at most about a second but has bigger files and rewrite cost. Use both (RDB preamble in AOF) for data you care about, neither for a pure cache.

**Q: How does Redis Cluster decide where a key lives, and how do you do a multi-key operation?**
`CRC16(key) mod 16384` gives the slot; each primary owns a slot range. Multi-key operations need all keys in one slot, which hash tags (`{tenant:42}:a`, `{tenant:42}:b`) guarantee.

**Q: Can Redis lose a write it acknowledged?**
Yes. Replication is asynchronous, so a primary can acknowledge, crash, and have a replica without the write promoted. `WAIT` and `min-replicas-to-write` narrow the window but do not close it.

**Q: Sentinel or Cluster?**
Sentinel gives failover for one dataset that fits on one primary. Cluster adds sharding across primaries, at the cost of cross-slot restrictions and smarter clients. Start with Sentinel or a managed primary-replica setup and move to Cluster when memory or write throughput outgrows one node.

**Q: Pub/sub or streams for cache invalidation messages?**
Pub/sub is fine when a missed message only means a short-lived stale L1 entry bounded by TTL. If a missed message means lasting wrong data, use streams (or CDC through Kafka) so consumers can resume.

**Q: When would you choose Memcached?**
A large, plain look-aside cache of serialized objects where you want multi-threaded throughput per box and predictable memory, and you need none of Redis's structures, persistence or scripting.

**Q: What happens when Redis reaches `maxmemory`?**
It applies `maxmemory-policy`: with an `allkeys-*` policy it evicts sampled keys; with `noeviction`, or a `volatile-*` policy and no expiring keys, writes fail with an OOM error while reads continue.

## Further reading

- Redis documentation: "Redis cluster specification", "Redis persistence", "Key eviction", "Redis Streams"
- Memcached wiki on GitHub, especially the pages on slabs, LRU and the meta protocol
- Nishtala et al., "Scaling Memcache at Facebook", NSDI 2013
- Josiah L. Carlson, "Redis in Action" (Manning)
