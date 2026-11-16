---
title: Rate limiting and throttling
section: backend
order: 26
phase: "Phase 3 · API design"
summary: Fixed window, sliding log, sliding counter, token and leaky bucket in code, Redis Lua limits, RateLimit headers, 429 handling and load shedding.
---

Every shared system needs a rule for "too much". Without one, a retry loop in one customer's integration, a scraper, or a single tenant's month-end export takes capacity from everyone else. Rate limits protect capacity, keep tenants fair, cap spend on metered dependencies (LLM tokens, SMS), enforce plan quotas, and slow down credential stuffing and enumeration. The algorithms are small; the real design questions are what to key on, where to enforce, how to share state across instances, and what to do when the limiter itself is the thing that's down.

```text
client
  |
CDN / WAF        per IP or network: floods, bots          cheap, coarse
  |
API gateway      per API key or plan: 1,000 req/min        shared Redis
  |
service          per tenant and endpoint cost: 5 exports/h business-aware
  |              in-flight cap per tenant, load shedding
  |
workers / DB     queue rates, pool size, provider quotas   protect deps
```

## Rate limiting vs throttling

The terms are used loosely (DRF calls its limiters "throttles"), but the distinctions matter in a design discussion:

| Mechanism | Decides on | Excess traffic | Typical home |
|---|---|---|---|
| Rate limit | Caller identity and a rate | Rejected with 429 | Gateway, service |
| Throttling (shaping) | Caller identity and a rate | Delayed or queued | nginx `limit_req`, job queues |
| Quota | Caller identity, long window | Rejected or billed | Billing, plan enforcement |
| Concurrency limit | Requests in flight | Rejected or queued | Service, connection pool |
| Load shedding | Server health | Rejected with 503 | Service, load balancer |

A fleet can be overloaded with every caller under its limit, so you need both per-caller limits and server-side shedding.

## Rate limiting algorithms

| Algorithm | State per key | Bursts | Accuracy | Use for |
|---|---|---|---|---|
| Fixed window | 1 counter | Up to 2x at boundaries | Coarse | Daily quotas, simple limits |
| Sliding log | 1 timestamp per request | None beyond limit | Exact | Low limits: login, OTP sends |
| Sliding window counter | 2 counters | Smoothed | Approximate | General API limits at scale |
| Token bucket | Tokens + timestamp | Up to capacity | Exact for its model | APIs with burst allowance, costs |
| Leaky bucket | Level or queue | None; output smooth | Exact | Shaping calls to fragile downstreams |

### Fixed window

Count requests per key per window index (`now // 60`) and reject once the count reaches the limit. In Redis it is `INCR` plus `EXPIRE` on a key that includes the window index. It is the cheapest option, but a client can send the full limit at 00:59 and again at 01:00, twice the limit in one second, and every client resets at the same instant. Fine for daily or monthly quotas, where the boundary effect is negligible.

### Sliding window log

Store the timestamp of each accepted request and count those inside the trailing window. It is exact, with no boundary effect, but memory is `O(limit)` per key: a 10,000-per-hour limit can mean 10,000 timestamps per client. In Redis it is a sorted set (`ZREMRANGEBYSCORE`, `ZCARD`, `ZADD` in one script) whose members must be unique, such as timestamp plus a random suffix, or same-millisecond requests collapse into one. Use it where limits are small and precision matters.

### Sliding window counter

Keep the counts for the current and previous fixed windows and estimate the trailing window as `prev * (1 - elapsed_fraction) + curr`. It uses two counters, smooths the boundary burst, and assumes the previous window's requests were spread evenly, which is where its small error comes from. Cloudflare publicly described using this approximation at very large scale and reported the error on real traffic as tiny.

### Token bucket

A bucket holds up to `capacity` tokens and refills at `rate` tokens per second; a request spends one token, or `cost` tokens for expensive operations. It allows bursts up to the capacity and enforces the long-run rate, and its two parameters map directly onto product language: "100 requests per second, bursts of 200". You don't need a timer: store `(tokens, last_refill)` and compute the refill lazily on each request. The wait until the next allowed request is `(cost - tokens) / rate`, which is your `Retry-After`. Variable cost makes it the natural fit for GraphQL query cost or LLM token budgets. AWS API Gateway documents its throttling as a token bucket with rate and burst settings, and Stripe's public write-up on its rate limiters uses token buckets.

