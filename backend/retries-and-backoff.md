---
title: Retries and backoff
section: backend
order: 40
phase: "Phase 5 · Distributed systems"
summary: Retry only what retrying can fix, with exponential backoff and jitter, retry budgets, deadlines, hedged requests and idempotent operations.
---

Most failures in a distributed system are transient: a connection reset during a deploy, a leader election in the database, a pod killed mid-request, a brief 503 from an overloaded service. A retry turns many of those into a slower success instead of an error. Retries are also the most common way a small incident becomes a large one. Every retry is extra load sent to a system that is already failing, and retries at several layers multiply. Done well, retries are selective (only transient errors), spaced out (exponential backoff with jitter), bounded (attempt caps, budgets, deadlines) and safe (only on idempotent operations). This note covers each of those. Circuit breakers and bulkheads, which decide when to stop calling altogether, are in [Fault tolerance, circuit breakers and bulkheads]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

## What to retry

Retry when a second attempt has a real chance of a different outcome. Don't retry when the request itself is wrong.

| Outcome | Retry? | Notes |
|---|---|---|
| Connection refused or reset | Yes | Request most likely never processed |
| Connect timeout | Yes | Never reached the server |
| Read timeout | Only if idempotent | The server may have done the work |
| 502, 503, 504 | Yes, with backoff | Upstream restart, overload, gateway timeout |
| 429 Too Many Requests | Yes, after `Retry-After` | Retrying sooner just gets throttled again |
| 500 | Sometimes | Often a bug that will fail again; cap at 1 retry |
| 400, 401, 403, 404, 422 | No | Same input gives the same answer |
| 409 Conflict | Depends | Re-read and recompute, not a blind resend |
| PostgreSQL `40001`, `40P01` | Yes, the whole transaction | Serialization failure and deadlock are designed to be retried |

Retry at one layer, usually the one closest to the failing dependency, and make every other layer fail fast. Two to four total attempts is typical for synchronous calls; background jobs can retry longer because no user is waiting.

## Exponential backoff

Waiting between attempts gives the dependency time to recover. Exponential backoff doubles the wait each time, up to a cap:

```text
sleep(n) = min(cap, base * 2^n)

base = 100 ms, cap = 10 s
attempt:   1      2      3      4      5      6      7      8
sleep:   100ms  200ms  400ms  800ms  1.6s   3.2s   6.4s   10s
```

The geometric growth means a dependency that stays down sees load from retries fall off quickly. The flaw is synchronization. If a thousand clients fail at the same moment (because the dependency restarted), they all retry at +100 ms, then +300 ms, then +700 ms. Each retry wave arrives as a spike, which can knock the dependency over again as it comes back. Adding jitter fixes that.

## Jitter: full, equal and decorrelated

Jitter randomizes each sleep so retries spread out over time. Marc Brooker's 2015 AWS Architecture Blog post "Exponential Backoff and Jitter" compared three variants:

| Strategy | Sleep | Character |
|---|---|---|
| None | `min(cap, base * 2^n)` | Synchronized waves |
| Full jitter | `uniform(0, min(cap, base * 2^n))` | Widest spread, simplest |
| Equal jitter | `t/2 + uniform(0, t/2)`, `t = min(cap, base * 2^n)` | Guarantees a minimum wait |
| Decorrelated jitter | `min(cap, uniform(base, prev_sleep * 3))` | Grows from the previous sleep |

The simulation below reproduces the shape of that comparison. A hundred clients try to update the same row at once with optimistic concurrency: in each time slot one write commits and every other write in that slot conflicts and backs off.

