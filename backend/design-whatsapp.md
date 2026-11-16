---
title: Design a WhatsApp backend
section: backend
order: 74
phase: "Phase 10 · System design case studies"
summary: "1:1 and group messaging over persistent connections: receipts, offline mailboxes, per-chat ordering, Signal-style E2EE, media, presence, multi-device."
---

A messaging backend is a routing and durability problem wrapped in a connection-management problem. Hundreds of millions of phones hold open connections that NATs and mobile operating systems keep killing, every message must survive any single failure between "sent" and "delivered", and with end-to-end encryption the server can't read what it is routing. The design below follows [the design framework]({{ '/backend/design-framework/' | relative_url }}). For a server-side-history chat product (Slack-style, searchable), see [Design a real-time chat system]({{ '/backend/design-chat-system/' | relative_url }}); this note focuses on the WhatsApp model, where devices own the history.

## Requirements

### Clarifying questions

- Does the server keep message history? No. It stores messages until every recipient device has them; devices own history. This is how WhatsApp publicly describes its model.
- Group size? Up to around 1,000 members; broadcast channels with millions of followers are a different product.
- End-to-end encryption? Yes, for all 1:1 and group messages and media.
- Devices per account? One phone plus a few linked devices (web, desktop, tablet).

### Functional

1. Send 1:1 and group text messages.
2. Sent, delivered and read receipts.
3. Deliver to devices that were offline when the message was sent.
4. Send images, video, voice notes and documents.
5. Presence (online, last seen) and typing indicators.
6. Use one account on several devices.

### Non-functional

| Property | Target |
|---|---|
| Daily active users | 1B |
| Messages sent | 100B/day (WhatsApp has publicly cited figures of this order) |
| Delivery latency | p99 < 500 ms when both sides are online |
| Durability | No message lost after the sender sees "sent" |
| Ordering | Consistent order within each chat on all devices |
| Availability | 99.99% for messaging |
| Privacy | Server never sees plaintext content |

### Out of scope

Voice and video calls, status updates, payments, cloud backups, broadcast channels.

## Capacity estimates

**Message rate.** 100B/day ÷ 86,400 ≈ 1.16M messages/s, about 3M/s at peak. Group fan-out multiplies deliveries; assume each message reaches two recipient devices on average: about 2.3M deliveries/s, 6M/s at peak.

**Connections.** If 40% of DAU are connected at peak, that is 400M persistent connections. At a planning figure of 500,000 connections per gateway (WhatsApp has publicly described running well over a million per server on Erlang), that's about 800 gateways, so plan 1,000+ for headroom and zone loss.

**Bandwidth.** An encrypted text envelope with headers is about 300 bytes. 2.3M deliveries/s × 300 B ≈ 700 MB/s ≈ 5.5 Gbps for text. Small.

**Mailbox storage.** Only undelivered messages are stored. If at any moment 20B deliveries are pending for offline devices: 20B × 300 B = 6 TB, 18 TB with 3× replication. Compare server-side history: 200B deliveries/day × 300 B = 60 TB/day, about 22 PB/year. The transient model is a large saving as well as a privacy property.

**Media.** Assume 10% of messages carry media at an average of 150 KB after client compression: 10B × 150 KB = 1.5 PB/day of uploads, about 140 Gbps of ingress, with downloads at least double that. Media bandwidth dwarfs everything else and belongs on object storage and a CDN.

**Heartbeats.** 400M connections pinging every 60 seconds is about 6.7M pings/s. They must terminate at the gateway and never touch a database.

## API design

Messaging runs over one long-lived, encrypted connection per device: WebSocket for web clients, a compact binary protocol over TCP for mobile ([real-time communication]({{ '/backend/real-time-communication/' | relative_url }})). Frames are shown as JSON for readability.

```json
{
  "type": "send",
  "client_msg_id": "c3f1-7a92-41d0",
  "chat_id": "g:8812",
  "envelopes": [
    {"device": "u42:0", "ciphertext": "base64..."},
    {"device": "u42:1", "ciphertext": "base64..."}
  ]
}
```

| Frame | Direction | Purpose |
|---|---|---|
| `send` | client → server | Message with per-device ciphertexts, or one sender-key ciphertext for groups |
| `ack {client_msg_id, chat_id, seq, server_ts}` | server → sender | Durably stored: "sent" |
| `msg {chat_id, seq, from, ciphertext}` | server → recipient | Delivery |
| `delivered {chat_id, upto_seq}` | recipient → server | Device has it; relayed to sender |
| `read {chat_id, upto_seq}` | recipient → server | User opened the chat; relayed to sender |
| `presence`, `typing` | both | Ephemeral, best-effort |

