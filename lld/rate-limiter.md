---
title: Design a rate limiter
section: lld
order: 26
phase: "Phase 5 · Intermediate"
summary: Token bucket, leaky bucket, fixed window, sliding log and sliding counter as strategies behind a thread-safe per-key registry, plus the Redis variant.
---

## Requirements

Clarifying questions to ask first:

- Limit per what: user, API key, IP, endpoint, or a combination?
- Hard limit, or are short bursts fine?
- One process, or many servers sharing one limit?
- On rejection: return 429, queue the request, or degrade?
- Should the algorithm be configurable per tier (free vs paid)?

Functional:

- `allow(key) -> bool`, for any number of keys, each with independent state.
- Algorithms are pluggable: token bucket, leaky bucket, fixed window, sliding window log, sliding window counter.
- Different limits per tier by using a different registry (or factory) per tier.

Non-functional: `O(1)` per call (the sliding log is `O(limit)` memory); thread-safe with no global bottleneck; deterministic tests via an injected clock.

Out of scope: distributed limiting (follow-up), `Retry-After` computation, idle-key eviction.

## Entities and relationships

```text
+--------------------------------+          +---------------------------+
| RateLimiter (per-key registry) | 1      * | Limiter (ABC)             |
| factory: () -> Limiter         |--------->| _lock                     |
| clock: () -> float             | creates  | allow(clock) -> bool      |
| _limiters: dict[str, Limiter]  | lazily   | _allow(now)   (abstract)  |
| allow(key) -> bool             |          +---------------------------+
+--------------------------------+            ^ is-a: one per algorithm
                                              |
                     +------------------------+
                     |  TokenBucket ............ tokens, last
                     |  LeakyBucket ............ level, last
                     |  FixedWindow ............ idx, count
                     |  SlidingWindowLog ....... deque of accepted times
                     +- SlidingWindowCounter ... idx, prev, curr
```

One `Limiter` instance holds the state for one key. The registry maps keys to instances.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---------|-------|------------------------|
| Strategy | `Limiter` subclasses | Switching algorithm per tier without touching the caller |
| Template method | `allow` (lock + clock) wraps `_allow` | Each algorithm re-implementing locking, and getting it wrong |
| Factory | `factory` callable in the registry | The registry knowing constructor arguments of every algorithm |
| Registry (lazy) | `RateLimiter._limiters` | Pre-creating state for keys that never call |
| Dependency injection | `clock` | Tests that depend on `sleep()` and real time |

## Algorithms at a glance

| Algorithm | State per key | Behavior | Weak spot |
|-----------|---------------|----------|-----------|
| Token bucket | tokens, last time | Steady rate, bursts up to capacity | Bursts may be unwanted |
| Leaky bucket (meter) | level, last time | Same decisions as token bucket, mirrored | Same |
| Leaky bucket (queue) | FIFO queue | Smooth, constant output; requests wait | Latency; needs a worker |
| Fixed window | window index, count | Simple counter per window | Up to 2× limit across a boundary |
| Sliding window log | timestamps | Exact | `O(limit)` memory per key |
| Sliding window counter | two counts | Near-exact, `O(1)` | Assumes even spread in the previous window |

## Code

