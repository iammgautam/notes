---
title: Design a notification service
section: backend
order: 81
phase: "Phase 10 · System design case studies"
summary: Multi-channel delivery with templates, preferences, quiet hours, priorities, per-user caps, dedup, retries, provider failover, scheduling and tracking.
---

Notification systems usually grow by accident: the order service calls an email API, the auth service adds SMS, marketing adds pushes, and users get three messages at 2 a.m. about one parcel. A shared service puts one pipeline between "something happened" and "a human is interrupted", and that pipeline holds the user's rules: channels, categories, hours, frequency. The hard parts are isolating urgent from bulk traffic, not sending twice, and surviving flaky providers.

## Requirements

### Clarifying questions

- Who sends? Internal services (transactional events) and a campaign tool (marketing to segments).
- Which channels? Push (iOS, Android, web), email, SMS, in-app inbox.
- Do producers choose channels, or does the service? The service decides from category defaults and user preferences; producers may restrict.
- Delivery guarantee? At-least-once to the provider, with deduplication to make duplicates rare; exactly-once to a human is not achievable.

### Functional

- Accept a notification request (single user or a segment) with category, priority, template and data.
- Render per channel and locale from versioned templates.
- Respect preferences (per category and channel), opt-outs and suppression lists, quiet hours in the user's time zone.
- Enforce per-user frequency caps; collapse duplicates.
- Deliver through providers with retries and failover; schedule for a time or the user's local time.
- Track status per channel (sent, delivered, opened, clicked, bounced) and expose it.
- In-app inbox with read state and real-time delivery when the user is online.

### Non-functional

| Property | Target |
|---|---|
| Users | 200M |
| Volume | 1B notifications/day; campaigns push peaks to ~100k/s |
| Channel mix | push 60%, in-app 25%, email 13%, SMS 2% |
| Latency, accept to provider | security (OTP) p99 under 5 s; transactional under 30 s; marketing within its window |
| Availability | 99.99% for the accept API; accepted means never silently lost |
| Duplicates | rare and bounded; never for SMS where avoidable (cost and annoyance) |

### Out of scope

Segment building, campaign authoring UI, email content design, phone and chat channels.

## Capacity estimates

```text
average rate     = 1B / 86,400                      = ~11.6k/s
campaign burst   = 50M users over 10 min            = ~83k/s
by channel/day   = push 600M, in-app 250M, email 130M, SMS 20M

notification row = ~1 KB x 1B                       = ~1 TB/day
retention 90 d   = 90 TB, x3 replicas               = ~270 TB
delivery events  = ~3 per notification x 200 B      = ~600 GB/day
preferences      = 200M x ~500 B                    = ~100 GB
device tokens    = 200M x 1.5 x ~300 B              = ~90 GB
email egress     = 130M x ~50 KB                    = ~6.5 TB/day
cap counters     = 50M active x 3 keys x ~50 B      = ~7.5 GB in Redis
```

SMS is 2% of volume but often most of the bill, at around a cent or more per message in many countries. That is why SMS gets stricter dedup, fraud limits and provider routing by price.

## API design

```http
POST /v1/notifications
{
  "idempotencyKey": "order-A-1009-shipped",
  "userId": "u_42",
  "category": "order_update",
  "priority": "transactional",
  "templateId": "order_shipped",
  "data": {"orderId": "A-1009", "trackingUrl": "https://..."},
  "channels": ["push", "email"],
  "sendAt": null,
  "expiresAt": "2026-11-17T18:00:00Z"
}
202 {"notificationId": "n_9f3", "status": "ACCEPTED"}
200 {"notificationId": "n_9f3", "status": "ACCEPTED"}   (same key again)

POST /v1/campaigns
{"segmentId": "seg_lapsed_30d", "templateId": "winback_v3",
 "category": "marketing", "sendAt": "2026-11-20T09:00", "localTime": true}
202 {"campaignId": "c_12", "estimatedRecipients": 4100000}

GET /v1/notifications/{id}
200 {"id": "n_9f3", "channels": [
       {"channel": "push", "status": "OPENED", "at": "..."},
       {"channel": "email", "status": "DELIVERED", "at": "..."}]}

GET  /v1/users/{userId}/inbox?cursor=...
POST /v1/users/{userId}/inbox/read     {"ids": ["n_9f3"]}
PUT  /v1/users/{userId}/preferences
{"timezone": "Asia/Kolkata",
 "quietHours": {"start": "22:00", "end": "07:00"},
 "optOut": [{"category": "marketing", "channel": "sms"}]}
POST /v1/devices                       {"platform": "ios", "token": "..."}
POST /v1/webhooks/{provider}           (delivery receipts, signed)
```

