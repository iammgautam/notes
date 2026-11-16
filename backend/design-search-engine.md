---
title: Design a search engine
section: backend
order: 86
phase: "Phase 10 · System design case studies"
summary: "Web search end to end: a polite crawler, parsing, an inverted index with BM25, document vs term sharding, query processing, ranking, autocomplete and caching."
---

A search engine is two systems joined by an index. The offline side crawls billions of pages, parses them and builds compressed inverted indexes. The online side answers a query in a few hundred milliseconds by fanning out to thousands of index shards, scoring candidates, re-ranking the best and assembling snippets. Interviews probe both: crawler politeness and dedup, how the index is sharded, and how ranking and caching keep latency flat.

## Requirements

### Clarifying questions

- Web-scale search or site search? Web-scale, but a subset: about 5B indexed pages.
- Which result types? Web pages only; no images, video, ads or generated answers.
- Freshness targets? News within minutes, popular pages within a day, the long tail within weeks.
- Personalization? Language and region only.

### Functional

- Crawl the web, respecting robots.txt and site load.
- Index page text, titles, anchor text and links.
- Keyword search with ranked results, snippets, pagination and spelling suggestions.
- Autocomplete as the user types.

### Non-functional

- 500M queries/day; p99 latency under 300 ms end to end.
- 99.95% availability; partial results are better than errors.
- Recrawl 5B pages on average every 30 days, faster for important or fast-changing pages.
- Read-heavy serving path; write-heavy, batch-friendly indexing path.

### Out of scope

Ads, image and video search, knowledge panels, LLM-generated answers, and anti-spam modeling beyond a mention.

## Capacity estimates

```text
Queries      500M/day / 86,400 s           ≈ 5,800 QPS avg, ~17k peak
Autocomplete ~5 requests per query         ≈ 85k QPS peak
Crawl        5B pages / 30 days            ≈ 167M pages/day ≈ 1,900/s
             + discovery and fast recrawl  ≈ 3-5k fetches/s
Fetch bw     4k/s x ~100 KB HTML           ≈ 400 MB/s ≈ 3.2 Gbps
Raw store    5B x ~20 KB compressed HTML   ≈ 100 TB
Text         5B x ~10 KB extracted text    ≈ 50 TB
Index        postings + positions, compressed, ~50-60% of text
                                           ≈ 30 TB per full copy
Shards       30 TB / 30 GB per shard       ≈ 1,000 shards (5M docs each)
Replicas     3 copies for QPS and failure  ≈ 3,000 shard servers
Fan-out      17k QPS x 1,000 shards        = 17M shard lookups/s
             → result cache and tiering are not optional
Result cache top 10M queries x 5 KB        ≈ 50 GB
```

## API design

```http
GET /search?q=distributed+cache&lang=en&region=IN&page_token=Cg8
```

```json
{"results": [
   {"url": "https://example.com/caching", "title": "Distributed caching",
    "snippet": "...a <b>distributed cache</b> spreads keys across nodes..."}
 ],
 "spell_suggestion": null, "next_page_token": "Cg9", "took_ms": 142}
```

```http
GET /complete?prefix=distrib&lang=en
```

```json
{"suggestions": ["distributed systems", "distributed cache", "distribution"]}
```

Internal: `Search(shard, terms, filters, top_k, deadline_ms) → [(doc_id, score)]` between the aggregator and shards; `Fetch(doc_ids, query) → snippets` against the document store; crawl and index events over Kafka.

## Data model

| Data | Store | Key and notes |
|---|---|---|
| URL state (frontier, last crawl, change rate, content hash) | Wide-column store (Bigtable/Cassandra-class), sharded by host hash | `url_hash`; billions of rows, write-heavy |
| Raw pages | Object storage in WARC-style files | `(crawl_date, file, offset)`; Common Crawl uses WARC |
| Parsed documents | Columnar files on a distributed file system | `doc_id → text, title, anchors, links, language` |
| Link graph | Distributed file system, batch-processed | `(src, dst)` edges for link analysis |
| Inverted index | Immutable segment files on shard servers (RAM + SSD) | `term → postings [(doc_id, tf, positions)]` |
| Per-shard doc metadata | Arrays in the segment | length, static rank, URL, title |
| Autocomplete | In-memory trie or FST, replicated | prefix → top suggestions |