```python
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable

Clock = Callable[[], float]


class Limiter(ABC):
    """State for ONE key. allow() is the template: lock, read clock, decide."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def allow(self, clock: Clock) -> bool:
        with self._lock:
            return self._allow(clock())     # read the clock inside the lock

    @abstractmethod
    def _allow(self, now: float) -> bool: ...


class TokenBucket(Limiter):
    """Up to `capacity` tokens, refilled at `rate` per second."""

    def __init__(self, capacity: int, rate: float):
        super().__init__()
        self.capacity, self.rate = capacity, rate
        self.tokens, self.last = float(capacity), None

    def _allow(self, now):
        if self.last is not None:           # lazy refill: no background thread
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class LeakyBucket(Limiter):
    """Meter form: drains at `rate` per second; each request adds one unit."""

    def __init__(self, capacity: int, rate: float):
        super().__init__()
        self.capacity, self.rate = capacity, rate
        self.level, self.last = 0.0, None

    def _allow(self, now):
        if self.last is not None:
            self.level = max(0.0, self.level - (now - self.last) * self.rate)
        self.last = now
        if self.level + 1 <= self.capacity:
            self.level += 1
            return True
        return False


class FixedWindow(Limiter):
    def __init__(self, limit: int, window: float):
        super().__init__()
        self.limit, self.window = limit, window
        self.idx, self.count = -1, 0

    def _allow(self, now):
        idx = int(now // self.window)
        if idx != self.idx:                 # new window: reset
            self.idx, self.count = idx, 0
        if self.count < self.limit:
            self.count += 1
            return True
        return False


class SlidingWindowLog(Limiter):
    def __init__(self, limit: int, window: float):
        super().__init__()
        self.limit, self.window = limit, window
        self.log: deque[float] = deque()    # accepted timestamps only

    def _allow(self, now):
        while self.log and self.log[0] <= now - self.window:
            self.log.popleft()
        if len(self.log) < self.limit:
            self.log.append(now)
            return True
        return False


class SlidingWindowCounter(Limiter):
    """Weights the previous window's count by how much of it still overlaps."""

    def __init__(self, limit: int, window: float):
        super().__init__()
        self.limit, self.window = limit, window
        self.idx, self.prev, self.curr = -1, 0, 0

    def _allow(self, now):
        idx = int(now // self.window)
        if idx != self.idx:
            self.prev = self.curr if idx == self.idx + 1 else 0
            self.idx, self.curr = idx, 0
        overlap = 1 - (now / self.window - idx)
        if self.prev * overlap + self.curr < self.limit:
            self.curr += 1
            return True
        return False


class RateLimiter:
    """Per-key registry: one Limiter per key, created on first use."""

    def __init__(self, factory: Callable[[], Limiter], clock: Clock = time.monotonic):
        self.factory, self.clock = factory, clock
        self._limiters: dict[str, Limiter] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        limiter = self._limiters.get(key)
        if limiter is None:
            with self._lock:                # double-checked: exactly one per key
                limiter = self._limiters.get(key)
                if limiter is None:
                    limiter = self._limiters[key] = self.factory()
        # Simplification: keys are never evicted; put idle keys in a TTL cache if unbounded
        return limiter.allow(self.clock)


if __name__ == "__main__":
    class FakeClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    def factories(n: int) -> dict[str, Callable[[], Limiter]]:
        return {"token": lambda: TokenBucket(n, n),
                "leaky": lambda: LeakyBucket(n, n),
                "fixed": lambda: FixedWindow(n, 1.0),
                "log": lambda: SlidingWindowLog(n, 1.0),
                "counter": lambda: SlidingWindowCounter(n, 1.0)}

    clock = FakeClock()
    for name, make in factories(3).items():
        rl = RateLimiter(make, clock)
        clock.now = 0.0
        assert [rl.allow("u1") for _ in range(4)] == [True] * 3 + [False], name
        assert rl.allow("u2"), name                  # keys are independent
        clock.now = 2.0
        assert rl.allow("u1"), name                  # quota is back

    # boundary burst: 3 requests at t=0.99 and 3 more at t=1.00
    for name, make in factories(3).items():
        rl = RateLimiter(make, clock)
        clock.now = 0.99
        n = sum(rl.allow("k") for _ in range(3))
        clock.now = 1.0
        n += sum(rl.allow("k") for _ in range(3))
        assert n == (6 if name == "fixed" else 3), (name, n)   # fixed window: 2x

    # stress: frozen clock, 16 threads x 50 calls on one key -> exactly 100 pass
    sys.setswitchinterval(1e-6)          # switch threads often so races show up
    clock.now = 5.0
    for name, make in factories(100).items():
        rl, passed = RateLimiter(make, clock), []

        def hammer() -> None:
            passed.append(sum(rl.allow("hot") for _ in range(50)))

        threads = [threading.Thread(target=hammer) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(passed) == 100, (name, sum(passed))
    print("ok")
```

The stress test also checks the registry: if two threads created separate limiters for `"hot"`, 200 requests would pass.

## Key flows

A request through middleware:

```text
client    middleware     RateLimiter        Limiter("u1")       clock
  | GET       |                |                   |               |
  |---------->|--allow("u1")-->|                   |               |
  |           |                | lookup; create    |               |
  |           |                | under lock if new |               |
  |           |                |--allow(clock)---->| lock          |
  |           |                |                   |--clock()----->|
  |           |                |                   |<--now---------|
  |           |                |                   | refill, take  |
  |           |                |<-- True/False ----| unlock        |
  |           |<-- decision ---|                   |               |
  |<-- 200 or 429 + Retry-After                    |               |
```

Token bucket decision at `now`:

```text
tokens = min(capacity, tokens + (now - last) * rate)
last = now
tokens >= 1 ?  yes: tokens -= 1, allow   no: reject (do not consume)
```

## Edge cases

- Read the clock inside the per-key lock. Reading it outside lets two threads apply times out of order: a fixed window can flip back to the previous window and reset its count.
- Use `time.monotonic()`; wall-clock jumps (NTP, manual changes) refill buckets or extend bans.
- Do not record rejected requests in the sliding log, or a client that keeps retrying never recovers.
- `capacity` below 1 means nothing is ever allowed; validate config at startup.
- The first request for a key creates its state; with unbounded key spaces (IPs), memory grows until you evict idle keys.

## Production follow-ups

**Is it thread-safe without a global bottleneck?** Each key has its own lock, so hot key A never waits on key B. The registry lock is held only to create a limiter, using double-checked lookup. The GIL does not make "refill, compare, decrement" atomic: remove the per-key lock and the stress test (with its tiny switch interval) lets more than 100 requests through the token and leaky buckets. See [Concurrency in LLD]({{ '/lld/concurrency-in-lld/' | relative_url }}).

**What happens on restart?** In-memory state is lost and every client gets a fresh quota. That is acceptable for abuse protection. For quotas that map to billing ("10,000 calls per month"), store counters in Redis or PostgreSQL and treat the in-memory limiter as a fast first check.

**How do retries interact with the limiter?** Retries count as requests, which is correct: they cost the backend the same. Return 429 with `Retry-After` so well-behaved clients back off with jitter. For writes, pair it with an idempotency key so a retried-and-allowed request does not apply twice.

**How do you add an algorithm or combine limits?** A new algorithm is one `Limiter` subclass. Several rules (10 per second AND 1,000 per day) become a composite that allows only if all children allow; to avoid charging the per-second bucket when the daily limit rejects, check all, then commit.

**What changes across machines (the Redis variant)?** With N servers each limiting locally, the effective limit is N times the configured one. Move state to Redis: fixed window is `INCR key` plus `EXPIRE` on first hit; sliding log is a sorted set (`ZREMRANGEBYSCORE`, `ZCARD`, `ZADD`); token bucket is a Lua script so read-refill-write is atomic on the server. Use Redis server time inside the script, not each app server's clock, to avoid skew. Decide fail-open or fail-closed for when Redis is down; most APIs fail open.

**What breaks at 10× traffic?** One Redis key per hot client becomes a hot shard; keep a small local token bucket per server that refills from Redis in batches. Per-key memory adds up for millions of IPs; evict idle keys with a TTL.

**What would you log and measure?** Allowed and rejected counts per rule, top rejected keys, limiter latency (especially Redis round trips), and the fail-open count. Log rejections at a sampled rate; a flood of 429s should not also flood the logs.

## Interview follow-ups

- **Which algorithm for a public API?** Token bucket: `O(1)`, allows reasonable bursts, simple to explain, easy in Redis.
- **Token bucket vs leaky bucket?** As meters they make the same decisions. As a queue, the leaky bucket delays requests to produce a perfectly smooth output rate; that is traffic shaping, not just rejecting.
- **Why is the fixed window inaccurate?** A client can send the full limit at the end of one window and again at the start of the next: 2× in a moment. The demo shows 6 requests passing a limit of 3.
- **How do you return remaining quota?** Have `_allow` return a small `Decision(allowed, remaining, retry_after)` dataclass and map it to `X-RateLimit-Remaining` and `Retry-After` headers.
