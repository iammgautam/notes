---
title: Multi-provider LLM routing
section: llm
order: 11
summary: Build a provider abstraction with fallbacks, timeouts, rate-limit handling, cost and latency routing, and normalized streaming across LLM vendors.
---

Depending on one LLM provider means inheriting its outages, rate limits, deprecations and price changes. A routing layer puts your application behind one internal interface and decides per request which provider and model serves it. Done well it improves availability and cost. Done badly it is a leaky abstraction that hides quality differences between models and makes debugging harder.

## How it works

### The abstraction boundary

Every provider has a chat-completion API with the same idea and different details: message shapes, system prompt placement, tool-call formats, structured-output options, streaming event types, usage fields, error codes and rate-limit headers. Your abstraction normalizes the common subset and makes everything else explicit.

```text
 app ──► LLMGateway.complete(task="rag_answer", messages, tools, schema)
                 │
                 ├── policy: task → ordered [(provider, model), ...]
                 ├── budget: per-tenant limits, token estimates
                 ├── for each candidate: adapter.request(...)
                 │      timeouts, retries, circuit breaker per provider
                 ├── normalize: response, usage, finish_reason, stream
                 └── log: provider, model, tokens, cost, latency, outcome
```

Route by **task**, not by model name, in application code. `task="rag_answer"` maps to a list of acceptable models that were evaluated for that task. Swapping models becomes a config change behind an eval, not a code change.

### Fallbacks are not free

Models differ in instruction following, tool-calling reliability, structured-output support, context length and tokenization. A prompt tuned for one model may degrade on another. Every model in a fallback chain must pass the task's eval set. A fallback that returns worse answers silently can be worse than a clean error.

### Timeouts

LLM calls have long, variable latency, so a single total timeout is wrong. Use several:

| Timeout | Typical | Purpose |
|---|---|---|
| Connect | 2–5 s | Fail fast on network issues |
| Time to first token | 5–20 s | Detect queued or stuck requests in streaming |
| Inter-token (idle) | 10–30 s | Detect stalled streams |
| Total | Task-dependent, 30–300 s | Bound worst-case; scale with `max_tokens` |

### Rate limits

Providers limit requests per minute and tokens per minute (input and output, sometimes separately), per model and per account or project. Responses use HTTP 429, often with `Retry-After` or reset headers.

- Respect `Retry-After`; otherwise exponential backoff with full jitter.
- Track remaining quota from response headers and route away *before* hitting 429s.
- Enforce your own per-tenant limits upstream so one tenant cannot exhaust the shared provider quota.
- Separate interactive and batch traffic into different keys or projects so a backfill does not starve users.

### Retries and circuit breakers

Retry transient errors (429, 5xx, overloaded, connection reset) with backoff, a small number of times. Do not retry 400-class errors (bad request, context too long, content policy); they will fail again. Do not retry after a stream has started emitting tokens to the user unless you can restart the UI cleanly.

A circuit breaker per (provider, model) opens after an error-rate threshold, sends traffic to the next candidate, and half-opens periodically to probe recovery. This avoids paying a full timeout on every request during an outage.

### Cost and latency routing

- **Static tiers:** cheap, fast models for classification, extraction and query rewriting; larger models for final answers and complex reasoning. This alone often cuts cost by an order of magnitude.
- **Cascades:** try the small model, check confidence or validation, escalate to the large model only on failure.
- **Latency-aware:** track rolling p50/p95 time-to-first-token per provider and prefer the fastest healthy one for interactive traffic.
- **Hedged requests:** for latency-critical calls, send a second request to another provider if the first has not produced a token after a deadline; cancel the loser. Costs extra tokens; use sparingly.

## In practice

### Normalized types and an adapter interface

```python
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str                       # raw JSON string

@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    usage: Usage
    provider: str
    model: str

@dataclass
class StreamEvent:
    type: Literal["text_delta", "tool_call_delta", "done", "error"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: FinishReason | None = None

class ProviderAdapter(Protocol):
    name: str
    async def complete(self, req: "ChatRequest") -> Completion: ...
    def stream(self, req: "ChatRequest") -> AsyncIterator[StreamEvent]: ...
```

