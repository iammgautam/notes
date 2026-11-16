---
title: Design a real-time chat system
section: backend
order: 87
phase: "Phase 10 · System design case studies"
summary: "Slack-style channels and DMs: WebSocket gateways, per-channel sequence IDs, fan-out, presence, unread counts, history, search and scaling connections."
---

A workplace chat system (Slack, Teams, Discord) looks like a messaging app, but its center of gravity is different. Channels have hundreds or tens of thousands of members, history lives on the server forever and must be searchable, and every client wants to know exactly which channels have unread messages and mentions. That pushes the design toward a durable per-channel log that many readers share, rather than per-device delivery queues. Compare it with [the WhatsApp design]({{ '/backend/design-whatsapp/' | relative_url }}) as you read.

## Requirements

### Clarifying questions

- Workspace-based and multi-tenant? Yes: users belong to workspaces; channels belong to workspaces.
- Channel sizes? Most under 50 members, a long tail up to 100k (company-wide announcements).
- Message history? Persistent and searchable on the server, subject to workspace retention policies.
- Multi-device? Desktop, web and mobile at once; read state syncs across them.
- Threads, reactions, edits, deletes? Yes, briefly. Files and calls are out of scope.

### Functional

- Public and private channels, DMs and group DMs.
- Send and receive messages in real time; edits, deletes, reactions, threads.
- History with infinite scroll; jump to a message.
- Unread state and mention counts per channel, synced across devices.
- Presence (active, away) and typing indicators.
- Search over messages the user can see.

### Non-functional

- 20M DAU, 10M concurrent connections at peak (desktop clients stay open all day).
- 800M messages/day; send-to-receive p99 under 500 ms in-region.
- Per-channel total order; no lost acknowledged messages; no duplicates shown.
- 99.99% availability for send and receive.

### Out of scope

File storage, voice and video, end-to-end encryption, bots and app platform, compliance export.

## Capacity estimates

```text
Messages     800M/day / 86,400 s         ≈ 9,300/s avg, ~46k/s peak (5x)
Deliveries   ~20 online recipients/msg    ≈ 190k/s avg, ~1M/s peak
Storage      ~500 B/msg incl. metadata    ≈ 400 GB/day ≈ 146 TB/year
             x3 replication               ≈ 440 TB/year
Connections  10M at peak; ~100k per gateway node (conservative)
             → ~100 nodes, ~200 with headroom for failover and deploys
Egress       1M frames/s x ~500 B         ≈ 500 MB/s
Heartbeats   10M connections / 30 s       ≈ 330k/s (kept in memory)
Memberships  20M users x ~200 channels    = 4B rows x ~50 B ≈ 200 GB
```

Writes are moderate; the load is fan-out, connection count and presence chatter.

## API design

REST for request-response work, a WebSocket for events.

```http
POST /api/channels/C42/messages
Content-Type: application/json

{"client_msg_id": "9b1e-4c7a", "text": "deploy at 5? @cy", "thread_seq": null}
```

```json
{"channel_id": "C42", "seq": 81234, "message_id": "1730000000123456789",
 "ts": "2026-11-16T10:02:11.481Z"}
```

| Endpoint | Purpose |
|---|---|
| `GET /api/channels/{id}/messages?before_seq=&limit=50` | History page, newest first |
| `GET /api/channels/{id}/messages?around_seq=` | Jump to a message |
| `POST /api/channels/{id}/read` `{seq}` | Mark read up to seq |
| `GET /api/boot` | Channels, unread and mention counts, user prefs |
| `GET /api/search?q=&in=&from=` | Message search |

WebSocket frames:

```json
{"type": "message", "channel_id": "C42", "seq": 81234, "user": "U7", "text": "..."}
{"type": "typing", "channel_id": "C42", "user": "U9"}
{"type": "presence_change", "user": "U9", "presence": "away"}
{"type": "channel_marked", "channel_id": "C42", "seq": 81234}
```

Sending can also go over the socket; either way `client_msg_id` makes the send idempotent and the ack returns the assigned `seq`. Slack's public API similarly identifies a message by its channel plus a per-channel `ts`.

## Data model

| Data | Store | Why |
|---|---|---|
| Messages | Wide-column store (Cassandra/ScyllaDB) or sharded MySQL | Append-heavy, read by channel and range |
| Channels, memberships, read cursors | Sharded relational DB by workspace | Transactions for joins, permissions, counts |
| Session routing, presence | Redis / in-memory on presence servers | Ephemeral, high churn |
| Search index | OpenSearch / Elasticsearch | Full-text with filters |