HTTP endpoints cover everything that isn't latency-critical:

| Endpoint | Purpose |
|---|---|
| `POST /v1/media` | Returns `media_id` and a presigned upload URL |
| `GET /v1/users/{id}/devices/keys` | Prekey bundles for each of a user's devices |
| `PUT /v1/devices/{id}/keys` | Upload signed prekey and a batch of one-time prekeys |
| `POST /v1/devices/link` | Register a companion device, signed by the primary |
| `GET /v1/sync?chat_id=&after_seq=` | Fetch messages missed in a gap |

## Data model

| Data | Store | Key | Why |
|---|---|---|---|
| Accounts, devices, identity public keys | MySQL/PostgreSQL, sharded by `user_id` | `user_id`, `(user_id, device_id)` | Small, relational, transactional |
| One-time prekeys | Same, or a KV store | `(user_id, device_id, key_id)` | Each key handed out once, deleted atomically |
| Groups, members | Relational, sharded by `group_id`, plus a `user_groups` index | `(group_id, user_id)` | Membership changes need transactions |
| Device route registry | Redis cluster | `device_id → gateway_id, conn_epoch` | Ephemeral, TTL refreshed by heartbeat |
| Device mailbox | Cassandra/ScyllaDB | partition `device_id`, clustering `inbox_seq` | Write-heavy, per-device scans, TTL |
| Chat sequence counters | Held by the chat's owner, persisted with each write | `chat_id → last_seq` | Single writer per chat |
| Presence | Redis | `user_id → status, last_seen` | Ephemeral |
| Media blobs | Object storage + CDN | `media_id` | Large, immutable, encrypted |

The mailbox is a queue per device: insert, read in order, delete on ack. That pattern produces tombstones in Cassandra, which slow reads if a partition accumulates many deletes. Mitigations: time-window compaction with TTLs, deleting acknowledged ranges instead of single rows, and keeping mailboxes short by delivering promptly. Discord has publicly described running its message store on Cassandra and later ScyllaDB, which is the relevant precedent if you need persistent history ([partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }})).

## High-level design

```text
   phones, web, desktop (each device = one connection)
           |  TLS / Noise-encrypted long-lived connection
           v
   +----------------+                   +---------------------+
   | L4 load        |                   | Key service         |
   | balancer       |                   | (prekey bundles)    |
   +-------+--------+                   +---------------------+
           v
   +----------------+  register/lookup  +---------------------+
   | Gateways       |<----------------->| Route registry      |
   | (hold sockets) |                   | (Redis, TTL)        |
   +-------+--------+                   +---------------------+
           | send / ack (RPC, routed by chat_id)
           v
   +----------------+  append    +-------------+   +---------------+
   | Chat service   |----------->| Mailboxes   |   | Group service |
   | (owner of each |            | (ScyllaDB)  |   | (members,     |
   |  chat: seq)    |            +-------------+   |  versions)    |
   +---+--------+---+                              +---------------+
       |        | recipient device offline
       |        v
       |   +----------+   APNs / FCM
       |   | Push svc |-------------> device wakes and reconnects
       |   +----------+
       | recipient device online: forward to its gateway
       v
   recipient's gateway --> recipient device

   media: client --presigned PUT--> object storage --> CDN --> recipient
```

### 1:1 messaging flow

1. Alice's client encrypts the message once per Bob's device and once per each of Alice's other devices, then sends a `send` frame with a `client_msg_id`.
2. Her gateway forwards it to the chat service instance that owns the chat; ownership is assigned by [consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}) on `chat_id`.
3. The owner assigns the next per-chat `seq`, appends one envelope to each recipient device's mailbox with a quorum write, and only then acks Alice: one tick.
4. For each recipient device it looks up the route registry. Online devices get the message pushed through their gateway; offline devices get a push notification.
5. Bob's device acks; the server deletes the mailbox entry and relays a `delivered` receipt to Alice's devices: two ticks.

## Deep dives

### Persistent connections

Gateways are the only stateful tier facing users. Each holds hundreds of thousands of sockets, so they run on an event loop or lightweight processes (Erlang, Go, Netty), not a thread per connection ([non-blocking I/O]({{ '/backend/non-blocking-io/' | relative_url }})). An L4 load balancer spreads new connections by least-connections; L7 adds cost and nothing useful for opaque binary frames.

On connect, the gateway authenticates the device and writes `device_id → gateway_id, conn_epoch` to the registry with a TTL. Heartbeats refresh it. The epoch prevents a stale gateway from overwriting a newer registration after a fast reconnect.

