---
title: Caching for LLM systems
section: llm
order: 49
phase: "Phase 8 · Production"
summary: Cache LLM work safely with exact and semantic response caches, provider prompt caching, KV prefix reuse, retrieval caches, invalidation and tenant scoping.
---

An LLM system has more cacheable work than a typical API: whole responses, embeddings, retrieval results, and the model's internal state for a shared prompt prefix. The useful split is between caches that **skip the model call** (response caches: you decide when a stored answer is still correct, so they can be wrong) and caches that **make the call cheaper** (prompt and KV prefix caches: the computation is identical, so the output is unchanged and the only risk is a silent miss). This note covers both, plus invalidation and the tenant-safety rules that keep a cache from becoming a data leak. For general cache patterns see [Caching]({{ '/hld/caching/' | relative_url }}).

## How it works

### Cache layers on the request path

```text
request
  │
  ├─► [1] exact response cache    key = hash(model, prompt_ver, input…)
  ├─► [2] semantic response cache key ≈ embed(query), sim ≥ τ, scoped
  │
  ├─► [3] retrieval cache         key = (tenant, query, filters, index_ver)
  │      └─► [4] embedding cache  key = (embed_model, sha256(text))
  │
  └─► provider or serving engine
         └─► [5] prompt / KV prefix cache   key = exact token prefix
```

| Layer | Saves | Typical hit rate | Main risk |
|---|---|---|---|
| Exact response | The whole call | Low for chat; high for repeated jobs and evals | Stale or wrongly scoped answers |
| Semantic response | The whole call | Medium on FAQ-like traffic | False hits: right-looking, wrong answer |
| Retrieval | Search and rerank | Medium | Stale results after ingest |
| Embedding | Embedding calls | High on re-ingest and repeated queries | None if keyed by model version |
| Provider prompt cache | Prefill time and input cost | High with a cache-friendly layout | Silent misses |
| Self-hosted KV prefix | Prefill compute | High with shared prefixes | Eviction, replica spread |

## Techniques

### Exact-match response caching

Hash everything that can change the output into the key:

- Model id including version (never a floating alias).
- Prompt template version, system prompt and tool definitions (or their hashes).
- Generation parameters: temperature, `max_tokens`, response schema, reasoning effort.
- The normalized input: canonical JSON with sorted keys, trimmed whitespace.
- Context identity: retrieval index version or the ids and versions of documents in the prompt.
- Scope: tenant always; user when the answer depends on the user's permissions, profile or history.

It suits deterministic-style tasks: classification, extraction, summarizing an immutable document, and re-running eval suites during development (often the biggest saving). It is wrong for personalized chat, time-sensitive answers and agent steps with side effects. Never cache errors, refusals, truncated outputs (`finish_reason = length`) or outputs that failed validation, or one bad response is served forever.

Protect against stampedes: when a popular key expires, many concurrent misses all call the model. Use single-flight (one in-flight call per key that other requests await) in-process, or a short Redis lock (`SET key value NX PX 30000`) across processes.

### Semantic caching

A semantic cache embeds the incoming query, finds the nearest cached query within the same scope, and returns the stored answer if cosine similarity is at least a threshold `τ`. It raises hit rates on paraphrased questions, but similarity measures topical closeness, not "has the same answer":

| Cached query | New query | Similar? | Same answer? |
|---|---|---|---|
| How do I reset my password? | How can I reset my password? | Very | Yes |
| Refund window in Germany? | Refund window in France? | Very | No |
| Cancel my renewal | Do not cancel my renewal | Very | Opposite |
| Q3 revenue for 2024 | Q3 revenue for 2025 | Very | No |
| What is on my calendar today? | (same text, next day) | Identical | No |

Raising `τ` reduces false hits and hit rate together; on short queries, a dangerous swap and a safe paraphrase can score the same, so no threshold separates them. Mitigations:

- Restrict semantic caching to intents where answers are generic (FAQ, documentation, policy), never personal or time-dependent queries.
- Scope exactly by tenant, locale, prompt version and ACL scope; similarity only applies within a scope.
- Guard with exact checks on entities, numbers, dates and negation extracted from both queries.
- Cache curated or verified answers rather than whatever the model said first.
- Optionally verify a candidate hit with a cross-encoder or small model ("do these two questions have the same answer?").
- Short TTLs, and invalidation when source documents change.

Pick `τ` from data: sample would-be hits at several thresholds, label whether the cached answer is correct for the new query, and choose the threshold whose hit precision meets your bar (for customer-facing answers, 99%+). Monitor negative feedback on cache-served answers separately. Libraries such as GPTCache and Redis vector search implement the mechanics; the scoping and guards are your job.

