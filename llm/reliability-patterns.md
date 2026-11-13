---
title: Reliability patterns for LLM calls
section: llm
order: 50
phase: "Phase 8 · Production"
summary: Make LLM calls dependable with layered timeouts, jittered retries, idempotent tools, circuit breakers, fallbacks, tenant rate limits and output checks.
---

LLM calls fail in more ways than a typical RPC. They are slow with long tails, rate-limited, periodically overloaded, can die halfway through a stream, and can "succeed" with HTTP 200 while returning a refusal, truncated text or invalid JSON. Agents add a new risk: a retried step can repeat a side effect such as sending an email or issuing a refund. Reliability here means bounding time, retrying only what retrying can fix, never repeating side effects, failing over or degrading deliberately, and validating output before acting on it. The general patterns are in [Idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }}); this note covers how they apply to LLM calls.

## Core ideas

### Failure taxonomy

| Failure | Signal | Retry the same call? | Response |
|---|---|---|---|
| Connection error | Exception before any response | Yes | Backoff and retry |
| Rate limited | 429, `retry-after` | Yes, after the delay | Wait, or route to another candidate |
| Overloaded or 5xx | 500, 502, 503, vendor overload codes | A few times | Backoff, then fail over |
| Slow first token | No token by the TTFT deadline | Maybe | Cancel, retry or fail over |
| Stalled stream | Gap between tokens too long | Not transparently | Keep partial output, surface error |
| Bad request | 400, context too long | No | Fix the request; trim or use a larger-context model |
| Auth | 401, 403 | No | Alert a human |
| Refusal | Finish reason or refusal text | Not blindly | Product decision, log for review |
| Truncated | `finish_reason = length` | With a larger cap | Raise cap or shorten the task |
| Malformed output | Validation fails | Once or twice, with the error | Repair loop, constrained decoding |

### The composed call path

```text
request (deadline = now + 30 s)
  │
  ├─ tenant token bucket ── over limit ──► 429 to client + retry-after
  ├─ bounded priority queue (interactive > batch) ── full ──► shed
  ├─ for candidate in role.candidates:
  │     breaker open? ──► skip, no timeout paid
  │     retry loop: backoff + jitter, honors retry-after,
  │                 stops when the deadline cannot be met
  │        connect, first-token, idle timeouts on each attempt
  │     validate: finish reason, refusal, schema
  │        ok ──► return
  └─ degrade: cached answer, simpler pipeline, honest error
```

Each layer has one job, and retries happen in exactly one place. If the SDK, your router and a gateway all retry three times, one user request can become 27 provider calls during an outage.

## Techniques

### Timeouts

| Timeout | Typical value | Catches |
|---|---|---|
| Connect | 2–5 s | Network and DNS problems |
| First token | 5–20 s, task-dependent | Queued or stuck requests |
| Idle (between tokens) | 10–30 s | Stalled streams |
| Total | Scale with `max_tokens`; 30–300 s | Worst-case runaway generation |
| Request deadline | Set at the edge | Everything downstream, including retries |

SDK defaults are generous (often minutes) because the SDK cannot know your use case; set them explicitly. An HTTP client's read timeout is usually the gap between received bytes, not total time, so a slowly trickling stream never trips it. Enforce first-token, idle and total deadlines yourself. Propagate one request deadline downward: a retry or fallback that cannot finish before the deadline should not start.

Illustrative SDK setup, with retries moved into your own layer:

```python
import httpx
from anthropic import Anthropic
from openai import OpenAI

timeout = httpx.Timeout(connect=3.0, read=20.0, write=10.0, pool=5.0)
openai_client = OpenAI(timeout=timeout, max_retries=0)
anthropic_client = Anthropic(timeout=timeout, max_retries=0)
```

### Retries with exponential backoff and jitter

- Retry only transient failures: connection errors, 429, 5xx, overload.
- Honor `retry-after` when present; it is the provider telling you exactly when capacity returns.
- Otherwise use exponential backoff with **full jitter**: `sleep = random(0, min(cap, base × 2^attempt))`. Jitter spreads synchronized clients apart so they do not retry in waves.
- Cap attempts (2–4) and respect the request deadline.
- Keep a **retry budget**: retries limited to a fraction of traffic (for example 10%) per client. When everything fails, a budget stops retries from multiplying load on a struggling provider.
- Retries are not free: a timed-out or cancelled stream can still be billed for the tokens already processed, and a retry pays for the prompt again.

