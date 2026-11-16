---
title: Caching patterns
section: backend
order: 29
phase: "Phase 4 · Caching"
summary: Cache-aside, read-through, write-through and write-behind, how each behaves under races and failures, and when to pick which.
---

Once you decide to cache, the next question is who talks to the database and when. Four patterns cover almost every design: cache-aside and read-through describe the read path, write-through and write-behind describe the write path. They differ in where the loading logic lives, how fresh the cache is after a write, what a cache outage does to your system, and whether a crash can lose data. Interviewers use them to test whether you understand consistency, not just vocabulary.

## Cache-aside

Cache-aside (lazy loading) is the default with Redis and Memcached, because those stores know nothing about your database. The application owns both sides.

```text
 read                                write
 app ---1 GET k--------> cache       app ---1 UPDATE row---> db
 app <--miss------------ cache       app ---2 DEL k-------> cache
 app ---2 SELECT-------> db              (after the commit)
 app <--row------------- db
 app ---3 SET k v EX ttl> cache
```

**Read path:** look up the key; on a hit return it; on a miss read the database, store the result with a TTL, return it.

**Write path:** update the database, then delete the key. The next read repopulates it. Delete instead of overwriting because:

- Two concurrent writers can commit to the database in one order and set the cache in the other, leaving the older value cached with no expiry in sight. Two deletes in any order leave the same result: no entry.
- The value you would compute on write may never be read, so building it is wasted work.
- A delete is idempotent and safe to retry.

The delete must run after the transaction commits. If it runs inside the transaction, a concurrent reader can miss, read the old committed row, and cache it before your commit lands.

**Consistency:** eventually consistent with a bounded window. A reader that misses, reads the old row, and is slow to write the cache can still overwrite a newer delete. That race and its fixes (leases, versioned sets, delayed double delete) are in [Cache invalidation, eviction and consistency]({{ '/backend/cache-invalidation-and-eviction/' | relative_url }}). The TTL is your safety net: it bounds how long any bug or race keeps stale data alive.

**Failure behavior:** if the cache is down, reads fall through to the database. That is graceful degradation only if the database can take the load and the client fails fast; set short timeouts and treat cache errors as misses. If the delete after a write fails, the entry stays stale until its TTL, so retry the delete or drive invalidation from the database log.

**Pros:** only requested data is cached; a cache outage is survivable; works with any store. **Cons:** a miss costs three round trips; the first request after a write or expiry is slow; every service that writes a table must remember to invalidate.

## Read-through

In read-through, the application asks only the cache. On a miss, the cache itself (or a cache library) calls a loader you registered, stores the result, and returns it.

```text
 app ---get(k)----> [ cache ] ---miss: loader(k)---> db
 app <--value------ [ cache ] <------row------------ db
```

Examples: Caffeine's `LoadingCache` in Java, Hazelcast's `MapLoader`, and Amazon DynamoDB Accelerator (DAX), which is publicly documented as a read-through and write-through cache in front of DynamoDB. In Python, `functools.lru_cache` is a read-through cache for a function, and Django's `cache.get_or_set(key, callable, timeout)` gives callers the same shape over Redis.

**Why use it:** loading logic, key format, serialization and TTL live in one place instead of in every caller. The cache can also coalesce concurrent misses for the same key into one load (Caffeine does this per key), which is built-in protection against [cache stampedes]({{ '/backend/cache-stampede/' | relative_url }}).

**Consistency:** same as the write path you pair it with. Read-through says nothing about writes.

**Failure behavior:** the cache is in the critical path. If a remote read-through tier is down and has no bypass, reads fail. Loader failures must not be cached as values; either propagate them or cache a short negative result deliberately.

**Read-through vs cache-aside:** the difference is who calls the database. With cache-aside the application does, so the cache can be any key-value store. With read-through the cache does, so it must be a library or product that supports loaders.

## Write-through

In write-through, every write goes through the cache layer, which writes the database synchronously and updates the cached value before acknowledging.

```text
 app ---put(k, v)---> [ cache ] ---write(k, v), sync---> db
 app <---ack--------- [ cache ] <-------ok-------------- db
```

