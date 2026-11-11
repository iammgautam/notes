---
title: Design a news feed
section: hld
order: 16
summary: Build a home timeline with fan-out on write, fan-out on read, and the hybrid that handles celebrity accounts.
---

## Requirements

**Functional**
- Users post (text, media) and follow other users.
- Home feed shows posts from followed users, newest first (optionally ranked), paginated.
- Likes/comments counts shown on posts.

**Non-functional**
- Feed load p99 < 200 ms; very read-heavy.
- Eventual consistency OK (a post may appear a few seconds late), but authors see their own post immediately.
- Highly available. Follower counts are skewed (some users have 50 M followers).

## Estimates

- 300 M DAU, 10 feed loads/day → 3 B/day ≈ 35K QPS avg, ~100K peak.
- 1 post/user/5 days → 60 M posts/day ≈ 700 writes/s.
- Avg 200 followers → 60 M × 200 = 12 B feed inserts/day ≈ 140K/s if pushing to every follower.
- Feed cache: 300 M users × 500 post ids × 8 B ≈ 1.2 TB in Redis (store only ids). Cap per user.

## API

```http
POST /v1/posts                 {"text": "...", "media_ids": [...]}
GET  /v1/feed?cursor=<opaque>&limit=20
-> {"items": [{"post_id": ..., "author": ..., "text": ..., "likes": ...}],
    "next_cursor": "..."}
POST /v1/users/{id}/follow
```

Cursor pagination (by `post_id` or score), never offset: new posts shift offsets.

## Data model

| Store | Data |
|---|---|
| Postgres (sharded by user_id) | `users`, `follows(follower_id, followee_id)` with indexes both ways |
| Posts store (sharded by post_id / Cassandra) | `posts(post_id, author_id, created_at, body, media)` |
| Redis | `feed:{user_id}` sorted set or list of post ids (capped ~500–800) |
| Redis / counters service | Like and comment counts |
| Object storage + CDN | Media |

Post ids: time-sortable (Snowflake/UUIDv7) so sorting by id ≈ sorting by time.

## High-level design

```text
          write path                           read path
client -> post service -> posts DB     client -> feed service
               |                                    |
           queue (post.created)              Redis feed:{uid}
               |                               | ids    | merge
          fan-out workers ---push ids--->  Redis       celebrity posts
               |                                    |   (pulled)
        follow graph (who follows A?)        hydrate posts (cache)
                                                    |
                                             ranked page + cursor
```

## Deep dives

### Fan-out on write (push)

On post, workers write the post id into each follower's feed cache.

- Read is cheap: one `ZREVRANGE` on the feed.
- Write cost = follower count. A 50 M-follower post means 50 M writes.
- Wasted work for inactive followers.

### Fan-out on read (pull)

On feed load, fetch recent posts of everyone you follow and merge.

- Write is cheap.
- Read is expensive: following 1000 users = 1000 lookups + k-way merge, on every load.
- Always fresh.

### Hybrid (the celebrity problem)

| Author type | Strategy |
|---|---|
| Normal (< ~10K followers) | Push to followers' feeds |
| Celebrity (≥ threshold) | Do not push; followers pull at read time |
| Inactive followers | Skip push; rebuild feed on next login |

Read = cached feed (pushed ids) ∪ recent posts of the few celebrities you follow (each cached per celebrity, hot in Redis) → merge by time/score → hydrate.

### Hydration

- Feed holds ids only. Batch-fetch post bodies (`MGET` from post cache, misses from DB), author profiles, counts.
- Filter deleted posts and blocked users at hydration time.

### Ranking

- Chronological: sort by post id.
- Ranked: candidate generation (last ~500 ids) → feature fetch → model score → top 20. Store the scored list briefly so pagination is stable.

### Edge cases

- **Read-your-writes:** insert the author's own post into their feed synchronously, or merge it in at read.
- **Unfollow:** filter at read time; lazily clean the feed.
- **Delete:** mark deleted in posts store; hydration drops it.
- **New follow:** pull recent posts of the new followee into the feed.

## Bottlenecks and trade-offs

- Fan-out workers are the write bottleneck; scale horizontally, partition queue by author.
- Redis memory: store ids only, cap length, skip inactive users.
- Counts (likes) are write-hot: buffer increments and flush, or shard counters.
- Push vs pull is the core trade-off: write amplification vs read latency. The hybrid threshold is tunable.

## Likely follow-ups

- What if a celebrity follows many celebrities? Pull path merges more lists; cap and cache per-celebrity timelines.
- How do you backfill feeds after a Redis loss? Rebuild lazily on read from the follow graph (pull path), warm active users first.
- How to paginate a ranked feed consistently? Snapshot ranked ids per session with a TTL; cursor indexes into it.
- How to show new posts badge? Poll or push "N new posts" via websocket; do not reorder the visible list.