### Idempotency for side-effecting calls

Re-sending an LLM request is safe for correctness (it only costs money). Re-executing a tool call is not. Agents retry steps after crashes, timeouts and redeploys, so every tool with side effects needs an idempotency key:

- Derive the key from your run, not from the model: `run_id:step:tool_name`. Model-generated tool call ids change if the step is regenerated.
- Checkpoint the model's response for each step before executing its tool calls. On resume, replay the recorded response instead of asking the model again, which could produce different arguments. LangGraph checkpointers and durable workflow engines exist for this.
- Pass the key to downstream APIs that support idempotency keys (payment APIs such as Stripe accept an `Idempotency-Key` header).
- Record executions with a unique constraint and return the stored result on repeats:

```sql
CREATE TABLE tool_executions (
    idempotency_key  text PRIMARY KEY,         -- run_id:step:tool_name
    tool             text NOT NULL,
    args             jsonb NOT NULL,
    status           text NOT NULL,            -- started | succeeded | failed
    result           jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Claim the execution; no row returned means it already ran or is running
INSERT INTO tool_executions (idempotency_key, tool, args, status)
VALUES ($1, $2, $3, 'started')
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING idempotency_key;
```

Gate irreversible actions (payments, deletions, external messages) behind human approval or a confirmation step as well (see [Agents]({{ '/llm/agents/' | relative_url }})).

### Circuit breakers

A breaker per (provider, model) tracks recent outcomes. When the failure rate crosses a threshold over a minimum number of calls, it **opens** and callers skip that candidate immediately instead of each paying a full timeout. After a cool-down it goes **half-open** and lets one probe through; success closes it, failure reopens it.

```text
                 failure rate ≥ threshold
    ┌────────┐   over min_calls             ┌──────┐
    │ CLOSED │─────────────────────────────►│ OPEN │◄──────┐
    └────────┘                              └──────┘       │
        ▲                                cool-down │       │ probe
        │                                  elapsed ▼       │ fails
        │       probe succeeds            ┌───────────┐    │
        └─────────────────────────────────│ HALF-OPEN │────┘
                                          └───────────┘
```

Count only failures that indicate provider health (timeouts, 5xx, overload), not 400s caused by your own bad requests. Treat 429s separately: they reflect your quota, and a rate limiter is the right tool for them.

### Fallbacks and graceful degradation

Fallbacks swap the provider or model; degradation reduces what the feature does. Plan a ladder per feature and record which rung served each request:

1. Primary model.
2. Same model on another platform, then an evaluated alternative model (see [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})).
3. A cheaper pipeline: skip the reranker, smaller context, smaller model.
4. A cached or precomputed answer, clearly marked if stale.
5. A non-LLM path: show search results instead of a synthesized answer.
6. An honest error with a retry option.

Back each rung with a feature flag or kill switch so on-call can force a rung during an incident. Under overload, shed low-priority work first: batch and background jobs before interactive requests.

### Partial streams

When a stream fails after tokens reached the user, you cannot transparently fail over: a second model would produce a different answer, not a continuation.

- Keep the partial text, show it with a clear error, and offer regenerate.
- Never act on partial structured output or partial tool-call arguments. Buffer until complete and validate.
- Continuation (send the partial text back and ask the model to continue) is possible on some APIs, but support varies and the seam is not guaranteed to be clean. Use it for long documents, not chat.
- Log the partial output, tokens consumed and where it failed. Partial streams are billed.

### Rate limiting and queueing

Admission control protects both the provider quota and fairness between tenants:

- A **token bucket per tenant** in tokens per minute (and optionally requests per minute). Reserve estimated input tokens plus `max_tokens` up front and settle with actual usage after the call.
- A **global bucket per provider key** set slightly below the provider's quota, so you queue or reroute before you see 429s.
- A **bounded priority queue**: interactive requests ahead of batch, and a maximum queue wait. When the queue is full, reject fast with 429 and `retry-after` rather than letting requests time out inside.
- Store bucket state in Redis (an atomic Lua script or a sliding-window counter) when you run more than one process.

