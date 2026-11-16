---
title: WebSockets, SSE and long polling
section: backend
order: 25
phase: "Phase 3 · API design"
summary: Push data to clients with WebSockets, Server-Sent Events or long polling, how each works on the wire, how to scale it, and which one to pick.
---

HTTP is client-initiated: the server can only answer. Notifications, chat, live dashboards, job progress and LLM token streams all need the server to speak first. There are three practical ways to do it in a browser: long polling, Server-Sent Events and WebSockets. The protocol is the easy part. The hard part is that each one turns a stateless request into a long-lived connection, which brings sticky state, proxy timeouts, cross-node fan-out, reconnect storms and per-connection memory.

```text
short polling  C: GET -> []   GET -> []   GET -> [m1]    fixed interval
long polling   C: GET ------(held up to ~30 s)------> [m1]
               C: GET ?cursor=m1 ---(held)----------> [m2, m3]
SSE            C: GET  Accept: text/event-stream
               S: 200  id:1 data:m1 ... id:2 data:m2 ...  (one response)
WebSocket      C: GET  Upgrade: websocket   S: 101 Switching Protocols
               C <========== frames in both directions ==========> S
```

## WebSockets

A WebSocket (RFC 6455) is a full-duplex, message-oriented channel over one TCP connection, started with an HTTP request and then switched to its own framing. Use it when the client also sends frequently: chat, collaborative editing, multiplayer state, trading screens.

### Handshake

The client sends an ordinary HTTP/1.1 GET asking to upgrade:

```http
GET /ws/notifications/ HTTP/1.1
Host: app.example.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
Origin: https://app.example.com

HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
```

`Sec-WebSocket-Accept` is `base64(SHA-1(key + fixed GUID))`. It proves the server really speaks WebSocket and that no cache replayed an old response; it is not authentication. After the 101, the socket carries frames, not HTTP.

Security lives in the handshake:

- **Check `Origin`.** Browsers attach cookies to WebSocket handshakes, and the same-origin policy does not block cross-site WebSocket connections. Without an origin check, any page a logged-in user visits can open an authenticated socket (cross-site WebSocket hijacking).
- **Authentication.** The browser API cannot set an `Authorization` header. Use the session cookie plus the origin check, or fetch a short-lived single-use ticket over REST and pass it in the query string (URLs end up in logs, so never a long-lived token).
- Subprotocols are negotiated with `Sec-WebSocket-Protocol`. WebSockets over HTTP/2 (RFC 8441) and HTTP/3 (RFC 9220) use extended CONNECT instead of `Upgrade`.

### Framing

Each message is one or more frames:

```text
 0               1               2               3
 FIN RSV1-3 opcode | MASK len7   | ext len (16 or 64 bit, if len7 = 126/127)
 masking key (4 bytes, client to server only)  | payload ...
```

- **Opcodes:** `0x1` text (must be UTF-8), `0x2` binary, `0x0` continuation, and control frames `0x8` close, `0x9` ping, `0xA` pong.
- **Length:** 7 bits up to 125; 126 means a 16-bit length follows; 127 means 64-bit.
- **Masking:** every client-to-server frame is XORed with a fresh random 4-byte key; server frames never are. This stops attacker-controlled bytes from appearing verbatim on the wire, which could poison misbehaving intermediary caches. A server must close the connection on an unmasked client frame.
- **Fragmentation:** a message can span frames (`FIN=0`, then continuations). Control frames carry at most 125 bytes, are never fragmented and may interleave with fragments.
- **Close:** a close frame carries a 2-byte code and optional reason. `1000` normal, `1001` going away, `1008` policy violation, `1009` message too big, `1011` server error; `4000` to `4999` are yours. `1006` means "closed without a close frame" and never appears on the wire.
- `permessage-deflate` (RFC 7692) compresses messages but keeps compression state per connection, which costs memory at scale.

The handshake and framing in stdlib Python (runnable):

