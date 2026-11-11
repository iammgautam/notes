---
title: Latency and cost engineering
section: llm
order: 15
summary: Measure where LLM time and money go, then cut them with streaming, prompt caching, shorter prompts and outputs, smaller models and batch APIs.
---

LLM features are slow and expensive compared to the rest of a backend: a single call can take seconds and cost more than thousands of database queries. Both latency and cost are driven by the same few variables (tokens in, tokens out, model size, number of calls) so the same discipline fixes both: measure per feature, then remove tokens, calls and model size you do not need.

## How it works

### Where the time goes

```text
E2E ≈ network + queue + prefill(input tokens) + decode(output tokens)
    ≈ TTFT + TPOT × output_tokens
```

Order-of-magnitude intuition (varies widely by model and provider):

| Component | Typical |
|---|---|
| TTFT, short prompt | 200 ms – 1 s |
| TTFT, very long prompt (100k+ tokens) | several seconds or more |
| Output speed | ~30–200 tokens/s per stream |
| 500 output tokens | ~3–15 s |
| Reasoning models | Hidden reasoning tokens add seconds to minutes |

Output tokens dominate latency. Input tokens dominate cost in RAG and agent workloads, because every call resends instructions, tools, retrieved context and history.

### Where the money goes

```text
cost per call = input_tokens × input_price
              + cached_input_tokens × cached_price   (much lower)
              + output_tokens × output_price         (several x input price)
```

Agents multiply this: a 10-step loop resends the growing context 10 times. Total input tokens for a run grow roughly quadratically with steps if you never trim history.

## In practice

### 1. Measure first

Log per call: feature/task, tenant, model, input tokens, cached tokens, output tokens, TTFT, E2E, cost. Then aggregate:

```sql
SELECT task,
       count(*)                                         AS calls,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e_ms) AS p95_ms,
       avg(input_tokens)::int                           AS avg_in,
       avg(output_tokens)::int                          AS avg_out,
       sum(cost_usd)                                    AS cost
FROM llm_calls
WHERE created_at > now() - interval '7 days'
GROUP BY task
ORDER BY cost DESC;
```

The top two or three tasks are usually most of the bill. Optimize those, not everything.

### 2. Stream

Streaming does not reduce E2E time, but it cuts perceived latency from E2E to TTFT. For chat and answer UIs it is mandatory. Stream through your backend with Server-Sent Events; disable proxy buffering (for example `X-Accel-Buffering: no` behind nginx), send keep-alives, and handle client disconnects by cancelling the upstream request so you stop paying for tokens nobody reads.

```python
from django.http import StreamingHttpResponse

def answer_stream(request):
    async def events():
        async for ev in gateway.stream(task="rag_answer", req=build(request)):
            if ev.type == "text_delta":
                yield f"event: delta\ndata: {json.dumps({'t': ev.text})}\n\n"
        yield "event: done\ndata: {}\n\n"
    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
```

### 3. Prompt caching

Providers and self-hosted engines can reuse the KV cache for an identical prompt prefix, which cuts TTFT and bills cached input tokens at a steep discount (see [PagedAttention and prefix caching]({{ '/llm/batching-and-paged-attention/' | relative_url }})). To benefit:

- Put stable content first: system prompt, tool definitions, few-shot examples, large shared documents.
- Put variable content last: retrieved passages, user question.
- Never put timestamps, request ids or per-user data at the top.
- Some providers cache automatically above a minimum prefix length; others require explicit cache markers. Caches expire after minutes of inactivity.
- Track the cached-token ratio per task. If it is near zero for a task with a big static prefix, something in the prefix varies.

### 4. Shrink prompts

- Retrieve fewer, better passages: rerank to the top 5–8 instead of stuffing 20.
- Trim conversation history: keep the last few turns verbatim, summarize older ones.
- Truncate tool results to the fields the model needs.
- Remove redundant instructions; compact JSON; drop unused tools from the request.
- Cap context per tenant or per request to bound worst-case cost.

### 5. Shrink outputs

- Ask for concise answers and set `max_tokens` per task.
- For extraction, return ids and short fields, not prose.
- For reasoning models, use the lowest reasoning effort that passes the eval.

### 6. Use smaller models where they suffice

Route by task: query rewriting, classification, routing, extraction and reranking-with-LLM often work with small models that are an order of magnitude cheaper and faster. Use a cascade for tasks with checkable outputs: small model first, validate, escalate to a large model only on failure. Every routing change must pass the task's eval (see [multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})).

### 7. Cache responses

| Cache | Key | Safe when |
|---|---|---|
| Exact response cache | Hash of (model, prompt version, normalized input, tenant) | Deterministic tasks: classification, extraction, embeddings |
| Embedding cache | (model, text hash) | Always |
| Retrieval cache | (tenant, query, index version) | Short TTL; invalidate on ingest |
| Semantic cache | Similar query embedding | Rarely for answers: near-duplicate questions can need different answers |

> **Gotcha:** Semantic caching of final answers returns one user's answer to a "similar" but different question, and across tenants it is a data leak. Scope keys by tenant and use high thresholds, or avoid it.

### 8. Batch offline work

Ingest-time enrichment, nightly classification, evals and backfills do not need low latency. Use provider batch APIs (typically about half price, with results within hours) or run them on your own schedule with high concurrency on self-hosted models. Keep them on separate quotas from interactive traffic.

### 9. Parallelize independent calls

Run independent LLM calls, retrieval and tool calls concurrently with `asyncio.gather`. Query rewriting and an initial retrieval with the raw query can run in parallel. Speculatively start the likely next step when it is cheap.

## Pitfalls

- **Optimizing without per-task attribution.** Fix: tag every call with task and tenant.
- **Cache-busting prefixes.** A date in the system prompt disables prompt caching. Fix: move it to the end.
- **Unbounded agent loops.** One run costs as much as thousands of normal requests. Fix: per-run token budgets and per-tenant daily caps with alerts.
- **Paying for abandoned streams.** Fix: cancel upstream on client disconnect.
- **Cheaper model, silent quality drop.** Fix: eval-gated routing changes.
- **Proxy buffering.** Streaming works locally and arrives all at once in production. Fix: disable buffering in every proxy hop and check gzip settings.

## Checklist

- Per-call logs with task, tenant, tokens (input, cached, output), TTFT, E2E and cost.
- Stream user-facing responses; cancel on disconnect.
- Stable-prefix prompt layout; monitor cached-token ratio.
- Rerank and trim context; cap `max_tokens` per task.
- Small models for simple tasks; cascades where outputs are checkable.
- Batch APIs for offline work; separate quotas.
- Budgets and alerts per tenant and per agent run.

## Interview angle

- **A feature's LLM bill doubled. How do you investigate?** Break down by task, tenant and model; compare input, cached and output tokens per call; look for prompt growth, cache-hit drops, agent step increases or routing changes.
- **How do you make a slow LLM feature feel fast?** Stream for TTFT, shorten outputs, parallelize independent calls, use prompt caching, and route sub-steps to small models.
- **Why does prompt order matter for cost?** Prefix caching matches exact token prefixes, so stable content must come first to be reused.