**Why use it:** the cache always holds the latest value for keys that were written, so read-after-write hits and is fresh. It pairs naturally with read-through: together the cache becomes the only data access interface, as in DAX.

**Ordering:** write the database first and update the cache only if that succeeds. If the database write fails after the cache was updated, readers see a value that was never committed.

**Consistency:** strong for a single writer per key. With concurrent writers, the database and cache updates can interleave in different orders, leaving an older value cached. Fix it by serializing writes per key, setting only if the version is newer, or deleting instead of setting on conflicts.

**Failure behavior:** writes fail when either the cache or the database is down, unless you choose to degrade by writing the database and deleting the key. A crash between the database write and the cache update leaves a stale entry, so keep a TTL even here.

**Costs:** write latency includes the cache write, and data written once and never read still occupies memory (cache pollution). TTLs limit both.

## Write-behind

Write-behind (write-back) acknowledges a write as soon as the cache has it and persists to the database later, asynchronously, usually in batches.

```text
 app ---put(k, v)---> [ cache ] ---ack now---> app
                      [ dirty keys ]
                           |
                           +---every N s or M keys: batch upsert---> db
```

**Why use it:** writes are as fast as the cache, repeated writes to one key coalesce (a thousand increments to a view counter become one `UPDATE`), and batching turns many small transactions into a few large ones. The database can even be briefly unavailable without failing user requests.

**Consistency:** the database lags the cache by up to the flush interval. Anything that reads the database directly (reports, other services, replicas, analytics exports) sees older data. Readers that go through the cache see the newest value.

**Failure behavior:** this is the pattern that can lose data. If the cache node dies before flushing, the unflushed writes are gone. Reduce the risk by making the cache durable (Redis with AOF and a replica), by writing to a durable log such as Kafka instead of a plain in-memory dirty set, and by bounding the age of dirty data. Flushes must be idempotent or versioned, because a flush that crashes midway will be retried.

**Fits:** counters (views, likes, API usage metering), `last_seen_at` timestamps, game state, aggregation before writing to analytics tables. **Does not fit:** orders, payments, anything a customer or auditor expects to survive a crash once acknowledged.

Write-behind done right is everywhere below your application: CPU caches are write-back, and PostgreSQL's shared buffers are written back lazily by the checkpointer and background writer. The database gets away with it because it appends every change to the write-ahead log and fsyncs that before acknowledging the commit. That is the lesson: write-behind is safe exactly when the first write is durable.

## Choosing a pattern

| Pattern | Loads on miss | Write path | Read after write | Stale risk | Loss risk |
|---|---|---|---|---|---|
| Cache-aside | application | DB, then delete key | miss, then fresh | race window, TTL | none |
| Read-through | cache loader | any | depends on writes | depends on writes | none |
| Write-through | cache loader | cache writes DB sync | hit, fresh | concurrent writers | none |
| Write-behind | cache loader | cache, DB later | hit, fresh in cache | DB lags | unflushed writes |

A related variant, refresh-ahead, reloads popular keys shortly before they expire so readers never see the miss; probabilistic early expiration in [Cache stampede and hot keys]({{ '/backend/cache-stampede/' | relative_url }}) is its decentralized form.

Rules of thumb:

- Default to cache-aside with delete-on-write and a TTL.
- Use read-through for in-process caches and when many call sites load the same data.
- Use write-through when reads right after writes are common and must be fresh (user settings, a profile just edited).
- Use write-behind only for high-rate writes you can afford to lose or can replay from a durable log.

## In practice

The runnable demo implements all four patterns against a fake database that counts reads and writes, and checks hits, invalidation, write coalescing, failure ordering and data loss on a crash.

