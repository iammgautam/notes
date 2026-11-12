---
title: Design a notification system
section: lld
order: 35
phase: "Phase 5 · Intermediate"
summary: Email, SMS and push behind one channel interface, templates, preferences, priorities, dedup keys, per-user rate limits and async retries with backoff.
---

This is the class-level design inside one service. For queues, fan-out and provider scale, see the [HLD notification system]({{ '/hld/notification-system/' | relative_url }}).

## Requirements

Clarifying questions worth asking:

- Which channels? (Email, SMS, push. More later.)
- Who triggers notifications? (Other modules publish domain events such as `order_shipped`.)
- Can users opt out? (Per channel, except security messages like OTPs.)
- What if the same event arrives twice? (Send once.)
- What if a provider is down? (Retry with backoff, then park it in a dead-letter list.)

Functional:

- Map an event type to a priority and one template per channel.
- Render the template with event data and send on every channel the user has not opted out of.
- Rate limit per user, so a buggy producer cannot spam anyone. High priority skips the limit.
- Send asynchronously: publishing an event never waits on a provider.

Non-functional:

- High priority (OTP) is sent before low priority (marketing) when the queue is backed up.
- No duplicate sends for a duplicate event; no lost sends for a transient provider error.
- Thread-safe under many producers and many workers.

Out of scope: delivery receipts, digests, localization, the providers themselves.

## Entities and relationships

```text
+-------------+ notifies +--------------------------------+
| EventBus    |--------->| NotificationService (observer) |
| subscribe() |          | rules: {event_type: Rule}      |
| publish()   |          | opted_out: {(user, channel)}   |
+-------------+          | _seen: {idempotency keys}      |
                         | on_event(event)                |
                         +--------------------------------+
                             | uses              | uses
                             v                   v
                    +----------------+  +----------------------+
                    | RateLimiter    |  | Dispatcher           |
                    | allow(user_id) |  | q: PriorityQueue     |
                    +----------------+  | workers, retries     |
                                        | dead_letters         |
+-------------------+                   +----------------------+
| Rule              |                            | uses (by ChannelType)
| priority          |                            v
| templates:        |                   +----------------------+
|  {channel: Tmpl}  |                   | <<abstract>> Channel |
+-------------------+                   | send(user, body)     |
                                        +----------------------+
Notification: key, user, channel,          ^ is-a
  body, priority, attempts              EmailChannel, SmsChannel,
                                        PushChannel
```

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Observer | `EventBus` → `NotificationService.on_event` | Order, payment and auth code each calling email and SMS APIs directly |
| Strategy | `Channel` subclasses | A `match channel:` block in the sender that grows with every provider |
| Factory / registry | `{ChannelType: Channel}` built once | Callers constructing providers and their credentials |
| Producer-consumer | `Dispatcher` queue + workers | Request threads blocked on slow providers |
| Template | `string.Template` per channel | Message text hard-coded in business code |

## Code

