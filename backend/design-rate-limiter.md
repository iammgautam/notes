---
title: Design a rate limiter
section: backend
order: 83
phase: "Phase 10 · System design case studies"
summary: "A distributed rate limiting service: algorithm choice, atomic Redis Lua scripts, sliding window counters, local plus global limits, failure policy and headers."
---

A rate limiter protects services from overload and abuse, enforces plan quotas, and keeps one noisy tenant from starving the rest. Writing a token bucket is the easy half; this case study is about the other half: making the decision consistent across hundreds of gateway nodes and several regions, in about a millisecond, without becoming the outage itself. The algorithms are covered in [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }}) and the single-process class design in [the LLD rate limiter]({{ '/lld/rate-limiter/' | relative_url }}).

## Requirements

### Clarifying questions

- Where does it run: in the API gateway, as a sidecar, or as a library in each service? Assume gateway-enforced, with a shared rate limit service behind it.
- What is the key: API key, user, tenant, IP, endpoint, or a combination? All of these, per rule.
- Hard limits (reject) or soft (log and alert)? Both, plus shadow mode for new rules.
- How accurate must global limits be? Within a few percent is fine for quotas; login attempts need to be strict.
- Single region or multi-region? Three regions, active-active.

### Functional

- Evaluate every request against all matching rules; reject with 429 if any enforced rule is exceeded.
- Rules keyed by descriptors (api_key, tenant, route, ip) with limit, window, algorithm and scope (local or global).
- Per-tenant overrides from plan tiers; changes without redeploys; shadow mode.
- Standard response headers so clients can back off.

### Non-functional

- 500k requests/s peak across regions; 10M distinct active subjects per day.
- Added latency: under 0.1 ms for local rules, p99 under 2 ms for global rules in-region.
- The limiter must be more available than what it protects: its failure must not take down the API.
- Global limit accuracy within about 5%; bounded memory.

### Out of scope

DDoS mitigation at the network layer (handled by the edge/CDN), billing, and adaptive load shedding based on server health (related but separate: see [Backpressure]({{ '/backend/backpressure/' | relative_url }})).

## Capacity estimates

```text
Decisions   500k req/s x ~2 global rules     = 1M counter checks/s
Redis       a short Lua script runs on the order of 50-100k/s per shard
            1M / ~60k ≈ 17 primaries → 24 primaries + replicas (headroom)
Memory      sliding window counter: one small hash per (rule, subject)
            ≈ 150 B with key overhead
            10M subjects x 2 rules x 150 B ≈ 3 GB (fits easily)
vs log      sliding log at 1,000 req/hour: up to 1,000 sorted-set entries
            x ~60 B = 60 KB per subject x 10M ≈ 600 GB (why not log)
Network     1M calls/s x ~200 B ≈ 200 MB/s ≈ 1.6 Gbps across the cluster
Rules       100k rules incl. overrides x 200 B ≈ 20 MB per gateway node
```

The counter store is small; the cost is operations per second. Every design choice below tries to avoid a network round trip per request.

## API design

The gateway calls the rate limit service once per request with all descriptors (a gRPC API in practice; shown as JSON). This mirrors the descriptor model of Envoy's global rate limit service.

```json
POST /v1/ratelimit:check
{"domain": "public_api", "hits": 1,
 "descriptors": [
   {"api_key": "k_123", "route": "POST /v1/search"},
   {"tenant": "t_9"}
 ]}
```

```json
{"decision": "OVER_LIMIT",
 "statuses": [
   {"rule": "search_per_key", "limit": 100, "window_s": 60,
    "remaining": 0, "reset_ms": 12000},
   {"rule": "tenant_daily", "limit": 1000000, "window_s": 86400,
    "remaining": 40120, "reset_ms": 3600000}
 ]}
```

The client sees:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1763287212
Content-Type: application/json

