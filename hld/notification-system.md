---
title: Design a notification system
section: hld
order: 18
summary: Send email, SMS and push at scale with priorities, retries, user preferences, rate limits and deduplication.
---

## Requirements

**Functional**
- Internal services trigger notifications (`order_shipped` for user X).
- Channels: push (APNs/FCM), email, SMS, in-app.
- Templates with variables and localization.
- User preferences: opt-out per channel/category, quiet hours, frequency caps.
- Scheduled and bulk (campaign) sends.

**Non-functional**
- Transactional (OTP, password reset) delivered in seconds; marketing can lag minutes.
- At-least-once to providers with deduplication: no lost OTPs, no double charges/spam.
- Survive provider outages; track delivery status.

## Estimates

- 10 M notifications/day typical, bursts of 1 M in minutes for campaigns.
- Avg ~115/s, peaks ~5–10K/s. Provider limits (SMS/email per second) are usually the true bottleneck.
- Log of every notification: 10 M × 1 KB = 10 GB/day.

## API

```http
POST /v1/notifications
Idempotency-Key: order-981-shipped
{
  "user_id": 42,
  "type": "order_shipped",
  "priority": "high",
  "data": {"order_id": 981, "eta": "Tue"},
  "channels": ["push", "email"]
}
-> 202 {"notification_id": "n_7f3a"}

GET /v1/notifications/{id}          -> status per channel
PUT /v1/users/{id}/preferences
```

## Data model

```text
notifications(id, user_id, type, idempotency_key UNIQUE, payload jsonb,
              created_at)
deliveries(id, notification_id, channel, status, attempts, provider_msg_id,
           last_error, next_attempt_at)
preferences(user_id, category, channel, enabled, quiet_start, quiet_end, tz)
templates(type, channel, locale, version, subject, body)
devices(user_id, platform, token, last_seen)
```

## High-level design

```text
services -> notification API --(validate, idempotency, persist)--> Postgres
                  |
            router / preference check / dedup / rate cap
                  |
     +------------+------------+-------------+
     v            v            v             v
 q:push-high  q:email-high  q:sms-high   q:*-low (bulk)
     |            |            |             |
 push workers email workers sms workers  (same, lower concurrency)
     |            |            |
   APNs/FCM   SES/SendGrid  Twilio      -> provider webhooks
                                           -> status updates
```

## Deep dives

### Priority

- Separate queues per channel and priority. High-priority workers are never blocked behind a 1 M campaign.
- Bulk sends: expand the audience in batches (cursor over users), enqueue gradually, throttle to provider limits.

### Preferences and policy

Checked at routing time, before enqueueing per channel:
- Opt-outs per category and channel; legal unsubscribe for marketing.
- Quiet hours in the user's time zone: delay (set `next_attempt_at`) rather than drop, except critical types.
- Frequency caps: Redis counter per user per category per day.
- Transactional/security messages bypass marketing opt-outs (not channel opt-outs where required by law).

### Deduplication

- API level: `Idempotency-Key` unique per producer event (`order-981-shipped`).
- Worker level: deliveries are at-least-once from the queue; before sending, atomically claim the delivery (`UPDATE deliveries SET status='sending' WHERE id=? AND status IN ('queued','retry')`).
- Provider level: pass an idempotency key where supported.
- Content-level: suppress identical notifications to the same user within N minutes (hash of type + data).

### Retries

- Retry transient provider errors (timeouts, 5xx, 429) with exponential backoff + jitter, max N attempts, then DLQ and mark `failed`.
- Do not retry permanent errors (invalid token, hard bounce): mark the device token or email invalid.
- **Failover:** a secondary provider per channel behind a circuit breaker.
- Critical fallback: push fails or not read in X minutes → send SMS.

### Delivery tracking

- Store `provider_msg_id`; consume provider webhooks (delivered, bounced, opened) to update status.
- Metrics per channel/provider: send rate, error rate, latency, queue age.

### Templates

- Versioned templates per channel and locale, rendered in the worker with a sandboxed engine; escape user data.

## Bottlenecks and trade-offs

- Provider rate limits and cost (SMS is expensive): batch, throttle, prefer push/in-app.
- Strict dedup vs latency: DB checks add a round trip per send; Redis `SET NX` with TTL is faster but less durable.
- Queue choice: SQS/RabbitMQ per channel, or a Postgres queue (`SKIP LOCKED`) since deliveries are already rows. Kafka if you also need the event stream for analytics.
- Preference lookups on every send: cache per user with short TTL.

## Likely follow-ups

- How do you guarantee an OTP arrives fast? High-priority queue, dedicated workers, provider failover, short TTL so stale OTPs are not sent late.
- How do you avoid spamming during a bug that loops events? Per-user frequency caps and global circuit breaker per notification type.
- In-app notifications? Store in a per-user table; push via WebSocket if online; unread counter in Redis.
- How to send to 10 M users at once? Segment query → batched enqueue → workers throttled to provider quotas; spread over minutes.