The accept call returns 202 once the request is durably stored and enqueued. The idempotency key is scoped to the producer, and a repeat returns the original notification ID. `expiresAt` lets a producer say "this is worthless after 5 minutes" (an OTP), so a backlog drops it instead of delivering it late.

## Data model

| Table | Store | Key / index | Notes |
|---|---|---|---|
| templates | PostgreSQL | (template_id, version, channel, locale) | Immutable versions; status draft/live |
| user_settings | PostgreSQL + Redis cache | user_id | timezone, locale, quiet hours |
| preferences | PostgreSQL + Redis cache | (user_id, category, channel) | Absent row = category default |
| suppressions | PostgreSQL | (channel, address) | Hard bounces, complaints, SMS STOP |
| devices | PostgreSQL | device_id; (user_id) | token, platform, last_seen, invalid_at |
| notifications | Cassandra / DynamoDB, TTL 90 d | notification_id; unique (producer, idempotency_key) | Request plus resolved template version |
| deliveries | Cassandra / DynamoDB | (notification_id, channel); lookup by provider_message_id | Attempts, provider, status |
| inbox | Cassandra | (user_id, created_at DESC) | In-app list and read flags |
| scheduled | Partitioned by minute bucket | (due_minute, notification_id) | Scheduler scans the current bucket |

Settings, preferences and templates are small, relational and read on every send, so they live in PostgreSQL behind a cache. Notification and delivery records are high-volume, append-mostly, and expire, which suits a wide-column store with TTLs. The uniqueness check on `(producer, idempotency_key)` needs a conditional insert (a lightweight transaction in Cassandra, a condition expression in DynamoDB) or a separate PostgreSQL table with a unique index.

## High-level design

```text
 producers: order, auth, billing services; campaign tool
       | POST /notifications            +-----------------------+
       v                                | notifications DB      |
 +-------------+  insert if new key     | (unique producer+key) |
 | notify API  |----------------------->+-----------------------+
 +-------------+
       | enqueue by priority
       v
 +------------------------------------+     +-----------------+
 | Kafka topics: critical, normal,    |<----| scheduler       |
 | bulk (separate consumer pools)     |     | (sendAt, quiet  |
 +------------------------------------+     |  hours, retry)  |
       |                                    +-----------------+
       v                                            ^
 +------------------------------------+   defer     |
 | router: prefs, suppressions, quiet |-------------+
 | hours, caps, dedup, render         |
 +------------------------------------+
       | per-channel queues
       v
 +--------+  +---------+  +---------+  +----------------------+
 | push   |  | email   |  | SMS     |  | in-app: inbox row +  |
 | APNs,  |  | vendor  |  | vendor  |  | WebSocket if online  |
 | FCM    |  | A, B    |  | A, B    |  +----------------------+
 +--------+  +---------+  +---------+
       |          |            |
       +----------+------------+--> providers
                                       | webhooks, app events
                                       v
                          tracking -> deliveries, analytics
```