### Leaky bucket

Two different things share the name:

- **As a meter:** a level drains at a fixed rate, each request adds one, and overflow is rejected. This is the token bucket upside down. GCRA (generic cell rate algorithm) is a compact form of it that stores a single timestamp per key.
- **As a queue (shaper):** requests enter a FIFO and leave at a constant rate; when the queue is full, new requests are rejected. Output is perfectly smooth and excess costs latency instead of errors. It's the right tool in front of a provider that allows strictly 10 calls per second.

nginx's `limit_req` is a leaky bucket: without `burst`, excess is rejected; with `burst=N`, up to N excess requests queue and are released at the configured rate; `nodelay` serves the burst immediately while still counting it.

### All five (runnable)

Time is passed in explicitly so the behavior is deterministic.

```python
from collections import deque


class FixedWindow:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.index, self.count = None, 0

    def allow(self, now):
        index = int(now // self.window)
        if index != self.index:  # new window: reset
            self.index, self.count = index, 0
        if self.count < self.limit:
            self.count += 1
            return True
        return False


class SlidingLog:
    def __init__(self, limit, window):
        self.limit, self.window, self.log = limit, window, deque()

    def allow(self, now):
        while self.log and self.log[0] <= now - self.window:
            self.log.popleft()
        if len(self.log) < self.limit:
            self.log.append(now)  # log accepted requests only
            return True
        return False


class SlidingWindowCounter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.index, self.prev, self.curr = 0, 0, 0

    def allow(self, now):
        index = int(now // self.window)
        if index != self.index:
            self.prev = self.curr if index == self.index + 1 else 0
            self.index, self.curr = index, 0
        elapsed = (now % self.window) / self.window
        if self.prev * (1 - elapsed) + self.curr < self.limit:
            self.curr += 1
            return True
        return False


class TokenBucket:
    def __init__(self, rate, capacity):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.last = capacity, 0.0

    def allow(self, now, cost=1):
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def retry_after(self, cost=1):
        return max(0.0, (cost - self.tokens) / self.rate)


class LeakyBucketQueue:
    """Shaper: requests leave at a constant rate; admit returns the delay."""

    def __init__(self, rate, max_waiting):
        self.interval, self.max_waiting = 1 / rate, max_waiting
        self.next_free = 0.0

    def admit(self, now):
        start = max(now, self.next_free)
        if (start - now) / self.interval >= self.max_waiting:
            return None  # bucket full: reject
        self.next_free = start + self.interval
        return start - now


def accepted(limiter, times):
    return sum(limiter.allow(t) for t in times)


if __name__ == "__main__":
    edge = [59.0] * 10 + [60.0] * 10  # 20 requests in one second
    assert accepted(FixedWindow(10, 60), edge) == 20  # boundary burst
    assert accepted(SlidingLog(10, 60), edge) == 10

    log = SlidingLog(10, 60)
    accepted(log, [59.0] * 10)
    assert not log.allow(118.9) and log.allow(119.0)

    swc = SlidingWindowCounter(10, 60)
    assert accepted(swc, edge) == 10
    # Halfway through the next window, half of the previous 10 still count.
    assert accepted(swc, [90.0] * 10) == 5

    tb = TokenBucket(rate=1, capacity=5)
    assert accepted(tb, [0.0] * 6) == 5  # burst up to capacity
    assert tb.retry_after() == 1.0
    assert accepted(tb, [2.5] * 3) == 2  # refilled 2.5 tokens

    lb = LeakyBucketQueue(rate=2, max_waiting=3)
    assert [lb.admit(0.0) for _ in range(4)] == [0.0, 0.5, 1.0, None]
    assert lb.admit(10.0) == 0.0
    print("ok")
```

A thread-safe, per-key version of these as an object-oriented design is in the [LLD rate limiter]({{ '/lld/rate-limiter/' | relative_url }}); the full system design interview walkthrough is in [design a rate limiter]({{ '/backend/design-rate-limiter/' | relative_url }}).

## Where to enforce