```python
class FakeDB:
    def __init__(self):
        self.rows, self.reads, self.writes = {}, 0, 0

    def get(self, key):
        self.reads += 1
        return self.rows.get(key)

    def put(self, key, value):
        self.writes += 1
        self.rows[key] = value

    def put_many(self, items):  # one batched round trip
        self.writes += 1
        self.rows.update(items)


class CacheAside:
    """The app owns both calls; the cache is a plain key-value store."""

    def __init__(self, db):
        self.db, self.cache = db, {}

    def read(self, key):
        if key in self.cache:
            return self.cache[key]
        value = self.db.get(key)
        if value is not None:
            self.cache[key] = value
        return value

    def write(self, key, value):
        self.db.put(key, value)    # 1. source of truth first
        self.cache.pop(key, None)  # 2. invalidate, don't overwrite


class ReadThrough:
    """The cache owns loading; callers never see the database."""

    def __init__(self, loader):
        self.loader, self.store = loader, {}

    def get(self, key):
        if key not in self.store:
            self.store[key] = self.loader(key)
        return self.store[key]


class WriteThrough(ReadThrough):
    def __init__(self, loader, writer):
        super().__init__(loader)
        self.writer = writer

    def put(self, key, value):
        self.writer(key, value)  # DB first: if it raises, cache is untouched
        self.store[key] = value


class WriteBehind(ReadThrough):
    def __init__(self, loader, batch_writer):
        super().__init__(loader)
        self.batch_writer, self.dirty = batch_writer, {}

    def put(self, key, value):
        self.store[key] = value
        self.dirty[key] = value  # repeated writes to a key coalesce

    def flush(self):
        if self.dirty:
            batch, self.dirty = self.dirty, {}
            self.batch_writer(batch)

    def crash(self):
        self.store, self.dirty = {}, {}  # process memory is gone


if __name__ == "__main__":
    db = FakeDB()
    db.rows["plan:1"] = "free"
    ca = CacheAside(db)
    assert ca.read("plan:1") == "free" and db.reads == 1
    assert ca.read("plan:1") == "free" and db.reads == 1  # hit
    ca.write("plan:1", "pro")
    assert "plan:1" not in ca.cache
    assert ca.read("plan:1") == "pro" and db.reads == 2

    db = FakeDB()
    db.rows["user:7"] = "Asha"
    rt = ReadThrough(db.get)
    assert [rt.get("user:7") for _ in range(3)] == ["Asha"] * 3
    assert db.reads == 1

    db = FakeDB()
    wt = WriteThrough(db.get, db.put)
    wt.put("seats", 10)
    assert db.rows["seats"] == 10 and wt.get("seats") == 10
    assert db.reads == 0  # read after write is a fresh hit

    def db_down(key, value):
        raise ConnectionError("db down")

    broken = WriteThrough(db.get, db_down)
    try:
        broken.put("seats", 99)
    except ConnectionError:
        pass
    assert "seats" not in broken.store  # never cache an uncommitted write

    db = FakeDB()
    wb = WriteBehind(db.get, db.put_many)
    for n in range(1, 1001):
        wb.put("views:post:1", n)
    wb.put("views:post:2", 5)
    assert db.writes == 0 and wb.get("views:post:1") == 1000
    wb.flush()
    assert db.writes == 1 and db.rows["views:post:1"] == 1000  # 1001 -> 1
    wb.put("views:post:1", 1001)
    wb.crash()
    assert db.rows["views:post:1"] == 1000  # the unflushed write is lost
    print("all four patterns behave as described")
```

Cache-aside in Django, with the delete deferred until commit (illustrative):

```python
from django.core.cache import cache
from django.db import transaction

MISSING = object()  # distinguishes "not cached" from a cached None


def plan_key(tenant_id):
    return f"t:{tenant_id}:plan:v2"


def get_plan(tenant_id):
    plan = cache.get(plan_key(tenant_id), MISSING)
    if plan is MISSING:
        plan = PlanSerializer(Plan.objects.get(tenant_id=tenant_id)).data
        cache.set(plan_key(tenant_id), plan, timeout=600)
    return plan


def change_plan(tenant_id, tier):
    with transaction.atomic():
        Plan.objects.filter(tenant_id=tenant_id).update(tier=tier)
        transaction.on_commit(lambda: cache.delete(plan_key(tenant_id)))
```