Mobile networks make this hard. Carrier NATs drop idle TCP mappings after a few minutes, so clients ping often enough to keep the mapping without draining the battery. iOS suspends background apps and kills their sockets, so for backgrounded devices the push notification channel (APNs, FCM) is the delivery path.

Two operational hazards: when a gateway dies, its 500,000 clients reconnect at once, so clients use jittered exponential backoff ([retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})). Deploys drain gateways gradually, telling a fraction of clients per second to reconnect elsewhere instead of dropping everyone.

### Group messaging

With end-to-end encryption, the naive group design is for the sender to encrypt the message separately for every member's device: a 500-member group with three devices each costs 1,500 encryptions and 1,500 uploads per message. The Signal protocol's **sender keys** avoid that. Each member generates a sender key for the group and distributes it once to every other member over the pairwise encrypted sessions. Afterwards, a group message is encrypted once and uploaded once; the server fans the single ciphertext out to each member device's mailbox.

Server fan-out runs on workers: the chat owner assigns `seq` and enqueues one fan-out task; workers write mailbox entries and push to online devices in parallel. When a member leaves or is removed, the remaining members rotate their sender keys so the departed member can't read new messages. Membership changes go through the group service transactionally and bump a group version that clients check.

### Delivery and read receipts

Receipts are three distinct facts: **sent** (the server stored it durably), **delivered** (a recipient device acked it), and **read** (the user viewed the chat). Each is a small message routed to all of the sender's devices.

Make them cumulative: "delivered up to seq 1093 in chat g:8812" covers every earlier message, so a user opening a chat with 200 unread messages produces one read receipt, not 200. In groups, receipts are per member, and the sender's client shows two ticks once every member has delivered. Users who disable read receipts simply stop sending `read` frames. Delivery on the wire is at-least-once; clients dedupe by `(chat_id, seq)` so receipts and messages are idempotent ([delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }})).

### Offline delivery

Every message is written to the recipient device's mailbox before the sender is acked, whether the device is online or not. Online delivery is an optimization on top of the mailbox, not a separate path, which makes crash recovery simple: anything not acked by the device is still in the mailbox.

When a device is offline, the push service sends an APNs or FCM notification. The payload carries no plaintext; either it is a generic "new message" wake-up, or it carries the ciphertext for an on-device extension to decrypt. On reconnect, the device sends its last acked mailbox position and the server streams the backlog in pages, deleting acknowledged ranges. Undelivered messages expire after a fixed window; WhatsApp publicly states 30 days.

After a regional outage, millions of devices reconnect with full mailboxes. Drain rate-limited per gateway and prioritize message frames over presence and typing ([backpressure]({{ '/backend/backpressure/' | relative_url }})).

### Message ordering

Client clocks are unreliable, so timestamps are for display only. Order comes from a per-chat sequence assigned by the chat's single owner. Concurrent sends from two group members are ordered by arrival at the owner, and every device shows the same order. There is no global order across chats, and none is needed ([ordering and dead-letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }})).

The owner must never issue a duplicate sequence number after failover. It persists `last_seq` with each mailbox write, and ownership is a lease with a fencing token, so a paused old owner can't keep assigning numbers ([distributed locking]({{ '/backend/distributed-locking/' | relative_url }})). Clients buffer out-of-order arrivals, detect gaps, and fetch missing ranges through `/v1/sync`. A sender's retry with the same `client_msg_id` gets the same `seq` back.