Both storage families are proven here: Discord has publicly described messages partitioned by `(channel_id, bucket)` in Cassandra and later ScyllaDB, and Slack has publicly described sharding MySQL with Vitess.

```sql
-- Illustrative CQL (Cassandra / ScyllaDB)
CREATE TABLE messages (
  channel_id    bigint,
  bucket        int,             -- seq / 10000 keeps partitions bounded
  seq           bigint,
  message_id    bigint,          -- Snowflake, globally unique
  sender_id     bigint,
  client_msg_id text,
  body          text,
  thread_seq    bigint,          -- parent message, null at top level
  edited_at     timestamp,
  deleted       boolean,
  PRIMARY KEY ((channel_id, bucket), seq)
) WITH CLUSTERING ORDER BY (seq DESC);
```

```sql
-- Illustrative relational tables, sharded by workspace_id
CREATE TABLE channels (
  workspace_id bigint NOT NULL,
  channel_id   bigint PRIMARY KEY,
  kind         text   NOT NULL,     -- public | private | dm | group_dm
  name         text,
  last_seq     bigint NOT NULL DEFAULT 0
);

CREATE TABLE memberships (
  channel_id    bigint NOT NULL,
  user_id       bigint NOT NULL,
  last_read_seq bigint NOT NULL DEFAULT 0,
  mention_count int    NOT NULL DEFAULT 0,
  notify        text   NOT NULL DEFAULT 'mentions',
  PRIMARY KEY (channel_id, user_id)
);
CREATE INDEX ON memberships (user_id, channel_id);
```

Bucketing by `seq / 10000` instead of by time keeps every partition the same size whether a channel is busy or quiet, and a page request always knows which partition holds a given seq.

## High-level design

```text
 clients (desktop, web, mobile)
   │ WebSocket (events)                │ HTTPS (boot, history, search)
   ▼                                   ▼
 L4 load balancer                  API servers ───► search cluster
   │                                   │
   ▼                                   ▼
 gateway servers ◄──── fan-out ──── channel servers ──► message store
  (sockets, auth,      by channel   (seq allocation,
   subscriptions)                    owned by consistent hash)
   │    ▲                               │
   │    └──── presence servers          ├──► Kafka ─► search indexer
   ▼                                    │           push notifications
 session registry (Redis)               │           mention counters
                                  membership DB (sharded SQL)
```

**Connect and boot.** The client authenticates and opens a socket to a gateway. It calls `/api/boot` for its channels and unread counts. The gateway subscribes, on the client's behalf, to the channel servers owning those channels.

**Send.** The channel server that owns the channel checks membership, deduplicates by `client_msg_id`, assigns the next `seq`, writes to the message store, then acks the sender and publishes the message to every gateway subscribed to the channel. Kafka carries the same event to the search indexer, the push service (mentions and DMs for users not active) and mention counters.

**Receive.** Each gateway forwards the message to its local connections that are members. Clients apply messages in `seq` order.

**Reconnect.** The client sends its last seen `seq` per open channel (or the server compares against stored cursors) and fetches gaps over REST before resuming live events.

## Deep dives

### WebSocket gateway

Gateways hold sockets and nothing else of value. They terminate TLS, authenticate the token, run heartbeats (ping every ~30 s; drop dead peers), and keep a bounded send buffer per connection. A client that cannot keep up is disconnected and told to resync rather than allowed to grow memory without limit. Gateways contain no business logic, which lets them be restarted, scaled and deployed independently.

Each gateway keeps a map of channel to local connections and subscribes to each channel once, however many of its users are in it. Slack has publicly described a similar split between gateway servers that hold connections and channel servers that own channels by consistent hashing. See [Real-time communication]({{ '/backend/real-time-communication/' | relative_url }}).

### Message storage and ordering per channel

Chat needs a total order per channel, not globally. Options:

| ID scheme | Ordering | Gap detection | Notes |
|---|---|---|---|
| Server timestamp | Breaks under clock skew and equal timestamps | No | Fine for display only |
| Snowflake ID | Roughly time-ordered, unique | No (gaps are normal) | Good global message id |
| Per-channel sequence | Exact order | Yes (seq 41 then 43 means 42 is missing) | Needs one allocator per channel |

Use both: a Snowflake `message_id` for global uniqueness and a per-channel `seq` for order, pagination and gap detection.