### Refusals and malformed output

- **Refusals:** detect via finish reason (`content_filter`, `refusal`) and, for soft refusals in plain text, a pattern list or small classifier. Do not loop retrying the same prompt. Track the refusal rate per role; a spike after a model or prompt change usually means false positives on legitimate traffic. Some providers offer fallback routing for refusals.
- **Malformed output:** prevent with structured outputs or constrained decoding (see [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }})). Validate everything with a schema (Pydantic). On failure, retry once or twice with the validation error in the prompt, then fall back to another model or fail. Never execute unvalidated tool arguments.
- **Truncation:** `finish_reason = length` means JSON may be incomplete. Raise the cap, ask for less, or split the task.
- **Empty output:** treat as a failure, not as a valid answer.

### Chaos testing

Put a fault-injecting wrapper around the provider adapter and run it in CI and in staging game days:

| Fault | What should happen |
|---|---|
| Added latency before first token | First-token timeout, fallback within the deadline |
| 429 with `retry-after` | Waits or reroutes; no retry storm |
| Burst of 5xx | Breaker opens, fallback share rises, alert fires |
| Disconnect after k tokens | Partial output kept, error shown, no duplicate side effects |
| Malformed JSON, refusal, empty output | Validation catches it, repair or degrade |
| Provider unreachable (blocked egress) | Whole-provider failover works end to end |

