---
title: WebSockets, SSE and long polling
section: networking
order: 8
summary: When to use polling, long polling, Server-Sent Events or WebSockets, how to scale persistent connections, and how SSE streams LLM tokens.
---

"Realtime" in a web app usually means the server needs to tell the client something without being asked: a job finished, a teammate edited a record, an LLM produced its next token. You have four tools, and the simplest one that meets the latency need is usually right. The hard part is never the protocol; it is holding many long-lived connections and fanning messages out across servers.

## How it works

### Short polling

Client asks every N seconds: `GET /v1/exports/exp_9`. Trivial, works everywhere, cacheable. Latency is up to N seconds, and most requests return "nothing new". Fine for job status with N = 2–5 s.

### Long polling

Client asks; server holds the request open until there is data or a timeout (say 25 s), then responds; client immediately asks again. Near-realtime over plain HTTP, but each waiting client ties up a connection and, in a sync worker model, a whole worker.

### Server-Sent Events (SSE)

One long-lived HTTP response with `Content-Type: text/event-stream`. The server writes events; the browser's `EventSource` parses them and **reconnects automatically**, sending `Last-Event-ID` so you can resume.

```text
id: 41
event: progress
data: {"pct": 40}

id: 42
event: done
data: {"url": "/files/exp_9.csv"}

```

- Server to client only. Text only (UTF-8).
- Plain HTTP: works through proxies, LBs, auth headers via cookies, HTTP/2 multiplexing.
- On HTTP/1.1 browsers allow ~6 connections per origin, so many SSE tabs can exhaust it; HTTP/2 removes that limit.

### WebSockets

An HTTP request upgrades to a full-duplex TCP channel:

```http
GET /ws HTTP/1.1
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13

HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
```

After that, both sides send framed messages (text or binary) at any time, with ping/pong frames for liveness. You own reconnection, message ordering, resume and auth.

| | Polling | Long polling | SSE | WebSocket |
|---|---|---|---|---|
| Direction | client pull | server push (emulated) | server to client | both |
| Latency | interval | low | low | lowest |
| Transport | HTTP | HTTP | HTTP | upgraded TCP |
| Auto reconnect | n/a | manual | built in | manual |
| Binary | yes | yes | no | yes |
| Proxy/LB friendliness | best | good | good | needs upgrade support |
| Use for | job status | legacy push | notifications, LLM streams, feeds | chat, collaborative editing, games |

## In practice

### SSE for LLM token streaming

LLM providers stream tokens as SSE; your backend relays them to the browser. Data flows one way, so SSE fits and is simpler than WebSockets.

```python
# Django 4.2+ async view, served by uvicorn/daphne (ASGI)
import json
from django.http import StreamingHttpResponse

async def chat_stream(request):
    prompt = json.loads(request.body)["prompt"]

    async def events():
        async for chunk in llm_client.stream(prompt):   # provider SDK
            yield f"data: {json.dumps({'token': chunk.text})}\n\n"
        yield "event: done\ndata: {}\n\n"

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"    # tell nginx not to buffer
    return resp
```

`EventSource` only does GET without a body, so for POST-based chat most frontends read the stream with `fetch`:

```javascript
const res = await fetch("/api/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ prompt }),
  signal: controller.signal,           // abort = stop generation
});
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  // split value on "\n\n", parse "data:" lines, append tokens
}
```

Details that matter:

- Disable proxy buffering (`proxy_buffering off;` in nginx or `X-Accel-Buffering: no`), or tokens arrive in one lump at the end.
- Raise proxy read timeouts above your longest generation, or send a comment line (`: ping`) every 15 s to keep idle connections alive.
- Detect client disconnects and cancel the upstream LLM call, or you pay for tokens nobody reads.
- Don't stream from a sync gunicorn worker: one slow 60 s generation pins a worker for 60 s. Use ASGI.

### WebSockets with Django Channels

```python
from channels.generic.websocket import AsyncJsonWebsocketConsumer

class ProjectConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope["user"]
        project_id = self.scope["url_route"]["kwargs"]["project_id"]
        if not user.is_authenticated or not await can_view(user, project_id):
            await self.close(code=4403)
            return
        self.group = f"project_{project_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def project_updated(self, event):        # type "project.updated"
        await self.send_json(event["payload"])

# From a view or Celery task, after commit:
# async_to_sync(get_channel_layer().group_send)(
#     f"project_{pid}", {"type": "project.updated", "payload": data})
```

## Scaling persistent connections

### The fan-out problem

```text
            +-----------+
 client A --| ws node 1 |--+
            +-----------+  |     +-------------------+
 client B --| ws node 2 |--+-----| Redis pub/sub /   |<-- Celery task
            +-----------+  |     | NATS / Kafka      |    publishes event
 client C --| ws node 3 |--+     +-------------------+
            +-----------+
```

Clients interested in project 42 are spread over many nodes. A publisher can't know which, so every node subscribes to a shared bus and forwards to its local connections. Channels' Redis channel layer does exactly this.

### Capacity

- Each connection is a file descriptor plus a few KB to tens of KB of memory. An async server handles 10k–100k idle connections per node; a sync worker model handles as many as it has workers. Raise `ulimit -n`.
- Connections are sticky by nature. Rolling deploys disconnect everyone on a node at once; clients must reconnect with **jittered backoff**, or you get a thundering herd.
- LBs need WebSocket support and an idle timeout above your ping interval (AWS ALB defaults to 60 s).
- Load balance on connection count, not request rate.

### Delivery guarantees

Pub/sub is fire-and-forget: a client that was reconnecting during a message misses it. For anything that matters, persist events with a sequence number and let clients resume with "send me everything after 1234" (SSE `Last-Event-ID` gives you this shape for free). Treat the realtime channel as a hint and the REST API as the source of truth.

## Pitfalls

- **Buffering proxies** turning a stream into one delayed response.
- **Sync workers holding connections**: long polling or streaming on gunicorn sync workers exhausts the pool fast.
- **Auth only at connect time**: a WebSocket opened before a user was removed from a project keeps receiving updates. Re-check on sensitive events or close connections on permission changes.
- **Cross-site WebSocket hijacking**: browsers send cookies on WebSocket upgrades from any origin, and CORS doesn't apply. Check the `Origin` header (`AllowedHostsOriginValidator` in Channels).
- **Publishing before commit**: a client gets the event, fetches, and the row isn't visible yet. Publish in `transaction.on_commit`.
- **No backpressure**: a slow client's send buffer grows without bound. Cap queues and drop or disconnect slow consumers.

## Rules of thumb

- Job progress every few seconds: polling. Server-to-client streams and notifications: SSE. Bidirectional, high-frequency: WebSocket.
- LLM token streaming: SSE over an async server, buffering off, cancel on disconnect.
- Fan out through Redis pub/sub or a broker; never assume the client is on the node that produced the event.
- Persist important events and support resume; realtime is a notification, not storage.
- Reconnect with exponential backoff and jitter.

## Interview angle

- **"WebSocket vs SSE?"** SSE is one-way server push over plain HTTP with built-in reconnect and resume; WebSocket is bidirectional over an upgraded connection and needs you to handle reconnect and resume. Use SSE unless the client must send frequent messages.
- **"How do you scale WebSockets to many servers?"** Async servers for many connections per node, a pub/sub backbone for fan-out, LB with upgrade support and long idle timeouts, jittered reconnects, and persisted events for missed-message recovery.
- **"How would you stream ChatGPT-style responses?"** Backend consumes the provider's streaming API and relays tokens to the browser as SSE from an async endpoint, with proxy buffering disabled and upstream cancellation on disconnect.