{"error": "rate_limited", "rule": "search_per_key", "retry_after_s": 12}
```

Admin API: `PUT /v1/rules/{rule_id}`, `GET /v1/rules?version=`, `POST /v1/rules/{rule_id}:shadow`, `GET /v1/subjects/{key}/usage` for support tooling.

## Data model

Counters live in Redis Cluster: in memory, sub-millisecond, atomic server-side scripts, TTLs, and hash-slot sharding. A relational database cannot absorb a write per request; Memcached has atomic increments but no scripting, which makes the multi-step algorithms racy.

| Data | Store | Key / shape |
|---|---|---|
| Counters | Redis Cluster, per region | `rl:<rule_id>:<subject>` hash with `win`, `curr`, `prev`; TTL 2 windows |
| Rule definitions | Git (YAML), reviewed and CI-validated | Compiled into a versioned snapshot |
| Tenant overrides | PostgreSQL (from billing / plans) | `(tenant_id, rule_id) → limit, window` |
| Config snapshots | Config service / object storage | `version → compiled rules`, pushed to gateways |
| Decision samples | Kafka, then a columnar store | Rejections plus 1% of allows |

```yaml
# Illustrative rule config
domain: public_api
rules:
  - id: search_per_key
    match: {route: "POST /v1/search"}
    key: api_key
    algorithm: sliding_window_counter
    limit: 100
    window: 60s
    scope: global
    mode: enforce            # or shadow
  - id: per_ip_burst
    key: client_ip
    algorithm: token_bucket
    rate: 20/s
    burst: 40
    scope: local             # per gateway node, no network call
  - id: login_attempts
    match: {route: "POST /v1/login"}
    key: username
    limit: 10
    window: 15m
    scope: global
    on_store_error: deny     # fail closed
overrides:
  - tenant: t_enterprise_42
    rule: search_per_key
    limit: 2000
```

## High-level design

```text
 clients
    │
    ▼
 edge / load balancer
    │
    ▼
 API gateway node ─────────────────────────────────► upstream services
  ├─ rules snapshot (memory) ◄── config service ◄── Git + overrides DB
  ├─ local limiter: token buckets, negative cache
  └─ global check (1 RPC) ──► rate limit service ──► Redis Cluster
                                   │                  (this region)
                                   │                       │
            metrics, sampled ◄─────┘          async count sync with
            decisions → Kafka                 other regions
```

Request flow:

1. The gateway matches the request against the in-memory rules snapshot (a hash lookup on descriptor keys, not a scan).
2. Local rules (per-IP bursts, per-node protection) run in-process. A rejection here never touches the network.
3. If the negative cache says this subject is over a global limit until time T, reject immediately.
4. Otherwise one RPC to the rate limit service evaluates all global rules. The service pipelines one Lua call per rule to Redis.
5. Allow, or 429 with headers. Shadow rules only emit a metric and a sampled log line.

A separate service rather than a library in each gateway keeps Redis connection counts sane (a thousand gateway processes with their own pools would exhaust Redis client limits), lets you upgrade the algorithm centrally, and serves any language. The cost is one extra in-region hop. Lyft's open-source `ratelimit` service, used with Envoy, is a well-known example of this shape.

## Deep dives

### Algorithm choice

| Algorithm | State per key | Bursts | Accuracy | Use for |
|---|---|---|---|---|
| Token bucket | tokens, last refill | Allows up to bucket size | Exact | Local burst control, per-node protection |
| Leaky bucket (queue) | queue | Smooths to constant rate | Exact | Outbound calls to a fragile dependency |
| Fixed window | one counter | Up to 2× at window boundary | Coarse | Daily or monthly quotas aligned to the calendar |
| Sliding log | every timestamp | None | Exact | Low-volume, strict limits (login, OTP) |
| Sliding window counter | two counters | Small | Approximate | Global per-key limits at scale |

The sliding window counter estimates the rolling count as `prev × (time left in current window / window) + curr`. It assumes the previous window's requests were spread evenly, which is close enough in practice; Cloudflare has publicly described using this approach and measuring a very small error rate on real traffic. It needs two integers per key instead of one entry per request.

### Redis and Lua atomicity

A read-check-write sequence from the client is a race: two gateways read 99, both write 100, and 101 requests get through. `INCR` followed by `EXPIRE` has a different bug: a crash between the two leaves a counter with no TTL that never resets. A Lua script runs atomically on the Redis shard (no other command interleaves), so the read, decision and write happen as one step.

```lua
-- KEYS[1] = rl:<rule>:<subject>
-- ARGV[1] = limit, ARGV[2] = window_ms, ARGV[3] = cost
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost   = tonumber(ARGV[3])
local t      = redis.call('TIME')
local now    = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local win    = math.floor(now / window)
local h      = redis.call('HMGET', KEYS[1], 'win', 'curr', 'prev')
local w      = tonumber(h[1])
local curr   = tonumber(h[2]) or 0
local prev   = tonumber(h[3]) or 0
if w == nil or w < win - 1 then
  curr, prev = 0, 0