Allocation: the channel server that owns a channel (by consistent hashing on `channel_id`) holds `last_seq` in memory and assigns sequence numbers serially. Persist before acknowledging. On owner failover, the new owner reads the highest stored seq and continues, and writes use a conditional insert (or carry an ownership epoch) so a stale former owner cannot assign the same seq twice. A simpler start is the database itself: `UPDATE channels SET last_seq = last_seq + 1 ... RETURNING last_seq` serializes per channel through the row lock, which comfortably handles the few hundred messages per second even a very busy channel produces.

Edits, deletes and reactions are events too. Either give them their own seq in the channel's event stream, or keep message seqs for messages only and send edits as separate update events keyed by message seq. Either way a client catching up sees them. See [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}).

### Fan-out to online members

The message is written once to the channel's log. There is no per-user inbox copy. Real-time delivery fans out at the gateway level: the channel server sends one frame to each subscribed gateway, and each gateway delivers to its local members. A 50,000-member channel spread across 200 gateways costs 200 sends from the channel server, not 50,000.

| Model | Write cost | Read cost | Fits |
|---|---|---|---|
| Fan-out on write (per-user inbox or per-device queue) | One copy per recipient | Read own inbox | Small groups, E2EE, delete-after-delivery |
| Shared channel log (fan-out on read) | One copy | Read channel range | Large channels, server-side history and search |

Offline users get nothing pushed: they catch up from the log on reconnect. Mobile push notifications go only for DMs, mentions and keywords, per user preferences, and only if the user is not active on another device.

### Presence

Presence comes from connections and activity: a user is active if any device has a live connection and recent input, away otherwise. Keep it in memory on presence servers (or Redis keys with a short TTL refreshed by heartbeats), never in the primary database.

The expensive part is fan-out: 10M users each watched by hundreds of others. Controls:

- **Subscribe, do not broadcast.** Clients ask for presence only of users currently visible (open DM list, the member list on screen) and update subscriptions as the view changes.
- **Debounce.** Delay going offline by 30–60 s so a network blip or a laptop lid does not flap.
- **Batch.** Send presence changes to gateways in batches every second or so.
- **Cap.** Large channels do not show live presence for every member.

### Unread counts

Each membership row stores `last_read_seq`. Because seqs are gap-free per channel, the unread count is `channel.last_seq − last_read_seq`, with no counting query. Mentions are tracked separately (a counter incremented when a message mentions you, or rows per mention) and reset by reading.

- Mark-read must never move backward: a stale device sending an older seq is ignored (`SET last_read_seq = GREATEST(last_read_seq, $1)`).
- Mark-read is broadcast to the user's other devices as `channel_marked`, so the badge clears everywhere.
- Clients debounce mark-read while scrolling; the server writes only when the seq increases.
- Boot needs 200 channels' `last_seq` at once: serve them from a cache keyed by channel, updated by the channel server on every send.
- If edits or joins consume seqs, either count only message seqs or show a bold channel name instead of an exact number.

The demo below shows per-channel sequencing, idempotent sends, unread and mention counts, keyset pagination and client-side gap detection:

```python
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    channel: str
    seq: int
    sender: str
    text: str


class ChatStore:
    def __init__(self):
        self.last_seq = defaultdict(int)       # channel -> last assigned seq
        self.log = defaultdict(list)           # channel -> messages, seq order
        self.sent = {}                         # (channel, sender, client_id) -> seq
        self.members = defaultdict(set)
        self.read = {}                         # (channel, user) -> last_read_seq
        self.mentions = defaultdict(list)      # (channel, user) -> [seq]

    def join(self, channel, user):
        self.members[channel].add(user)
        self.read[(channel, user)] = self.last_seq[channel]

    def send(self, channel, sender, client_msg_id, text):
        if sender not in self.members[channel]:
            raise PermissionError("not a member")
        key = (channel, sender, client_msg_id)
        if key in self.sent:                   # retry: same seq, no duplicate
            return self.sent[key]
        self.last_seq[channel] += 1            # single allocator per channel
        seq = self.last_seq[channel]
        self.log[channel].append(Message(channel, seq, sender, text))
        self.sent[key] = seq
        for user in self.members[channel]:
            if f"@{user}" in text:
                self.mentions[(channel, user)].append(seq)
        self.mark_read(channel, sender, seq)   # your own message is read
        return seq

    def mark_read(self, channel, user, seq):
        key = (channel, user)
        self.read[key] = max(self.read[key], seq)

    def unread(self, channel, user):
        last_read = self.read[(channel, user)]
        mentions = sum(s > last_read for s in self.mentions[(channel, user)])
        return self.last_seq[channel] - last_read, mentions

    def history(self, channel, before_seq=None, limit=50):
        """Newest first. Gap-free seqs: seq n lives at index n - 1."""
        msgs = self.log[channel]
        end = len(msgs) if before_seq is None else before_seq - 1
        page = msgs[max(0, end - limit):end]
        cursor = page[0].seq if page and page[0].seq > 1 else None
        return list(reversed(page)), cursor


class ClientView:
    """Applies pushed messages in seq order and reports gaps to backfill."""

    def __init__(self, last_seq=0):
        self.last_seq, self.pending = last_seq, {}

    def on_message(self, msg):
        if msg.seq <= self.last_seq:
            return []                          # duplicate delivery
        self.pending[msg.seq] = msg
        applied = []
        while self.last_seq + 1 in self.pending:
            self.last_seq += 1
            applied.append(self.pending.pop(self.last_seq))
        return applied

    def gap(self):
        return (self.last_seq + 1, min(self.pending)) if self.pending else None


if __name__ == "__main__":
    s = ChatStore()
    for u in ("ana", "bo", "cy"):
        s.join("eng", u)
    first = s.send("eng", "ana", "c-1", "deploy at 5?")
    assert s.send("eng", "ana", "c-1", "deploy at 5?") == first == 1
    s.send("eng", "bo", "c-9", "@cy can you review?")
    s.send("eng", "ana", "c-2", "thanks")
    assert s.unread("eng", "cy") == (3, 1)
    assert s.unread("eng", "ana") == (0, 0) and s.unread("eng", "bo") == (1, 0)
    s.mark_read("eng", "cy", 2)
    s.mark_read("eng", "cy", 1)                # stale device: ignored
    assert s.unread("eng", "cy") == (1, 0)

    for i in range(120):
        s.send("eng", "bo", f"bulk-{i}", f"msg {i}")
    page, cursor = s.history("eng")
    assert [m.seq for m in page[:2]] == [123, 122] and cursor == 74
    page, cursor = s.history("eng", before_seq=cursor)
    assert (page[0].seq, page[-1].seq, cursor) == (73, 24, 24)
    page, cursor = s.history("eng", before_seq=cursor)
    assert (len(page), page[-1].seq, cursor) == (23, 1, None)

    view = ClientView(last_seq=123)
    msg = lambda seq: Message("eng", seq, "bo", "x")
    assert view.on_message(msg(125)) == [] and view.gap() == (124, 125)
    assert [m.seq for m in view.on_message(msg(124))] == [124, 125]
    assert view.on_message(msg(124)) == [] and view.gap() is None

    try:
        s.send("eng", "mallory", "c-x", "hi")
        raise AssertionError("non-member posted")
    except PermissionError:
        pass
    print("chat ok")
```

### Typing indicators

Typing is ephemeral and best-effort: never stored, never retried. The client sends `typing` at most every 3 s while the user types; the gateway forwards it to the channel's subscribers except the sender; receivers show it for about 5 s unless refreshed. Under load, typing events are the first thing dropped, and channels above a few hundred members do not broadcast them at all.

### History pagination

Keyset pagination on `seq`, newest first: `WHERE channel_id = ? AND bucket = ? AND seq < ? ORDER BY seq DESC LIMIT 50`, moving to the previous bucket when one runs out. Never use `OFFSET`: page 200 would scan and discard 10,000 rows, and new messages shift every offset. Jump-to-message loads a window around a seq (`around_seq`), then paginates both ways. Threads store replies with `thread_seq` set and are fetched by `(channel_id, thread_seq)` through a secondary table or index. Clients cache recent history locally and only fetch beyond it. See [Pagination]({{ '/backend/pagination/' | relative_url }}).

### Search

A Kafka consumer indexes messages into OpenSearch or Elasticsearch, routed by `workspace_id` so a workspace's documents live together and big tenants can get dedicated indexes. Edits and deletes produce index updates. Near-real-time refresh (around a second) is acceptable.

Permissions are the hard part: results must include only channels the user can read now. Filter at query time with the user's channel ids (terms lookup for users in thousands of channels), and re-check membership on the returned hits so a just-revoked private channel never leaks. Retention policies delete from both the message store and the index. See [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}).

### Scaling connection servers

