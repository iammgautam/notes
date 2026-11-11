---
title: Design a URL shortener
section: hld
order: 14
summary: Short code generation, a read-heavy redirect path with caching, and the trade-offs between hashing, counters and random IDs.
---

## Requirements

**Functional**
- Create a short URL for a long URL; optional custom alias and expiry.
- Redirect `GET /{code}` to the long URL.
- Basic click analytics (count, by day/country).

**Non-functional**
- Very read-heavy (~100:1).
- Redirect p99 < 50 ms, highly available (99.99%).
- Codes are unguessable-ish and as short as possible; never collide.

Out of scope: user accounts UI, link editing history.

## Estimates

- 100 M new URLs/month → ~40 writes/s avg.
- 100:1 reads → ~4K redirects/s avg, ~20K peak.
- Record ~500 B → 100 M × 500 B = 50 GB/month → ~3 TB over 5 years. One Postgres can hold this; partition or shard later.
- Code length: 62^7 ≈ 3.5 T codes. 7 chars of base62 is plenty for 6 B URLs.
- Hot set: 20% of daily reads cached ≈ a few GB in Redis.

## API

```http
POST /v1/urls
{"long_url": "https://...", "alias": "promo24", "expires_at": "2027-01-01T00:00:00Z"}
-> 201 {"code": "aZ3k9Qx", "short_url": "https://sho.rt/aZ3k9Qx"}

GET /{code}
-> 302 Location: https://...      (404 if unknown, 410 if expired)

GET /v1/urls/{code}/stats?from=...&to=...
```

**301 vs 302:** 301 is cached by browsers, so fewer hits and lost analytics. Use 302 (or 307) when you need click tracking.

## Data model

```sql
CREATE TABLE urls (
  code        varchar(16) PRIMARY KEY,
  long_url    text NOT NULL,
  owner_id    bigint,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz
);

CREATE TABLE click_daily (
  code text, day date, country char(2), clicks bigint,
  PRIMARY KEY (code, day, country)
);
```

Access is purely by `code`, so a KV store (DynamoDB, Cassandra) also fits well. Postgres is fine at this scale.

## High-level design

```text
                 +--------+
 client -------> |  CDN   | (optional: cache 302s for hot codes)
                 +--------+
                     |
                   LB/L7
                     |
          +----------+-----------+
          |                      |
   write service           redirect service
          |                  |        |
   ID generator          Redis    click events
          |                  | miss     |
          +---> Postgres <---+      Kafka/queue
                                        |
                                 analytics workers
                                        |
                                  click_daily / OLAP
```

Redirect path: Redis `GET code` → on miss read Postgres, fill cache → emit click event async → return 302. The redirect never waits on analytics.

## Deep dives

### Generating codes

| Approach | How | Pros | Cons |
|---|---|---|---|
| Hash + truncate | base62(md5(url))[:7] | Same URL → same code | Collisions; need check-and-retry |
| Counter + base62 | Global sequence → base62 | No collisions, short | Predictable/enumerable; counter is a hotspot |
| Counter ranges | Each server leases a block of 1M ids | No per-request coordination | Gaps on crash (fine) |
| Random | 7 random base62 chars | Unguessable, stateless | Collision check on insert (rare at low fill) |
| Snowflake-like | time + machine + seq | Unique, no coordination | Longer (11 chars base62) |

Pick: **random 7 chars + `INSERT ... ON CONFLICT DO NOTHING`, retry on conflict.** At 6 B of 3.5 T used, collision probability per insert is ~0.2%. Or counter ranges shuffled through a bijective permutation if you want zero collisions and non-sequential codes.

Custom aliases: same table; unique PK rejects duplicates; validate charset and reserved words.

### Caching

- Cache-aside in Redis, key `u:{code}`, TTL ~24 h, LRU eviction.
- Cache negative lookups briefly to stop scans for random codes.
- Hot viral links: in-process LRU for 1–5 s and/or CDN caching of the 302.

### Analytics

- Redirect service publishes `{code, ts, country, referrer}` to a queue.
- Workers aggregate per minute in memory and upsert counts (`ON CONFLICT DO UPDATE SET clicks = clicks + EXCLUDED.clicks`).
- For heavy analytics use ClickHouse or similar.

### Expiry and cleanup

- Check `expires_at` on read (return 410).
- Background job deletes or archives expired rows in batches.

## Bottlenecks and trade-offs

- Read path is the bottleneck: cache hit rate matters most. Add read replicas for misses.
- Writes are low; a single primary is enough for years. Shard by `code` hash if needed (all access is by code, so no cross-shard queries).
- Dedupe same long URL? It saves space but leaks that someone else shortened the URL and breaks per-user analytics. Usually skip.
- Abuse: rate-limit creation per user/IP, scan URLs against malware lists, add a takedown flag.

## Likely follow-ups

- How do you prevent enumeration? Random codes, rate limiting on 404s.
- Multi-region? Replicate read-only copies globally; writes go to one region, or partition the code space per region.
- What if Redis is down? Fall back to DB with a circuit breaker; replicas absorb reads.
- 301 vs 302? Analytics vs fewer hits.