1. **Accept.** The API validates the payload against the template's variable schema, inserts the notification if the idempotency key is new, and publishes to the topic for its priority. Insert and publish go through an outbox so neither is lost.
2. **Route.** Router workers load settings and preferences (cached), drop suppressed or opted-out channels, defer anything that falls in quiet hours, check frequency caps, collapse duplicates, render each channel's content, and write one delivery row per channel.
3. **Send.** Channel workers call providers with retries and failover, recording the provider's message ID.
4. **Track.** Provider webhooks and app events (push opened, link clicked) update delivery status and flow to analytics.

### Channels

| Channel | Gateway | Address | Quirks |
|---|---|---|---|
| Push | APNs (Apple), FCM (Android, web) | Device token, rotates | ~4 KB payload limit; invalid tokens reported on send; collapse IDs replace older pushes |
| Email | A sending provider (several for redundancy) | Address | SPF, DKIM, DMARC; bounces and complaints feed suppressions; one-click unsubscribe expected by major mailbox providers for bulk mail |
| SMS | Aggregators | E.164 phone number | Cost per message; per-country sender rules; STOP keywords must be honored; fraud risk |
| In-app | Your own inbox + WebSocket/SSE | user_id | Always works; only seen when the user opens the app |

Push has no alternative vendor: APNs and FCM are the only routes to their devices. Failover there means retrying and queueing; switching providers applies to email and SMS.

## Deep dives

### Priorities and isolation

A 50M-user campaign must never delay a login code. Give each priority its own topic and its own worker pool:

| Priority | Examples | Quiet hours | Caps | Expiry |
|---|---|---|---|---|
| critical | OTP, password reset, security alert | Ignored | None (abuse limits at the producer) | Minutes |
| transactional | Order shipped, payment receipt | Push deferred; email sent | Collapsed, not capped | Hours |
| bulk | Marketing, digests, recommendations | Deferred | Strict per-user caps | Campaign window |

Reserve capacity: critical workers are never borrowed by bulk. Provider throughput is shared, so each provider account gets a token bucket (in Redis, shared across workers) with a slice reserved for critical traffic. When backlogs build, pause bulk consumers first. Workers check `expiresAt` before sending and mark stale messages `EXPIRED`.

### Templates and localization

Templates are versioned and immutable once live: `(template_id, version, channel, locale)`. A notification records the version it resolved, so a resend or audit renders the same text. Rendering happens in the router, after preferences decide the channels, with a locale fallback chain (`pt-BR` then `pt` then `en`). Variables are validated at accept time against a schema, so a missing `orderId` is a 400 for the producer, not a broken email to a customer. Escape variables per channel (HTML for email), respect SMS segment and push payload limits, and prefer a logic-less template language for templates non-engineers edit.

### User preferences and quiet hours

Evaluation order for each channel: suppression list, explicit opt-out for (category, channel), category default, then quiet hours.

Quiet hours are evaluated in the user's time zone, not the server's, using IANA zones so daylight saving time is handled. A window like 22:00–07:00 wraps midnight. A bulk message that lands inside quiet hours is not dropped; it is rescheduled for the window's end, via the scheduler. Critical messages ignore quiet hours. Preferences are read on every send, so cache them per user with invalidation on update; a stale opt-out is a compliance problem, so invalidate on write rather than waiting for a TTL.

### Per-user rate limits

Frequency caps protect users from being spammed by your own teams: for example, at most 3 marketing pushes a day and 1 per hour, counted across all campaigns. Implement with Redis counters keyed by user, category and local date (`INCR` plus `EXPIRE`), or a sliding window for hourly caps; see [rate limiting]({{ '/backend/rate-limiting/' | relative_url }}). When a cap is hit, drop or downgrade (send to the in-app inbox instead of push). Transactional bursts are collapsed instead: ten "item shipped" events in a minute become one digest.

The producer side needs limits too. OTP endpoints that send SMS attract SMS pumping fraud, where attackers trigger messages to premium numbers they profit from. Rate limit OTP requests per phone number, IP and destination country before they reach this service.

### Dedup and idempotency

Duplicates come from three places, and each needs its own guard:

1. **Producer retries.** The API's unique `(producer, idempotency_key)` turns a retried request into a lookup that returns the original ID. See [idempotency]({{ '/backend/idempotency/' | relative_url }}).
2. **Pipeline redelivery.** Kafka consumers are at-least-once; a worker can crash after sending and before committing its offset. Before calling a provider, the worker claims the delivery with a conditional update (`status QUEUED -> SENDING` for that notification and channel). A redelivered message finds it already `SENDING` or `SENT` and skips; a stuck `SENDING` past a timeout is checked against the provider by message ID before resending.
3. **Semantic duplicates.** Two services both announce the same shipment. Collapse on `(user_id, template_id, entity_id)` within a window, and use push collapse IDs so the device shows only the latest.

The remaining gap: a provider call that times out after the provider accepted it. Retrying may duplicate, not retrying may lose. Choose per channel: retry for push and email (a rare duplicate is tolerable), and for SMS prefer checking status or letting the user request a new code.

### Retries, backoff and provider failover

Classify every error before deciding what to do:

| Class | Examples | Action |
|---|---|---|
| Transient | Timeout, 5xx, 429 | Retry with exponential backoff and full jitter; honor `Retry-After` |
| Bad recipient | APNs 410 Unregistered, FCM UNREGISTERED, hard bounce, invalid number | No retry; invalidate token or add to suppressions |
| Bad request | Render error, payload too large | No retry; dead-letter queue and alert the owning team |
| Provider outage | Error rate spike across many requests | Circuit breaker opens; fail over to the secondary provider |