### Edge and gateway

The CDN, WAF or API gateway rejects floods before they cost you anything: per-IP limits for anonymous traffic, per-API-key limits by plan. It's cheap and protects the whole fleet, but it knows little about the business: it can't tell a cheap read from a 30-second export. See [API gateway and BFF]({{ '/backend/api-gateway-and-bff/' | relative_url }}).

### In the service

The service knows the tenant, the plan and the cost of the operation, so business limits live here: 5 exports per hour, 1,000 LLM tokens per minute per seat, 3 OTP SMS per phone number per 10 minutes. Concurrency limits also live here, such as at most 4 in-flight reports per tenant, because a slow endpoint can exhaust workers well within any per-minute rate.

### Choosing the key

| Key | Stops | Watch out for |
|---|---|---|
| IP address | Anonymous floods, scraping, login spraying | NAT and corporate proxies share one IP; IPv6 clients rotate within a /64, so key on the prefix |
| User or API key | One account or integration misbehaving | Attackers with many accounts |
| Tenant | One customer starving others | Large tenants need higher, plan-based limits |
| Endpoint or cost | Expensive operations | Needs a cost model per endpoint |
| Global | Total load beyond capacity | Punishes everyone; pair with shedding |

Layer them: a global limit protects capacity, per-tenant limits give fairness in [multi-tenant systems]({{ '/backend/multi-tenant-architecture/' | relative_url }}), and per-user limits stop one runaway integration from consuming its whole tenant's allowance. Behind a load balancer, take the client IP from the `X-Forwarded-For` entry your own trusted proxy appended, never the leftmost one, which the client controls.

## Distributed limits with Redis and Lua

An in-process limiter multiplies by your replica count: 100 per minute per pod across 10 pods is 1,000 per minute, and it changes whenever the autoscaler acts. A shared limit needs shared state, usually Redis.

- **Atomicity.** Read-then-write from two app servers loses updates. `INCR` is atomic, which is enough for a fixed window. Multi-step algorithms (token bucket, sliding log) run as a Lua script, which Redis executes atomically.
- **One clock.** Call `TIME` inside the script so app servers with skewed clocks agree. This is allowed in Redis 5 and later, where scripts replicate their effects instead of the script itself.
- **Redis Cluster.** A script may only touch keys in one hash slot. Keep a limit's keys together with a hash tag, such as `rl:{t42}:user7` and `rl:{t42}:total`.
- **Cost.** One round trip, typically well under a millisecond in-zone; check several limits in one script call.
- **Hot keys.** A huge tenant's key lives on one shard. Split it into N sub-keys with limit/N each, or have each app node lease batches of tokens and spend them locally.
- **When Redis is down.** Set a tight timeout (tens of milliseconds). Fail open, to a local per-pod limiter, for general API limits, so a Redis blip doesn't become an outage. Fail closed for abuse-sensitive limits such as logins, OTP sends and paid provider calls.

A token bucket script (illustrative):

```lua
-- KEYS[1] bucket key; ARGV: rate (tokens/s), capacity, cost
local rate, capacity, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local t = redis.call("TIME")              -- one clock for every app server
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state = redis.call("HMGET", KEYS[1], "tokens", "ts")
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now

tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed, wait_ms = 0, 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  wait_ms = math.ceil((cost - tokens) / rate * 1000)
end
redis.call("HSET", KEYS[1], "tokens", tokens, "ts", now)
redis.call("PEXPIRE", KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {allowed, math.floor(tokens), wait_ms}  -- Lua floats become integers
```

Calling it with redis-py, which caches the script and uses `EVALSHA` (illustrative):

```python
import redis

r = redis.Redis(host="redis", socket_timeout=0.05)
token_bucket = r.register_script(TOKEN_BUCKET_LUA)

def check(tenant_id, rate=100, burst=200, cost=1):
    try:
        allowed, remaining, wait_ms = token_bucket(
            keys=[f"rl:tb:tenant:{tenant_id}"], args=[rate, burst, cost])
    except redis.RedisError:
        return local_limiter.check(tenant_id)  # fail open, but not unlimited
    return bool(allowed), remaining, wait_ms
```

## Rate limit headers

