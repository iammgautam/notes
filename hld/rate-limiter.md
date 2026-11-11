---
title: Design a distributed rate limiter
section: hld
order: 15
summary: Token bucket and sliding window limits in Redis, atomic Lua scripts, where to place the limiter, and how to fail safely.
---

## Requirements

**Functional**
- Limit requests per key (user, API key, tenant, IP) per rule, e.g. 100 req/min per API key, 10 logins/min per IP.
- Multiple rules per request (per-user and per-tenant and global).
- Return `429 Too Many Requests` with `Retry-After` and `RateLimit-*` headers.
- Rules configurable without deploy.

**Non-functional**
- Adds < 2 ms p99 to each request.
- Shared across all gateway/app instances (distributed).
- Highly available; a limiter outage must not take down the API.
- Approximately accurate is fine (a few % over the limit is acceptable).

## Estimates

- 50K req/s peak at the gateway, ~2 rules per request → ~100K Redis ops/s. A small Redis Cluster (3–6 shards).
- 10 M active keys × ~100 B → ~1 GB. Fits in memory easily.

## API

Internal check, called by middleware:

```text
allow(key="apikey:abc:/v1/search", limit=100, window=60s, cost=1)
  -> {allowed: true, remaining: 42, reset_after_ms: 18000}
```

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 18
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 18
```

## Data model

- Rules: in Postgres/config (`key_type, route, limit, window, burst`), cached in each gateway, reloaded on change.
- Counters: Redis keys with TTL, e.g. `rl:{apikey:abc}:search`. The hash tag in braces pins related keys to one cluster slot.

## High-level design

```text
client -> LB -> API gateway / middleware --(allow?)--> Redis Cluster
                      |    ^                             (Lua scripts)
                      |    | rules cache (refresh 30s)
                      v
                  services          rules store (Postgres/config)
```

## Algorithms

| Algorithm | Idea | Memory | Burst behavior | Accuracy |
|---|---|---|---|---|
| Fixed window counter | `INCR` per window, reset each minute | 1 int | 2× limit at window edge | Low |
| Sliding window log | Sorted set of timestamps | O(limit) per key | Exact | Exact |
| Sliding window counter | Weighted current + previous window | 2 ints | Smooth | ~Approximate (good) |
| Token bucket | Tokens refill at rate r up to capacity b | 2 values | Allows bursts up to b | Exact for its model |
| Leaky bucket | Queue drains at fixed rate | Queue | Smooths output | Exact |

**Default:** token bucket (allows short bursts, smooth average) or sliding window counter (cheap, accurate enough).

### Token bucket in Lua (atomic)

Read-modify-write must be atomic. Two gateways doing `GET` then `SET` race. A Lua script runs atomically in Redis.

```lua
-- KEYS[1] = bucket key
-- ARGV: capacity, refill_per_sec, now_ms, cost
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts     = tonumber(b[2]) or now

tokens = math.min(cap, tokens + (now - ts) / 1000 * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate * 1000))
return { allowed, math.floor(tokens) }
```

- Pass `now` from the caller, or use `redis.call('TIME')` to avoid gateway clock skew.
- Load once with `SCRIPT LOAD`, call with `EVALSHA`.

### Sliding window counter

```text
count = curr_count + prev_count * (1 - elapsed_in_curr / window)
allow if count < limit; then INCR curr (with TTL = 2 * window)
```

## Deep dives

### Placement

| Where | Pros | Cons |
|---|---|---|
| Client SDK | Saves network | Untrusted, advisory only |
| Edge / CDN / WAF | Blocks floods early (IP limits) | Coarse, little user context |
| API gateway | Central, per API key/tenant | Needs shared store |
| Service middleware | Business-aware limits (per tenant plan) | Duplicated across services |
| Before expensive dependency | Protects LLM/DB quotas | Another hop |

Common answer: IP limits at the edge, API key/tenant limits at the gateway, cost-based limits (tokens, rows) in the service.

### Failure handling

- Redis down or slow: **fail open** (allow) with a short timeout (~5 ms) and a circuit breaker; alert. Fail closed only for security-sensitive limits (login attempts).
- Local fallback: per-instance in-memory bucket at limit / N instances.

### Scaling and hot keys

- Shard by key across Redis Cluster; one key's ops hit one shard.
- A single huge tenant's key gets hot: use **local pre-aggregation** (each gateway takes a batch of tokens from Redis, e.g. 10 at a time, and spends locally). Trades accuracy for fewer Redis calls.
- Multi-region: per-region limits (limit / regions) or async sync of counts. Global exact limits need cross-region round trips; usually not worth it.

## Bottlenecks and trade-offs

- Redis round trip on every request is the main latency cost. Pipelining multiple rules into one script helps.
- Accuracy vs cost: local batching and per-region limits overshoot slightly.
- Fixed window is cheapest but allows edge bursts; token bucket costs a hash per key.
- Memory: sliding log is exact but O(limit) per key; avoid for high limits.

## Likely follow-ups

- Different limits per plan tier? Rule lookup by tenant plan, cached.
- Cost-based limits (LLM tokens)? Use `cost` > 1 and debit after the response for actual usage.
- How do clients behave? Honor `Retry-After`, exponential backoff with jitter.
- Why not a DB? Too slow and write-heavy for every request; Redis is in-memory and atomic.