Index segments are immutable and versioned: a new index build is swapped in atomically, and the previous version stays available for rollback. Lucene's segment model is the same idea at smaller scale.

## High-level design

```text
 OFFLINE (crawl and index)
 seeds ─► URL frontier ─► fetchers ─► raw store ─► parser/extractor
            ▲   (politeness,  (DNS, robots    │        │
            │    priority)     caches)        │   links│  docs
            └──────── new URLs ◄───── dedup ◄─┘        ▼
                                              indexer (batch + realtime)
                                link graph ─► static rank   │
                                                            ▼
 ONLINE (serving)                                  index shards x replicas
 user ─► frontend ─► result cache ─► query service ─► aggregator ─┘
          │  autocomplete            (spell, rewrite)   │ top-k merge
          ▼                                             ▼
     suggest service                re-ranker ─► snippets (doc store)
```

**Indexing path:** the frontier hands URLs to fetchers by host; pages land in the raw store; the parser extracts text, links and anchors; new links go back to the frontier after URL dedup; documents go to the indexer, which writes segments per shard. A batch job over the link graph computes static rank.

**Query path:** the frontend checks the result cache. On a miss, the query service normalizes and rewrites the query; the aggregator broadcasts it to one replica of every shard with a deadline; each shard returns its top k; the aggregator merges, the re-ranker reorders the best few hundred, and snippets are generated for the final page.

## Deep dives

### Crawler: frontier, politeness and dedup

**Frontier.** Two layers, as in the Mercator crawler design: front queues by priority (page importance times estimated staleness) and back queues per host. A heap keyed by "next time this host may be fetched" picks the next host whose politeness delay has expired, then the highest-priority URL for it.

**Politeness.**

- Fetch robots.txt per host, cache it (typically up to a day), and obey it; RFC 9309 standardizes the Robots Exclusion Protocol.
- One connection per host at a time, with a delay that adapts to the server (a multiple of its last response time) and backs off on 429 and 503.
- Identify the crawler with a User-Agent that links to an info page.
- Shard the frontier by host so one node owns each host's rate; no global coordination needed.

**URL dedup.** Normalize before checking (lowercase scheme and host, drop default ports and fragments, resolve relative paths, strip known session parameters). The seen-URL set has tens of billions of entries: keep 8-byte URL hashes in a disk-backed store per frontier shard, fronted by a Bloom filter for the common "already seen" case.

**Content dedup.** Many URLs serve the same content. Exact duplicates collapse by hashing normalized text. Near-duplicates (same article, different ads) use SimHash: a 64-bit fingerprint where similar documents differ in few bits. Google publicly described using it at web scale with a Hamming distance threshold of 3. Keep one canonical document, honoring `rel=canonical` when it is consistent.

**Traps.** Infinite calendars and parameter explosions: cap URLs and depth per host, detect repeating path patterns, and lower priority for hosts that generate URLs faster than useful content.

### Parsing

- HTML in the wild is malformed: use a forgiving parser, detect charset (headers, meta tags, sniffing), and normalize Unicode (NFKC).
- Strip boilerplate (navigation, footers, cookie banners) so it does not dilute relevance.
- Extract title, headings, body, links with **anchor text** (attributed to the target page, which is often the best description of it), `rel=canonical`, `noindex`, language and structured data.
- Tokenize per language (CJK needs word segmentation), case-fold, and stem or lemmatize. Keep stop words in the index for phrase queries; positions make "to be or not to be" searchable.
- JavaScript-heavy pages go to a separate, rate-limited headless-rendering queue: it is orders of magnitude more expensive than a plain fetch.