```python
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed by RFC 6455
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def encode_frame(payload: bytes, opcode=TEXT, masked=True, fin=True) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    bit, n = (0x80 if masked else 0), len(payload)
    if n < 126:
        head.append(bit | n)
    elif n < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if not masked:  # server -> client frames are never masked
        return bytes(head) + payload
    key = os.urandom(4)  # client -> server frames always are
    return bytes(head) + key + mask(payload, key)


def decode_frame(buf: bytes):
    """Return (fin, opcode, payload, bytes_used) for one complete frame."""
    fin, opcode = bool(buf[0] & 0x80), buf[0] & 0x0F
    masked, n, i = bool(buf[1] & 0x80), buf[1] & 0x7F, 2
    if n == 126:
        n = struct.unpack_from("!H", buf, i)[0]
        i += 2
    elif n == 127:
        n = struct.unpack_from("!Q", buf, i)[0]
        i += 8
    key = buf[i:i + 4] if masked else None
    i += 4 if masked else 0
    payload = buf[i:i + n]
    return fin, opcode, mask(payload, key) if key else payload, i + n


if __name__ == "__main__":
    # The handshake example from RFC 6455 section 1.3
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    # Unmasked single-frame "Hello" from RFC 6455 section 5.7
    assert encode_frame(b"Hello", masked=False) == b"\x81\x05Hello"

    for size in (5, 300, 70_000):  # 7-bit, 16-bit and 64-bit lengths
        data = os.urandom(size)
        frame = encode_frame(data, BINARY)
        assert decode_frame(frame) == (True, BINARY, data, len(frame))
    assert len(encode_frame(b"hi")) == 2 + 4 + 2  # header + mask key + data

    close = encode_frame(struct.pack("!H", 1001) + b"going away", CLOSE)
    fin, op, body, _ = decode_frame(close)
    assert op == CLOSE and struct.unpack("!H", body[:2])[0] == 1001
    print("ok")
```

### Scaling: sticky sessions and pub/sub fan-out

A WebSocket is one TCP connection, so it stays on one server for its whole life without any configuration. Two things still need care.

**Sticky sessions.** Libraries such as Socket.IO start with, or fall back to, HTTP long polling, which spreads one session across many requests that must all hit the same node, so they need load balancer affinity. Plain WebSockets don't, and it's better to keep it that way: keep session state in Redis or the database so a reconnect can land on any node.

**Fan-out.** User A is connected to node 1 and user B to node 7. A message for B arrives at node 1, or at a REST endpoint or Celery task with no sockets at all. Nodes need a backplane:

```text
 API / Celery ----publish("user.42", msg)----+
                                             v
                                   +-------------------+
                                   | Redis pub/sub,    |
                                   | NATS or Kafka     |
                                   +-------------------+
                  subscribe "user.42" |       | subscribe "room.7"
                                      v       v
 [ws node 1]  [ws node 2]  ...  [ws node 7] --frames--> user 42
```

| Fan-out strategy | How | Trade-off |
|---|---|---|
| Broadcast | Every node receives every message, delivers to local sockets | Simple; cost grows with nodes times messages |
| Topic subscription | Nodes subscribe to channels of their connected users or rooms | Efficient; many subscriptions in the broker |
| Presence routing | Registry maps user to node; publish to that node's channel | Precise; registry must expire stale entries |

Redis Pub/Sub is fire-and-forget: a node that is disconnected or slow misses messages. For anything users must not miss, write to a durable store first (database, Redis Streams, Kafka) and use pub/sub only as the wake-up signal.

Operational facts that drive the design:

- **Split the tier.** Gateways only hold sockets; business logic stays in the stateless API tier, which deploys often without dropping connections.
- **Capacity is memory and file descriptors.** Each connection costs kernel buffers, TLS and app state, on the order of tens of KB. Raise `ulimit -n` and watch ephemeral ports on proxies that talk to a single backend address. Tuned nodes hold a lot: the Phoenix team publicly reported two million connections on one large machine.
- **Scaling out doesn't rebalance.** New nodes only get new connections. Cap connection age (close with `1001` after a few hours, jittered) so load spreads.

### Heartbeats

