---
title: Design an Instagram backend
section: backend
order: 75
phase: "Phase 10 · System design case studies"
summary: "Photo upload pipeline, feed fan-out on write vs read with a celebrity hybrid, stories, counters at scale, the follow graph, explore ranking and CDN."
---

Instagram is the canonical read-heavy social product: a few posts per user, hundreds of feed views per post, and media bandwidth that dwarfs everything else. The interview centers on feed generation (push, pull, or both), keeping like counts from melting a database row, and getting images to a billion phones cheaply. The structure follows [the design framework]({{ '/backend/design-framework/' | relative_url }}); the closely related text-first version is [Design a Twitter backend]({{ '/backend/design-twitter/' | relative_url }}).

## Requirements

### Clarifying questions

- Photos only, or video too? Photos and short videos; video transcoding is covered in [Design a YouTube backend]({{ '/backend/design-youtube/' | relative_url }}).
- Is the feed chronological or ranked? Ranked, with recent posts from followed accounts as the main candidates.
- Do counts need to be exact? Eventually exact; a few seconds of staleness is fine.
- Private accounts? Yes; only approved followers see posts.

### Functional

1. Upload a post with one or more photos and a caption.
2. Follow and unfollow accounts.
3. Home feed of posts from followed accounts, ranked.
4. Stories that disappear after 24 hours.
5. Like and comment, with visible counts.
6. Explore: recommended posts from accounts you don't follow.

### Non-functional

| Property | Target |
|---|---|
| Daily active users | 500M |
| New posts | 100M/day |
| Feed loads | 5B/day (10 per user) |
| Likes | 4B/day |
| Feed latency | p99 < 300 ms server-side |
| Post visibility | In followers' feeds within seconds |
| Availability | 99.95% for feed and upload |
| Consistency | Eventual for feeds and counts; read-your-writes for your own actions |

### Out of scope

Direct messages, ads, search, Reels-specific ranking, creator analytics.

## Capacity estimates

**Writes.** 100M posts/day ≈ 1,160/s, about 3,500/s at peak. Likes: 4B/day ≈ 46,000/s, about 140,000/s at peak. Comments: 500M/day ≈ 5,800/s.

**Reads.** 5B feed loads/day ≈ 58,000/s, about 175,000/s at peak. Read:write for posts is about 50:1 by request and far higher by bytes.

**Fan-out.** With an average of 200 followers per author, push-based fan-out writes 100M × 200 = 20B feed entries/day ≈ 230,000/s. That's heavy but feasible for a Redis cluster with pipelining. The average hides the problem: one post from an account with 300M followers is 300M writes.

**Media storage.** An original photo after client compression is about 2 MB; renditions at several widths add about 0.5 MB. 100M × 2.5 MB = 250 TB/day, about 90 PB/year before redundancy. Originals of old posts move to cold storage with erasure coding.

**Metadata.** About 1 KB per post: 100 GB/day, 36 TB/year. Comments add a similar amount.

**Feed cache.** 300 entries per active user × 16 bytes (post ID plus author ID) ≈ 4.8 KB; × 500M users ≈ 2.4 TB raw, several TB with data-structure overhead. Feeds are kept only for recently active users.

**Egress.** 5B feed loads × 10 images × 150 KB (mobile rendition) ≈ 7.5 PB/day ≈ 700 Gbps average. Only a CDN makes that viable.

## API design

| Endpoint | Purpose |
|---|---|
| `POST /v1/uploads` | Start an upload; returns `upload_id` and a presigned URL |
| `POST /v1/posts` | `{upload_ids, caption, client_post_id}` → `202 {post_id, status: "processing"}` |
| `GET /v1/feed?cursor=&limit=20` | Ranked home feed |
| `PUT /v1/posts/{id}/like` / `DELETE` | Idempotent like and unlike |
| `POST /v1/posts/{id}/comments` / `GET ...?cursor=` | Comments |
| `PUT /v1/users/{id}/follow` / `DELETE` | Follow and unfollow |
| `POST /v1/stories`, `GET /v1/stories/tray` | Post a story, load the tray |
| `POST /v1/stories/{id}/seen` | Mark seen |
| `GET /v1/explore?cursor=` | Recommendations |

```json
{
  "items": [
    {
      "post_id": "3141592653589793238",
      "author": {"id": "17", "username": "maya"},
      "media": [
        {"url": "https://cdn.example.com/p/ab12_1080.jpg", "width": 1080}
      ],
      "caption": "Morning run",
      "like_count": 1284,
      "comment_count": 37,
      "viewer_has_liked": false,
      "created_at": "2026-11-16T08:12:00Z"
    }
  ],
  "next_cursor": "eyJzZXNzaW9uIjoiOWYyIiwib2Zmc2V0IjoyMH0"
}
```