```python
import random


def backoff(strategy, attempt, prev, rng, base=1.0, cap=1000.0):
    exp = min(cap, base * 2 ** attempt)
    if strategy == "none":
        return exp
    if strategy == "full":
        return rng.uniform(0, exp)
    if strategy == "equal":
        return exp / 2 + rng.uniform(0, exp / 2)
    if strategy == "decorrelated":
        return min(cap, rng.uniform(base, prev * 3))
    raise ValueError(strategy)


def simulate(strategy, clients=100, seed=1):
    """Every client writes one row at t=0. Per time slot, one write commits."""
    rng = random.Random(seed)
    pending = {c: (0.0, 0, 1.0) for c in range(clients)}  # next_time, attempt, last sleep
    calls = finished_at = 0
    while pending:
        slot = int(min(t for t, _, _ in pending.values()))
        contenders = [c for c, (t, _, _) in pending.items() if int(t) == slot]
        calls += len(contenders)
        winner = rng.choice(contenders)
        del pending[winner]
        finished_at = slot
        for c in contenders:
            if c != winner:
                _, attempt, prev = pending[c]
                sleep = backoff(strategy, attempt, prev, rng)
                pending[c] = (slot + 1 + sleep, attempt + 1, sleep)
    return calls, finished_at


if __name__ == "__main__":
    results = {}
    for strategy in ("none", "equal", "full", "decorrelated"):
        runs = [simulate(strategy, seed=s) for s in range(5)]
        results[strategy] = tuple(sum(r[i] for r in runs) // len(runs) for i in (0, 1))
        print(f"{strategy:>12}: {results[strategy][0]:>5} calls, done at t={results[strategy][1]}")

    assert results["none"][0] == 100 * 101 // 2   # one winner per synchronized wave
    for strategy in ("equal", "full", "decorrelated"):
        assert results[strategy][0] * 5 < results["none"][0]
        assert results[strategy][1] * 50 < results["none"][1]
```

A run prints roughly:

```text
        none:  5050 calls, done at t=90122
       equal:   746 calls, done at t=441
        full:   727 calls, done at t=324
decorrelated:   724 calls, done at t=228
```

Without jitter, every wave has all remaining clients colliding in one slot, so only one succeeds per wave: 5,050 calls and a completion time dominated by the cap. Any jitter cuts work by about 7× and time by more than 100×. Among the jittered variants the differences are small and depend on the workload; Brooker's conclusion was similar (equal jitter slightly worse, full and decorrelated close). Use full jitter by default. It is one line, has no state, and is what most libraries mean by "exponential backoff with jitter".

## Retry budgets

A per-request cap (say, 3 attempts) still allows retries to triple your load during an outage, exactly when the dependency can least afford it. A retry budget caps retries across all requests from a client: for example, retries may be at most 10% of requests over a recent window. When the dependency is healthy, the occasional transient error gets retried. When it is broadly failing, the budget runs dry and further failures surface immediately, so the extra load stays near 10% instead of 200%.

The common implementation is a token bucket. Each request deposits a fraction of a token (0.1 for a 10% budget) and each retry spends a whole token. gRPC's retry throttling works this way (`retryThrottling` with `maxTokens` and `tokenRatio` in the service config; retries stop once the bucket drops to half or less). Envoy has a `retry_budget` on clusters, and Finagle ships retry budgets by default. See [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }}) for token buckets in general.

## Retry storms

A retry storm is a feedback loop. A dependency slows down, callers time out, callers retry, the dependency receives more work while it is already slow, it slows further, more callers time out. Retries at several layers multiply:

```text
 browser --> API gateway --> orders --> inventory --> PostgreSQL
 3 tries      3 tries        3 tries     3 tries

 one failing query can be attempted 3 x 3 x 3 x 3 = 81 times,
 and each timed-out attempt may still be running on the server
```

The paper "Metastable Failures in Distributed Systems" (Bronson et al., HotOS 2021) names the worst case: the system stays failed after the original trigger is gone, because the retry load alone keeps it overloaded. Recovery then needs an intervention that removes load: shedding, disabling retries, or blocking traffic.

Defenses, in order of impact:

1. Retry at one layer only. Everything else fails fast.
2. Retry budgets per client, not just per-request caps.
3. Jittered exponential backoff, so retries spread out instead of arriving in waves.
4. Circuit breakers, so a dependency that is clearly down gets no calls at all.
5. Server-side load shedding and an explicit "don't retry" signal (a `Retry-After` header, or an error type the client library treats as final).
6. Deadlines, so work nobody is waiting for is dropped instead of computed.

