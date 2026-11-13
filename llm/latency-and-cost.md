---
title: Latency and cost engineering
section: llm
order: 48
phase: "Phase 8 · Production"
summary: Find where LLM time and money go, then cut both with streaming, parallel calls, smaller models, shorter prompts and outputs, batch APIs and budgets.
---

LLM calls are slow and expensive compared to the rest of a backend: one call can take seconds and cost more than thousands of database queries. Latency and cost are driven by the same few variables (tokens in, tokens out, model size, number of sequential calls), so the same discipline fixes both: attribute every call to a feature and tenant, find the two or three features that dominate, and remove the tokens, calls and model size they do not need. This note covers where the time and money go, the techniques that cut them, the cost math to do on paper before shipping, and the budgets and alerts that keep spend from surprising you.

## How it works

### Where latency goes

```text
time ─────────────────────────────────────────────────────────────►
│ net+TLS │ gateway │ queue │ prefill (input) │ decode (output) ... │
│◄────────────────── TTFT ──────────────────►│                     │
│◄───────────────────────────── E2E ───────────────────────────────►│

TTFT ≈ network + queue + prefill(input_tokens)
E2E  ≈ TTFT + output_tokens × TPOT        (TPOT: time per output token)
```

Prefill processes all input tokens in parallel and is compute-bound, so it grows with prompt length. Decode generates one token at a time and is memory-bandwidth-bound, so each output token costs roughly the same wall time regardless of prompt size (see [Inference basics]({{ '/llm/inference-basics/' | relative_url }})). Order-of-magnitude numbers for hosted APIs, as of 2025–2026 (they vary widely by model, provider and load):

| Component | Typical | Driven by |
|---|---|---|
| Network, TLS, gateway | 10–100 ms; more cross-region | Region, connection reuse |
| Queue | 0 to seconds | Provider load, your quota, tier |
| TTFT, short prompt | 200 ms – 1 s | Model size, load |
| TTFT, 100k+ token prompt | Several seconds or more | Input tokens, prompt caching |
| Decode speed | ~30–200+ tokens/s per stream | Model size, hardware |
| 500 output tokens | ~3–15 s | Output length |
| Hidden reasoning | Seconds to minutes | Reasoning effort |
| Tool call or retrieval step | 50 ms to seconds | Your systems |

Three consequences drive most decisions:

- **Output tokens dominate end-to-end latency.** Cutting 500 output tokens to 150 saves seconds; cutting 2,000 input tokens saves tens to hundreds of milliseconds.
- **Sequential calls multiply everything.** An agent with eight sequential steps at two seconds each takes 16 seconds before the final answer starts.
- **Input tokens dominate cost in RAG and agents**, because every call resends instructions, tool definitions, retrieved context and history.

### Perceived latency

Users judge time to first visible progress and steady flow, not total time. Streaming turns a 10-second wait into under a second to the first word. For multi-step pipelines, stream status events ("Searching documents…") while tools run. Streaming does not help when the consumer needs the whole result (a JSON payload filling a form); there, E2E is the metric and output length is the lever.

A latency budget for a streamed RAG answer makes the trade-offs concrete:

| Step | Budget |
|---|---|
| Auth, tenant lookup | 20 ms |
| Query rewrite (small model) | 400 ms |
| Embed query + hybrid search | 130 ms |
| Rerank | 150 ms |
| Answer model TTFT (with prompt cache hit) | 700 ms |
| **First token to user** | **~1.4 s** |
| Generation of 300 tokens at ~60 tokens/s | ~5 s, streamed |

### Where the money goes

```text
cost = uncached_input × p_in + cache_reads × p_cache_read
     + cache_writes × p_cache_write + output × p_out
     (reasoning tokens are billed as output)
```

Price structure, as of 2025–2026: output tokens cost several times input tokens (3–5× is common); cached input reads cost a fraction of normal input, and some providers charge a premium to write the cache; batch APIs discount around 50%; model tiers within one vendor span 10–100× in price. Costs that teams forget: retries and fallbacks, reasoning tokens you never see, embeddings, guardrail and judge calls, evals, and abandoned streams still generating.