### Provider prompt caching

The provider keeps the attention key/value state for a prompt prefix for a few minutes. A later request whose tokens start with exactly the same prefix skips prefill for that part: lower TTFT and discounted input tokens. The output is the same as without caching. Styles differ by vendor, as of 2025–2026 (details change; check the docs):

| | OpenAI | Anthropic | Google Gemini |
|---|---|---|---|
| Activation | Automatic above a minimum prompt length (~1,024 tokens) | Explicit `cache_control` breakpoints; an automatic mode also exists | Implicit caching on recent models; explicit cached-content objects |
| Lifetime | Minutes of inactivity, best effort | 5 minutes by default, refreshed on each hit; optional 1-hour TTL | Explicit caches have a TTL you set |
| Pricing | Discounted cached input, no write fee | Write premium (1.25× input for 5-minute), reads about 0.1× input or less | Discounted reads; explicit caches also bill storage time |
| Usage field | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens`, `cache_creation_input_tokens` | Cached token count in usage metadata |

Minimum cacheable prefix lengths vary by model (from hundreds to a few thousand tokens); shorter prefixes silently do not cache. Caches are scoped to your organization or workspace and to one model: switching models, and on some providers changing tools or reasoning settings, starts a new cache.

The whole game is prompt layout. Order content from most to least stable:

```text
┌─ stable for every request ──────────────────────────────┐
│ tool definitions (fixed order, deterministic JSON)       │
│ system prompt (no dates, ids, or user names)             │
│ few-shot examples                                        │
├─ stable per tenant or document ─────────────────────────┤
│ tenant policy, a large shared document                   │
├─ stable per conversation ───────────────────────────────┤
│ conversation history (append-only)                       │
├─ changes every request ─────────────────────────────────┤
│ retrieved passages, current date, current question      │
└──────────────────────────────────────────────────────────┘
```

Rules that follow:

- Nothing volatile above the cached part: no timestamps, request ids or user names in the system prompt.
- Serialize deterministically: sorted JSON keys, a fixed tool order. Python dict iteration order follows insertion, so a tool list built from a set or from a database query without `ORDER BY` can reorder silently.
- Keep history append-only. Summarizing or editing earlier turns rewrites the prefix; do it rarely and in large steps.
- Keep the tool set stable within a conversation; dynamic tool selection breaks the prefix at the tools.
- Do the break-even math for write premiums: with writes at 1.25× and reads at 0.1×, two requests within the TTL already beat no caching (`1.25 + 0.1 = 1.35` vs `2`).
- Measure the cached-token ratio per feature, and add a test that sends a request twice and asserts cached tokens on the second. Caching fails silently: requests succeed, the bill just goes up.
- For latency-critical first requests, pre-warm the cache at deploy or worker start.

### KV prefix caching in self-hosted serving

Serving engines do the same thing inside your own GPUs (see [Batching and PagedAttention]({{ '/llm/batching-and-paged-attention/' | relative_url }})):

- **vLLM automatic prefix caching** hashes fixed-size KV blocks by their tokens and everything before them, so any request sharing a prefix reuses the blocks; unreferenced blocks are evicted LRU. Recent versions enable it by default; older ones use `--enable-prefix-caching`.
- **SGLang RadixAttention** keeps prefixes in a radix tree with LRU eviction and schedules requests to maximize reuse.
- **Replica spread:** with N replicas behind a round-robin balancer, a conversation's prefix is cached on one replica but its next turn lands elsewhere. Route with prefix or session affinity (consistent hashing on a prefix hash or conversation id) to keep hit rates up.
- **Capacity:** cached prefixes compete with running requests for KV memory (see [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }})). Under load, eviction lowers the hit rate exactly when you need it; offloading KV to CPU memory extends capacity at some latency cost.

### Retrieval and embedding caches

- **Embedding cache:** key on (embedding model and version, sha256 of normalized text). Always safe. On re-ingest only changed chunks are re-embedded; store a `content_hash` column next to each chunk (see [pgvector]({{ '/postgresql/pgvector/' | relative_url }})).
- **Retrieval results:** key on (tenant, normalized query, filters, ACL scope, index version) and store document ids and scores, not text. Fetch text fresh so deletions and permission changes apply. TTL in minutes; bump the index version on ingest.
- **Reranker scores:** key on (query hash, document id, document version).

### Invalidation

- **Version everything in the key.** Model, prompt template, tool set, index version and document versions. A deploy that changes a prompt then invalidates by construction, with no purge step.
- **TTL as a backstop**, sized to how stale an answer may be.
- **Event-driven purge** for deletions: keep a reverse index from document id to cache keys (or tag keys with document ids) so deleting a document removes answers derived from it. Right-to-erasure requests must reach caches too.
- **Generation counters:** prefix keys with a per-tenant generation (`llm:g{gen}:{tenant}:…`); incrementing it invalidates a tenant's entire cache in O(1).

### Personalization and tenant safety

- Tenant id in every key, no exceptions. Add the user id or an ACL-scope hash whenever retrieval or the answer depends on permissions: two users in one tenant with different access must not share entries.
- Never let a semantic cache match across tenants. A "similar question" from another customer returning their answer is a data breach, not a cache bug.
- Cached values containing personal data inherit retention, encryption and erasure duties (see [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).
- Provider prompt caches are scoped to your organization, and sharing a static prefix across your tenants is fine because outputs are computed fresh. Keep tenant-specific data after the shared prefix anyway.
- Shared prefix caches leak timing: a fast TTFT reveals that someone recently sent the same prefix. Research has demonstrated this against real APIs, which is why providers scope caches. In self-hosted multi-tenant serving, isolate caches per tenant when prefixes are sensitive.

## In practice

### Exact-match cache with scoped keys

{% raw %}
```python
import hashlib
import json
import time
from collections import OrderedDict