## Deadlines and propagation

A per-attempt timeout bounds one try. A deadline bounds the whole operation, including all retries and all downstream calls. Without a deadline, three attempts with a 2 s timeout and backoff can take more than 7 s while the user's browser gave up after 5.

Propagate the deadline. The edge sets it (say, 3 s from arrival), each hop subtracts what it has used, and each outbound call gets a timeout no longer than what remains. Pass the remaining duration, not an absolute timestamp, across machines, because clocks are skewed. gRPC does this natively: the client's deadline travels as the `grpc-timeout` header, and servers can check the remaining time and stop. Over HTTP you add your own header, such as `X-Request-Timeout-Ms`, and enforce it in middleware.

Two rules follow. Don't start an attempt that cannot finish before the deadline. And when a server receives a request whose deadline has already passed (it sat in a queue), drop it instead of doing the work; a result delivered to nobody is pure waste during an overload.

## Hedged requests

Retries handle failure. Hedging handles slowness. Instead of waiting for a timeout, send a second copy of a slow request to another replica after a short delay, use whichever answer arrives first, and cancel the other. Dean and Barroso's "The Tail at Scale" (CACM 2013) suggests hedging after the p95 latency, which limits the extra load to about 5% while cutting the tail sharply; they report a Bigtable benchmark where a 10 ms hedge delay brought the 99.9th percentile from about 1.8 s to 74 ms with about 2% more requests.

Hedge only idempotent reads, against replicas that fail independently, and only if you can cancel the loser. Hedging writes or hedging during a fleet-wide slowdown doubles load for no gain, so give hedges their own budget too.

```python
import asyncio
import time


async def hedged(call_replica, hedge_after):
    """Call replica 0; if it hasn't answered after hedge_after seconds, also call
    replica 1. Return the first answer and cancel the other."""
    first = asyncio.ensure_future(call_replica(0))
    done, _ = await asyncio.wait({first}, timeout=hedge_after)
    if done:
        return first.result()
    second = asyncio.ensure_future(call_replica(1))
    done, pending = await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return done.pop().result()   # production code also waits for the other if this one failed


if __name__ == "__main__":
    async def replica(i):        # replica 0 is in a long GC pause
        await asyncio.sleep(1.0 if i == 0 else 0.01)
        return f"replica-{i}"

    start = time.monotonic()
    assert asyncio.run(hedged(replica, hedge_after=0.05)) == "replica-1"
    assert time.monotonic() - start < 0.5
```

## Idempotency requirement

A retry is only safe if doing the operation twice has the same effect as doing it once. The ambiguous case is the read timeout: the request reached the server, maybe it charged the card, and the response was lost. A blind retry charges twice.

- HTTP `GET`, `HEAD`, `PUT` and `DELETE` are idempotent by definition (RFC 9110). `POST` and `PATCH` are not, unless you make them so.
- Make writes idempotent with a client-generated idempotency key stored with the result, so a repeat returns the stored response instead of re-executing. Stripe's `Idempotency-Key` header is the well-known public example. See [Idempotency]({{ '/backend/idempotency/' | relative_url }}).
- Queue consumers get the same problem from at-least-once delivery; see [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}).
- Natural idempotency helps: "set status to shipped" can be repeated, "add 1 to balance" cannot. Upserts keyed on a business ID beat inserts.

## In practice

A runnable retry helper with full jitter, `Retry-After`, a total deadline that shrinks each attempt's timeout, and a shared retry budget:

```python
import math
import random
import time


class TransientHTTPError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(status)
        self.status, self.retry_after = status, retry_after


RETRYABLE = (ConnectionError, TimeoutError, TransientHTTPError)


class RetryBudget:
    """Retries may be at most `ratio` of requests, plus a small burst."""

    def __init__(self, ratio=0.1, max_tokens=5.0):
        self.ratio, self.max_tokens, self.tokens = ratio, max_tokens, max_tokens

    def on_request(self):
        self.tokens = min(self.max_tokens, self.tokens + self.ratio)

    def try_spend(self):
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def call_with_retries(fn, *, deadline, per_try=2.0, max_attempts=4, base=0.1, cap=5.0,
                      budget=None, sleep=time.sleep, clock=time.monotonic, rand=random.random):
    """fn(timeout=...) performs one attempt. deadline is an absolute clock() value."""
    if budget:
        budget.on_request()
    for attempt in range(max_attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("deadline exceeded")
        try:
            return fn(timeout=min(per_try, remaining))
        except RETRYABLE as exc:
            if attempt == max_attempts - 1:
                raise
            delay = rand() * min(cap, base * 2 ** attempt)           # full jitter
            if getattr(exc, "retry_after", None):
                delay = max(delay, exc.retry_after)
            if clock() + delay >= deadline:
                raise                                                 # no time for another try
            if budget and not budget.try_spend():
                raise                                                 # widespread failure: stop
            sleep(delay)


if __name__ == "__main__":
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    opts = dict(sleep=sleep, clock=clock)

    tries = []
    def flaky(timeout):                   # fails twice, then works
        tries.append(timeout)
        if len(tries) < 3:
            raise ConnectionError("reset by peer")
        return "ok"
    assert call_with_retries(flaky, deadline=10, rand=lambda: 1.0, **opts) == "ok"
    assert len(tries) == 3 and math.isclose(now[0], 0.1 + 0.2)

    calls = []
    def invalid(timeout):                 # 4xx-style errors are not retried
        calls.append(1)
        raise ValueError("422 unprocessable")
    try:
        call_with_retries(invalid, deadline=10, **opts)
        raise AssertionError("expected ValueError")
    except ValueError:
        assert calls == [1]

    now[0] = 0.0
    replies = iter([TransientHTTPError(429, retry_after=2.0)])
    def throttled(timeout):
        err = next(replies, None)
        if err:
            raise err
        return "ok"
    assert call_with_retries(throttled, deadline=10, **opts) == "ok" and now[0] >= 2.0

    now[0] = 0.0
    def hangs(timeout):                   # every attempt times out
        now[0] += timeout
        raise TimeoutError
    try:
        call_with_retries(hangs, deadline=3.0, per_try=1.0, rand=lambda: 1.0, **opts)
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        assert now[0] <= 3.0 + 1e-9       # never runs past the deadline

    budget, attempts = RetryBudget(ratio=0.1, max_tokens=5), [0]
    def down(timeout):
        attempts[0] += 1
        raise ConnectionError("refused")
    for _ in range(100):
        try:
            call_with_retries(down, deadline=now[0] + 60, budget=budget, **opts)
        except ConnectionError:
            pass
    assert 100 < attempts[0] < 120        # without a budget: 400 attempts
    print("retry helper ok, attempts during outage:", attempts[0])
```

Library equivalents (illustrative; check versions). `requests` with urllib3's `Retry`, which by default already refuses to retry `POST`:

```python
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry = Retry(
    total=3,
    status_forcelist=(429, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD", "PUT", "DELETE"}),
    backoff_factor=0.2,              # exponential: grows as backoff_factor * 2**n
    backoff_jitter=0.1,              # urllib3 2.x
    respect_retry_after_header=True,
)
session = Session()
session.mount("https://", HTTPAdapter(max_retries=retry))
session.get("https://inventory.internal/v1/skus/123", timeout=(0.3, 2.0))
```

Tenacity for arbitrary functions, Celery for background jobs:

```python
from celery import shared_task
from requests import ConnectionError as ReqConnectionError, Timeout
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      stop_after_delay, wait_random_exponential)


@retry(
    retry=retry_if_exception_type((ReqConnectionError, Timeout)),
    wait=wait_random_exponential(multiplier=0.1, max=5),     # full jitter
    stop=stop_after_attempt(4) | stop_after_delay(10),
    reraise=True,
)
def fetch_fx_rates(currency): ...


@shared_task(
    autoretry_for=(ReqConnectionError, Timeout),
    retry_backoff=2,            # 2, 4, 8, ... seconds
    retry_backoff_max=600,
    retry_jitter=True,          # uniform between 0 and the backoff value
    max_retries=6,
    acks_late=True,
)
def sync_invoice_to_erp(invoice_id): ...
```