```python
import random


class ChatOwner:
    """Server side: the one owner of a chat assigns gapless sequence numbers."""

    def __init__(self):
        self.last_seq = {}           # chat_id -> last assigned seq
        self.accepted = {}           # (chat_id, client_msg_id) -> seq

    def accept(self, chat_id, client_msg_id):
        key = (chat_id, client_msg_id)
        if key in self.accepted:     # sender retried after a lost ack
            return self.accepted[key]
        seq = self.last_seq.get(chat_id, 0) + 1
        self.last_seq[chat_id] = seq
        self.accepted[key] = seq     # real system: durable write, then ack
        return seq


class DeviceInbox:
    """Client side: drop duplicates, show in seq order, ack cumulatively."""

    def __init__(self):
        self.shown_upto = {}         # chat_id -> highest contiguous seq shown
        self.buffer = {}             # chat_id -> out-of-order messages
        self.timeline = []

    def receive(self, chat_id, seq, body):
        upto = self.shown_upto.get(chat_id, 0)
        if seq > upto:
            pending = self.buffer.setdefault(chat_id, {})
            pending[seq] = body
            while upto + 1 in pending:
                upto += 1
                self.timeline.append((chat_id, upto, pending.pop(upto)))
            self.shown_upto[chat_id] = upto
        return upto                  # "delivered up to" receipt for sender

    def missing(self, chat_id):
        pending = self.buffer.get(chat_id, {})
        upto = self.shown_upto.get(chat_id, 0)
        top = max(pending, default=upto)
        return [s for s in range(upto + 1, top) if s not in pending]


if __name__ == "__main__":
    owner = ChatOwner()
    sends = ([("alice", f"a{i}") for i in range(30)]
             + [("bob", f"b{i}") for i in range(30)])
    random.seed(7)
    random.shuffle(sends)            # two members typing concurrently
    log = [(owner.accept("g1", mid), mid) for _, mid in sends]
    assert owner.accept("g1", log[5][1]) == log[5][0]   # retry: same seq
    assert [s for s, _ in log] == list(range(1, 61))

    # At-least-once network: duplicates and reordering.
    wire = log + random.sample(log, 15)
    random.shuffle(wire)
    inbox = DeviceInbox()
    for seq, mid in wire[:-20]:
        inbox.receive("g1", seq, mid)
    held_back = wire[-20:]
    gaps = inbox.missing("g1")
    assert all(seq in {s for s, _ in held_back} for seq in gaps)
    for seq, mid in held_back:       # client fetched the gaps via sync
        ack = inbox.receive("g1", seq, mid)
    assert ack == 60 and inbox.missing("g1") == []
    assert [s for _, s, _ in inbox.timeline] == list(range(1, 61))
    assert [m for _, _, m in inbox.timeline] == [m for _, m in log]
    print("gaps before sync:", gaps[:5], "final ack:", ack)
```

### End-to-end encryption

WhatsApp's encryption is based on the Signal protocol and is documented in its public security whitepaper. The concepts that matter for the backend:

- **Keys per device.** Each device has a long-term identity key pair, a medium-term signed prekey, and a batch of one-time prekeys. Only public keys are uploaded.
- **Asynchronous session setup (X3DH).** To message a device that is offline, the sender fetches its prekey bundle from the key service and combines several Diffie-Hellman results into a shared secret. The recipient derives the same secret from the first message. Signal has since added a post-quantum variant (PQXDH); the server's role is the same.
- **Double Ratchet.** Every message advances a key derivation chain (forward secrecy: old keys can't be recomputed), and each reply round adds a fresh Diffie-Hellman step (post-compromise security: a stolen key stops working).
- **Sender keys** for groups, as above.
- **Verification.** Users can compare safety numbers out of band; clients warn when a contact's identity key changes.

The server is a key directory and an untrusted router. It still sees metadata: who messages whom, when, sizes, IP addresses. The key service must hand out each one-time prekey at most once, and run out gracefully (fall back to the signed prekey alone) when a device hasn't replenished them. Encryption in transit and at rest is covered in [encryption]({{ '/backend/encryption/' | relative_url }}).

### Media via object storage

Media never flows through the chat path. The sender's client compresses the file, generates a thumbnail locally (the server can't, since it never sees plaintext), encrypts the file with a fresh random key, and uploads the ciphertext with a presigned URL directly to object storage. The chat message then carries the media ID, the key, and a hash of the ciphertext inside the encrypted envelope. Recipients download through the CDN, verify the hash, and decrypt.

Consequences: the CDN caches only ciphertext; cross-user dedupe is impossible because every upload uses a different key; forwarding can reuse the original blob and key without a re-upload; large videos use chunked, resumable uploads (see [Design a YouTube backend]({{ '/backend/design-youtube/' | relative_url }})). Lifecycle rules delete blobs after a retention window, since devices keep their own copies.

### Presence

Naive presence broadcasts every online/offline transition to every contact: a user with 500 contacts produces 500 updates each time a flaky mobile connection drops. Instead, make presence pull-plus-subscribe. A client subscribes to a contact's presence only while that chat is open, and fetches "last seen" on demand. Gateways own the heartbeat; Redis holds `status, last_seen` with a TTL of about twice the heartbeat interval. Transitions are debounced by a few seconds so network flaps don't flicker.

Typing indicators are pure ephemera: forwarded only to subscribed, online devices, rate-limited, never stored, and the first thing shed under load. Privacy settings (hide last seen) are enforced server-side before anything is sent.

### Multi-device