Agents are the main cost multiplier. If each step resends the base prompt plus everything appended so far, total input tokens for a run are `steps × base + growth × steps × (steps − 1) / 2`: quadratic in steps. Doubling the steps roughly triples the tokens, as the calculator below shows.

## Techniques

| Technique | Latency | Cost | Trade-off |
|---|---|---|---|
| Streaming | Perceived ↓↓ | None | Proxy buffering, partial failures |
| Parallel independent calls | ↓↓ | None | Rate-limit bursts, complexity |
| Fewer sequential calls | ↓↓ | ↓ | Merged steps can lose quality |
| Smaller or faster models | ↓↓ | ↓↓ | Quality; needs evals |
| Lower reasoning effort | ↓↓ | ↓↓ | Quality on hard tasks |
| Prompt caching | TTFT ↓ | Input ↓↓ | Prompt layout discipline |
| Shorter prompts | TTFT ↓ | ↓ | Recall loss if over-trimmed |
| Output length control | ↓↓ | ↓ | Truncation |
| Response caching | ↓↓↓ on hit | ↓↓↓ on hit | Staleness, leaks |
| Batch APIs | Hours | ~50% off | Offline only |

### Streaming

Stream through your backend with Server-Sent Events, disable buffering at every proxy hop, and cancel the upstream request when the client disconnects so you stop paying for tokens nobody reads. Illustrative Django view (the `gateway` is your routing layer):

```python
import json
from django.http import StreamingHttpResponse

async def answer_stream(request):
    async def events():
        async for ev in gateway.stream(role="rag_answer", req=build(request)):
            if ev.type == "text_delta":
                yield f"event: delta\ndata: {json.dumps({'t': ev.text})}\n\n"
        yield "event: done\ndata: {}\n\n"
    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"        # nginx: do not buffer
    return resp
```

Async views and async streaming responses need an ASGI server. Under WSGI each open stream holds a worker for its full duration, which caps concurrency at the worker count.

### Parallelize and remove sequential steps

- Run independent work concurrently: query rewriting alongside a first retrieval with the raw query, several retrieval sources at once, and all tool calls a model emits in one turn.
- Merge steps: one structured-output call that classifies and extracts beats two calls, if the eval holds.
- Speculate: start the likely next step early (prefetch documents for the top intent) and cancel it if the guess was wrong.

```python
import asyncio
import time

async def fake_llm(name: str, seconds: float) -> str:
    await asyncio.sleep(seconds)           # stands in for network + generation
    return name

STEPS = [("rewrite", 0.20), ("search", 0.15), ("classify", 0.10)]

async def serial() -> list[str]:
    return [await fake_llm(n, s) for n, s in STEPS]

async def parallel() -> list[str]:
    return list(await asyncio.gather(*(fake_llm(n, s) for n, s in STEPS)))

if __name__ == "__main__":
    t0 = time.perf_counter()
    s = asyncio.run(serial())
    t_serial = time.perf_counter() - t0
    t0 = time.perf_counter()
    p = asyncio.run(parallel())
    t_parallel = time.perf_counter() - t0
    assert s == p == ["rewrite", "search", "classify"]   # gather keeps order
    assert t_serial > 0.4 and t_parallel < 0.35          # sum vs max
    print(f"serial {t_serial:.2f}s, parallel {t_parallel:.2f}s")
```

`asyncio.TaskGroup` (Python 3.11+) is the better choice when one failure should cancel its siblings. Parallel fan-out also creates bursts against provider rate limits; cap concurrency with a semaphore.

### Smaller and faster models

Route by role: query rewriting, classification, routing, extraction and summarization of tool output often work on models an order of magnitude cheaper and faster than your answer model. Use cascades where outputs are checkable (see [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})), and [distillation]({{ '/llm/distillation/' | relative_url }}) when a high-volume task needs a small model to match a large one. Some providers sell faster output at a premium (priority or "fast" tiers); for steady high volume, a self-hosted small model on vLLM can be cheaper per token if you keep GPUs busy (see [Serving engines]({{ '/llm/serving-engines/' | relative_url }})). Judge cost per completed task, not per request: a cheap model that needs retries or extra agent steps can cost more.

