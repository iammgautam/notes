---
title: Design a chat system
section: hld
order: 17
summary: 1:1 and group chat over WebSockets with per-conversation ordering, delivery receipts, presence and write-heavy message storage.
---

## Requirements

**Functional**
- 1:1 and group chats (up to ~500 members).
- Send/receive messages in real time; offline users get them on reconnect (and a push notification).
- Delivery/read receipts, online presence, typing indicator.
- Message history across devices.

**Non-functional**
- Delivery latency p99 < 300 ms when both online.
- No message loss; no duplicates shown; per-conversation order preserved.
- Highly available; multi-device.

Out of scope: end-to-end encryption details, voice/video.

## Estimates

- 100 M DAU, 50 messages/day → 5 B/day ≈ 60K msg/s avg, ~200K peak.
- 100 B avg → 500 GB/day → ~180 TB/yr (×3 replication). Write-heavy, append-only.
- Concurrent connections: ~30 M. At ~100K–500K connections per gateway box → 100–300 boxes.

## API

Connection: WebSocket after HTTP auth. Messages are JSON frames.

```json
{"type": "send", "client_msg_id": "c-uuid", "conv_id": "g42", "body": "hi"}
{"type": "ack", "client_msg_id": "c-uuid", "msg_id": 98123, "seq": 1051}
{"type": "message", "conv_id": "g42", "seq": 1051, "from": "u7", "body": "hi"}
{"type": "read", "conv_id": "g42", "up_to_seq": 1051}
```

REST for history and setup:

```http
GET  /v1/conversations/{id}/messages?before_seq=1051&limit=50
GET  /v1/sync?since=<per-conversation seqs>
POST /v1/conversations       {"members": [...]}
```

## Data model

| Table | Key | Store |
|---|---|---|
| `messages` | PK `(conv_id)`, clustering `seq DESC` | Cassandra/ScyllaDB (or Postgres partitioned by conv hash) |
| `conversations`, `members` | `conv_id`; index by `user_id` | Postgres |
| `user_inbox` | `(user_id) → conv_id, last_seq, unread` | Cassandra / Redis |
| `read_state` | `(conv_id, user_id) → last_read_seq` | Cassandra / Postgres |
| Presence | `presence:{user_id} → gateway_id, last_seen` | Redis with TTL |

Messages are always read by conversation in time order, so partition by `conv_id`. Bucket huge conversations by time (`conv_id, month`) to bound partition size.

## High-level design

```text
 clients <==WebSocket==> chat gateways (stateful, many)
                            |        ^
                            v        | deliver
                       chat service -+---- Redis: user -> gateway
                       |    |    \
         seq per conv  |    |     pub/sub or per-gateway queue
                       v    v
                 messages DB  queue -> push notification service
                 (Cassandra)       -> search indexer
```

Send flow:
1. Client sends with `client_msg_id` over WebSocket to its gateway.
2. Chat service assigns `seq` for the conversation, writes the message (idempotent on `client_msg_id`).
3. Acks the sender with `msg_id` and `seq`.
4. Looks up recipients' gateways in Redis; routes the message to those gateways, which push to devices.
5. Offline recipients: enqueue a push notification; they sync on reconnect.

## Deep dives

### Ordering

- Global ordering is unnecessary; **per-conversation** ordering is what users see.
- Assign a monotonic `seq` per conversation at a single owner: route each conversation to one partition (consistent hash on `conv_id`) and use an atomic counter (`INCR conv:{id}:seq` in Redis, or a row lock in Postgres).
- Clients render by `seq`, not by arrival time or client clock.
- Gap detection: if a client sees seq 1051 after 1049, it fetches 1050.

### Delivery guarantees

- At-least-once delivery with dedupe: client retries `send` with the same `client_msg_id`; server dedupes.
- Server pushes with `seq`; clients ack; unacked messages are re-sent or fetched via sync.
- **Sync on reconnect:** client sends last seen `seq` per conversation (or a single per-user inbox cursor); server returns missing messages.

### Connections and routing

- Gateways are stateful (hold sockets). Keep them thin: auth, heartbeat, forward frames.
- `user_id → [gateway_id, device]` in Redis with TTL refreshed by heartbeat.
- Route via direct RPC to the gateway, or each gateway subscribes to its own channel (Redis pub/sub / Kafka topic partition).
- Deploys: drain gateways gradually; clients reconnect with jittered backoff to avoid a thundering herd.

### Group messages

- Small groups (≤ 500): fan-out on write to each member's inbox and online gateways.
- Large channels: store once, members pull by `seq` (like a celebrity feed); push only a "new messages" hint.

### Presence

- Heartbeat every ~30 s; `SET presence:u7 online EX 60`.
- Publish presence changes only to contacts who are online and have the chat open; debounce flapping.
- Last seen = timestamp of last heartbeat.

### Receipts

- Delivered: device acks. Read: client sends `up_to_seq`. Store only the max seq per user per conversation, not per message.

## Bottlenecks and trade-offs

- Message write rate: wide-column store handles append-heavy writes; Postgres works with partitioning up to a point.
- Per-conversation seq is a serialization point; fine because a single conversation's rate is low.
- Huge groups: fan-out cost and receipts explode; switch to pull and drop per-member receipts.
- Presence fan-out is often more traffic than messages; limit scope.

## Likely follow-ups

- Multi-device? Each device has its own cursor; messages fan out to all of a user's connections.
- Message search? Async index into Elasticsearch/OpenSearch per user (or client-side with E2EE).
- Edits/deletes? New event with the same `msg_id`; clients apply it.
- Why not HTTP long polling? Works as a fallback; WebSockets give lower latency and less overhead.