In production, retries do not sleep inside the worker: the message goes to a delay topic (for example retry-30s, retry-5m) or the scheduler with its next attempt time, keeping workers free for fresh traffic. After the last attempt, it goes to a dead-letter queue with the full error history. Failover needs preparation to work when needed: the secondary email provider must have your DKIM keys and warmed-up sending reputation, and the secondary SMS route must be registered for the same sender IDs. See [retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}) and [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

### Putting the rules together

A runnable sketch of router and sender: dedup, opt-outs, quiet hours (defer bulk, not security), a daily cap, full-jitter retries, failover and rendering. In-memory sets stand in for the database and Redis.

```python
import random
from datetime import datetime, timedelta, timezone
from string import Template
from zoneinfo import ZoneInfo


class TransientError(Exception):      # timeout, 5xx, 429: retry
    pass


class PermanentError(Exception):      # bad number, hard bounce: never retry
    pass


TEMPLATES = {("order_shipped", "sms", "en"): Template(
    "Hi $name, order $order_id has shipped. Track: $url")}


def in_quiet_hours(local, start_h, end_h):
    h = local.hour
    if start_h > end_h:                                  # wraps midnight
        return h >= start_h or h < end_h
    return start_h <= h < end_h


def quiet_end(local, end_h):
    end = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
    return end if end > local else end + timedelta(days=1)


class Dispatcher:
    def __init__(self, users, providers, clock, sleep, marketing_cap=3):
        self.users, self.providers = users, providers  # channel -> senders
        self.clock, self.sleep, self.cap = clock, sleep, marketing_cap
        self.seen = set()      # production: unique index on (producer, key)
        self.daily = {}        # production: Redis INCR + EXPIRE per user-day
        self.deferred = []     # production: scheduler table / sorted set

    def submit(self, n):
        if n["key"] in self.seen:
            return "duplicate"
        self.seen.add(n["key"])
        user = self.users[n["user"]]
        if (n["category"], n["channel"]) in user["opt_out"]:
            return "suppressed"
        local = self.clock().astimezone(ZoneInfo(user["tz"]))
        quiet = in_quiet_hours(local, *user["quiet"])
        if n["category"] != "security" and quiet:
            self.deferred.append((quiet_end(local, user["quiet"][1]), n))
            return "deferred"
        if n["category"] == "marketing":
            day = (n["user"], local.date())
            if self.daily.get(day, 0) >= self.cap:
                return "rate_limited"
            self.daily[day] = self.daily.get(day, 0) + 1
        return self.deliver(n)

    def deliver(self, n, attempts=4, base_s=0.5, cap_s=8.0):
        for send in self.providers[n["channel"]]:        # failover order
            for attempt in range(attempts):
                try:
                    return "sent:" + send(n)
                except PermanentError:
                    return "failed_permanent"
                except TransientError:                     # full jitter
                    backoff = min(cap_s, base_s * 2 ** attempt)
                    self.sleep(random.uniform(0, backoff))
        return "dead_letter"


if __name__ == "__main__":
    random.seed(1)
    now = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
    no_sms_promos = {("marketing", "sms")}
    users = {
        "asha": {"tz": "Asia/Kolkata", "quiet": (22, 7),
                 "opt_out": no_sms_promos},
        "ben": {"tz": "America/New_York", "quiet": (22, 7),
                "opt_out": set()},
    }
    calls, sleeps = [], []

    def flaky_sms(n):
        calls.append("primary")
        raise TransientError("503 from provider A")

    def backup_sms(n):
        calls.append("backup")
        if n["to"] == "+000":
            raise PermanentError("invalid number")
        return "B-" + n["key"]

    def push(n):
        return "P-" + n["key"]

    d = Dispatcher(users, {"sms": [flaky_sms, backup_sms], "push": [push]},
                   clock=lambda: now, sleep=sleeps.append)

    # 23:30 in Kolkata: marketing waits for 07:00, a login OTP does not.
    promo = {"key": "m1", "user": "asha", "category": "marketing",
             "channel": "push"}
    assert d.submit(promo) == "deferred"
    assert d.deferred[0][0].hour == 7
    otp = {"key": "otp1", "user": "asha", "category": "security",
           "channel": "sms", "to": "+91"}
    assert d.submit(otp) == "sent:B-otp1"                  # failed over
    assert calls == ["primary"] * 4 + ["backup"]
    assert len(sleeps) == 4 and max(sleeps) <= 8.0
    assert d.submit(dict(otp)) == "duplicate"               # producer retry
    assert d.submit({"key": "m2", "user": "asha", "category": "marketing",
                     "channel": "sms"}) == "suppressed"

    # 13:00 in New York: three marketing pushes allowed, the fourth capped.
    results = [d.submit({"key": f"b{i}", "user": "ben",
                         "category": "marketing", "channel": "push"})
               for i in range(4)]
    assert results == ["sent:P-b0", "sent:P-b1", "sent:P-b2", "rate_limited"]

    calls.clear()
    bad = {"key": "t1", "user": "ben", "category": "transactional",
           "channel": "sms", "to": "+000"}
    assert d.submit(bad) == "failed_permanent"
    assert calls.count("backup") == 1

    body = TEMPLATES[("order_shipped", "sms", "en")].substitute(
        name="Ben", order_id="A-1009", url="https://example.com/t/A-1009")
    assert body.startswith("Hi Ben, order A-1009")
    print("ok")
```

### Scheduling

Two cases. A notification with `sendAt` (or deferred by quiet hours) is written to a table partitioned by due minute; a scheduler leader scans the current bucket every few seconds and publishes due items to their priority topic. For short delays, a Redis sorted set scored by due time works too: `ZRANGEBYSCORE` then a `ZREM` to claim each item so two schedulers never send the same one. Campaigns at "9:00 local time" are split by time-zone offset into a few dozen groups, each scheduled separately, and each group is spread over 15–30 minutes. Otherwise millions of users open your app in the same second. The general machinery is in [design a job scheduler]({{ '/backend/design-job-scheduler/' | relative_url }}).

### Delivery tracking

Each delivery moves through `QUEUED -> SENDING -> SENT -> DELIVERED -> OPENED -> CLICKED`, with terminal branches `FAILED`, `BOUNCED`, `SUPPRESSED`, `EXPIRED`. Sources:

- **Provider responses.** Acceptance gives `SENT` and a provider message ID.
- **Webhooks.** Email and SMS providers post delivered, bounced and complaint events. Verify signatures, look up the delivery by provider message ID, and upsert idempotently. Events arrive out of order (an "opened" before "delivered"), so store status as the furthest state reached, ranked, not the last event received.
- **App events.** APNs and FCM confirm acceptance, not display; the app reports receipt and open. Email opens come from a tracking pixel and are unreliable (privacy features prefetch images), so treat clicks as the stronger signal.

Status changes go to Kafka for analytics (delivery rate per provider, open rate per template) and for automation: a spike in bounces from one provider trips its circuit breaker.

## Scaling and bottlenecks

| Scale | What breaks first | Fix |
|---|---|---|
| 10× campaigns | Router reads of preferences | Cache per user; batch lookups for segment sends; precompute eligibility for a campaign once |
| 10× campaigns | Provider rate limits | More provider accounts and sending IPs; spread sends; per-provider token buckets |
| 10× | Delivery-row writes | Wide-column store scales horizontally; write deliveries in batches |
| 100× | Kafka partitions per priority | More partitions keyed by user_id; independent consumer groups per channel |
| 100× | Webhook ingestion | Accept and enqueue webhooks immediately, process asynchronously |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Provider outage | Channel stalls | Circuit breaker, failover to secondary, queue and drain on recovery |
| Bulk backlog | Critical delayed | Separate topics and pools; pause bulk first |
| Router crash after send | Possible duplicate | Delivery claim CAS before send; status check on recovery |
| Preference cache stale | Message to an opted-out user | Invalidate on write; short TTL as backstop |
| Scheduler leader dies | Delayed sends pause | Leader election with lease; buckets scanned idempotently on takeover |
| Invalid device tokens pile up | Wasted sends, throttling by APNs/FCM | Invalidate on provider error; prune tokens unseen for months |

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Who picks channels | Producer | No | Ignores preferences; inconsistent behavior |
| Who picks channels | Notification service, producer may restrict | Yes | One place for user rules |
| Queueing | One queue for all | No | Campaigns delay OTPs |
| Queueing | Topic and pool per priority | Yes | Isolation and reserved capacity |
| Quiet hours | Drop the message | No | Users miss useful notifications |
| Quiet hours | Defer to window end (bulk), bypass (critical) | Yes | Respectful and still useful |
| Retries | Sleep in worker | No | Ties up workers during outages |
| Retries | Delay topics with backoff and jitter | Yes | Workers stay free; no synchronized retries |
| Delivery guarantee | Exactly-once | No | Impossible across external providers |
| Delivery guarantee | At-least-once + layered dedup | Yes | Rare, bounded duplicates; nothing lost |

## Interview follow-ups

**How do you guarantee an accepted notification is never lost?** The accept API writes the notification and an outbox row in one transaction before returning 202; a relay publishes to Kafka, consumers commit offsets only after recording the outcome, and failures end in a dead-letter queue that is monitored and replayable.

**A campaign to 50M users starts. How do OTPs stay fast?** Different topic, different workers, reserved provider capacity. The campaign drains at a controlled rate on its own pool.

**User is in quiet hours and an order ships. What happens?** Push is deferred to the end of quiet hours, email is sent (it is silent), and the in-app inbox gets it immediately.

**APNs returns 410 for a token. What do you do?** Mark the token invalid, never retry it, and stop sending to that device until the app registers a new token.

**How do you avoid sending the same push twice when a worker crashes?** Claim the delivery row with a conditional update before calling the provider; on redelivery, skip anything already `SENDING` or `SENT`, and resolve stuck `SENDING` rows by checking provider status.

**How would you add WhatsApp as a channel?** Add a channel worker and provider adapter, a template type (WhatsApp requires pre-approved templates), preference entries for the new channel, and delivery-status webhooks. Router logic does not change, which is the test of a good design.