```python
from __future__ import annotations

import itertools
import queue
import random
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum, IntEnum
from string import Template
from typing import Callable


class ChannelType(Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


class Priority(IntEnum):
    HIGH = 0            # OTP, security: skips opt-outs and the rate limit
    NORMAL = 1
    LOW = 2             # marketing


@dataclass(frozen=True)
class User:
    id: str
    email: str
    phone: str
    device: str


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    user_id: str
    data: dict[str, str]


@dataclass
class Notification:
    key: str                        # idempotency key: event id + channel
    user: User
    channel: ChannelType
    body: str
    priority: Priority
    attempts: int = 0


class Channel(ABC):
    """One per provider. Real ones call SES, Twilio or FCM; these record."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @abstractmethod
    def send(self, user: User, body: str) -> None:
        """Raise on failure; the dispatcher retries."""


class EmailChannel(Channel):
    def send(self, user, body):
        self.sent.append((user.email, body))


class SmsChannel(Channel):
    def send(self, user, body):
        self.sent.append((user.phone, body))


class PushChannel(Channel):
    def send(self, user, body):
        self.sent.append((user.device, body))


@dataclass(frozen=True)
class Rule:
    priority: Priority
    templates: dict[ChannelType, Template]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Event], None]]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        self._subs[event_type].append(handler)

    def publish(self, event: Event) -> None:
        for handler in self._subs[event.type]:
            handler(event)


class RateLimiter:
    """Sliding-window log: at most `limit` sends per user per `window` seconds."""

    def __init__(self, limit: int, window: float, clock=time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, user_id: str) -> bool:
        with self.lock:
            now, hits = self.clock(), self.hits[user_id]
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


class Dispatcher:
    """Priority queue drained by worker threads; failures retry with backoff."""

    def __init__(self, channels: dict[ChannelType, Channel], workers: int = 4,
                 max_attempts: int = 4, base_delay: float = 0.5) -> None:
        self.channels, self.workers = channels, workers
        self.max_attempts, self.base_delay = max_attempts, base_delay
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.dead_letters: list[Notification] = []
        self._seq = itertools.count()           # FIFO tie-break within a priority
        self._pending = 0
        self._cv = threading.Condition()

    def start(self) -> None:
        for _ in range(self.workers):
            threading.Thread(target=self._work, daemon=True).start()

    def submit(self, n: Notification) -> None:
        with self._cv:
            self._pending += 1
        self._enqueue(n)

    def _enqueue(self, n: Notification) -> None:
        with self._cv:
            seq = next(self._seq)
        self.q.put((n.priority, seq, n))

    def _work(self) -> None:
        while True:
            _, _, n = self.q.get()
            n.attempts += 1
            try:
                self.channels[n.channel].send(n.user, n.body)
                failed = False
            except Exception:
                failed = True
                if n.attempts < self.max_attempts:
                    delay = self.base_delay * 2 ** (n.attempts - 1)
                    delay *= random.uniform(0.5, 1.5)               # jitter
                    # One Timer thread per retry; production uses delayed delivery.
                    threading.Timer(delay, self._enqueue, [n]).start()
                    continue
            with self._cv:
                if failed:
                    self.dead_letters.append(n)
                self._pending -= 1
                self._cv.notify_all()

    def drain(self, timeout: float = 5.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)


class NotificationService:
    """Observer on the bus: event -> one notification per channel -> queue."""

    def __init__(self, bus: EventBus, rules: dict[str, Rule],
                 users: dict[str, User], dispatcher: Dispatcher,
                 limiter: RateLimiter) -> None:
        self.rules, self.users = rules, users
        self.dispatcher, self.limiter = dispatcher, limiter
        self.opted_out: set[tuple[str, ChannelType]] = set()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        for event_type in rules:
            bus.subscribe(event_type, self.on_event)

    def on_event(self, event: Event) -> None:
        rule, user = self.rules[event.type], self.users[event.user_id]
        urgent = rule.priority is Priority.HIGH
        for channel, template in rule.templates.items():
            if not urgent and (user.id, channel) in self.opted_out:
                continue
            key = f"{event.id}:{channel.value}"
            with self._lock:                    # check-and-add in one step
                if key in self._seen:
                    continue                    # event delivered twice upstream
                self._seen.add(key)
            if not urgent and not self.limiter.allow(user.id):
                continue                        # log + count "rate_limited"
            body = template.substitute(event.data)
            self.dispatcher.submit(Notification(key, user, channel, body,
                                                rule.priority))


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor

    E, S, P = ChannelType.EMAIL, ChannelType.SMS, ChannelType.PUSH

    class FlakySms(SmsChannel):
        def __init__(self, failures: int) -> None:
            super().__init__()
            self.failures = failures

        def send(self, user, body):
            if self.failures > 0:
                self.failures -= 1
                raise ConnectionError("provider timeout")
            super().send(user, body)

    def build(limit: int, workers: int, sms: Channel):
        channels = {E: EmailChannel(), S: sms, P: PushChannel()}
        rules = {
            "otp": Rule(Priority.HIGH, {S: Template("Your code is $code")}),
            "order_shipped": Rule(Priority.NORMAL, {
                E: Template("Order $order shipped"),
                P: Template("Order $order is on its way")}),
            "promo": Rule(Priority.LOW, {P: Template("$pct% off today")}),
        }
        users = {f"u{i}": User(f"u{i}", f"u{i}@x.io", f"+91{i}", f"dev{i}")
                 for i in range(50)}
        bus, d = EventBus(), Dispatcher(channels, workers, base_delay=0.01)
        svc = NotificationService(bus, rules, users, d, RateLimiter(limit, 60))
        return bus, svc, d, channels

    bus, svc, d, ch = build(limit=3, workers=1, sms=FlakySms(failures=2))
    bus.publish(Event("p1", "promo", "u1", {"pct": "20"}))
    bus.publish(Event("o1", "order_shipped", "u1", {"order": "A1"}))
    bus.publish(Event("o1", "order_shipped", "u1", {"order": "A1"}))   # duplicate
    d.start()                                         # queue was backed up
    assert d.drain()
    assert ch[P].sent == [("dev1", "Order A1 is on its way"), ("dev1", "20% off today")]
    assert ch[E].sent == [("u1@x.io", "Order A1 shipped")]

    bus.publish(Event("p2", "promo", "u1", {"pct": "5"}))  # u1 hit 3 per minute
    svc.opted_out.add(("u1", S))
    bus.publish(Event("otp1", "otp", "u1", {"code": "4821"}))  # HIGH ignores both
    assert d.drain()
    assert len(ch[P].sent) == 2
    assert ch[S].sent == [("+911", "Your code is 4821")] and ch[S].failures == 0

    ch[S].failures = 10                               # provider down for good
    bus.publish(Event("otp2", "otp", "u1", {"code": "1111"}))
    assert d.drain() and [n.attempts for n in d.dead_letters] == [4]

    # Stress: 50 users x 20 events, each event published twice, 16 producers.
    bus, svc, d, ch = build(limit=1000, workers=8, sms=SmsChannel())
    d.start()

    def storm(u: int) -> None:
        for i in range(20):
            for _ in range(2):
                bus.publish(Event(f"{u}-{i}", "order_shipped", f"u{u}", {"order": str(i)}))

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(storm, range(50)))
    assert d.drain()
    assert len(ch[E].sent) == len(ch[P].sent) == 1000
    print("notification system ok")
```