TCP does not tell you when the peer vanished: a laptop that sleeps or a phone that loses signal leaves a half-open connection that looks alive until a write fails. Linux TCP keepalive defaults to probing after two hours. Meanwhile load balancers and NATs drop idle connections, often after 60 seconds (the default for both nginx `proxy_read_timeout` and AWS ALB's idle timeout).

So send a heartbeat every 20 to 30 seconds and close the connection after about two missed replies. Servers use protocol pings; browsers answer pings automatically but JavaScript cannot send them, so client-side liveness checks need an application-level `{"type": "ping"}` message.

### Reconnection

The browser `WebSocket` API never reconnects by itself. The client must:

- **Back off with full jitter**, starting around 1 second and capped at 30 to 60 seconds, or every client hits the recovering node at once.
- **Resume from a cursor.** Each message carries a sequence number; on reconnect the client sends the last one it saw and the server replays the gap from a durable log. If the gap is older than retention, the server says "resync" and the client refetches state over REST.
- **Re-authenticate.** Tokens expire during long sessions; validate again on every reconnect, and close sockets when a user's access is revoked.
- **Dedupe.** Replays overlap; client message IDs make handling [idempotent]({{ '/backend/idempotency/' | relative_url }}).

## Server-Sent Events

SSE is a one-way stream from server to client over a plain HTTP response, defined in the WHATWG HTML standard and exposed in browsers as `EventSource`. Client-to-server traffic stays ordinary HTTP requests. It's the natural fit for notifications, feeds, progress bars and LLM token streaming, which is how major LLM APIs publicly stream their responses.

### Format

The response is `Content-Type: text/event-stream`, UTF-8 only. Each event is a set of `field: value` lines ended by a blank line:

- `data:` the payload; several `data` lines join with newlines.
- `event:` a type name (`addEventListener("progress", ...)`); the default is `message`.
- `id:` sets the last event ID that the browser remembers.
- `retry:` reconnection delay in milliseconds.
- Lines starting with `:` are comments, used as keep-alives.

`EventSource` reconnects automatically after a dropped connection. To make it stop, respond with `204 No Content`. It can't send custom headers, so authentication uses cookies, or a `fetch()`-based client that reads the response body as a stream. Under HTTP/1.1 browsers allow about six connections per origin shared by all tabs, so a few open tabs with SSE can starve normal requests; HTTP/2 multiplexes them and removes the problem.

An encoder and a minimal parser (runnable):

```python
def sse_event(data: str, event=None, event_id=None, retry_ms=None) -> str:
    lines = []
    if event:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines += [f"data: {line}" for line in data.split("\n")]
    return "\n".join(lines) + "\n\n"  # a blank line ends the event


def parse_sse(stream: str):
    """Minimal parser for the event-stream rules; returns (event, data, id)."""
    events, data, event, last_id = [], [], "message", ""
    for line in stream.split("\n"):  # real streams may also use \r\n or \r
        if line == "":  # dispatch
            if data:
                events.append((event, "\n".join(data), last_id))
            data, event = [], "message"
        elif line.startswith(":"):
            continue  # comment, used as a keep-alive
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
            elif field == "id":
                last_id = value  # sticks until changed: becomes Last-Event-ID
    return events


if __name__ == "__main__":
    stream = (
        sse_event("job started", event="progress", event_id=41, retry_ms=5000)
        + ": keep-alive\n\n"
        + sse_event('{"pct": 50}\n{"pct": 60}', event="progress", event_id=42)
        + sse_event("no id here")
        + "data: cut off mid-event"  # no blank line: never dispatched
    )
    assert parse_sse(stream) == [
        ("progress", "job started", "41"),
        ("progress", '{"pct": 50}\n{"pct": 60}', "42"),
        ("message", "no id here", "42"),
    ]
    print("ok")
```

### Last-Event-ID

When `EventSource` reconnects, it sends the last `id` it received in a `Last-Event-ID` request header. The server resumes from there, which gives SSE built-in gap-free delivery that WebSockets make you build yourself. It only works if events are replayable: give them monotonic IDs and keep them in a table or Redis Stream (`XRANGE stream (last_id +`) for a retention window. If the requested ID has aged out, send a `reset` event that tells the client to refetch state.

### Proxies and buffering

SSE breaks in transit more often than in code. Send `Cache-Control: no-cache`, and watch for:

- **nginx buffers proxied responses by default**, so events arrive in bursts or only when the response ends. Send `X-Accel-Buffering: no` from the app or set `proxy_buffering off` on that location.
- **Compression middleware** may buffer to compress efficiently. Exclude `text/event-stream`.
- **Idle timeouts** kill quiet streams at 60 seconds. Send a `:` comment every 15 to 30 seconds.
- **Worker model.** On WSGI, each open stream pins a sync worker for its lifetime; ten tabs can take down a ten-worker Gunicorn. Serve streams from an ASGI app with async generators.
- Some corporate proxies and antivirus products buffer regardless. Long polling is the fallback.

## Long polling

The client asks for updates after a cursor; the server holds the request until there is data or a timeout (25 to 30 seconds, below every idle timeout on the path), then responds. The client immediately asks again with the new cursor. Because the cursor is in each request, messages that arrive between polls are not lost.

```text
C: GET /updates?after=120   ...held...   200 [{"id":121}, {"id":122}]
C: GET /updates?after=122   ...held 25 s, nothing...   204 No Content
C: GET /updates?after=122   ...
```

- **Pros:** plain HTTP, works through every proxy and firewall, easy to authenticate and load balance, resumable by design.
- **Cons:** a full request and headers per batch, one held connection per client, and up to one round trip of extra latency after each response.
- **Server side:** hold requests on an async server, not sync workers. Wake waiters with Postgres `LISTEN/NOTIFY` or Redis pub/sub, then re-query by cursor; the notification is a hint, the table is the truth.
- **Public examples:** the Telegram Bot API's `getUpdates` takes a `timeout` for long polling, Amazon SQS's `ReceiveMessage` waits up to 20 seconds, and Consul's blocking queries wait on an index.

## Comparison and decision table

| | Long polling | SSE | WebSocket |
|---|---|---|---|
| Direction | Server to client (per response) | Server to client | Both |
| Transport | Repeated HTTP requests | One long HTTP response | Upgraded TCP connection |
| Data | Any HTTP body | UTF-8 text | Text and binary |
| Reconnect and resume | Built in via cursor | Built in via `Last-Event-ID` | You build both |
| Proxies and firewalls | Best | Good, watch buffering | Usually fine; some block upgrades |
| Per-message overhead | Full request | A few bytes | 2 to 14 bytes |
| Server model | Async held requests | Async streaming response | Stateful socket server |

| Use case | Pick | Why |
|---|---|---|
| Notifications, activity feeds | SSE | One-way, auto-resume, plain HTTP |
| LLM token streaming | SSE | One response per request, streamed |
| Background job progress | SSE or long polling | Low rate; simplest wins |
| Chat, presence, typing indicators | WebSocket | Frequent messages both ways |
| Collaborative editing, games | WebSocket | Low latency, binary, bidirectional |
| Locked-down corporate networks | Long polling | Works wherever HTTP works |
| Mobile app in the background | APNs or FCM push | The OS kills idle sockets |
| Service to service | gRPC streaming, webhooks or a broker | Browsers are not involved |

## In practice

### Django Channels and ASGI example

Django Channels runs consumers on an ASGI server (Daphne or Uvicorn) and uses a channel layer, usually Redis, as the cross-node fan-out backplane. The snippets are illustrative for Channels 4.

```python
# notifications/consumers.py
from channels.generic.websocket import AsyncJsonWebsocketConsumer

class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close()  # before accept(): rejects the handshake
            return
        self.group = f"tenant.{user.tenant_id}.user.{user.pk}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content):
        if content.get("type") == "ping":  # app-level heartbeat
            await self.send_json({"type": "pong"})

    async def notify(self, event):  # handles group_send(type="notify")
        await self.send_json(event["payload"])
```

```python
# config/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_app = get_asgi_application()  # initialize Django before other imports

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import path
from notifications.consumers import NotificationConsumer

application = ProtocolTypeRouter({
    "http": django_app,
    "websocket": AllowedHostsOriginValidator(  # the Origin check
        AuthMiddlewareStack(URLRouter([
            path("ws/notifications/", NotificationConsumer.as_asgi()),
        ]))
    ),
})

# settings.py
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [("redis", 6379)]},
    },
}
```

Publishing from a view or a Celery task, after the notification row is committed:

```python
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

def push(user, payload):
    group = f"tenant.{user.tenant_id}.user.{user.pk}"
    async_to_sync(get_channel_layer().group_send)(
        group, {"type": "notify", "payload": payload})
```

The channel layer delivers at most once and keeps nothing for offline users. Store the notification first, include its ID in the payload, and let a reconnecting client fetch everything after its last ID.

### SSE endpoint on ASGI (illustrative)

Django 4.2 and later accept async iterators in `StreamingHttpResponse` when served over ASGI:

```python
import asyncio
from django.http import StreamingHttpResponse

async def job_events(request, job_id):
    try:
        last_id = int(request.headers.get("Last-Event-ID", 0))
    except ValueError:
        last_id = 0

    async def stream():
        nonlocal last_id
        while True:
            events = await fetch_job_events(job_id, after=last_id)
            for e in events:
                last_id = e.id
                yield f"id: {e.id}\nevent: progress\ndata: {e.json}\n\n"
            if not events:
                yield ": keep-alive\n\n"
            await asyncio.sleep(2)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # nginx: don't buffer this response
    return response
```

### nginx for WebSockets and SSE (illustrative)

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    location /ws/ {
        proxy_pass http://asgi;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;   # default 60s drops quiet sockets
    }
    location /events/ {
        proxy_pass http://asgi;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

## Pitfalls

- **Connections die at exactly 60 seconds.** A proxy or load balancer idle timeout. Send heartbeats more often than the shortest timeout on the path, and raise the timeout for these routes.
- **SSE events arrive in bursts.** nginx or compression buffering. `X-Accel-Buffering: no`, and exclude the route from gzip.
- **Sync workers exhausted.** Each stream or held poll pins a WSGI worker. Serve them from ASGI.
- **No `Origin` check.** Cookie-authenticated sockets become a cross-site hijacking hole. Use `AllowedHostsOriginValidator` or the equivalent.
- **Messages lost during reconnects.** Pub/sub drops what nobody is subscribed to. Persist first, resume by ID.
- **Reconnect storm after a deploy.** Every client reconnects at once and the new nodes fall over. Jittered backoff on clients, gradual draining on servers.
- **Slow consumers.** A client on a bad network can't keep up, and its send buffer grows without bound. Cap per-connection queues and drop or disconnect slow clients (see [backpressure]({{ '/backend/backpressure/' | relative_url }})).
- **Stale permissions.** A user removed from a tenant keeps receiving events on a socket opened yesterday. Re-check on events and close on revocation.

## Interview questions

**WebSockets or SSE for a notification feed?**
SSE. The traffic is one-way, it's plain HTTP (simple auth, proxies, HTTP/2 multiplexing), and `Last-Event-ID` gives resumption for free. Client actions such as "mark as read" are ordinary REST calls. Pick WebSockets when the client also sends at high frequency.

**How do you deliver a message to a user connected to a different server?**
A backplane. Gateway nodes subscribe to topics (per user, room or tenant) on Redis pub/sub, NATS or Kafka, or a presence registry maps users to nodes. The producer stores the message durably, then publishes; the node holding the socket delivers it.

**How would you scale WebSockets to millions of connections?**
A dedicated, horizontally scaled gateway tier tuned for file descriptors and memory, a pub/sub backplane for fan-out, a stateless API tier for logic, connection age caps for rebalancing, and clients with jittered reconnects and resume cursors. Capacity planning is connections per node times nodes, with headroom for a node's clients reconnecting elsewhere.

**Why do you need heartbeats when TCP has keepalive?**
Keepalive defaults to two hours and isn't visible to the application, while proxies cut idle connections after about a minute and half-open connections waste server resources. Application or protocol pings every 20 to 30 seconds keep the path open and detect dead peers.

**How do you avoid losing messages during a reconnect?**
Give messages monotonic IDs in a durable log, have the client send the last ID it processed, replay the gap, and fall back to a full resync if the gap is past retention. SSE has this built in with `Last-Event-ID`.

## Further reading

- RFC 6455, *The WebSocket Protocol*; RFC 8441, *Bootstrapping WebSockets with HTTP/2*.
- WHATWG HTML Living Standard, "Server-sent events" section.
- Django Channels documentation: "Consumers", "Channel Layers" and "Deploying".
- Ilya Grigorik, *High Performance Browser Networking*, chapters on WebSocket and Server-Sent Events.
- nginx documentation, "WebSocket proxying" and `ngx_http_proxy_module` (`proxy_buffering`, `proxy_read_timeout`).