elseif w == win - 1 then
  curr, prev = 0, curr
end
local into = now - win * window
local est  = prev * (1 - into / window) + curr
if est + cost > limit then
  return {0, math.max(0, math.floor(limit - est)), window - into}
end
redis.call('HSET', KEYS[1], 'win', win, 'curr', curr + cost, 'prev', prev)
redis.call('PEXPIRE', KEYS[1], window * 2)
return {1, math.floor(limit - est - cost), window - into}
```

Design points:

- **One key per rule and subject.** In Redis Cluster every key a script touches must live in the same hash slot. A single hash key sidesteps cross-slot errors. If a request must consume from several rules all-or-nothing, put the subject in a hash tag (`rl:{k_123}:search`, `rl:{k_123}:daily`) so all its keys share a slot and one script checks every rule before incrementing any.
- **Server time.** Calling `TIME` inside the script (allowed with effects replication, the default since Redis 5) removes clock skew between gateway nodes from the decision.
- **Keep scripts tiny.** A script blocks its shard while it runs. No loops over large structures.
- **Load once, call by hash.** `EVALSHA` avoids resending the script; clients fall back to `EVAL` on `NOSCRIPT`.

```python
# Illustrative: redis-py (use redis.cluster.RedisCluster against a cluster)
import redis

r = redis.Redis(host="ratelimit-redis", port=6379, socket_timeout=0.005)
check = r.register_script(SLIDING_WINDOW_LUA)    # handles EVALSHA / NOSCRIPT

def allow(rule_id, subject, limit, window_ms, cost=1):
    ok, remaining, reset_ms = check(keys=[f"rl:{rule_id}:{subject}"],
                                    args=[limit, window_ms, cost])
    return bool(ok), remaining, reset_ms
```

### Sliding window counter, runnable

The same algorithm in plain Python with an injectable clock, compared against a fixed window at a boundary:

```python
import math


class SlidingWindowCounter:
    """Mirrors the Lua script: one (win, curr, prev) record per subject."""

    def __init__(self, limit, window_ms, clock):
        self.limit, self.window, self.clock = limit, window_ms, clock
        self.store = {}

    def allow(self, subject, cost=1):
        now = self.clock()
        win = now // self.window
        w, curr, prev = self.store.get(subject, (None, 0, 0))
        if w is None or w < win - 1:
            curr, prev = 0, 0
        elif w == win - 1:
            curr, prev = 0, curr
        into = now - win * self.window
        est = prev * (1 - into / self.window) + curr
        if est + cost > self.limit:
            return False, max(0, math.floor(self.limit - est))
        self.store[subject] = (win, curr + cost, prev)
        return True, math.floor(self.limit - est - cost)