Retrying a PostgreSQL transaction after a serialization failure or deadlock. Retry the whole transaction, never a single statement, and only from the outermost `atomic` block (illustrative):

```python
import random
import time

from django.db import OperationalError, transaction

RETRYABLE_SQLSTATES = {"40001", "40P01"}   # serialization_failure, deadlock_detected


def run_in_transaction(fn, attempts=5):
    for attempt in range(attempts):
        try:
            with transaction.atomic():
                return fn()
        except OperationalError as exc:
            cause = exc.__cause__        # psycopg 3: .sqlstate, psycopg2: .pgcode
            code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            if code not in RETRYABLE_SQLSTATES or attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0, 0.02 * 2 ** attempt))
```

## Pitfalls

- **Retrying non-idempotent writes.** Symptom: duplicate orders or charges after a network blip. Fix: idempotency keys, or don't retry.
- **Retries at every layer.** Symptom: during an incident, downstream request rate jumps several-fold while user traffic is flat. Fix: one retrying layer, budgets everywhere.
- **No jitter.** Symptom: periodic load spikes at the dependency every few hundred milliseconds after a restart. Fix: full jitter.
- **Ignoring `Retry-After`.** You retry into the throttle and may get banned or deprioritized. Honor it, capped by your deadline.
- **Retries without a deadline.** Total latency becomes attempts × timeout + backoff, far beyond what the caller waits. Fix: a total deadline that shrinks each attempt's timeout.
- **Retrying inside an open breaker.** Make "circuit open" a non-retryable error.
- **Retrying deterministic failures.** A 500 caused by a bug fails identically every time. Cap 500 retries at one, and alert on the rate.

## Interview questions

**Which errors do you retry?** Transient ones: connection failures, connect timeouts, 502/503/504, 429 after `Retry-After`, database serialization failures and deadlocks. Read timeouts only for idempotent operations. Never 4xx validation or auth errors.

**Why exponential backoff, and why jitter?** Backoff reduces load on a struggling dependency geometrically. Jitter breaks synchronization: clients that failed together would otherwise retry together in spikes.

**Full versus equal versus decorrelated jitter?** Full picks uniformly in `[0, backoff]`; equal keeps half the backoff fixed and randomizes the other half; decorrelated draws from `[base, 3 × previous sleep]`. Full and decorrelated perform about the same and better than equal; full is the default because it is stateless.

**What is a retry budget?** A cap on retries as a fraction of total requests per client (for example 10%), usually a token bucket. It keeps retry load bounded during broad failures, which per-request attempt caps do not.

**How do retries cause outages?** Retry storms: retries add load to an overloaded system, layered retries multiply it, and the system can stay overloaded after the trigger is gone (metastable failure). Fixes: single-layer retries, budgets, jitter, breakers, load shedding, deadlines.

**What is deadline propagation?** Passing the remaining time budget to each downstream call, so each hop sets timeouts from it and drops work whose caller has given up. gRPC carries it in `grpc-timeout`.

**When would you use hedged requests?** For latency-sensitive idempotent reads with a long tail, against independent replicas. Send a second request after about the p95 latency, take the first answer, cancel the other, and budget the extra load.

**How do you make a POST safe to retry?** Client-generated idempotency key; the server stores the key with the result in the same transaction as the write and returns the stored result on repeats.

## Further reading

- "Exponential Backoff and Jitter" (Marc Brooker, AWS Architecture Blog, 2015).
- AWS Builders' Library: "Timeouts, retries, and backoff with jitter".
- "The Tail at Scale" (Jeffrey Dean and Luiz André Barroso, Communications of the ACM, 2013).
- "Metastable Failures in Distributed Systems" (Bronson, Aghayev, Charapko, Zhu; HotOS 2021).
- *Site Reliability Engineering* (Beyer et al.), chapter "Addressing Cascading Failures".