Network-level tools such as Toxiproxy inject latency and disconnects without code changes. Verify the outcomes on dashboards and alerts, not just in logs (see [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).

## In practice

### Retries with full jitter, retry-after and a deadline

```python
import random

class Retryable(Exception):
    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after

class Fatal(Exception): ...

def backoff_delays(base: float = 0.5, cap: float = 20.0, rng=random.random):
    attempt = 0
    while True:
        yield rng() * min(cap, base * 2 ** attempt)     # full jitter
        attempt += 1

def call_with_retries(fn, *, sleep, clock, max_attempts=4, deadline_s=30.0,
                      rng=random.random):
    start, delays = clock(), backoff_delays(rng=rng)
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Retryable as e:
            if attempt == max_attempts:
                raise
            delay = e.retry_after if e.retry_after is not None else next(delays)
            if clock() - start + delay > deadline_s:
                raise                                   # cannot finish in time
            sleep(delay)

if __name__ == "__main__":
    now, sleeps = [0.0], []
    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s
    clock = lambda: now[0]

    outcomes = iter([Retryable("503"), Retryable("429", retry_after=2.0), "ok"])
    def flaky():
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o
    assert call_with_retries(flaky, sleep=fake_sleep, clock=clock, rng=lambda: 1.0) == "ok"
    assert sleeps == [0.5, 2.0]          # backoff, then the server's retry-after

    gen = backoff_delays(rng=random.Random(7).random)
    delays = [next(gen) for _ in range(8)]
    assert all(0 <= d <= min(20.0, 0.5 * 2 ** i) for i, d in enumerate(delays))

    calls = []
    def bad():
        calls.append(1)
        raise Fatal("400")
    try:
        call_with_retries(bad, sleep=fake_sleep, clock=clock)
    except Fatal:
        pass
    assert len(calls) == 1               # 4xx is never retried

    def throttled():
        raise Retryable("429", retry_after=60.0)
    try:
        call_with_retries(throttled, sleep=fake_sleep, clock=clock)
        raise AssertionError("should give up")
    except Retryable:
        pass                             # 60 s wait exceeds the 30 s deadline
    print("ok")
```

### Circuit breaker with half-open probes

```python
import time
from collections import deque

class CircuitBreaker:
    def __init__(self, window=20, min_calls=10, threshold=0.5, open_s=30.0,
                 clock=time.monotonic):
        self.min_calls, self.threshold, self.open_s, self.clock = (
            min_calls, threshold, open_s, clock)
        self.results: deque[bool] = deque(maxlen=window)
        self.state, self.opened_at, self.probing = "closed", 0.0, False

    def allow(self) -> bool:
        if self.state == "open":
            if self.clock() - self.opened_at < self.open_s:
                return False
            self.state = "half_open"
        if self.state == "half_open":
            if self.probing:
                return False             # exactly one probe at a time
            self.probing = True
        return True

    def record(self, ok: bool) -> None:
        if self.state == "half_open":
            self.probing = False
            if ok:
                self.state = "closed"
                self.results.clear()
            else:
                self._open()
            return
        self.results.append(ok)
        n = len(self.results)
        if n >= self.min_calls and self.results.count(False) / n >= self.threshold:
            self._open()

    def _open(self) -> None:
        self.state, self.opened_at = "open", self.clock()

if __name__ == "__main__":
    now = [0.0]
    cb = CircuitBreaker(clock=lambda: now[0])
    for ok in [True] * 4 + [False] * 4:
        assert cb.allow()
        cb.record(ok)
    assert cb.state == "closed"          # below min_calls: no decision yet
    for ok in [False, False]:
        cb.record(ok)
    assert cb.state == "open" and not cb.allow()

    now[0] = 31
    assert cb.allow() and cb.state == "half_open"
    assert not cb.allow()                # second caller waits for the probe
    cb.record(False)
    assert cb.state == "open"            # probe failed: open again

    now[0] = 62
    assert cb.allow()
    cb.record(True)
    assert cb.state == "closed" and cb.allow()
    print("ok")
```

### Token buckets per tenant

```python
class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float, clock):
        self.rate, self.capacity, self.clock = rate_per_s, capacity, clock
        self.tokens, self.updated = capacity, clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self, n: float) -> float:
        """Take n tokens. Returns 0.0 if granted, else seconds to wait."""
        if n > self.capacity:
            raise ValueError("request larger than the bucket")
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        return (n - self.tokens) / self.rate

    def settle(self, reserved: float, actual: float) -> None:
        self._refill()
        self.tokens = min(self.capacity, self.tokens + reserved - actual)

class TenantLimiter:
    def __init__(self, tokens_per_min: dict[str, int], default_tpm: int, clock):
        self.limits, self.default, self.clock = tokens_per_min, default_tpm, clock
        self.buckets: dict[str, TokenBucket] = {}

    def bucket(self, tenant: str) -> TokenBucket:
        if tenant not in self.buckets:
            tpm = self.limits.get(tenant, self.default)
            self.buckets[tenant] = TokenBucket(tpm / 60, tpm, self.clock)
        return self.buckets[tenant]

    def admit(self, tenant: str, est_input: int, max_tokens: int) -> float:
        return self.bucket(tenant).try_acquire(est_input + max_tokens)

if __name__ == "__main__":
    now = [0.0]
    lim = TenantLimiter({"enterprise": 60_000}, default_tpm=6_000, clock=lambda: now[0])
    assert lim.admit("small", 2_000, 1_000) == 0.0
    assert lim.admit("small", 2_000, 1_000) == 0.0
    wait = lim.admit("small", 2_000, 1_000)
    assert round(wait, 6) == 30.0                    # 3,000 tokens at 100/s
    assert lim.admit("enterprise", 2_000, 1_000) == 0.0   # isolated from "small"

    lim.bucket("small").settle(reserved=3_000, actual=1_200)  # refund unused
    assert round(lim.admit("small", 2_000, 1_000), 6) == 12.0
    now[0] = 12
    assert lim.admit("small", 2_000, 1_000) == 0.0
    print("ok")
```

### First-token, idle and total deadlines on a stream

```python
import asyncio

class FirstTokenTimeout(Exception): ...
class StreamStalled(Exception): ...

async def guarded(stream, first_token_s: float, idle_s: float):
    it, first = aiter(stream), True
    while True:
        try:
            chunk = await asyncio.wait_for(anext(it), first_token_s if first else idle_s)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise (FirstTokenTimeout() if first else StreamStalled()) from None
        first = False
        yield chunk

async def fake_stream(delays):
    for i, d in enumerate(delays):
        await asyncio.sleep(d)
        yield f"t{i}"

async def collect(delays, total_s=1.0):
    out = []
    try:
        async with asyncio.timeout(total_s):            # total deadline
            async for tok in guarded(fake_stream(delays), first_token_s=0.2, idle_s=0.1):
                out.append(tok)
    except (FirstTokenTimeout, StreamStalled, TimeoutError) as e:
        return out, type(e).__name__                    # keep partial output
    return out, None

if __name__ == "__main__":
    assert asyncio.run(collect([0.05, 0.02, 0.02])) == (["t0", "t1", "t2"], None)
    assert asyncio.run(collect([0.3])) == ([], "FirstTokenTimeout")
    assert asyncio.run(collect([0.05, 0.02, 0.3])) == (["t0", "t1"], "StreamStalled")
    out, err = asyncio.run(collect([0.05] * 10, total_s=0.2))
    assert err == "TimeoutError" and 0 < len(out) < 10
    print("ok")
```

## Pitfalls

- **Retry storms.** Retries in the SDK, router and gateway multiply. Symptom: provider traffic far above user traffic during incidents. Fix: one retry layer, retry budgets, jitter.
- **Timeouts that never fire.** A read timeout does not bound a trickling stream. Symptom: requests running for many minutes. Fix: explicit first-token, idle and total deadlines.
- **Duplicate side effects.** Symptom: two emails or refunds after a timeout. Fix: idempotency keys from run and step, checkpointed model responses, unique constraints.
- **Breaker counting your own bugs.** 400s from a bad deploy open the breaker and hide the real error. Fix: count only health-related failures.
- **Silent degradation.** Fallbacks and degraded rungs serve traffic for weeks unnoticed. Fix: log the rung, alert on fallback share.
- **Unbounded queues.** Requests wait past their deadline and then fail anyway. Fix: bounded queues, maximum wait, fast rejection.
- **Acting on partial or invalid output.** Fix: buffer and validate before executing tools or writing data.
- **Retrying refusals.** The same prompt gets the same refusal, at a cost. Fix: classify refusals; handle them as product cases.

## Checklist

- Connect, first-token, idle and total timeouts; one request deadline propagated down.
- Retries in one layer: transient errors only, `retry-after` honored, full jitter, capped attempts, retry budget.
- Idempotency keys from `run_id:step:tool`; checkpoint model output before executing tools.
- Circuit breaker per (provider, model) with half-open probes; 429s handled by limiters.
- Degradation ladder per feature with kill switches; rung logged on every response.
- Per-tenant token buckets with reserve-and-settle; bounded priority queue; fast 429s.
- Validate finish reason, refusals and schema before using output.
- Fault-injection tests in CI and periodic game days.

## Interview questions

**Which LLM API errors do you retry?**
Connection errors, 429 (after `retry-after`), 5xx and overload, with capped exponential backoff and full jitter inside a request deadline. Not 400s, auth errors, refusals or context-length errors; those need a different request, not the same one again.

**Why add jitter to exponential backoff?**
Without it, clients that failed together retry together, producing synchronized waves that keep the provider overloaded. Full jitter spreads retries uniformly across the backoff window.

**How do you stop an agent from sending the same email twice?**
Derive an idempotency key from the run and step, claim it with a unique constraint before executing, return the stored result on repeats, pass the key to downstream APIs, and checkpoint the model's response so a resumed run replays it instead of regenerating different arguments.

**What does a circuit breaker add on top of retries?**
Retries help one request survive a blip. A breaker protects all requests during an outage: once a candidate is failing, callers skip it immediately instead of each waiting for a timeout, and half-open probes detect recovery.

**A stream fails after 200 tokens. What do you do?**
Keep and show the partial output with an error and a regenerate option. You cannot transparently fail over mid-answer. Never act on partial structured output; log the partial for debugging and billing.

**How do you keep one tenant from exhausting your provider quota?**
Per-tenant token buckets that reserve input plus `max_tokens` and settle after the call, under a global bucket per provider key set below quota, with bounded priority queues and fast 429s with `retry-after`.

## Further reading

- Exponential Backoff and Jitter (Marc Brooker, AWS Architecture Blog, 2015).
- Release It! Design and Deploy Production-Ready Software (Michael Nygard), for circuit breakers and bulkheads.
- Site Reliability Engineering (Beyer et al., 2016), chapters "Handling Overload" and "Addressing Cascading Failures".
- The Tail at Scale (Dean and Barroso, 2013).
- Chaos Engineering (Basiri et al., IEEE Software, 2016).