class FixedWindow:
    def __init__(self, limit, window_ms, clock):
        self.limit, self.window, self.clock = limit, window_ms, clock
        self.counts = {}

    def allow(self, subject):
        key = (subject, self.clock() // self.window)
        if self.counts.get(key, 0) >= self.limit:
            return False
        self.counts[key] = self.counts.get(key, 0) + 1
        return True


if __name__ == "__main__":
    now = [0]
    clock = lambda: now[0]
    swc = SlidingWindowCounter(limit=10, window_ms=60_000, clock=clock)
    fw = FixedWindow(limit=10, window_ms=60_000, clock=clock)

    now[0] = 59_000                    # burst at the end of window 0
    assert all(swc.allow("k")[0] for _ in range(10))
    assert all(fw.allow("k") for _ in range(10))
    assert not swc.allow("k")[0] and not fw.allow("k")

    now[0] = 60_500                    # just after the boundary
    assert sum(fw.allow("k") for _ in range(10)) == 10        # 20 in 1.5 s
    assert sum(swc.allow("k")[0] for _ in range(10)) == 0     # prev weighs 99%

    now[0] = 90_000                    # halfway: prev weighs 0.5 -> est 5
    assert sum(swc.allow("k")[0] for _ in range(10)) == 5

    now[0] = 200_000                   # two windows later: fully reset
    assert sum(swc.allow("k")[0] for _ in range(12)) == 10
    assert swc.allow("other", cost=4) == (True, 6)            # weighted cost
    assert swc.allow("other", cost=7) == (False, 6)
    print("sliding window ok")
```

### Multi-region, local and global limits

A user's requests can land in any region. A single central Redis gives exact counts but adds 50–150 ms cross-region round trips to every request, which is unacceptable inline.

| Approach | Accuracy | Latency | Notes |
|---|---|---|---|
| Split the limit per region (limit / R) | Poor when traffic is skewed | Local | Simplest; a user pinned to one region gets a third of their quota |
| Central counter store | Exact | Cross-region RTT | Only for low-QPS strict limits |
| Local counts + async sync | Overshoot bounded by sync interval | Local | Each region adds others' last-known counts; sync every ~1 s |
| Token leasing | No overshoot, some under-use | Local most of the time | Nodes or regions lease batches of tokens from a global pool |

Async sync: each region counts locally and periodically publishes per-subject deltas (only for subjects near a limit, to keep the stream small). Worst case, every region spends the same remaining budget before hearing from the others, so overshoot is bounded by roughly the traffic admitted in one sync interval times (R − 1). That is acceptable for quotas and unacceptable for login attempts, which stay home-region and strict.

The same idea works one level down. A two-tier design keeps a **local** limiter in every gateway (token buckets per IP and per key, sized generously) that absorbs abusive bursts without a network call, and a **global** limiter for the real quota. A **negative cache** completes it: when Redis says a subject is over limit until T, the gateway rejects it locally until min(T, 1 s). A client hammering at 50k requests per second then costs Redis a handful of calls per second instead of 50k, which also defuses hot keys.

### Rules configuration

- **Matching:** every matching rule applies; the request must pass all of them. For the same rule, the most specific override wins (tenant override, then plan tier, then default).
- **Compilation:** rules compile into a map from descriptor keys to rule lists, so matching is O(rules for this route), not O(all rules).
- **Distribution:** the config service pushes versioned snapshots; gateways swap the pointer atomically, report the loaded version as a metric, and keep the last known good snapshot if the config service is down.
- **Safety:** CI validates syntax and sanity (no limit of 0 on a broad route without an explicit flag); new rules ship in shadow mode first; rollout is staged by region.
- **Cost-based limits:** a rule can charge a weight per request (search costs 5, a GET costs 1, an LLM call costs its token count). The `cost` argument in the script exists for this.

### Fail-open versus fail-closed

When Redis is slow or down, the limiter must decide without it. Set a tight timeout (a few milliseconds) and a circuit breaker around the store.

| Rule type | On store failure | Why |
|---|---|---|
| General API quotas | Fail open, fall back to local buckets at limit / node count | An outage of the limiter must not become an API outage |
| Login, OTP, password reset | Fail closed | Unlimited attempts is a security hole |
| Expensive or paid calls (SMS, LLM tokens) | Fail closed or tight local cap | Unbounded cost |
| Internal service-to-service | Fail open | Overload protection lives in load shedding |

The local fallback keeps some protection while open: each node allows `limit / N` where N is the current node count from service discovery. Count every fail-open decision; an unnoticed fail-open period is a silent loss of protection.

### Headers and the client contract

- Use **429** for rate limits and **503** for server overload; clients treat them differently.
- Always send **Retry-After** on 429 (seconds or an HTTP date). Well-behaved clients wait that long, plus jitter.
- `X-RateLimit-Limit`, `-Remaining` and `-Reset` are the de facto convention. The IETF HTTPAPI working group has a draft defining standard `RateLimit` and `RateLimit-Policy` fields; check its current status before adopting the syntax.
- Name the rule in the error body so support can explain the rejection.
- Decide whether rejected requests count. Usually they do not, so a client that backs off recovers on schedule; for abuse rules, counting them punishes retry storms.

### Observability

- Counter `ratelimit_decisions_total{rule, decision, mode, region}` where decision is allowed, limited, or shadow_limited. Never put the API key or IP in a label; that explodes metric cardinality.
- Histogram of decision latency, and counters for store errors, timeouts and fail-open decisions.
- Gauge of loaded config version per node, to catch nodes stuck on an old snapshot.
- Top offenders from sampled decision logs (or a heavy-hitters sketch), not from metrics.
- Alerts: fail-open above zero for five minutes; 429 rate for a single tenant jumping (customer impact, often a broken client or a bad override); Redis CPU above 70%.

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× (5M req/s) | Redis ops/s; hot subjects pinning one shard | More shards; negative cache; token leasing (a node takes 50 tokens per call, cutting calls 50×) |
| 10× rules | Matching cost, config size | Compiled maps, per-domain snapshots |
| 100× | Any per-request remote call | Gateways count in memory and gossip counts; Redis only for strict rules; regional cells |

Hot keys deserve a specific answer: one subject at 50k requests per second lands on one Redis shard. Local buckets and the negative cache absorb it; for legitimately huge tenants, split the key into K sub-counters (`rl:search:t_9:0..7`) each enforcing limit / K, chosen at random per request.

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Redis shard down | Global rules for its keys unavailable | Fail open to local buckets; replica promotion |
| Failover loses recent writes | Counters briefly reset low, short over-admit | Accept for quotas; strict rules on a separate store with AOF |
| Bad config (limit 0) | Mass 429s | Shadow first, staged rollout, last-known-good, instant rollback |
| Config service down | No updates | Gateways keep the current snapshot |
| Slow Redis | Added latency on every request | 5 ms timeout, breaker, negative cache |
| Clock skew between nodes | Window misalignment | Server-side `TIME` in the script |
| Retry storm after mass 429 | Load spike when windows reset | Retry-After with jitter; count rejected requests for abuse rules |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| Sliding window counter for global rules | Yes | Two integers per key, no boundary burst, small error |
| Sliding log | Only for strict low-volume rules | Exact but memory grows with the limit |
| Central service vs library | Service | Connection pooling, central upgrades, any language |
| Lua script vs MULTI/EXEC | Lua | Needs read-decide-write; transactions cannot branch on reads |
| Exact global counts across regions | No | Cross-region latency; async sync with bounded overshoot |
| Fail open by default | Yes | Availability of the API wins, except security and cost rules |
| Negative cache of rejections | Yes | Protects Redis from abusive and hot subjects |

## Interview follow-ups

**Why not `INCR` and `EXPIRE` from the client?** A crash between them leaves a key with no TTL, and any read-then-decide logic races across gateways. A Lua script makes the whole step atomic.

**How do you limit across regions?** Count locally, sync deltas asynchronously, and accept a bounded overshoot for quotas. For strict limits, route the decision to a home region or lease tokens from a global pool.

**What if Redis is down?** Fail open for general quotas with a per-node fallback of `limit / N`, fail closed for login and paid operations, and alert on fail-open counts.

**How do you stop one abusive client from overloading Redis?** Local token buckets reject most of the burst in-process, and a negative cache remembers "over limit until T", so Redis sees a few calls per second for that subject.

**Token bucket or sliding window?** Token bucket when bursts are acceptable and you want a smooth refill rate (per-node protection); sliding window counter when you are enforcing "N per window" quotas consistently across nodes.

**How do you roll out a new limit safely?** Shadow mode first: evaluate and record would-be rejections without enforcing. Check who would be hit, contact them or adjust, then enforce region by region.

**How would you rate limit by cost, say LLM tokens?** Pass a cost per request into the same script. When the cost is only known after the call, reserve an estimate up front and reconcile the difference afterward.