Each adapter translates `ChatRequest` to the vendor format and maps vendor errors to your own exception types: `RateLimited(retry_after)`, `Overloaded`, `ContextTooLong`, `ContentFiltered`, `BadRequest`, `ProviderError`.

### The routing loop

```python
RETRYABLE = (RateLimited, Overloaded, ProviderTimeout, ProviderError)

async def complete(task: str, req: ChatRequest, tenant_id: int) -> Completion:
    await tenant_limiter.acquire(tenant_id, estimate_tokens(req))
    last_exc = None
    for provider, model in POLICY[task]:
        breaker = breakers[(provider, model)]
        if breaker.is_open():
            continue
        adapter = ADAPTERS[provider]
        for attempt in range(2):
            try:
                resp = await adapter.complete(req.with_model(model))
                breaker.record_success()
                log_call(task, tenant_id, resp)
                return resp
            except RateLimited as e:
                last_exc = e
                if e.retry_after and e.retry_after < 2:
                    await asyncio.sleep(e.retry_after)
                    continue
                break                                   # move to next candidate
            except RETRYABLE as e:
                last_exc = e
                breaker.record_failure()
                await asyncio.sleep(backoff_with_jitter(attempt))
            except (BadRequest, ContextTooLong, ContentFiltered):
                raise                                   # not fixed by retrying
    raise AllProvidersFailed(task) from last_exc
```

### Streaming normalization

Vendors stream Server-Sent Events with different event names, chunk shapes and placement of usage data (some only in the final event, some only when explicitly requested). Tool-call arguments arrive as partial JSON fragments that you must concatenate per call id or index before parsing. Normalize to your `StreamEvent` type at the adapter, and expose one SSE format to your frontend:

```text
event: text_delta      data: {"text": "Refunds are"}
event: text_delta      data: {"text": " available within"}
event: done            data: {"finish_reason": "stop", "usage": "..."}
```

Fallback during streaming is only safe before the first token reaches the client. After that, surface an error event and let the client retry.

> **In production:** Log the provider and model actually used on every response and expose it in traces. When quality complaints arrive, the first question is "which model answered this?"

## Pitfalls

- **Lowest-common-denominator abstraction.** Hiding features like prompt caching, strict schemas or reasoning controls throws away real wins. Fix: normalized core plus explicit, optional capability flags per adapter.
- **Unevaluated fallbacks.** Symptom: quality dips during a provider incident and nobody knows why. Fix: every candidate passes the task eval; alert when fallback traffic share rises.
- **Retry storms.** Every layer retries, multiplying load on a struggling provider. Fix: retry in one layer, with jitter and a budget.
- **Token estimates by one tokenizer.** Context-length errors on fallback models. Fix: per-model limits and headroom.
- **Shared quota across workloads.** Batch jobs cause user-facing 429s. Fix: separate keys and per-tenant limiters.
- **Cost accounting from list prices in code.** Prices change. Fix: price table in config with effective dates; compute cost from logged usage.

## Checklist

- Application code routes by task; config maps tasks to evaluated model lists.
- Adapters normalize messages, tool calls, usage, finish reasons, errors and streams.
- Separate connect, first-token, idle and total timeouts.
- Respect `Retry-After`; backoff with jitter; circuit breaker per provider/model.
- Per-tenant token limits; separate interactive and batch quotas.
- Log provider, model, tokens, cost and latency on every call.

## Interview angle

- **How do you make an LLM feature resilient to provider outages?** Provider abstraction, ordered evaluated fallbacks, circuit breakers, layered timeouts, and graceful degradation when all fail.
- **How do you handle 429s?** Honor `Retry-After`, backoff with jitter, track quota from headers, route to alternates, and limit tenants upstream.
- **How do you cut LLM cost without hurting quality?** Route easy tasks to small models, cascade on validation failure, cache, and trim prompts, all verified by per-task evals.