`client_post_id` makes post creation [idempotent]({{ '/backend/idempotency/' | relative_url }}) across mobile retries. Feed pagination uses opaque cursors ([pagination]({{ '/backend/pagination/' | relative_url }})).

## Data model

Core entities (users, posts, follows) live in sharded PostgreSQL: they are relational, need per-shard transactions, and are read by primary key. Instagram has publicly described this approach: thousands of logical shards mapped onto fewer physical PostgreSQL servers, with 64-bit IDs generated inside the database from 41 bits of milliseconds, 13 bits of logical shard ID, and 10 bits of per-shard sequence. IDs sort by time and encode their shard, so any service can route a post ID without a lookup ([partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }})).

| Table | Store | Key / partition | Notes |
|---|---|---|---|
| `users` | PostgreSQL | `user_id` | Profile, privacy flag |
| `posts` | PostgreSQL, by `author_id` | `post_id`; index `(author_id, post_id DESC)` | Caption, media keys, status |
| `following` | PostgreSQL, by `follower_id` | `(follower_id, followee_id)` | "Whom do I follow" |
| `followers` | PostgreSQL, by `followee_id` | `(followee_id, follower_id)` | "Who follows me"; drives fan-out |
| `likes` | Cassandra, by `post_id` | `(post_id, user_id)` | Primary key makes likes idempotent |
| `post_counters` | PostgreSQL or Redis + durable store | `post_id` | `like_count`, `comment_count` |
| `comments` | Cassandra, by `post_id` | clustering `comment_id DESC` | Append-heavy, paged |
| `feed_inbox` | Redis sorted set per user | `user_id` → `post_id` scored by time | Rebuildable cache |
| `stories` | Cassandra, by `author_id`, TTL 24 h | clustering `story_id` | Ephemeral |
| `story_views` | Cassandra, by `story_id` | clustering `viewer_id` | Write-heavy |

Wide-column storage takes the append-heavy, high-cardinality data (likes, comments, story views) where the access pattern is always "by post" or "by story". The feed inbox is a cache: if it is lost, it is rebuilt from the graph and posts.

## High-level design

```text
 clients --- media GET ---> CDN --- miss ---> object storage
    |                                          ^          ^
    | API                    presigned PUT     |          | renditions
    v                    (raw bytes) ----------+          |
 +-------------+                                  +-------+-------+
 | API gateway |                                  | media workers |
 +------+------+                                  | resize, scan  |
        |                                         +-------+-------+
        +--> Upload svc ---- upload.done ---------------->|
        |                                                 | media.ready
        +--> Post svc -----> PostgreSQL shards            v
        |                         +--- post.published --> Kafka
        |                                                 |
        |                              +------------------+------+
        |                              v                         v
        +--> Feed svc <--- Redis <--- fan-out workers    counters,
        |       |          inboxes                       explore,
        |       +--- pull celebrity posts from Post svc  search
        +--> Engagement svc --> likes, comments, counters
        +--> Graph svc -------> following / followers
        +--> Stories svc -----> Cassandra (24 h TTL), seen state
        +--> Explore svc -----> candidate stores, ranking models
```

### Post flow

1. The client uploads compressed media straight to object storage with a presigned URL, then calls `POST /v1/posts`.
2. The post service writes the post as `processing` and emits an event; media workers produce renditions and run safety checks.
3. When media is ready, the post flips to `published` and a `post.published` event goes to Kafka through a transactional outbox, so the database write and the event can't diverge ([event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }})).
4. Fan-out workers push the post ID into followers' inboxes, unless the author is a celebrity.

### Feed flow

1. The feed service reads the user's inbox, pulls recent posts from followed celebrities, and adds a few recommended posts.
2. A ranking service scores the candidates; the top results are hydrated (post data, author, counts, `viewer_has_liked`) from caches with batched multi-gets.
3. The ranked list is stored per session so the cursor pages through a stable order.

## Deep dives

### Photo upload pipeline

The client does the first round of work: it resizes to a maximum dimension, compresses, and strips most metadata before upload, which cuts upload size several-fold on mobile networks. Uploads go directly to object storage through presigned URLs, so API servers never stream bytes. Large files and flaky connections use resumable chunked uploads.

Media workers consume `upload.done` events and, per photo:

1. Validate the format, fix orientation, and strip remaining EXIF data (location privacy).
2. Generate renditions (for example 1080, 640 and 320 pixels wide) in modern formats, plus a tiny placeholder for progressive loading.
3. Compute a perceptual hash for duplicate and known-bad-content matching, and run safety classifiers.
4. Write renditions under immutable keys and mark the media ready.

Workers are stateless and idempotent: output keys are derived from the media ID and rendition, so a retry overwrites the same objects. Failed jobs retry with backoff and land in a [dead-letter queue]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}) after a few attempts. The author sees their post immediately from the local copy, which hides processing latency.

### Feed generation: fan-out on write vs read

| Model | Write cost | Read cost | Freshness | Breaks on |
|---|---|---|---|---|
| Fan-out on write (push) | One inbox write per follower | One inbox read | Seconds | Celebrities: 300M writes per post |
| Fan-out on read (pull) | One post write | Query every followee, merge | Immediate | Users following thousands of accounts; read QPS × followees |
| Hybrid | Push for normal authors | Inbox + pull from followed celebrities | Seconds | Tuning the threshold |

**Push.** On publish, workers page through the author's followers and append the post ID to each inbox (a Redis sorted set capped at a few hundred entries). Reads are a single range query. Two refinements matter: skip users inactive for 30+ days (build their feed on next login instead), and process large follower lists in parallel batches.

**Pull.** At read time, fetch recent posts from every followee and merge. No write amplification, but a user following 1,000 accounts turns one feed load into 1,000 lookups, at 175,000 feed loads/s.

**Hybrid.** Accounts above a follower threshold (somewhere in the tens of thousands to millions, tuned on cost) are not fanned out. At read time the feed service merges the inbox with recent posts from the handful of celebrities the user follows. Since most users follow few celebrities, the pull side stays small, and the push side never sees a 300M-follower post. The demo below also shows two details interviewers like: unfollows are filtered lazily at read time, and a post can be both pushed and pulled when an author crosses the threshold, so the merge dedupes.

```python
import heapq
import itertools


class FeedService:
    """Hybrid fan-out: push for normal authors, pull for celebrities."""

    def __init__(self, celebrity_threshold, inbox_cap=500):
        self.followers = {}       # author -> set of followers
        self.following = {}       # user -> set of authors
        self.posts = {}           # author -> [(ts, post_id, author)]
        self.inbox = {}           # user -> pushed [(ts, post_id, author)]
        self.threshold = celebrity_threshold
        self.inbox_cap = inbox_cap
        self.clock = itertools.count(1)
        self.fanout_writes = 0

    def follow(self, user, author):
        self.followers.setdefault(author, set()).add(user)
        self.following.setdefault(user, set()).add(author)

    def unfollow(self, user, author):  # inbox cleaned lazily at read time
        self.followers.get(author, set()).discard(user)
        self.following.get(user, set()).discard(author)

    def is_celebrity(self, author):
        return len(self.followers.get(author, ())) >= self.threshold

    def publish(self, author, post_id):
        item = (next(self.clock), post_id, author)
        self.posts.setdefault(author, []).append(item)
        if self.is_celebrity(author):
            return                       # pulled at read time instead
        for follower in self.followers.get(author, ()):
            box = self.inbox.setdefault(follower, [])
            box.append(item)
            del box[:-self.inbox_cap]    # keep the newest N
            self.fanout_writes += 1

    def feed(self, user, limit=10):
        following = self.following.get(user, set())
        pushed = self.inbox.get(user, [])[-limit:]
        pulled = [self.posts.get(a, [])[-limit:]
                  for a in following if self.is_celebrity(a)]
        out, seen = [], set()
        for ts, post_id, author in heapq.nlargest(
                limit * 2, itertools.chain(pushed, *pulled)):
            if author in following and post_id not in seen:
                seen.add(post_id)        # a post can be pushed and pulled
                out.append(post_id)
        return out[:limit]


if __name__ == "__main__":
    svc = FeedService(celebrity_threshold=3)
    for fan in ("alice", "u1", "u2"):
        svc.follow(fan, "star")          # star now counts as a celebrity
    svc.follow("alice", "bob")

    svc.publish("bob", "b1")
    svc.publish("star", "s1")
    svc.publish("bob", "b2")
    assert svc.fanout_writes == 2        # star's post wrote nothing
    assert svc.feed("alice") == ["b2", "s1", "b1"]
    assert svc.feed("u1") == ["s1"]

    svc.unfollow("alice", "bob")
    assert svc.feed("alice") == ["s1"]   # stale inbox entries filtered

    svc.follow("carol", "dan")
    svc.publish("dan", "d1")             # pushed while dan is small
    for fan in ("x", "y"):
        svc.follow(fan, "dan")           # dan crosses the threshold
    svc.publish("dan", "d2")
    assert svc.feed("carol") == ["d2", "d1"]   # no duplicate d1
    print("fan-out writes:", svc.fanout_writes, "ok")
```

