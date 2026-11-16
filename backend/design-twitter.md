---
title: Design a Twitter/X backend
section: backend
order: 80
phase: "Phase 10 · System design case studies"
summary: Tweet writes, fan-out timelines with the celebrity fix, Snowflake IDs, real-time search, trends with a count-min sketch, notifications and counters.
---

Twitter is the canonical read-heavy social system with a twist: a single write can need to reach 100 million readers within seconds. The design centers on one question, when to do the work of building each user's home timeline: at write time (fan-out on write), at read time (fan-out on read), or a mix. Search, trends, counters and notifications all hang off the same stream of new tweets.

## Requirements

### Clarifying questions

- Chronological home timeline, ranked, or both? Both; start with chronological, add ranking.
- Media? Yes, uploaded separately to object storage and referenced by ID.
- How fresh must the timeline be? A new tweet should reach most followers within ~5 s.
- Edits and deletes? Deletes yes; edits as new versions, out of scope.

### Functional

- Post a tweet (280 characters, optional media), reply, retweet, quote; delete.
- Follow and unfollow.
- Home timeline (tweets from people you follow), user timeline (one author's tweets).
- Like; see like, retweet and reply counts.
- Search recent tweets; see trending topics per location.
- Notifications for mentions, replies, likes and new followers.

### Non-functional

| Property | Target |
|---|---|
| Daily active users | 250M (design target) |
| Tweets | 500M/day; spikes 10× or more during live events |
| Home timeline load | p99 under 300 ms |
| Tweet visible to followers | within ~5 s for most followers |
| Availability | 99.99% for reads; posting may degrade but should not fail silently |
| Consistency | eventual for timelines and counts; read-your-own-writes for the author |
| Read/write | timelines read ~100× more than tweets are written |

### Out of scope

DMs, ads, Spaces, moderation pipelines, edit history.

## Capacity estimates

```text
tweet writes    = 500M / 86,400                  = ~5.8k/s avg, ~15k/s peak
event spike     = Twitter publicly reported ~143k tweets/s in 2013
timeline reads  = 250M x 20 / 86,400             = ~58k/s avg, ~200k/s peak
likes           = 5B/day                         = ~58k/s avg
fan-out inserts = 500M x 200 avg followers       = 100B/day = ~1.2M/s
                  (far less with the celebrity and inactive-user rules)

tweet storage   = 500M x ~300 B (text + meta)    = 150 GB/day, ~55 TB/year
media           = 10% of tweets x ~1 MB          = ~50 TB/day (object store)
timeline cache  = 250M x 800 entries x 16 B      = ~3.2 TB (x3 = ~10 TB)
search index    = 7 days x 500M x ~1 KB          = ~3.5 TB hot
```

Fan-out inserts are the dominant write load, about 200× the tweet rate. The timeline cache is the dominant memory: on the order of 100 Redis nodes. Twitter's widely cited "Timelines at Scale" talk described the same shape: Redis-backed home timelines of a few hundred tweet IDs per user, built by fan-out at write time.

## API design

```http
POST /v1/tweets                       Idempotency-Key: 9a2f...
{"text": "Shipping the new API today", "mediaIds": ["m_81"],
 "replyToId": null}
201 {"id": "1858283749235625984", "createdAt": "2026-11-16T18:00:00Z"}

DELETE /v1/tweets/{id}

GET /v1/timelines/home?maxId=1858283749235625983&limit=50
200 {"tweets": [{"id": "...", "author": {"id": "u7", "handle": "ana"},
                 "text": "...", "likes": 1203, "retweets": 88}],
     "nextMaxId": "1858283700000000000"}

GET /v1/users/{userId}/tweets?maxId=...
POST /v1/users/me/following           {"userId": "u7"}
PUT /v1/tweets/{id}/like              (idempotent; DELETE to unlike)
GET /v1/search?q=eclipse&mode=latest&maxId=...
GET /v1/trends?location=IN
GET /v1/notifications?cursor=...
```

Because tweet IDs are time-ordered, they double as pagination cursors: `maxId` means "older than this", `sinceId` means "newer than this". No offsets, and new tweets arriving at the top do not shift pages; see [pagination]({{ '/backend/pagination/' | relative_url }}). `PUT` for like makes double taps harmless.

## Data model

| Data | Store | Key / index | Notes |
|---|---|---|---|
| Tweets | Distributed KV or sharded SQL | tweet_id (hash-sharded) | Immutable body; deletes are tombstones |
| User timelines | Wide-column / sharded SQL | (author_id, tweet_id DESC) | Range scan for a profile page |
| Follow graph | Two tables, each sharded by its first column | (follower_id, followee_id), (followee_id, follower_id) | Forward for "who I follow", reverse for fan-out |
| Home timelines | Redis lists | user_id -> [tweet_id, author_id, flags] capped ~800 | Rebuildable cache, not truth |
| Likes | Wide-column | (tweet_id, user_id) and (user_id, tweet_id) | Primary key makes a like idempotent |
| Counters | Counter service | tweet_id -> likes, retweets, replies, views | Aggregated, eventually consistent |
| Search | Real-time inverted index | term -> postings, partitioned by time | Recent in memory, older on SSD |
| Media | Object storage + CDN | media_id | Referenced from tweets |

Twitter has publicly described building its own stores for this, such as Manhattan (a distributed key-value store) and, earlier, FlockDB for the follow graph. Any store that does fast key lookups and range scans per partition fits; the key choices matter more than the product.

## High-level design

```text
            POST /tweets                     GET /timelines/home
                 |                                   |
                 v                                   v
         +---------------+                  +----------------+
         | tweet service |                  | timeline svc   |
         | Snowflake id  |                  | merge, rank,   |
         +---------------+                  | hydrate        |
           |         |                      +----------------+
           v         v                        |      |      |
      +--------+  +-------+                   v      v      v
      | tweets |  | Kafka |             +-------+ +------+ +-------+
      | store  |  |tweets |             | home  | |tweet | |celeb  |
      +--------+  +-------+             | lists | |cache | |recent |
                    |  |  |  |          |(Redis)| |      | |tweets |
        +-----------+  |  |  +-----+    +-------+ +------+ +-------+
        v              v  v        v        ^
   +---------+   +------+ +------+ +-----+  |
   | fan-out |   |search| |trends| |notif|  |
   | workers |   |index | |(CMS) | | svc |  |
   +---------+   +------+ +------+ +-----+  |
        |  followers from graph service     |
        +-------- LPUSH + LTRIM ------------+
```

### Tweet write path

1. **Media first.** Images and video upload separately to object storage and return media IDs; the tweet only references them, so the tweet write stays small and fast.
2. **Validate.** Auth, length (280 weighted characters; links count as a fixed length after shortening), spam and rate checks, idempotency key lookup.
3. **Persist.** Assign a Snowflake ID and write the tweet row plus an outbox row in one local transaction on the tweet's shard. The author gets 201 now.
4. **Publish.** A relay moves the outbox row to the Kafka `tweets` topic. Consumers add the tweet to the author's user timeline (sharded by author_id), fan it out, index it for search, count it for trends and create notifications, each independently and idempotently keyed by tweet ID.
5. **Read your own writes.** The client inserts the tweet locally, and the timeline service merges the author's latest tweets on read, so the author never waits for fan-out.

**Fan-out.** Workers page through the author's followers, skip inactive ones, and push `(tweet_id, author_id)` onto each follower's Redis list (`LPUSH` then `LTRIM` to ~800).

### Timeline read path

The timeline service reads the user's list, merges recent tweets from any celebrities they follow, drops deleted or blocked content, hydrates tweet bodies and counts with a multi-get from the tweet cache, and returns a page.

## Deep dives

### Timelines: fan-out on write, read, or hybrid

| Approach | Write cost | Read cost | Good for | Bad for |
|---|---|---|---|---|
| Fan-out on write (push) | One insert per follower | One list read | Users with normal follower counts; fast reads | Celebrities: 100M inserts per tweet |
| Fan-out on read (pull) | One insert | Merge N followees' recent tweets | Celebrities; inactive readers | Users following 2,000 accounts: slow reads |
| Hybrid | Push for most authors | Pull only for followed celebrities | Both | Two code paths |

The hybrid is the standard answer. Details that interviewers probe:

- **Inactive users.** Do not fan out to users who have not logged in for 30 days. When they return, rebuild their list by pulling recent tweets from followees, asynchronously, and serve a partial timeline meanwhile.
- **Cache miss.** A home list is a cache. If it is evicted or a Redis node dies, rebuild the same way. Replicate the lists so one node loss does not trigger a rebuild storm.
- **Deletes.** A deleted tweet becomes a tombstone; hydration drops it at read time immediately, and an async job removes its ID from lists.
- **Unfollow.** Filter at read time against the current follow set; let the stale entries age out of the capped list.
- **Ranking.** A ranked "For you" timeline treats the home list as one candidate source, adds out-of-network candidates (from embeddings and engagement graphs), scores them with a model and filters. Twitter open-sourced much of its recommendation pipeline in 2023; the chronological list remains the backbone.

### Celebrities and hot keys

An account with 100M followers would need 100M list inserts per tweet; at 1M inserts/s that is minutes of lag and a flood of Redis writes. Set a threshold (say 100k–1M followers): above it, skip fan-out. At read time, the timeline service looks up which celebrities the user follows (a short list) and merges their recent tweets.

That moves the load to reads of a few very hot keys: every timeline load reads the same celebrity's recent tweets. Handle it like any hot key: keep those lists in an in-process cache on each timeline server with a TTL of a second or two, replicate them across many cache nodes, and coalesce concurrent misses into one fetch. The same applies to a viral tweet's body and counts. See [cache stampede]({{ '/backend/cache-stampede/' | relative_url }}).

### Snowflake IDs

Tweet IDs must be unique across hundreds of servers with no coordination per ID, fit in 64 bits, and sort by time so they work as cursors and as clustering keys. Twitter's Snowflake (open-sourced in 2010) packs a 41-bit millisecond timestamp, a 10-bit machine ID and a 12-bit sequence: 4,096 IDs per millisecond per machine and about 69 years of range from a custom epoch.

```python
import threading
import time

EPOCH_MS = 1288834974657   # epoch of Twitter's open-source Snowflake


class Snowflake:
    """41-bit ms timestamp | 10-bit machine id | 12-bit per-ms sequence."""

    def __init__(self, machine_id,
                 clock=lambda: time.time_ns() // 1_000_000):
        if not 0 <= machine_id < 1024:
            raise ValueError("machine_id must fit in 10 bits")
        self.machine_id, self.clock = machine_id, clock
        self.last_ms, self.seq = -1, 0
        self.lock = threading.Lock()

    def next_id(self):
        with self.lock:
            now = self.clock()
            if now < self.last_ms:
                raise RuntimeError("clock moved backwards; refusing IDs")
            if now == self.last_ms:
                self.seq = (self.seq + 1) & 0xFFF
                if self.seq == 0:                 # 4,096 IDs this ms: wait
                    while now <= self.last_ms:
                        now = self.clock()
            else:
                self.seq = 0
            self.last_ms = now
            ts = now - EPOCH_MS
            return (ts << 22) | (self.machine_id << 12) | self.seq


def parse(snowflake_id):
    return {
        "ms": (snowflake_id >> 22) + EPOCH_MS,
        "machine": (snowflake_id >> 12) & 0x3FF,
        "seq": snowflake_id & 0xFFF,
    }


if __name__ == "__main__":
    gen = Snowflake(machine_id=42)
    ids = [gen.next_id() for _ in range(20_000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)   # unique, sorted
    assert all(parse(i)["machine"] == 42 for i in ids)
    assert abs(parse(ids[-1])["ms"] - time.time() * 1000) < 5_000

    ticks = iter([1_700_000_000_000, 1_699_999_999_000])       # NTP step back
    skewed = Snowflake(machine_id=1, clock=lambda: next(ticks))
    skewed.next_id()
    try:
        skewed.next_id()
        raise AssertionError("expected a clock error")
    except RuntimeError:
        pass
    print("ok", ids[0], parse(ids[0]))
```

Operational points: machine IDs are leased from a coordinator (ZooKeeper or etcd) so two processes never share one; clocks are kept with NTP slewing, and a backwards step makes the generator refuse rather than risk duplicates. IDs from different machines in the same millisecond are not ordered relative to each other, which is fine: they are "k-sorted", close enough for timelines.

### Search

Search over tweets is unusual: the corpus grows by 500M documents a day, and users mostly want the last few minutes. Twitter has publicly described Earlybird, a Lucene-based real-time index, for this. A design in that spirit:

- **Ingest.** Indexers consume the tweets topic and add documents to an in-memory index segment within seconds of posting.
- **Partitioning.** Documents are partitioned by tweet ID across shards (each holds a slice of all terms), and by time into tiers: the current days in memory, older weeks on SSD, the full archive on a separate cluster.
- **Query.** A front end scatters the query to all partitions of the relevant tiers, each returns its top hits by a score mixing text relevance, recency and engagement, and the front end merges them. "Latest" mode is mostly a reverse-chronological scan of postings, which is fast because postings are appended in ID order.
- **Updates.** Engagement counts change after indexing; keep them in a side store joined at ranking time instead of re-indexing documents.

### Trends

A trend is not the most-used hashtag (that is always something like "#news"); it is a term whose rate jumped relative to its own baseline in a location. Pipeline:

1. A stream job (Flink, or Heron, which Twitter built to replace Storm) extracts hashtags and phrases per tweet with location.
2. Per location and one-minute bucket, count terms with a count-min sketch: fixed memory regardless of how many distinct terms appear, with a bounded overestimate.
3. Track heavy hitters per bucket alongside the sketch.
4. Score each candidate as current rate against its baseline (for example, last hour vs the same hour over past days), filter spam and bots, and publish the top N to a cache read by `GET /trends`.

```python
import hashlib
import random


class CountMinSketch:
    """Fixed-memory frequency counts; overestimates, never underestimates.
    Error <= (e / width) * total with probability 1 - exp(-depth)."""

    def __init__(self, width=2048, depth=5):
        self.width, self.rows = width, [[0] * width for _ in range(depth)]

    def _cols(self, key):
        h = hashlib.blake2b(key.encode(), digest_size=16).digest()
        a, b = int.from_bytes(h[:8], "big"), int.from_bytes(h[8:], "big") | 1
        return [(a + i * b) % self.width for i in range(len(self.rows))]

    def add(self, key, n=1):
        cols = self._cols(key)
        for row, c in zip(self.rows, cols):
            row[c] += n
        return min(row[c] for row, c in zip(self.rows, cols))

    def estimate(self, key):
        return min(row[c] for row, c in zip(self.rows, self._cols(key)))


class HeavyHitters:
    """Top-k candidates tracked next to the sketch (k is small)."""

    def __init__(self, k=10, **sketch_args):
        self.k, self.cms, self.top = k, CountMinSketch(**sketch_args), {}

    def add(self, key):
        est = self.cms.add(key)
        if key in self.top or len(self.top) < self.k:
            self.top[key] = est
        else:
            weakest = min(self.top, key=self.top.get)
            if est > self.top[weakest]:
                del self.top[weakest]
                self.top[key] = est

    def topk(self):
        return sorted(self.top, key=self.top.get, reverse=True)


if __name__ == "__main__":
    rng = random.Random(7)
    hot = {"#worldcup": 5000, "#election": 4000, "#eclipse": 3000,
           "#launch": 2000, "#outage": 1000}
    stream = [tag for tag, n in hot.items() for _ in range(n)]
    stream += [f"#tag{rng.randrange(60_000)}" for _ in range(100_000)]
    rng.shuffle(stream)

    hh = HeavyHitters(k=5)
    for tag in stream:
        hh.add(tag)
    assert hh.topk() == list(hot)                       # exact top 5, in order
    bound = 2.72 / hh.cms.width * len(stream)           # about 153
    for tag, true_count in hot.items():
        est = hh.cms.estimate(tag)
        assert true_count <= est <= true_count + bound, (tag, est)
    print("ok", hh.topk(), round(bound))
```

A 5 × 2,048 sketch is 10,240 counters (tens of KB) whether the stream has 60 or 60 million distinct terms. Sketches with the same shape and hash functions add element-wise, so per-minute sketches roll up into hourly ones and per-city into per-country. For large k, swap the dict scan for a min-heap.

### Notifications

Mentions, replies, likes, retweets and new followers are events on Kafka. A notification service consumes them, drops ones the recipient muted or blocked, aggregates bursts ("Ana and 37 others liked your post") by grouping on (recipient, tweet, type) over a short window, writes them to a per-user notification timeline (same shape as the home list), and hands push delivery to a shared notification platform with per-user rate limits. A celebrity's viral tweet generates millions of like events for one recipient; aggregation is what keeps that to a handful of rows. See [design a notification service]({{ '/backend/design-notification-service/' | relative_url }}).

### Counters

A viral tweet can take 50k likes a second. Incrementing one database row at that rate serializes on its lock.

- **Truth vs display.** The truth is the likes table: `(tweet_id, user_id)` as primary key, so a like is idempotent and unlike is a delete. The displayed number is a derived counter.
- **Aggregate before writing.** Like events go to Kafka keyed by tweet_id; a stream job sums them per tweet per second and applies one increment. 50k events become one write per second.
- **Shard hot counters.** Split a counter into N sub-counters (`tweet_id:shard`), increment a random one, sum on read, cache the sum for a second.
- **Accept approximation.** Counts are eventually consistent and may lag a few seconds; views can use HyperLogLog-style approximate distinct counts. Periodically reconcile the counter from the likes table.

## Scaling and bottlenecks

| Scale | What breaks first | Fix |
|---|---|---|
| 10× tweets | Fan-out workers and Redis write rate | Lower the celebrity threshold; skip inactive users; more Kafka partitions and Redis shards |
| 10× reads | Tweet hydration multi-gets | Larger tweet cache; in-process cache for hot tweets; coalescing |
| Live event spike | Kafka lag on fan-out, trends, search | Prioritize fan-out; let trends and analytics lag; autoscale consumers |
| 100× | Follow graph reads for fan-out | Cache follower lists for big accounts; page them in parallel |
| 100× | Timeline cache memory | Shorter lists (fewer than 800 entries), store only IDs, cache only active users |
| 100× | Search tiers | More partitions and replicas; shorter in-memory window |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Redis timeline node lost | Users on it get empty timelines | Replicas; rebuild from the graph and user timelines on miss |
| Fan-out lag | Tweets show up late | Alert on consumer lag; readers also merge followees' latest tweets when the list looks stale |
| Snowflake clock step back | Possible duplicate IDs | Generator refuses; machine drained until clock recovers |
| Kafka outage | No fan-out, search or trends | Tweet write still commits; an outbox replays events when Kafka returns |
| Hot tweet | Cache node overloaded | Replicated hot keys, in-process caches, request coalescing |
| Search partition down | Partial results | Return results from healthy partitions with a flag; replicas per partition |

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Home timeline | Pure fan-out on write | No | Celebrity writes explode |
| Home timeline | Pure fan-out on read | No | Slow reads for heavy followers |
| Home timeline | Hybrid with celebrity threshold | Yes | Fast reads, bounded writes |
| IDs | Database auto-increment | No | Single point of coordination |
| IDs | UUIDv4 | No | 128 bits, not time-sortable |
| IDs | Snowflake 64-bit | Yes | Unique without coordination, k-sorted, cursor-friendly |
| Trend counting | Exact hash map per window | No | Memory grows with distinct terms |
| Trend counting | Count-min sketch + heavy hitters | Yes | Fixed memory, mergeable, bounded error |
| Like counts | Synchronous row increment | No | Hot-row contention |
| Like counts | Idempotent likes table + aggregated counter | Yes | Exact truth, cheap display |

## Interview follow-ups

**How do you guarantee the author sees their own tweet immediately?** The client renders it optimistically, and the timeline service merges the author's latest tweets from their user timeline on read. Fan-out does not need to finish first.

**What if a user follows 5,000 accounts including 50 celebrities?** Their list holds pushed tweets; the 50 celebrity user timelines are read from hot caches and merged. The merge is a k-way merge of sorted ID lists, bounded by page size.

**Why cap timelines at ~800 entries?** Almost nobody scrolls further; deeper pages fall back to pull-based assembly. Capping keeps memory proportional to active users.

**How do you handle deleting a tweet that is already in 10M timelines?** Tombstone first: hydration filters it immediately. Then an async job removes the ID from lists; correctness does not depend on that job finishing.

**Can two Snowflake IDs collide?** Only if two generators share a machine ID or a clock goes backwards. Lease machine IDs with a coordinator, and refuse to issue IDs when the clock regresses.

**Why does a count-min sketch never undercount?** Every occurrence increments one counter per row; collisions only add other keys' counts. The minimum across rows is the least polluted estimate.

**How do you rank trends without always surfacing the same big hashtags?** Score by acceleration against each term's own baseline, not by raw volume, and apply per-location windows.