### Reduce prompt size

- Log token counts per prompt section (system, tools, history, retrieved context, question). You cannot trim what you cannot see.
- Retrieve fewer, better passages: rerank to the top 5–8 instead of stuffing 20.
- Keep the last few turns verbatim and summarize older ones, in large steps (rewriting history every turn breaks prompt caching).
- Truncate tool results to the fields the model needs; compact JSON.
- Tool definitions are tokens too. Thirty verbose tool schemas can add thousands of tokens to every call; send only the tools the current step can use.
- Cap context per request to bound worst-case cost (see [Context engineering]({{ '/llm/context-engineering/' | relative_url }})).

### Control output length

Set `max_tokens` per role, ask for concise answers, return ids and short fields instead of prose for extraction, and use stop sequences where formats allow. For reasoning models, use the lowest reasoning effort that passes the eval; reasoning tokens are billed and add latency even though you never see them. Monitor the rate of `finish_reason = length`: rising truncation means the cap is too tight or prompts are drifting.

### Batch APIs for offline jobs

Ingest-time enrichment, nightly classification, eval runs and backfills do not need answers in seconds. OpenAI's Batch API and Anthropic's Message Batches (Gemini has a batch mode too) accept many requests at once, return results within a completion window of up to 24 hours (often much sooner) and charge roughly half price, with quota separate from interactive traffic. Design for it:

- Give each request a stable `custom_id` (for example `doc-123:summary:v4`); results can come back in any order.
- Handle per-item failures and expirations; resubmit only those.
- Make the write-back idempotent so a re-run does not duplicate rows.

An OpenAI batch input file is JSONL, one request per line (illustrative):

{% raw %}
```json
{"custom_id": "doc-123:summary:v4", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "<model-id>", "max_tokens": 300, "messages": [{"role": "user", "content": "Summarize: ..."}]}}
```
{% endraw %}

### Caching

Provider prompt caching cuts TTFT and input cost for long stable prefixes; it only works if stable content comes first and nothing volatile (timestamps, request ids) sits in the prefix. Response, embedding and retrieval caches skip work entirely. Both are covered in [Caching for LLM systems]({{ '/llm/llm-caching/' | relative_url }}).

## In practice

### Cost math before you ship

Run the numbers per request, per user and per agent run before launch. The prices below are illustrative round numbers, not any vendor's list price; load real ones from a versioned price table.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Price:                      # USD per 1M tokens (illustrative)
    input: float
    output: float
    cache_read: float
    cache_write: float