Tell clients where they stand on every response, not just on rejections, so well-behaved clients can pace themselves.

- **`Retry-After`** (RFC 9110): delay in seconds or an HTTP date. Send it on 429 and 503. Prefer seconds; client clocks drift.
- **De facto `X-RateLimit-*`.** `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` are widespread. GitHub, for example, sends `x-ratelimit-limit`, `-remaining`, `-used`, `-reset` (as epoch seconds) and `-resource`.
- **IETF draft.** The httpapi working group's "RateLimit header fields for HTTP" draft has changed shape across revisions: early ones used `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`; recent ones use a `RateLimit-Policy` field describing the quota and a `RateLimit` field with the remaining amount and seconds until reset. As of 2025 to 2026 it is still a draft, so check the current revision before adopting its exact syntax.

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 12
RateLimit-Policy: "tenant";q=6000;w=3600
RateLimit: "tenant";r=0;t=12

{"type": "https://api.example.com/problems/rate-limited",
 "title": "Rate limit exceeded", "status": 429,
 "detail": "Tenant limit of 6000 requests per hour reached."}
```

On security-sensitive endpoints such as login, don't advertise exact remaining counts.

## Handling 429

On the server:

- **Reject cheaply.** Run the limiter before expensive work, and sample rejection logs: during an attack, rejections are most of your traffic.
- **429 vs 503.** `429 Too Many Requests` (RFC 6585) means this caller is over its allowance. `503 Service Unavailable` means the server is overloaded for everyone. Clients and dashboards treat them differently.
- **Count accepted requests only**, unless you want a deliberate penalty box. A limiter that counts rejections never lets a tight retry loop recover.
- **Make the body useful:** a problem-details body (see [validation and error handling]({{ '/backend/validation-and-error-handling/' | relative_url }})) with a stable code, which limit was hit, and `Retry-After`.

On the client:

- Honor `Retry-After`; without it, use exponential backoff with full jitter and a retry budget (see [retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})).
- A 429 from a limiter that runs before the handler means nothing executed, so even a POST is safe to retry. You can't always tell where a 429 came from, so send an idempotency key anyway.
- Pace yourself against providers you call: a shared token bucket per provider and API key, sized slightly under their published limit.
- Celery's `rate_limit="10/m"` task option is enforced per worker instance, not across the cluster: eight workers give 80 per minute. Use a shared Redis limiter for provider quotas.

## Adaptive throttling and load shedding

Static limits assume known capacity, but capacity moves: a slow query plan, a degraded dependency, a GC pause, a noisy neighbor on the host. Adaptive techniques react to the server's state instead.

- **Load shedding.** Reject work when the server is near saturation, measured by in-flight requests, queue length, time spent queued, or CPU. Shed by priority: background and batch traffic first, then unauthenticated, and health checks and checkout last. Reject fast with 503 and `Retry-After`. Stripe publicly described load shedders that reserve part of the fleet for critical requests alongside its per-user limiters.
- **Queue deadlines.** A request that waited in a queue longer than the client's timeout is wasted work: the client has already given up. Drop it. Facebook publicly described adaptive LIFO and CoDel-style queue management for this in "Fail at Scale".
- **Adaptive concurrency limits.** Treat the in-flight limit like a TCP congestion window: raise it slowly while latency stays near the baseline, cut it multiplicatively when latency rises or timeouts appear (AIMD). Netflix open-sourced a concurrency-limits library built on this idea.
- **Client-side adaptive throttling.** The Google SRE book describes clients that track `requests` and `accepts` over a trailing window and reject locally with probability `max(0, (requests - K * accepts) / (requests + 1))`, with `K` typically 2. When the backend rejects most traffic, clients stop sending it, so rejections stop consuming backend capacity.
- **Retries undo shedding.** Without retry budgets, every shed request returns two or three times. See [backpressure]({{ '/backend/backpressure/' | relative_url }}) and [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

## In practice

### nginx per-IP limit (illustrative)

```nginx
# a 10 MB zone tracks on the order of 100,000 client IPs
limit_req_zone $binary_remote_addr zone=per_ip:10m rate=10r/s;
limit_req_status 429;            # the default is 503