### Inverted index and BM25

An inverted index maps each term to a posting list: the sorted doc ids containing it, with term frequencies and positions. Sorted ids allow delta encoding (store gaps, not ids) with variable-byte or SIMD-friendly block codecs, and skip pointers let intersections jump ahead. Indexes are built in batch (map documents to `(term, doc_id, tf)`, sort by term, write segments) and updated incrementally with small new segments that are merged in the background, LSM-style.

BM25 scores a document for a query as the sum over query terms of `IDF × tf × (k1 + 1) / (tf + k1 × (1 − b + b × len / avg_len))`. Term frequency saturates (the tenth mention matters less than the first), rare terms weigh more, and long documents are normalized. Typical defaults: `k1 = 1.2`, `b = 0.75`.

```python
import heapq
import math
import re
from collections import Counter, defaultdict

STOP = {"a", "an", "and", "at", "for", "is", "of", "the", "to", "with"}


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]


class InvertedIndex:
    def __init__(self, k1=1.2, b=0.75):
        self.k1, self.b = k1, b
        self.postings = defaultdict(list)          # term -> [(doc_id, tf)]
        self.doc_len = {}

    def add(self, doc_id, text):                   # ids arrive in increasing order
        terms = tokenize(text)
        self.doc_len[doc_id] = len(terms)
        for term, tf in Counter(terms).items():
            self.postings[term].append((doc_id, tf))

    def idf(self, term):
        n, total = len(self.postings.get(term, ())), len(self.doc_len)
        return math.log(1 + (total - n + 0.5) / (n + 0.5))

    def search(self, query, k=10):
        avg_len = sum(self.doc_len.values()) / len(self.doc_len)
        scores = defaultdict(float)
        for term in set(tokenize(query)):          # term-at-a-time scoring
            idf = self.idf(term)
            for doc_id, tf in self.postings.get(term, ()):
                norm = self.k1 * (1 - self.b + self.b * self.doc_len[doc_id] / avg_len)
                scores[doc_id] += idf * tf * (self.k1 + 1) / (tf + norm)
        return [d for d, _ in heapq.nlargest(k, scores.items(),
                                             key=lambda kv: (kv[1], -kv[0]))]


def encode_gaps(doc_ids):
    """Delta + varint: 7 data bits per byte, high bit means 'more bytes'."""
    out, prev = bytearray(), 0
    for d in doc_ids:
        gap, prev = d - prev, d
        while gap >= 0x80:
            out.append((gap & 0x7F) | 0x80)
            gap >>= 7
        out.append(gap)
    return bytes(out)


def decode_gaps(data):
    ids, cur, shift, prev = [], 0, 0, 0
    for byte in data:
        cur |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            prev += cur
            ids.append(prev)
            cur, shift = 0, 0
    return ids


if __name__ == "__main__":
    idx = InvertedIndex()
    docs = [
        "Redis is an in-memory cache used for distributed locks and rate limits",
        "PostgreSQL is a relational database with MVCC and write-ahead logging",
        "Consistent hashing spreads cache keys across a distributed cache cluster",
        "Kafka is a distributed log for event streaming between services",
        "A cache stampede happens when many requests miss the cache at once",
    ]
    for i, text in enumerate(docs, start=1):
        idx.add(i, text)

    assert idx.search("distributed cache")[0] == 3      # both terms, cache twice
    assert idx.search("kafka streaming") == [4]
    assert idx.search("mvcc postgresql")[0] == 2
    assert idx.idf("kafka") > idx.idf("cache")         # rarer term, higher IDF
    assert idx.search("cache stampede")[0] == 5         # rare term dominates

    short = InvertedIndex()                             # length normalization
    short.add(1, "cache tuning")
    short.add(2, "cache " + "unrelated words here " * 10)
    assert short.search("cache") == [1, 2]

    ids = list(range(0, 1_000_000, 37))
    blob = encode_gaps(ids)
    assert decode_gaps(blob) == ids and len(blob) == len(ids)   # 1 byte per id
    assert decode_gaps(encode_gaps([5, 300, 70_000])) == [5, 300, 70_000]
    print("bm25 ok")
```