PRICES = {
    "large": Price(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "small": Price(input=0.25, output=1.25, cache_read=0.025, cache_write=0.3125),
}

def call_cost(model: str, input_tokens: int, output_tokens: int,
              cache_read: int = 0, cache_write: int = 0, batch: bool = False) -> float:
    """input_tokens is the whole prompt; cache_read/cache_write are parts of it."""
    p = PRICES[model]
    uncached = input_tokens - cache_read - cache_write
    if uncached < 0:
        raise ValueError("cached parts exceed the prompt")
    usd = (uncached * p.input + cache_read * p.cache_read
           + cache_write * p.cache_write + output_tokens * p.output) / 1e6
    return usd * (0.5 if batch else 1.0)

def agent_input_tokens(steps: int, base: int, growth: int) -> int:
    """Each step resends the base prompt plus everything appended so far."""
    return sum(base + i * growth for i in range(steps))

def monthly_per_user(cost_per_request: float, requests_per_day: int, days: int = 30) -> float:
    return cost_per_request * requests_per_day * days

def e2e_seconds(ttft: float, output_tokens: int, tokens_per_s: float) -> float:
    return ttft + output_tokens / tokens_per_s

if __name__ == "__main__":
    # RAG answer: 2,000 static prefix + 3,500 retrieved + 500 question/history
    plain = call_cost("large", 6_000, 400)
    cached = call_cost("large", 6_000, 400, cache_read=2_000)
    assert round(plain, 4) == 0.024           # 0.018 input + 0.006 output
    assert round(cached, 4) == 0.0186
    assert round(call_cost("small", 6_000, 400), 4) == 0.002
    assert call_cost("large", 6_000, 400, batch=True) == plain / 2

    # 20 requests/day per user vs a $20/month seat
    assert round(monthly_per_user(plain, 20), 2) == 14.40
    assert round(monthly_per_user(cached, 20), 2) == 11.16

    # Agent loop: 2x steps -> ~3.2x input tokens
    t10 = agent_input_tokens(10, 3_000, 800)
    t20 = agent_input_tokens(20, 3_000, 800)
    assert t10 == 10 * 3_000 + 800 * 10 * 9 // 2 == 66_000
    assert t20 == 212_000

    # Latency: output length dominates
    assert round(e2e_seconds(0.7, 500, 50), 2) == 10.7
    assert round(e2e_seconds(0.7, 150, 50), 2) == 3.7

    # Cascade: small model first, 30% escalate to the large one
    expected = call_cost("small", 6_000, 400) + 0.3 * plain
    assert round(expected, 4) == 0.0092 and expected < plain
    print(f"plain={plain:.4f} cached={cached:.4f} agent10={t10} agent20={t20}")
```

Reading the output: at these prices a heavy user costs about $14 a month on the large model, a large share of a $20 seat. Caching the static prefix saves about 22%; routing the task to the small model saves over 90% if the eval allows it; the agent case shows why per-run step and token caps matter.

### Attribute every call

Log one row per LLM call with the feature (role), tenant, user, run id, provider, model, token breakdown, latency and cost computed at write time from a versioned price table:

```sql
CREATE TABLE llm_usage (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    tenant_id      bigint NOT NULL,
    user_id        bigint,
    feature        text NOT NULL,            -- role, e.g. rag_answer
    run_id         uuid,                     -- request or agent run
    provider       text NOT NULL,
    model          text NOT NULL,
    input_tokens   int NOT NULL,
    cached_tokens  int NOT NULL DEFAULT 0,
    output_tokens  int NOT NULL,             -- includes reasoning tokens
    cost_usd       numeric(12, 6) NOT NULL,
    ttft_ms        int,
    e2e_ms         int
);
CREATE INDEX ON llm_usage (tenant_id, created_at);

-- Where do time and money go, by feature?
SELECT feature,
       count(*)                                                AS calls,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e_ms)    AS p95_ms,
       avg(input_tokens)::int                                  AS avg_in,
       avg(cached_tokens::float / nullif(input_tokens, 0))     AS cache_ratio,
       avg(output_tokens)::int                                 AS avg_out,
       sum(cost_usd)                                           AS cost