Each linked device is a full endpoint with its own identity keys and its own mailbox; there is no shared secret copied between devices. WhatsApp has publicly described this approach for its multi-device release. The primary phone signs the account's device list, so senders can verify that a new device was really added by the account owner.

The cost is client fan-out: a 1:1 message from a user with 3 devices to a user with 4 devices produces 4 + 2 = 6 ciphertexts. That's why the number of linked devices is capped. Groups absorb this through sender keys, distributed once per device. Linking a device shows a QR code, the primary signs the new device's identity key, and recent history is transferred from the phone to the new device end-to-end encrypted. Unlinking removes the device from the signed list; contacts' clients stop encrypting to it.

## Scaling and bottlenecks

**At 10×.** Gateways scale linearly. The route registry grows to billions of entries (about 100 bytes each, hundreds of GB); shard Redis by `device_id`. Mailbox writes reach tens of millions per second: batch writes per gateway, use large ScyllaDB clusters, and keep partitions small by draining promptly. Some designs skip the mailbox write when the recipient acks within milliseconds, but that opens a window where the sender saw "sent" for a message held only in memory. Keep the durable write unless you have a replicated in-memory log.

**Large groups.** Fan-out per message grows with membership. Fan-out workers scale horizontally; per-member receipts are what get expensive, so large groups aggregate them. Beyond a few thousand members, the product becomes a broadcast channel: one writer, fan-out on read like a feed (see [Design an Instagram backend]({{ '/backend/design-instagram/' | relative_url }})).

**At 100×, multi-region.** Devices connect to the nearest region. Each chat has a home region for sequencing, placed near most of its members; cross-region members pay one extra hop. Mailboxes live in the recipient's home region.

## Reliability and failure modes

| Failure | Behavior |
|---|---|
| Gateway crash | Clients reconnect with jitter elsewhere; unacked messages are still in mailboxes and are redelivered; clients dedupe |
| Chat owner crash | Lease expires, a new owner reads `last_seq` and continues; fencing blocks the old owner |
| Mailbox node loss | Quorum writes with replication factor 3 tolerate one node |
| Push provider outage | Messages wait in mailboxes; delivered when the app opens |
| Region outage | Clients reconnect to another region; mailboxes replicated asynchronously, so some duplicates, no loss of acked messages |
| Overload | Shed typing and presence first, delay receipts, protect message delivery |

Key metrics: end-to-end delivery latency, mailbox depth per device, reconnect rate, one-time prekey stock per device, and push notification failure rate. Tracing frames through gateway and chat service is covered in [distributed tracing and OpenTelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}).

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| History | Transient mailboxes | Yes | Privacy, orders of magnitude less storage; devices own history |
| | Server-side history | No | Needed for server search; at odds with E2EE |
| Ordering | Per-chat server sequence | Yes | Same order on all devices, simple gap detection |
| | Client timestamps | No | Clock skew |
| Group encryption | Sender keys + server fan-out | Yes | Encrypt once per message |
| | Pairwise per member | No | Encryptions grow with members × devices |
| Multi-device | Per-device keys, client fan-out | Yes | No shared secrets to copy |
| Presence | Subscribe while chat open | Yes | Avoids broadcasting to every contact |
| Transport | L4 LB + stateful gateways | Yes | Long-lived opaque connections |

## Interview follow-ups

**How do you guarantee no message is lost?**
Persist to the recipient device's mailbox with a quorum write before acking the sender, delete only after the device acks, and let senders retry with the same `client_msg_id`, which the owner maps back to the same `seq`.

**Why not a Kafka topic or partition per user?**
Kafka isn't designed for billions of topics, and per-user partitions would be absurdly small. A wide-column mailbox keyed by device gives the same append-and-drain semantics at that cardinality ([Kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }})).

**How do you deliver to a backgrounded iOS app?**
Through APNs. The notification wakes the app or a notification service extension, which fetches or decrypts the message. The server never puts plaintext in the push payload.

**How would you add message search?**
Not on the server under E2EE. Build an index on each device. Server-side search requires server-readable history, which is a different product decision.

**How does "delete for everyone" work?**
It is a new message referencing the original ID. Recipient clients hide the original when they process it; offline devices apply it when they catch up. It is best-effort, which is why products limit the time window.

**How do you handle a contact's key change?**
The client detects the new identity key on the next session setup, shows a security notice, and lets the user re-verify the safety number. The server just serves the new bundle.

**What breaks first if a celebrity joins a 1,000-member group that's very active?**
Fan-out and per-member receipts for that chat. Workers absorb fan-out; aggregate receipts and rate-limit typing for large groups.