def cache_key(*, tenant: str, model: str, prompt_version: str, params: dict,
              messages: list[dict], tools: list[dict] | None = None,
              index_version: str = "") -> str:
    payload = {"tenant": tenant, "model": model, "prompt": prompt_version,
               "params": params, "messages": messages, "tools": tools or [],
               "index": index_version}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return "llm:v1:" + hashlib.sha256(canon.encode()).hexdigest()

class TTLCache:
    def __init__(self, max_items: int, ttl_s: float, clock=time.monotonic):
        self.max_items, self.ttl_s, self.clock = max_items, ttl_s, clock
        self.data: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def get(self, key: str) -> dict | None:
        item = self.data.get(key)
        if item is None:
            return None
        expires, value = item
        if self.clock() >= expires:
            del self.data[key]
            return None
        self.data.move_to_end(key)                  # LRU order
        return value

    def put(self, key: str, value: dict) -> None:
        self.data[key] = (self.clock() + self.ttl_s, value)
        self.data.move_to_end(key)
        while len(self.data) > self.max_items:
            self.data.popitem(last=False)

def cacheable(resp: dict) -> bool:
    return resp.get("finish_reason") == "stop" and resp.get("valid", True)

if __name__ == "__main__":
    now = [0.0]
    cache = TTLCache(max_items=2, ttl_s=60, clock=lambda: now[0])
    base = dict(model="m-2025-01", prompt_version="triage_v3",
                params={"temperature": 0, "max_tokens": 5},
                messages=[{"role": "user", "content": "Classify: refund request"}])
    k1 = cache_key(tenant="acme", **base)
    reordered = {**base, "params": {"max_tokens": 5, "temperature": 0}}
    assert k1 == cache_key(tenant="acme", **reordered)       # canonical JSON
    assert k1 != cache_key(tenant="globex", **base)          # tenant-scoped
    assert k1 != cache_key(tenant="acme", **{**base, "prompt_version": "triage_v4"})

    llm_calls = []
    def classify(key: str) -> dict:
        if (hit := cache.get(key)) is not None:
            return hit
        llm_calls.append(key)
        resp = {"label": "refund", "finish_reason": "stop"}
        if cacheable(resp):
            cache.put(key, resp)
        return resp

    classify(k1)
    classify(k1)
    assert len(llm_calls) == 1                     # second call is a hit
    now[0] = 61
    classify(k1)
    assert len(llm_calls) == 2                     # expired after the TTL
    assert not cacheable({"finish_reason": "length"})
    for k in ("a", "b", "c"):
        cache.put(k, {})
    assert len(cache.data) == 2                    # size bound holds
    print("ok")
```
{% endraw %}

### Semantic cache: why similarity is not enough

A toy bag-of-words embedding stands in for a real model, but the failure is the same with real embeddings: a safe paraphrase and an entity swap score almost identically, so only exact guards separate them.

```python
import math
import re
from collections import Counter

NEGATIONS = {"not", "no", "never", "don't", "dont", "without", "cannot"}

def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())

def embed(text: str) -> Counter:
    return Counter(tokens(text))              # toy embedding: bag of words

