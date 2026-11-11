---
title: System design framework
section: hld
order: 1
summary: A 45-minute plan for any system design round, what to say in each phase, and what interviewers actually score.
---

A system design round is a structured conversation, not a quiz. Drive it. The interviewer scores your process as much as the final diagram.

## The 45-minute plan

| Phase | Time | Output |
|---|---|---|
| 1. Requirements | 5 min | Functional list, non-functional targets, out of scope |
| 2. Estimates | 3–5 min | QPS (avg and peak), storage/yr, bandwidth, read:write ratio |
| 3. API | 3–5 min | 3–6 endpoints with request/response shape |
| 4. Data model | 5 min | Entities, keys, access patterns, choice of store |
| 5. High-level design | 10 min | Box diagram, one request traced end to end |
| 6. Deep dives | 10–15 min | 2–3 hard parts in depth |
| 7. Trade-offs and wrap-up | 3–5 min | Bottlenecks, failure modes, what you would do next |

## 1. Requirements

- **Functional:** what the user can do. Pick the 3–4 core flows; say what is out of scope.
- **Non-functional:** scale (DAU, data size), latency targets (p99), availability (99.9 vs 99.99), consistency needs, durability, geo.
- Ask: "Read-heavy or write-heavy?", "Is eventual consistency acceptable for X?", "Multi-region?"

> **Interview tip:** Write requirements on the board. Every later decision should point back to one of them.

## 2. Estimates

- DAU × actions/day ÷ 86,400 ≈ average QPS. Peak ≈ 2–5× average.
- Storage = writes/day × object size × retention.
- Round aggressively. The goal is the order of magnitude: does it fit on one Postgres box or not?
- See [Back-of-the-envelope estimation]({{ '/hld/estimation/' | relative_url }}).

## 3. API

- REST or gRPC; name resources, verbs, pagination (cursor), idempotency keys on writes.
- Mention auth (token in header) and rate limits once, not per endpoint.

```http
POST /v1/urls            {"long_url": "...", "alias": "opt"}  -> 201 {"code": "aZ3k9"}
GET  /v1/urls/{code}     -> 302 Location: <long_url>
```

## 4. Data model

- List entities and their primary access patterns first; schema follows the queries.
- Pick the store from access patterns: relational by default, KV/wide-column for huge simple-key workloads.
- Call out indexes, partition/shard key, and what is denormalized.

## 5. High-level design

- Start simple: client → LB → stateless app → DB + cache. Add components only when a requirement forces it.
- Trace one write and one read through the diagram.

```text
client -> LB -> app (stateless) -> cache -> DB (primary + replicas)
                     |
                     +-> queue -> workers -> object store / search
```

## 6. Deep dives

Pick the parts that are actually hard for *this* problem. Common ones:

- Scaling the hottest path (cache, replicas, sharding).
- Consistency of a critical write (transactions, idempotency, outbox).
- Hot keys / celebrity users / skew.
- Failure handling: retries, timeouts, DLQ, degraded mode.

## 7. Trade-offs and wrap-up

- Name the bottleneck at 10× scale and how you would remove it.
- State what you traded: "eventual consistency on counts for write throughput".
- Mention observability: metrics (QPS, p99, error rate, queue lag), alerts, tracing.

## What interviewers score

| Signal | Looks like |
|---|---|
| Problem scoping | Asks clarifying questions, sets explicit targets |
| Structured approach | Moves through phases, manages time |
| Technical depth | Correct mechanics in deep dives (not buzzwords) |
| Trade-off reasoning | Says "X because Y, at the cost of Z" |
| Scale awareness | Numbers drive decisions |
| Communication | Thinks aloud, checks in, adapts to hints |

## Pitfalls

- Jumping to Kafka/microservices before requirements justify them.
- Spending 15 minutes on estimates or on the API.
- Designing every box shallowly instead of 2–3 boxes deeply.
- Ignoring failure modes and operations.
- Not listening when the interviewer steers toward a deep dive.

> **Rule of thumb:** Simple first, then scale the bottleneck the numbers point to. A single Postgres primary handles far more than most candidates assume.