Write-behind view counters with Redis and a periodic Celery task (illustrative). Redis absorbs every increment; the database gets one update per post per interval:

```python
from django.db.models import F


def record_view(post_id):
    r.hincrby("views:pending", post_id, 1)


@app.task  # scheduled every 10 s by Celery beat
def flush_views():
    if not r.exists("views:flushing"):  # resume a crashed flush first
        try:
            r.rename("views:pending", "views:flushing")  # atomic hand-off
        except redis.ResponseError:  # no pending views
            return
    counts = r.hgetall("views:flushing")
    with transaction.atomic():
        for post_id, n in counts.items():
            Post.objects.filter(pk=post_id).update(views=F("views") + int(n))
    r.delete("views:flushing")  # a crash before this line double-counts once
```

The trailing comment is the honest trade-off: this flush is at-least-once. If exact counts matter, record a batch id in the same database transaction and skip batches already applied, which is the [idempotency]({{ '/backend/idempotency/' | relative_url }}) pattern applied to a flush.

## Pitfalls

- **Setting the cache on write instead of deleting.** Out-of-order writers leave old values cached. Delete, or set only when the version is newer.
- **Invalidating inside the transaction.** A reader repopulates from the old row before commit. Use `transaction.on_commit`.
- **Treating a cached `None` as a miss.** Every read of a nonexistent row goes to the database. Use a sentinel and cache "not found" briefly on purpose.
- **No TTL on write-through entries.** Any missed invalidation or race lives forever. Keep a TTL as a backstop.
- **Write-behind without a durable buffer.** A restart silently drops writes, and other services reading the database see stale data. Use a durable log, bound dirty age, monitor flush lag.
- **Cache errors failing requests in cache-aside.** A Redis timeout should become a miss, not a 500. Wrap the client with short timeouts and a circuit breaker.
- **Invalidation scattered across services.** One forgotten writer (an admin script, a migration, another service) leaves stale data. Centralize invalidation with change data capture.

## Interview questions

**Q: Cache-aside or read-through?**
Both load lazily on a miss. In cache-aside the application calls the database and populates the cache; in read-through the cache calls a registered loader. Read-through centralizes loading and can coalesce concurrent misses, but needs a cache that supports loaders and puts the cache in the critical path.

**Q: After updating the database, should you update or delete the cache entry?**
Delete. Concurrent updates can reach the cache out of order and leave a stale value; deletes commute. The next read loads the current row.

**Q: Update the database first or delete the cache first?**
Database first, then delete after commit. Deleting first leaves a window where a reader caches the old row before your write lands. Even database-first has a smaller race, which leases or versioned sets close.

**Q: Write-through vs write-behind?**
Write-through writes the database synchronously before acknowledging: fresh cache, slower writes, no extra loss risk. Write-behind acknowledges after the cache write and persists later: fast, batched writes, but unflushed data is lost on a crash and the database lags.

**Q: What happens to each pattern when the cache cluster goes down?**
Cache-aside degrades to direct database reads (if the database can take it). Read-through and write-through fail unless they have a bypass. Write-behind loses unflushed writes and must fall back to direct writes.

**Q: Which pattern for a post view counter, a user's settings page, and a product catalog?**
Write-behind for the counter (high write rate, small loss acceptable). Write-through or cache-aside with delete for settings (users expect to see their change). Cache-aside with a TTL and event-driven invalidation for the catalog (read-heavy, rarely written).

**Q: How do you make write-behind safe enough for important data?**
Make the first write durable (append to Kafka or a Redis AOF with replicas), make flushes idempotent with versions or batch ids, bound how long data can stay dirty, and alert on flush lag. If you need all of that, consider writing to the database directly.

## Further reading

- AWS whitepaper, "Database Caching Strategies Using Redis"
- Microsoft Azure Architecture Center, "Cache-Aside pattern"
- Amazon DynamoDB Developer Guide, the DynamoDB Accelerator (DAX) chapter
- Nishtala et al., "Scaling Memcache at Facebook", NSDI 2013
- Martin Kleppmann, "Designing Data-Intensive Applications" (O'Reilly)