def cosine(a: Counter, b: Counter) -> float:
    dot = sum(a[t] * b[t] for t in a)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0

def entities(text: str) -> set[str]:
    words = text.split()
    caps = {w.strip("?.,!") for w in words[1:] if w[:1].isupper()}
    return caps | set(re.findall(r"\d+", text))

def negated(text: str) -> bool:
    return any(t in NEGATIONS for t in tokens(text))

class SemanticCache:
    def __init__(self, threshold: float, guard: bool):
        self.threshold, self.guard = threshold, guard
        self.items: list[tuple[str, str, Counter, str]] = []  # scope, query, vec, answer

    def put(self, scope: str, query: str, answer: str) -> None:
        self.items.append((scope, query, embed(query), answer))

    def get(self, scope: str, query: str) -> str | None:
        vec, best, best_sim = embed(query), None, 0.0
        for s, q, v, answer in self.items:
            if s != scope:
                continue                          # scope matches exactly
            sim = cosine(vec, v)
            if sim < self.threshold or sim <= best_sim:
                continue
            if self.guard and (entities(q) != entities(query)
                               or negated(q) != negated(query)):
                continue
            best, best_sim = answer, sim
        return best

if __name__ == "__main__":
    pairs = {
        "paraphrase": ("How do I reset my password?", "How can I reset my password?"),
        "entity swap": ("What is the refund window in Germany?",
                        "What is the refund window in France?"),
        "negation": ("Cancel my subscription renewal",
                     "Do not cancel my subscription renewal"),
    }
    sims = {k: cosine(embed(a), embed(b)) for k, (a, b) in pairs.items()}
    assert abs(sims["paraphrase"] - sims["entity swap"]) < 0.05   # indistinguishable
    assert min(sims.values()) > 0.75

    naive = SemanticCache(0.75, guard=False)
    guarded = SemanticCache(0.75, guard=True)
    for cache in (naive, guarded):
        for cached_q, _ in pairs.values():
            cache.put("acme:faq_v2", cached_q, f"answer for: {cached_q}")
    for cached_q, new_q in pairs.values():
        assert naive.get("acme:faq_v2", new_q) == f"answer for: {cached_q}"  # 3 hits, 2 wrong
    assert guarded.get("acme:faq_v2", pairs["paraphrase"][1]) is not None
    assert guarded.get("acme:faq_v2", pairs["entity swap"][1]) is None
    assert guarded.get("acme:faq_v2", pairs["negation"][1]) is None
    assert naive.get("globex:faq_v2", pairs["paraphrase"][1]) is None
    print({k: round(v, 3) for k, v in sims.items()})