FROM llm_usage
WHERE created_at > now() - interval '7 days'
GROUP BY feature
ORDER BY cost DESC;
```

At high volume, write rows asynchronously (a queue or batched inserts) and roll them up hourly; partition by time once the table is large.

### Budgets

- **Hierarchy:** organization, tenant, user and agent run, each with a period and a limit.
- **Soft limit:** alert and degrade (smaller model, shorter context, fewer agent steps).
- **Hard limit:** reject with a clear message; never fail silently.
- **Admission:** estimate cost before the call (input tokens plus `max_tokens` at output price), reserve it against the budget, settle with real usage afterwards. A counter in Redis or a row lock in PostgreSQL both work; the reserve-then-settle pattern matters more than the store.
- **Per-run caps for agents:** maximum steps, tokens and cost per run, enforced in the loop (see [Agents]({{ '/llm/agents/' | relative_url }})).

### Cost alerts

Alert on rate of change, not just absolute spend:

```sql
WITH daily AS (
    SELECT tenant_id, date_trunc('day', created_at) AS day, sum(cost_usd) AS cost
    FROM llm_usage
    WHERE created_at >= date_trunc('day', now()) - interval '7 days'
    GROUP BY 1, 2
)
SELECT t.tenant_id, t.cost AS today, avg(h.cost) AS baseline
FROM daily t
JOIN daily h ON h.tenant_id = t.tenant_id AND h.day < t.day
WHERE t.day = date_trunc('day', now())
GROUP BY t.tenant_id, t.cost
HAVING t.cost > 3 * avg(h.cost) AND t.cost > 5;   -- ignore tiny tenants
```

Also alert on cost per request p95 after deploys, a drop in cached-token ratio, rising tokens per request, and agent runs hitting their caps. Set provider-side spend limits as a backstop, and reconcile your ledger with provider usage reports monthly; drift means you are missing calls (often retries, evals or batch jobs).

## Pitfalls

- **Optimizing without attribution.** Symptom: effort spent on features that are 2% of the bill. Fix: tag every call with feature and tenant.
- **Averages instead of percentiles.** p50 looks fine while p95 TTFT is 8 seconds. Fix: track p95 and p99 per feature and model.
- **Cache-busting prefixes.** A date or request id at the top of the system prompt disables prompt caching. Symptom: cached-token ratio near zero. Fix: volatile content last.
- **Unbounded agent loops.** One run costs as much as thousands of normal requests. Fix: per-run caps and per-tenant daily limits with alerts.
- **Paying for abandoned streams.** Fix: cancel upstream on client disconnect.
- **Proxy buffering.** Streaming works locally and arrives in one burst in production. Fix: disable buffering and compression on every hop for SSE.
- **Cheaper model, silent quality drop.** Fix: eval-gated routing changes; compare cost per completed task.
- **Invisible reasoning spend.** Fix: log reasoning tokens separately and tune effort per role.
- **Prices hardcoded in code.** Fix: versioned price table with effective dates.

## Checklist

- Per-call rows with feature, tenant, tokens (input, cached, output, reasoning), TTFT, E2E and cost.
- Stream user-facing responses; cancel on disconnect; disable proxy buffering.
- Parallelize independent calls; cap concurrency; remove sequential steps where evals allow.
- Small models for simple roles; lowest reasoning effort that passes the eval.
- Stable-prefix prompts; watch the cached-token ratio.
- Rerank and trim context; `max_tokens` per role; watch truncation rate.
- Batch APIs for offline work, with idempotent write-back.
- Budgets with reserve-then-settle admission, per-run agent caps, and rate-of-change alerts.

## Interview questions

**A feature's LLM bill doubled. How do you investigate?**
Break spend down by feature, tenant and model over time. Compare input, cached and output tokens per call and calls per request: prompt growth, a cache-hit drop after a prompt change, more agent steps, retries, or a routing change to a pricier model. Check whether one tenant explains it.

**What drives TTFT versus total latency?**
TTFT is network, queueing and prefill, which scales with input tokens and is cut by prompt caching and shorter prompts. Total latency adds output tokens times per-token decode time, so output length and the number of sequential calls dominate it.

**How do you make a slow LLM feature feel fast?**
Stream tokens and progress events, shorten outputs, parallelize independent calls, cut sequential steps, use prompt caching for long stable prefixes, and route sub-steps to small models.

**Estimate monthly cost for a feature.**
Tokens in and out per request times prices, adjusted for the cached share, times requests per user per day, times users and days. Add retries, fallbacks, embeddings, guardrails and evals, and compare per-user cost to the price of the plan.

**When would you use a batch API?**
For work nobody waits on: evals, backfills, enrichment, nightly classification. About half the price and separate quota, at the cost of hours of latency and per-item failure handling.

**How do you enforce per-tenant LLM budgets?**
Attribute every call; estimate and reserve cost before calling, settle after; soft limits degrade to cheaper behavior, hard limits reject clearly; agents also get per-run step and token caps; alert on spend relative to each tenant's baseline.

## Further reading

- Efficient Memory Management for Large Language Model Serving with PagedAttention (Kwon et al., 2023).
- DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving (Zhong et al., 2024).
- FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance (Chen et al., 2023).
- OpenAI Batch API guide and Anthropic Message Batches documentation.
- Provider pricing and rate-limit documentation for the models you use.