- **Capacity per node** depends on memory per connection (TLS buffers, send queues: tens of kilobytes) and on event rate, not raw socket count. Event-loop servers routinely hold hundreds of thousands of idle connections; WhatsApp famously publicized around 2M connections on a single Erlang server back in 2012. Plan for 100k per node and measure.
- **Routing:** a session registry maps user to gateways (for user-targeted events like `channel_marked`); channel-to-gateway subscriptions handle channel events.
- **Deploys are the real risk.** Restarting a gateway disconnects 100k clients that reconnect at once. Drain gradually (stop accepting, ask clients to reconnect elsewhere over a few minutes), and clients reconnect with exponential backoff and jitter. A thundering reconnect can also overload boot; make boot cheap and cached. See [Graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).
- **Cells:** group workspaces into cells, each with its own gateways, channel servers and stores. A cell outage affects a slice of customers, and the largest enterprises can get their own cell.

### Contrast with the WhatsApp design

| Aspect | WhatsApp-style messenger | Slack-style workspace chat |
|---|---|---|
| History | On devices; server queues until delivered | On the server, permanent, searchable |
| Encryption | End-to-end; server cannot read content | Server-side; enables search and compliance |
| Delivery model | Per-recipient or per-device queues (fan-out on write) | Shared channel log (fan-out on read), live push to online members |
| Group size | Hundreds to about a thousand | Up to tens of thousands per channel |
| Read state | Per-message receipts (delivered, read) | Per-channel read cursor and unread counts |
| Tenancy | Global consumer graph | Workspaces with admin policies and retention |
| Offline | Messages wait in the queue | Client pulls from the log |

The shared log is why unread counts are a subtraction, search is possible, and a 50k-member channel costs one write per message.

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× messages | Channel servers for very hot channels; message store write load | Spread channel ownership; batch writes; more store nodes |
| 10× connections | Gateway count and reconnect storms on deploy | More gateways, cells, slow drains, jittered reconnect |
| Huge channels | Presence and typing fan-out; mention fan-out for `@channel` | Disable typing and live presence above a size; rate-limit `@channel`; lazy unread counts |
| 100× | Single-region workspace cells | Regional gateways connected to the workspace's home cell; read replicas of history per region |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Gateway crash | Its clients disconnect | Reconnect with backoff to another gateway; catch up by seq |
| Channel server failover | Brief send pause for its channels | New owner reloads `last_seq`; epoch fencing prevents duplicate seqs |
| Message published but gateway missed it | Client gap | Gap detection by seq, backfill over REST |
| Send retried after timeout | Possible duplicate | `client_msg_id` dedupe returns the original seq |
| Search indexer lag | Recent messages missing from search | Monitor consumer lag; search is eventually consistent |
| Presence store loss | Everyone appears away briefly | Rebuilt from heartbeats within a minute |
| Mass reconnect after an incident | Boot endpoint overload | Jittered reconnect, cached boot, load shedding |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| Shared channel log | Yes | Large channels, cheap writes, searchable history |
| Per-channel seq plus Snowflake id | Yes | Order and gap detection, plus global uniqueness |
| Gateway-level channel subscriptions | Yes | Fan-out cost bounded by gateways, not members |
| Presence by subscription | Yes | Broadcasting to every watcher does not scale |
| Persist typing events | No | Ephemeral by nature |
| OFFSET pagination | No | Slow and unstable; keyset on seq |
| Wide-column store for messages | Yes | Append-heavy, range reads per channel; sharded SQL also works |

## Interview follow-ups

**How do you guarantee message order in a channel?** One owner assigns a gap-free seq per channel and persists before acking; clients order by seq and backfill any gap.

**How does a 50k-member channel not melt the system?** One write to the log, one frame per subscribed gateway, local delivery by each gateway, no typing or live presence at that size, and push only for mentions.

**How are unread counts computed?** `last_seq − last_read_seq` per membership, with mention counters kept separately and read cursors that only move forward.

**A client was offline for a day. How does it catch up?** Boot returns channels with unread counts; the client fetches recent pages per open channel by seq and lazily loads others when opened.

**How do you avoid duplicate messages on retry?** The client sends a `client_msg_id`; the channel server stores it with the seq and returns the original seq on a retry.

**How would you design presence for 10M users?** In-memory presence servers fed by gateway heartbeats, subscription-based delivery for visible users only, debounced offline transitions and batched updates.

**Why not reuse the WhatsApp design?** Its per-device queues and end-to-end encryption fit small groups and device-held history. Workspace chat needs server-side searchable history and huge channels, which favor a shared log.