```

In production the same design maps onto PostgreSQL with pgvector, with scope and freshness as exact filters before the vector comparison:

```sql
CREATE TABLE semantic_cache (
    id          bigserial PRIMARY KEY,
    tenant_id   bigint NOT NULL,
    scope       text NOT NULL,            -- prompt version + locale + intent
    query       text NOT NULL,
    entities    text[] NOT NULL,          -- guard: must match exactly
    embedding   vector(1024) NOT NULL,
    answer      jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON semantic_cache USING hnsw (embedding vector_cosine_ops);

SELECT id, answer, 1 - (embedding <=> $1) AS similarity
FROM semantic_cache
WHERE tenant_id = $2 AND scope = $3 AND entities = $4
  AND created_at > now() - interval '1 day'
ORDER BY embedding <=> $1
LIMIT 1;                                  -- accept only if similarity >= τ
```

Selective filters run after the approximate HNSW search and can starve results; see [pgvector]({{ '/postgresql/pgvector/' | relative_url }}) for filtering strategies.

### Prompt layout and break-even

Two requests rendered with a timestamp first share almost nothing; the same content with stable parts first shares the whole tools-plus-system prefix. Character prefixes stand in for token prefixes here.

{% raw %}
```python
import json
import os

SYSTEM = "You are the support assistant for Acme. " + "Policy text. " * 300

def render_bad(tools: dict, question: str, now: str, request_id: str) -> str:
    return (f"Date: {now} Request: {request_id}\n" + SYSTEM + "\n"
            + json.dumps(tools) + "\nQ: " + question)

def render_good(tools: dict, question: str, today: str) -> str:
    return (json.dumps(tools, sort_keys=True) + "\n" + SYSTEM
            + f"\nDate: {today}\nQ: " + question)

def prefix_cost(n_requests: int, write: float = 1.25, read: float = 0.10):
    """Cost of the prefix over n requests within the TTL, in uncached units."""
    return float(n_requests), write + read * (n_requests - 1)

if __name__ == "__main__":
    t1 = {"search_docs": {"q": "string"}, "create_ticket": {"title": "string"}}
    t2 = {"create_ticket": {"title": "string"}, "search_docs": {"q": "string"}}
    bad = [render_bad(t1, "Where is my order?", "2026-03-01T10:00:01", "r-1"),
           render_bad(t2, "Cancel order 42", "2026-03-01T10:00:07", "r-2")]
    good = [render_good(t1, "Where is my order?", "2026-03-01"),
            render_good(t2, "Cancel order 42", "2026-03-01")]
    assert len(os.path.commonprefix(bad)) < 30          # diverges in the timestamp
    assert len(os.path.commonprefix(good)) > len(SYSTEM)

    uncached, cached = prefix_cost(1)
    assert cached > uncached                            # one request: write premium only
    uncached, cached = prefix_cost(2)
    assert round(cached, 2) == 1.35 and cached < uncached
    uncached, cached = prefix_cost(10)
    assert round(cached, 2) == 2.15                     # vs 10.0 uncached
    print("ok")
```
{% endraw %}

## Pitfalls

- **Silent prompt-cache misses.** Symptom: cached-token ratio near zero, TTFT and bill higher than expected. Cause: a timestamp, request id or reordered tool list in the prefix. Fix: diff two rendered prompts byte by byte; move volatile content last.
- **Caching bad responses.** One truncated or refused answer is served to everyone. Fix: cache only successful, validated, complete responses.
- **Semantic false hits.** Symptom: confident answers to the wrong question, complaints clustered on cache-served responses. Fix: scope, entity and negation guards, high `τ`, restrict to generic intents.
- **Cross-tenant leaks.** A key without tenant or ACL scope serves one customer's answer to another. Fix: scope in the key builder, not at call sites, and test it.
- **Stale answers after content changes.** Fix: version keys by index and document versions; purge on delete.
- **Stampedes on expiry.** Fix: single-flight per key, jittered TTLs.

## Checklist

- Exact cache keys include model version, prompt version, params, tools, normalized input, context version and tenant.
- Cache only complete, validated responses; single-flight on misses.
- Semantic caching only for generic intents, scoped exactly, with entity and negation guards and a threshold chosen from labeled data.
- Prompt layout: tools, system, examples, shared documents, history, then volatile content; deterministic serialization.
- Cached-token ratio tracked per feature, with a regression test.
- Self-hosted: prefix caching on, affinity routing across replicas.
- Embedding cache keyed by model and content hash; retrieval cache keyed by index version.
- Deletions purge derived cache entries; per-tenant generation counters for bulk invalidation.

## Interview questions

**What is the difference between prompt caching and response caching?**
Prompt caching reuses the model's computed state for an identical prefix; the model still generates a fresh answer, so correctness is unaffected and only latency and cost change. Response caching skips the model and returns a stored answer, so you own the correctness of the key and its invalidation.

**How would you lay out a prompt for prefix caching?**
Most stable first: tool definitions in a fixed order, the system prompt without dates or ids, few-shot examples, shared documents, append-only history, then retrieved passages and the current question. Verify with cached-token counts in the usage data.

**Is semantic caching a good idea for a RAG assistant?**
Only for generic, non-personal intents, scoped by tenant and prompt version, with exact guards on entities, numbers and negation, and a threshold chosen from labeled near-miss pairs. Similarity measures topic, not answer equivalence, so unguarded semantic caching returns confident wrong answers.

**How do you invalidate cached LLM answers when documents change?**
Put index and document versions in keys so new content changes keys, keep TTLs as a backstop, and maintain a document-to-key mapping to purge answers derived from deleted documents. Per-tenant generation counters handle bulk invalidation.

**Why does prefix-cache hit rate drop when you scale out vLLM replicas?**
Each replica has its own KV cache, and a load balancer without affinity sends requests sharing a prefix to different replicas. Route by prefix hash or conversation id so related requests land on the replica that holds their prefix.

## Further reading

- Efficient Memory Management for Large Language Model Serving with PagedAttention (Kwon et al., 2023).
- SGLang: Efficient Execution of Structured Language Model Programs (Zheng et al., 2024), which introduces RadixAttention.
- vLLM documentation: automatic prefix caching.
- OpenAI prompt caching guide and Anthropic prompt caching documentation.
- Auditing Prompt Caching in Language Model APIs (Gu et al., 2025).
