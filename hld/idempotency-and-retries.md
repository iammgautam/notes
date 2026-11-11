---
title: Idempotency, retries and timeouts
section: hld
order: 11
summary: Make writes safe to retry with idempotency keys, retry with backoff and jitter, and stop cascades with timeouts and circuit breakers.
---

Networks fail in the worst way: the request may have succeeded and only the response was lost. Retries are mandatory; idempotency makes them safe.

## Idempotency

An operation is idempotent if doing it twice has the same effect as once.

| Naturally idempotent | Not idempotent |
|---|---|
| `GET`, `PUT` (full replace), `DELETE` | `POST /payments`, `counter += 1` |
| `UPDATE ... SET status = 'paid'` | `INSERT` without a unique key |
| Upsert on a natural key | Sending an email |

### Idempotency keys

Client generates a unique key (UUID) per logical operation and sends it on every retry.

```http
POST /v1/payments
Idempotency-Key: 6f1c2a8e-...
```

```sql
CREATE TABLE idempotency_keys (
  tenant_id   bigint,
  key         text,
  request_hash text,
  status      text,          -- in_progress | done
  response    jsonb,
  created_at  timestamptz DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);
```

Server flow:
1. `INSERT ... ON CONFLICT DO NOTHING`. If inserted, you own it; process.
2. If it exists and `done`: return the stored response.
3. If it exists and `in_progress`: return `409` (or wait).
4. If the request hash differs: return `422` (key reused with a different body).
5. Store the response in the **same transaction** as the side effect where possible.
6. Expire keys after 24 h–7 d.

For external side effects (charging a card), pass your key downstream; Stripe and most payment APIs accept one.

## Retries

Retry only **transient** errors: timeouts, connection resets, `429`, `502/503/504`. Never retry `400/401/403/404/422`.

### Exponential backoff with jitter

```python
import random, time

def retry(fn, attempts=5, base=0.1, cap=10.0):
    for i in range(attempts):
        try:
            return fn()
        except TransientError:
            if i == attempts - 1:
                raise
            # full jitter: sleep uniform(0, min(cap, base * 2^i))
            time.sleep(random.uniform(0, min(cap, base * 2 ** i)))
```

- Without jitter, clients that failed together retry together (synchronized waves).
- Respect `Retry-After` headers.
- **Retry budget:** cap retries to ~10% of traffic so a degraded dependency is not hit with 3–5× load.
- Retry at **one** layer. Retries at client, gateway and service multiply (3 × 3 × 3 = 27 attempts).

## Timeouts

- Every network call needs a timeout: connect timeout (short, ~1 s) and read timeout (based on p99.9 of the dependency).
- **Deadline propagation:** pass the remaining budget downstream (gRPC deadlines). If the caller gave up, stop working.
- Timeout ≠ failure. The operation may have completed, which is why the retry must be idempotent.
- Postgres: `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`.

## Circuit breaker

Stop calling a dependency that is failing, fail fast, and probe for recovery.

```text
        failures > threshold
 CLOSED ---------------------> OPEN
   ^                             | after cooldown
   | probe ok                    v
   +-------------------------- HALF-OPEN
           probe fails -> OPEN
```

- **Closed:** normal; count failures in a rolling window.
- **Open:** reject immediately (or serve a fallback) for a cooldown.
- **Half-open:** let a few requests through; success closes, failure reopens.
- Fallbacks: cached data, default value, degraded feature, queue for later.

## Related patterns

| Pattern | Purpose |
|---|---|
| Bulkhead | Separate pools per dependency so one slow dependency cannot exhaust all threads/connections |
| Load shedding | Reject excess requests early (`503`) instead of queueing to death |
| Hedged requests | Send a duplicate after p95 latency, use the first reply (reads only) |
| Rate limiting | Protect yourself from callers |

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Aggressive retries | Hide transient blips | Amplify outages |
| Short timeouts | Fast failure, free resources | False failures on slow-but-OK calls |
| Circuit breaker | Protect dependency and caller | Tuning thresholds; may trip on brief blips |
| Idempotency table | Safe retries | Extra write per request, storage, cleanup |

## Gotchas

- Celery/SQS/Kafka consumers are at-least-once. The task must be idempotent regardless of HTTP-level keys.
- Idempotency key scope must include tenant/user, or one user's key can collide with another's.
- Retrying inside a DB transaction that already failed does nothing useful; retry the whole transaction (serialization failures `40001`).
- Do not hold DB transactions open across external HTTP calls.

> **Interview tip:** For any payment, order or "create" endpoint, say "client sends an idempotency key, server stores key + response in the same transaction, retries return the stored response".