Ranking sits on top: Instagram publicly moved from a chronological to a ranked feed in 2016. Candidates from the inbox and celebrity pull are scored by a model predicting engagement, then lightly diversified so one author doesn't fill the screen.

### Likes and comments counters at scale

A like is two things: a fact (`user 42 likes post 9`) and a number (`1,284 likes`). Store the fact as a row keyed `(post_id, user_id)`. The primary key makes double-taps and retries idempotent, and it is the source of truth.

Never update the counter row synchronously per like. A celebrity post can draw tens of thousands of likes per second, all contending on one row. Options:

- **Aggregate in the stream.** Each new like emits an event; a stream processor sums per post over one-second windows and issues one `like_count = like_count + n` per post per window. Writes drop by orders of magnitude for hot posts.
- **Sharded counters.** Split a hot post's counter into N rows (`post_id, shard`) incremented at random, summed and cached on read.
- **Redis as the display counter.** `INCRBY` in Redis, flushed periodically to the durable store. If Redis loses data, a reconciliation job recounts from the likes table.

Display is approximate above a threshold ("1.2M likes"), which hides staleness. Read-your-writes is handled on the client: the heart turns red immediately. For `viewer_has_liked` on 20 feed posts, the feed service batch-checks the likes table, or a per-user cache of recent likes. Comments follow the same split: rows in Cassandra by `post_id`, counts aggregated asynchronously, spam filtering off the write path.

### Follow graph

Edges are stored twice, once sharded by follower ("whom I follow", used by pull and the feed) and once by followee ("my followers", used by fan-out). The two rows usually live on different shards, so a follow writes one direction synchronously and the other through an outbox event, with a periodic reconciliation job catching drift. Follower and following counts are maintained asynchronously, like likes.

Read volume is high (feeds, profiles, "followed by" hints), so a read-through graph cache sits in front. Facebook's TAO is the well-known public example of a cache built for this association-list access pattern. Two behaviors worth stating:

- **Follow** triggers a backfill: the followee's last few posts go into the follower's inbox so the new follow shows up immediately.
- **Unfollow** is filtered at read time and cleaned from the inbox asynchronously.

Private accounts add follow requests and an authorization check on every read of their posts, including at the CDN through signed URLs. Celebrity follower lists (hundreds of millions of rows) are only ever paged by cursor, never loaded whole.

### Stories

Stories are ephemeral (24 hours), highly visual, and consumed in a tray at the top of the app. Store story metadata in Cassandra with a 24-hour TTL and media under an object-storage lifecycle rule; copying to permanent storage happens only if the author saves it to highlights.

The tray answers "which followees have unseen active stories, in what order." A user posts few stories but views many, so the tray is built mostly on read: load the following list, multi-get each followee's `latest_story_at` from cache, keep those within 24 hours, and rank by closeness and unseen status. For very active users, the same hybrid as the feed applies: push a "has new story" marker into followers' tray inboxes for normal authors, pull for celebrities.

Per-viewer seen state (`viewer → author → last_seen_story_id`) decides ordering and the colored ring. Authors see who viewed each story, which makes `story_views` the write-heaviest table: tens of billions of rows per day, written once and read rarely, partitioned by story and expired with it. Clients prefetch the first frames of the next stories so tapping through feels instant.

### Explore and ranking

Explore recommends content from accounts the user doesn't follow, which means searching a huge space within a few hundred milliseconds. Meta has publicly described Explore as a multi-stage system; the general shape is:

1. **Candidate generation.** Retrieve a few thousand candidates from sources such as embedding nearest neighbors (accounts and posts similar to what the user engaged with) and co-engagement ("people who liked X also liked Y").
2. **Filtering.** Remove already-seen posts, blocked accounts, and content that fails integrity checks.
3. **Ranking in stages.** A cheap model cuts thousands to hundreds; a heavy model predicts several outcomes (like, save, share, "see fewer") and combines them into one score.
4. **Blending.** Apply diversity rules (authors, topics) and mix formats.

Embeddings and models are trained offline on engagement logs. Candidate lists for active users can be precomputed every few hours, with real-time features (what you engaged with in the last minutes) applied at ranking time. The same infrastructure feeds the "suggested posts" appended to the home feed.

### CDN