server {
    location /api/ {
        limit_req zone=per_ip burst=20 nodelay;
        proxy_pass http://app;
    }
}
```

### DRF throttling per tenant plan (illustrative)

DRF throttles keep a sliding log of timestamps in Django's cache. They are not atomic, and DRF's docs warn they can let a few extra requests through under concurrency and are not a security control. With the per-process `LocMemCache`, each worker has its own counts; point them at a shared Redis cache.

```python
# settings.py
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": ["core.throttles.TenantRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"tenant": "6000/hour", "exports": "5/hour"},
}

# core/throttles.py
from rest_framework.throttling import SimpleRateThrottle

class TenantRateThrottle(SimpleRateThrottle):
    scope = "tenant"
    PLAN_RATES = {"free": "600/hour", "pro": "6000/hour", "scale": "60000/hour"}

    def allow_request(self, request, view):
        tenant = getattr(request, "tenant", None)  # set by tenant middleware
        if tenant is None:
            return True
        self.rate = self.PLAN_RATES.get(tenant.plan, self.rate)
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.tenant.pk}
```

Expensive views set `throttle_classes = [TenantRateThrottle, ScopedRateThrottle]` (a view's list replaces the defaults) and `throttle_scope = "exports"`. DRF sets `Retry-After` on its 429 responses from the throttle's `wait()`.

## Pitfalls

- **Limits multiplied by replicas.** Per-process limiters (DRF with `LocMemCache`, Celery `rate_limit`) scale with pod and worker count. Use shared state.
- **Everyone has the load balancer's IP.** Keying on `REMOTE_ADDR` behind a proxy limits all users as one. Parse the trusted `X-Forwarded-For` hop; never trust the leftmost value.
- **The limiter takes the site down.** Redis latency or an outage blocks every request. Tight timeouts, fail-open with a local fallback, and alerts.
- **Retry storms.** Clients ignoring `Retry-After` turn a limit into sustained load. Return it, document it, and penalize repeat offenders at the edge.
- **One limit for all endpoints.** A cheap read and a full export share a budget. Weight by cost or use scoped limits.
- **Invisible limits.** Customers find limits by hitting them in production. Publish them, send headers on every response, and expose usage in the dashboard.

## Interview questions

**Token bucket or leaky bucket?**
Token bucket permits bursts up to its capacity while enforcing an average rate, and it rejects excess. A leaky bucket used as a queue smooths output to a constant rate and adds latency instead of rejecting. Use a token bucket for API limits and a leaky queue in front of a downstream that cannot absorb bursts.

**How do you rate limit across 50 API servers?**
Shared state in Redis, with each check as one atomic Lua script (token bucket or sliding window), Redis `TIME` for a single clock, hash tags for cluster slot locality, a tight timeout and a fallback policy. For very hot keys, shard the key or lease token batches to nodes.

**What happens when Redis is unavailable?**
Decide per limit. General API limits fail open to a local per-pod limiter, so availability doesn't depend on the limiter. Abuse-sensitive limits, such as login attempts, OTP SMS and paid provider calls, fail closed.

**How do you keep one tenant from starving others in a multi-tenant SaaS?**
Per-tenant limits sized by plan, per-tenant concurrency caps on expensive endpoints, separate queues or weighted fair scheduling for background work, and a global limit plus load shedding behind them.

**What is the difference between rate limiting and load shedding?**
Rate limiting is per caller and policy-driven: you exceeded your allowance, 429. Load shedding is per server and health-driven: I can't take more work, 503, regardless of who you are. You need both.

**How would you limit LLM usage per user?**
A token bucket keyed by user or tenant where the cost is the token count: reserve an estimate before the call, settle the difference after the response reports actual usage, and add a monthly quota for spend.

## Further reading

- *Site Reliability Engineering* (Google), chapters "Handling Overload" and "Addressing Cascading Failures".
- Stripe engineering blog, "Scaling your API with rate limiters".
- Cloudflare blog, "How we built rate limiting capable of scaling to millions of domains".
- RFC 6585, *Additional HTTP Status Codes* (429), and the IETF httpapi draft "RateLimit header fields for HTTP".
- nginx documentation, `ngx_http_limit_req_module`; Redis documentation, "Scripting with Lua".