### Sharding by document versus term

| | Document-partitioned (local index) | Term-partitioned (global index) |
|---|---|---|
| Each shard holds | All terms for a subset of documents | Full posting lists for a subset of terms |
| Query touches | Every shard | Only shards owning the query's terms |
| Multi-term queries | Intersect locally | Ship long posting lists across the network |
| Load balance | Even (random doc assignment) | Skewed; "the" and trending terms are hot |
| Indexing a document | Writes one shard | Writes a shard per distinct term |
| Shard failure | Lose a slice of results, degrade gracefully | Lose whole terms |

Large engines use document partitioning with replicas; Google's 2003 "Web Search for a Planet" paper describes exactly this shape. The cost is fan-out: every query hits every shard, so tail latency is the enemy. With 1,000 shards, a one-in-a-thousand slow shard affects most queries. Mitigations from "The Tail at Scale": hedged requests (send to a second replica after the 95th-percentile latency), and returning partial results when the deadline passes. **Tiering** cuts fan-out too: a small tier of high-quality documents answers most queries; the full tier is consulted only when the first tier returns too few good results.

### Query processing

1. **Normalize** (case, Unicode, whitespace) and check the result cache.
2. **Spell-correct** using query logs and an edit-distance model; offer "did you mean" or auto-correct when confident.
3. **Tokenize** exactly as the indexer did. A mismatch here silently returns nothing.
4. **Rewrite:** synonyms, stemming variants, operators (`"phrase"`, `site:`, `-term`).
5. **Retrieve** on each shard with dynamic pruning (WAND, Block-Max WAND) that skips documents which cannot enter the top k, instead of scoring every posting.
6. **First-stage score:** BM25 across fields (title, anchors, body) plus static rank; return the top few hundred per shard.
7. **Merge and re-rank** the best candidates with a learned model.
8. **Diversify** (no more than two results per host) and generate query-dependent **snippets** from the document store.

Each stage has a latency budget, and the aggregator enforces the overall deadline.

### Ranking signals

| Group | Examples |
|---|---|
| Text match | BM25 per field (BM25F), term proximity, exact phrase, title and anchor-text matches |
| Link authority | PageRank-style scores from the link graph, host-level authority |
| Quality | Spam and content-quality classifiers, duplication, page experience |
| Freshness | Publish and update time, weighted up for time-sensitive queries |
| User behavior | Click-through and dwell time, corrected for position bias |
| Context | Language, region, device |

Signals are combined by learning-to-rank models (gradient-boosted trees such as LambdaMART, and increasingly neural re-rankers on the top candidates) trained on human relevance labels and click data. Dense embedding retrieval with approximate nearest-neighbor search is commonly added as a second candidate source and fused with BM25 results (hybrid search).

### Autocomplete

- Build offline from query logs: count queries with time decay, drop rare, offensive or personally identifying ones, and keep the top suggestions per prefix.
- Serve from an in-memory trie (or a finite-state transducer) where each node stores its precomputed top 10, so lookup is O(prefix length). 100M candidate queries fit in tens of gigabytes, so replicate the whole structure rather than shard it.
- Merge a small real-time layer (counts over the last hour from a stream) so trending queries appear without waiting for the daily rebuild.
- Client side: debounce keystrokes (around 100 ms), cancel in-flight requests, and cache short prefixes in the browser and CDN.
- Typo tolerance: match within edit distance 1 for prefixes longer than a few characters.

### Freshness