Media is immutable once processed: a URL names exactly one rendition, so it can be cached with `Cache-Control: public, max-age=31536000, immutable`. Edits create new URLs instead of invalidating old ones. Clients choose the rendition width for their screen, and format variants get distinct URLs rather than relying on `Vary`, which fragments caches.

Layers: edge PoPs close to users, a regional origin shield that collapses concurrent misses into one origin fetch, then object storage. A celebrity post produces a sudden burst of requests for the same few objects; the shield and request collapsing keep that from becoming an origin stampede ([distributed caching and CDN]({{ '/backend/distributed-caching-and-cdn/' | relative_url }})). Private-account media is served through signed URLs with short expiry, so a leaked URL stops working and deleted posts age out without a purge. At this scale, large platforms run their own edge networks plus commercial CDNs, and the edge hit ratio is the single biggest cost lever in the system.

## Scaling and bottlenecks

**At 10×.** Fan-out reaches millions of inbox writes per second, so feeds are kept only for recently active users and the celebrity threshold drops, shifting cost from write to read. PostgreSQL scales by remapping logical shards onto more physical servers, which is why logical shards are created up front. Hot posts stress counters and hydration caches; aggregate counters in streams and keep post objects in local in-process caches for a few seconds.

**At 100×.** Single-region writes and cross-region reads become the problem. Each user gets a home region for writes; posts and graph data replicate asynchronously to other regions; fan-out runs where followers' inboxes live. Media storage grows by exabytes, so cold originals move to cheaper tiers and rarely viewed renditions are regenerated on demand rather than stored ([replication]({{ '/backend/replication/' | relative_url }})).

## Reliability and failure modes

| Failure | Behavior |
|---|---|
| Feed cache shard lost | Rebuild affected inboxes from graph and posts in pull mode; slower feeds, no data loss |
| Fan-out lag | Posts appear late; alert on consumer lag; autoscale workers |
| Ranking service down | Fall back to reverse-chronological inbox |
| Counter pipeline down | Show last cached counts; likes still recorded as rows |
| Media worker failures | Retries, DLQ; post stays `processing`; author sees local preview |
| PostgreSQL primary failure | Replica promoted; asynchronous replication may lose the last moments of writes, so clients retry with `client_post_id` |
| CDN PoP failure | DNS or anycast steers users to the next PoP |

Graceful degradation is the theme: every enrichment (ranking, counts, suggestions) is optional, and the feed still renders without it ([fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }})).

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Feed | Hybrid fan-out | Yes | Push keeps reads cheap; pull caps celebrity cost |
| | Pure push | No | One post can mean hundreds of millions of writes |
| | Pure pull | No | Read cost grows with followee count |
| Core store | Sharded PostgreSQL with logical shards | Yes | Relational data, transactions, easy rebalancing |
| Engagement store | Cassandra | Yes | Append-heavy, partitioned by post |
| Counters | Stream-aggregated, approximate display | Yes | Removes hot-row contention |
| | Synchronous row update | No | Hot posts serialize on one row |
| Stories tray | Built on read, hybrid for heavy users | Yes | Few posts, many views, 24 h lifetime |
| Media URLs | Immutable, long TTL | Yes | Near-perfect CDN cacheability |
| Upload | Direct to object storage | Yes | API servers never proxy bytes |

## Interview follow-ups

**Why not fan-out on write for everyone?**
A single post from an account with 300M followers would mean 300M inbox writes, taking minutes and competing with every other post. Pulling celebrity posts at read time costs a few extra lookups per feed load.

**How do you pick the celebrity threshold?**
By cost: compare the write cost of pushing to N followers against the read cost of pulling for those followers' feed loads. Make it a dynamic setting, and consider posting frequency as well as follower count.

**How do you paginate a ranked feed without duplicates?**
Rank once per session, store the ordered list of post IDs in cache, and make the cursor an index into it. New posts appear on the next refresh, not mid-scroll.

**How do you make like counts correct in the end?**
The likes table is the source of truth. Counters are a derived, cached aggregate, reconciled periodically by recounting rows for posts whose counters drifted.

**What happens when a user deletes a post?**
Soft-delete the post; feeds filter deleted IDs at hydration; an async job removes it from inboxes; media URLs stop being issued and expire at the CDN.

**How do you show "liked by alice and 1,203 others"?**
Intersect the post's recent likers with the viewer's following set, using a capped sample of likers from cache. Exactness isn't needed; picking one or two friends is.

**A user follows 7,500 accounts. Does the design still work?**
Yes. Pushed posts make the follow count irrelevant on read, and products cap follows anyway. Only the celebrity pull grows, and that's bounded by how many celebrities they follow.