## Key flows

```text
Producer   EventBus   NotificationSvc   RateLimiter   Dispatcher   Channel
  | publish  |              |                |             |           |
  |--------->| on_event --->|                |             |           |
  |          |              | per channel:   |             |           |
  |          |              | opted out? skip|             |           |
  |          |              | key seen? skip |             |           |
  |          |              | allow(user) -->|             |           |
  |          |              |<---- true -----|             |           |
  |          |              | render, submit ------------->|           |
  |<---------|  (returns; nothing waits on a provider)     |           |
  |          |              |                | worker: get by priority |
  |          |              |                |             | send ---->|
```

```text
Worker            Channel           Timer               PriorityQueue
  | send() ------->|                  |                      |
  |<-- error ------|                  |                      |
  | attempts < max: delay = base * 2^(n-1) * jitter          |
  |-------------------------------->  | after delay: put --->|
  | (worker free for other work)      |                      |
  | attempts == max: dead_letters, pending -= 1              |
```

## Edge cases

- The same event published twice (at-least-once upstream): the `event_id:channel` key stops the second send.
- User opted out of every channel: nothing is sent; that is correct, not an error.
- Template references data the event lacks: `substitute` raises `KeyError` at publish time, so the bug surfaces in the producer, not silently as "Hello $name".
- Provider returns a permanent error (invalid phone number): retrying is waste. Real channels raise a distinct exception that goes straight to dead letters.
- A user without a device token: the push channel skips or dead-letters immediately.
- High priority during a marketing blast: it jumps the queue, but workers already busy finish their current send first.

## Production follow-ups

**Where are the thread-safety risks?** `queue.PriorityQueue` is already thread-safe (Java's `PriorityBlockingQueue`). The rest is check-then-act: the dedup set, the rate limiter's deque and the pending counter each sit behind a lock, because the GIL does not make "check, then add" atomic. Workers never hold a lock while calling a provider. See [producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

**What survives a restart?** Nothing in this version: the queue is in memory. In production the producer writes a `notification` row (status `PENDING`, unique `key`) in the same transaction as the business change, the transactional outbox, and workers claim rows with `SELECT ... FOR UPDATE SKIP LOCKED` or read from SQS/Kafka. After a crash, `PENDING` and stale `SENDING` rows are simply picked up again.

**How do retries and idempotency fit together?** Delivery is at-least-once, so every step must tolerate repeats. The `event_id:channel` key is a unique constraint (or Redis `SET key 1 NX EX 86400`), retries reuse the same notification, and the key is passed to providers that accept one. Retry only timeouts, 5xx and 429, with exponential backoff and jitter so a recovering provider is not hit by a synchronized wave. See [idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }}).

**How do you add WhatsApp?** Add `WhatsAppChannel(Channel)`, register it under a new `ChannelType`, and add templates to the rules that should use it. The dispatcher, service and producers do not change.

**What breaks at 10×?** One queue lets a marketing blast of millions delay OTPs, so split queues per priority (and often per channel) with dedicated workers. The dedup set and rate limiter move to Redis so every worker sees the same state. Providers have their own send limits, so add a token bucket per provider in front of each channel. See the [rate limiter]({{ '/lld/rate-limiter/' | relative_url }}).

**What do you log and measure?** Log the notification key, channel, attempt and provider response on every attempt. Measure per channel: sent, failed, retried, dead-lettered, rate-limited, queue depth per priority, and event-to-delivery latency (p50/p99). Alert on dead-letter growth and on OTP latency, since a slow OTP is a failed login.

**What if a provider is down for an hour?** A circuit breaker per channel stops hammering it after repeated failures, and a fallback provider (a second SMS vendor) takes over. Notifications that still fail sit in the dead-letter store with enough data to replay.

**Quiet hours and time zones?** Store each user's time zone; LOW priority messages that fall inside quiet hours are scheduled for the morning instead of dropped. HIGH priority ignores quiet hours.

## Interview follow-ups

- **Why a priority queue instead of FIFO?** When the queue is backed up, FIFO makes an OTP wait behind every promo already queued. With a sequence number as tie-break, order is still FIFO within one priority.
- **Can you guarantee exactly once?** Not to a phone. You get at-least-once delivery plus dedup keys, which is effectively once except when a provider accepts a message and the acknowledgement is lost.
- **Daily digest instead of 20 pushes?** Route LOW events to a digest store keyed by user; a scheduled job renders one notification per user per day.
- **How do you test it?** Fake channels that record, a fake clock for the limiter, and a tiny `base_delay`, as in the demo. No sleeps in tests.
- **Template changes while messages are queued?** Render at enqueue time (as here) so a queued message never changes, and version templates so you can tell which text a user received.