- **Recrawl scheduling:** estimate each URL's change rate from history (a Poisson model is the classic choice) and prioritize by importance × probability of having changed.
- **Hints:** sitemaps with `lastmod`, RSS feeds, and push protocols such as IndexNow let sites announce changes instead of waiting to be recrawled.
- **Real-time tier:** recently crawled documents go into a small in-memory index merged at query time with the main index, which is rebuilt in large batches. News appears in minutes without rebuilding billions of documents.
- **Deletions:** a tombstone or live-docs bitmap hides deleted or `noindex` pages immediately; segment merges remove them physically.

### Caching

| Layer | Key | Notes |
|---|---|---|
| Result cache | normalized query + language + region + page | Head queries follow a power law, so a modest cache absorbs a large share; short TTL for news-sensitive queries |
| Posting list cache | term, on shard servers | Hot terms' lists in RAM |
| Document and snippet cache | doc_id (+ query for snippets) | Avoids document store reads |
| Autocomplete | prefix | Browser and CDN for short prefixes |

Invalidate the result cache by time, not by events: an index update touches millions of queries. When an entry for a very popular query expires, refresh it in the background or coalesce concurrent misses so a thousand identical requests do not all fan out to 1,000 shards at once. See [Cache stampede]({{ '/backend/cache-stampede/' | relative_url }}) and [Caching patterns]({{ '/backend/caching-patterns/' | relative_url }}).

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× queries | Shard fan-out CPU and tail latency | More replicas, bigger result cache, tiered index, hedged requests |
| 10× documents | Index size per shard; build time | More shards; incremental segments; distributed index builds |
| 100× | Network fan-out from aggregators; cross-region serving | Two-level aggregation (rack, then root); full index replicas per region |
| Crawl growth | Frontier and seen-URL store | Shard by host; Bloom filters; prune low-value URL spaces |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Shard replica down | Slower or partial results | Replicas, hedged requests, partial results flagged internally |
| Bad index build | Relevance collapse | Immutable versioned indexes, canary on a slice of traffic, instant rollback |
| Parser crash on a malicious page | Indexing stalls | Sandboxed parsing, per-document timeouts, poison documents to a DLQ |
| Crawler overloads a site | Complaints, blocks | Adaptive per-host delay, back off on 429 and 503, robots.txt compliance |
| Cache cluster loss | Fan-out spike to shards | Load shedding, request coalescing, warm standby cache |
| Region outage | Capacity loss | Serve from other regions; each holds a full index |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| Document-partitioned index | Yes | Even load, local intersections, graceful degradation |
| Term-partitioned index | No | Hot terms and cross-network posting transfers |
| Immutable index segments | Yes | Simple caching, atomic swaps, easy rollback |
| Two-stage ranking | Yes | Cheap BM25 recall, expensive models only on the top few hundred |
| Real-time tier plus batch index | Yes | Freshness without rebuilding everything |
| Exact dedup only | No | Near-duplicates waste index space; SimHash catches them |
| Event-based result cache invalidation | No | Too many affected queries; short TTLs instead |

## Interview follow-ups

**Why is the index sharded by document, not term?** Load stays even, intersections stay on one machine, and losing a shard degrades results rather than removing words. The cost, fan-out to every shard, is handled with replicas, tiering and caching.

**How do you keep the crawler polite at thousands of fetches per second?** Shard the frontier by host so each host has exactly one owner, keep a per-host next-fetch time with adaptive delays, cache robots.txt, and back off on errors.

**How does BM25 differ from TF-IDF?** Term frequency saturates through `k1` and document length is normalized through `b`, so keyword stuffing and long pages are not over-rewarded.

**How do you get news into results within minutes?** Prioritize news hosts in the frontier, use feeds and push hints, and index fresh documents into a small real-time tier that is merged with the main index at query time.

**How do you handle a slow shard?** Hedged requests to another replica, a hard deadline, and partial results if one slice is missing.

**How does autocomplete stay under 50 ms?** Precomputed top-k per trie node in memory, replicated near users, with debounced client requests and edge caching.

**How do you detect near-duplicate pages?** SimHash fingerprints compared by Hamming distance, with lookups made fast by permuting fingerprint bits into several sorted tables.
