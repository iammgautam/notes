---
title: Design search autocomplete
section: hld
order: 20
summary: Return top-k query suggestions per prefix in under 100 ms using precomputed tries or prefix indexes, offline aggregation and layered caching.
---

## Requirements

**Functional**
- As the user types, return the top 5–10 suggestions for the current prefix.
- Ranked by popularity (and optionally recency, personalization, locale).
- Filter offensive/blocked terms.

**Non-functional**
- p99 < 100 ms end to end (users type ~1 char per 150 ms).
- Very read-heavy, highly available.
- Freshness: new trending queries appear within minutes to an hour (not real time).

## Estimates

- 100 M DAU × 10 searches × ~6 keystrokes (after debounce) = 6 B requests/day ≈ 70K QPS avg, ~200K peak.
- Unique queries to index: ~100 M after dropping the long tail (count < threshold).
- Prefix entries: ~100 M queries × ~8 useful prefix lengths, top-10 lists of ids → tens of GB. Fits in memory across a few shards.

## API

```http
GET /v1/suggest?q=how%20to%20ma&limit=8&locale=en-IN
-> 200 {"prefix": "how to ma", "suggestions": ["how to make tea", "how to make money", ...]}
Cache-Control: public, max-age=300
```

Client: debounce (~100–150 ms), cancel in-flight requests, cache prefixes locally, start after 1–2 chars.

## Data model

- `query_counts(query, locale, day, count)` aggregated from logs (warehouse / ClickHouse).
- Serving index: `prefix → [top-k (query, score)]` precomputed. Stored in-memory in suggest servers, or in Redis as sorted sets / plain lists.

## High-level design

```text
          read path                               build path
client -> CDN -> LB -> suggest service      search logs -> Kafka
                          |  in-memory                     |
                          |  trie/prefix map         stream agg (minutely)
                          v                                |
                    Redis (optional)              batch agg (hourly/daily)
                                                           |
                                          builder: filter, score, top-k
                                                           |
                                           snapshot -> object storage
                                                           |
                                    suggest servers load new snapshot
```

## Deep dives

### Trie vs prefix index

| | Trie with top-k at each node | Flat prefix → top-k map | Search engine (edge n-grams) |
|---|---|---|---|
| Lookup | Walk `O(len(prefix))`, read cached top-k | One hash lookup `O(1)` | Index query |
| Memory | Shared prefixes compress well | Duplicates prefixes, larger | On disk + cache |
| Build | Bottom-up merge of children's top-k | Group by prefix, take top-k | Index documents |
| Fuzzy/typos | Hard | Hard | Built-in |
| Fits | Custom in-memory service | Redis / KV store | Elasticsearch/OpenSearch `completion` suggester |

Key idea in both: **precompute top-k per prefix** at build time. Never traverse the subtree at query time.

```python
# build: top-k per prefix from (query, score) pairs
from collections import defaultdict
import heapq

def build(pairs, k=10, max_prefix=20):
    buckets = defaultdict(list)
    for q, s in pairs:
        for i in range(1, min(len(q), max_prefix) + 1):
            buckets[q[:i]].append((s, q))
    return {p: [q for _, q in heapq.nlargest(k, lst)] for p, lst in buckets.items()}
```

In production this is a distributed batch job (Spark/SQL `GROUP BY prefix` with a window rank), not a Python loop.

### Ranking

- Score = decayed count: `sum(count_d * 0.9^age_days)` blends popularity and recency.
- Trending boost from the minutely stream aggregation (spike vs baseline).
- Personalization: merge global top-k with the user's recent queries at request time (small per-user list in Redis).

### Updates and freshness

- Do not update the trie per query. Rebuild periodically (hourly/daily) and swap atomically (load new snapshot, flip pointer).
- Fast lane for trending: a small delta index updated every few minutes, merged at read time.
- Blocklist applied at build and again at serve time (instant removal without rebuild).

### Sharding

- Index fits on one box per replica? Just replicate. Simplest and fastest.
- If not: shard by prefix range (first 1–2 chars) with care for skew (`s`, `a` are heavy), or by hash of prefix. Each request hits exactly one shard.

### Caching

- Browser cache per prefix; CDN cache for short prefixes (1–3 chars are a tiny, extremely hot set).
- Service-level in-process LRU of hot prefixes.
- Short TTL (minutes) bounds staleness.

## Bottlenecks and trade-offs

- Keystroke QPS: debounce, client cache and CDN cut origin load by a large factor.
- Freshness vs cost: full rebuilds are simple but slow; delta index adds complexity.
- Memory vs latency: in-process trie is fastest; Redis is easier to share but adds a network hop (~1 ms).
- Long-tail queries dropped below a count threshold to bound size; they get no suggestions (acceptable).

## Likely follow-ups

- Typo tolerance? Fuzzy matching via edit distance on a search engine, or a spelling-correction pass before lookup.
- Multi-language? Separate indexes per locale; normalize (lowercase, Unicode NFKC, strip accents).
- Abuse (people spamming a query to make it trend)? Count unique users per query, not raw hits; rate limit; manual blocklist.
- Why not query Postgres `LIKE 'prefix%'`? Works for small catalogs with a `text_pattern_ops` index or `pg_trgm`, but cannot rank top-k across 100 M queries at 200K QPS.
