---
title: Design a rate limiter (LLD)
section: lld
order: 10
summary: Class design for a per-client rate limiter with token bucket, sliding window log and sliding window counter as pluggable strategies, plus thread safety.
---

## Requirements

- `allow(client_id) -> bool` for each incoming request.
- Limits configurable per client or tier (e.g. free: 10 req/min, pro: 100 req/min).
- Algorithm pluggable: token bucket, sliding window log, sliding window counter.
- Thread-safe; many requests per client concurrently.
- Out of scope (say it): distributed limiting across servers; covered in HLD with Redis.

## Entities

| Entity | Responsibility |
|--------|----------------|
| `RateLimitConfig` | `limit`, `window_seconds` (value object) |
| `RateLimitStrategy` | Algorithm interface; owns per-client state |
| `TokenBucket`, `SlidingWindowLog`, `SlidingWindowCounter` | Concrete algorithms |
| `RateLimiter` | Resolves config per client, delegates to a strategy |
| `Clock` | Injected time source, so tests are deterministic |

## Algorithms at a glance

| Algorithm | Memory per client | Accuracy | Bursts |
|-----------|-------------------|----------|--------|
| Fixed window counter | `O(1)` | Up to 2x at window edges | Edge bursts |
| Sliding window log | `O(limit)` | Exact | No |
| Sliding window counter | `O(1)` | Approximate (weighted) | Smoothed |
| Token bucket | `O(1)` | Exact for rate + burst | Allows up to capacity |
| Leaky bucket | `O(queue)` | Smooth output rate | No (queues) |

## Class sketch

```python
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitConfig:
    limit: int            # requests (or bucket capacity)
    window: float         # seconds


class RateLimitStrategy(ABC):
    def __init__(self, cfg: RateLimitConfig, clock=time.monotonic):
        self.cfg, self.clock = cfg, clock
        self._locks = defaultdict(threading.Lock)  # one lock per client

    @abstractmethod
    def _allow(self, key: str, now: float) -> bool: ...

    def allow(self, key: str) -> bool:
        with self._locks[key]:          # per-client lock
            return self._allow(key, self.clock())
```

> **Gotcha:** `defaultdict(threading.Lock)` creating the lock is itself racy under free-threaded Python. Guard creation with a global lock or pre-create locks when you register a client.

### Token bucket

Bucket holds up to `limit` tokens, refilled at `limit / window` per second. Each request takes one.

```python
class TokenBucket(RateLimitStrategy):
    def __init__(self, cfg, clock=time.monotonic):
        super().__init__(cfg, clock)
        self.rate = cfg.limit / cfg.window
        self.state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)

    def _allow(self, key, now):
        tokens, last = self.state.get(key, (self.cfg.limit, now))
        tokens = min(self.cfg.limit, tokens + (now - last) * self.rate)
        allowed = tokens >= 1
        self.state[key] = (tokens - 1 if allowed else tokens, now)
        return allowed
```

Lazy refill on access: no background thread needed.

### Sliding window log

Keep timestamps of accepted requests; drop those older than the window.

```python
class SlidingWindowLog(RateLimitStrategy):
    def __init__(self, cfg, clock=time.monotonic):
        super().__init__(cfg, clock)
        self.logs: dict[str, deque] = defaultdict(deque)

    def _allow(self, key, now):
        log = self.logs[key]
        while log and log[0] <= now - self.cfg.window:
            log.popleft()
        if len(log) < self.cfg.limit:
            log.append(now)
            return True
        return False
```

### Sliding window counter

Two fixed-window counts; weight the previous window by how much of it still overlaps.

```python
class SlidingWindowCounter(RateLimitStrategy):
    def __init__(self, cfg, clock=time.monotonic):
        super().__init__(cfg, clock)
        self.state: dict[str, list] = {}   # key -> [window_start, curr, prev]

    def _allow(self, key, now):
        w = self.cfg.window
        start = now - (now % w)
        s = self.state.setdefault(key, [start, 0, 0])
        if start != s[0]:
            # moved on: previous = current if adjacent, else 0
            s[2] = s[1] if start - s[0] == w else 0
            s[0], s[1] = start, 0
        overlap = 1 - (now - start) / w
        if s[2] * overlap + s[1] < self.cfg.limit:
            s[1] += 1
            return True
        return False
```

### The limiter (facade over strategies)

```python
class RateLimiter:
    def __init__(self, default: RateLimitStrategy,
                 per_tier: dict[str, RateLimitStrategy] | None = None):
        self.default, self.per_tier = default, per_tier or {}

    def allow(self, client_id: str, tier: str = "free") -> bool:
        strategy = self.per_tier.get(tier, self.default)
        return strategy.allow(client_id)


limiter = RateLimiter(
    default=TokenBucket(RateLimitConfig(10, 60)),
    per_tier={"pro": SlidingWindowLog(RateLimitConfig(100, 60))},
)
```

## Key flows

1. Request arrives → middleware calls `limiter.allow(client_id, tier)`.
2. Limiter picks strategy for the tier.
3. Strategy takes the client's lock, reads clock, updates state, returns decision.
4. Denied → HTTP `429` with `Retry-After`.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Strategy | Algorithm classes | Swap algorithm per tier without touching callers |
| Template method | `allow` wraps `_allow` | Locking and clock handled once in the base |
| Facade | `RateLimiter` | One call for middleware |
| Dependency injection | `clock` | Deterministic unit tests |

## Pitfalls

- Using `time.time()`; wall clock can jump. Use `time.monotonic()` in-process.
- Unbounded per-client state; evict idle clients (LRU or TTL).
- Locking globally: one slow client blocks everyone.
- Counting rejected requests in the log (lets attackers extend their own ban).

## Extensions interviewers ask

- **Distributed:** Redis per client; token bucket as a Lua script for atomicity, or `INCR` + `EXPIRE` for fixed window, sorted sets for sliding log.
- **Multiple rules:** per-second and per-day together; chain limiters, all must allow.
- **Per-endpoint limits:** key = `f"{client}:{endpoint}"`.
- **Return remaining quota:** strategy returns a `Decision(allowed, remaining, retry_after)` dataclass.
- **Dynamic config reload:** config provider plus observer to rebuild strategies.
