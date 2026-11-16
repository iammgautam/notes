---
title: Streaming LLM APIs with FastAPI
section: ai-stack
order: 7
phase: "Phase 1 · FastAPI"
summary: Stream LLM tokens and tool events over SSE and WebSockets, handle disconnects, timeouts and backpressure, and test the streams.
---

A finance assistant that shows nothing for eight seconds feels broken, even if the answer is right. Streaming puts the first token on screen in a few hundred milliseconds. After this note you can stream tokens from a LangChain model over Server-Sent Events, use FastAPI's built-in SSE support, send structured tool events alongside tokens, run a cancellable chat over WebSockets, enforce timeouts, stop work when the client leaves, and test all of it without a live server. In the job this is the API layer in front of every RAG answer and agent run.

## Prerequisites

[Async and performance]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}) and [security]({{ '/ai-stack/fastapi-security/' | relative_url }}). The LangChain side is covered in [models, prompts and structured output]({{ '/ai-stack/langchain-models-prompts-structured-output/' | relative_url }}).

## Pick a transport

| Transport | Direction | Use for | Watch out for |
|---|---|---|---|
| Chunked `StreamingResponse` | server to client | plain text, NDJSON exports | no event types or reconnect semantics |
| Server-Sent Events | server to client | token streaming, progress, tool events | proxies that buffer; browser `EventSource` is GET-only |
| WebSocket | both ways | chat with cancel, voice, collaborative sessions | auth, sticky connections, more state to manage |

Default to SSE: it is plain HTTP, so auth headers, load balancers and logging keep working. Use WebSockets when the client must talk while the server streams, for example to cancel.

```text
browser ──POST /chat/stream──► FastAPI route
   ▲                              │ async for chunk in model.astream()
   │  event: token                ▼
   │  data: {"text": "Net"}    LLM provider (streams chunks)
   │  ...
   │  event: done
   └──────── text/event-stream, one HTTP response, kept open
```

## The SSE wire format

An SSE response is `Content-Type: text/event-stream` and a sequence of events. Each event is a few `field: value` lines and ends with a blank line:

```text
event: token
id: 7
data: {"text": " days"}

: keep-alive comment, ignored by clients

event: done
data: {"chunks": 23}

```

- `data:` carries the payload. Send JSON, not raw text: a token containing a newline would otherwise split into two `data:` lines.
- `event:` names the type, so the client can route tokens, tool events and errors differently.
- `id:` lets a reconnecting browser send `Last-Event-ID`; `retry:` sets the reconnect delay in milliseconds.
- Lines starting with `:` are comments. Send one every 15 seconds or so during long tool calls so proxies and load balancers do not close an idle connection.

Two headers matter: `Cache-Control: no-cache`, and `X-Accel-Buffering: no` so nginx does not hold the stream until it ends.

## Hand-rolled SSE from a LangChain model

Every LangChain chat model has `astream()`, which yields `AIMessageChunk` objects. Wrap it in an async generator that formats events and return a `StreamingResponse`. The model comes from a dependency, so tests and environments can swap it.

```python
import json
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field


def get_model() -> BaseChatModel:
    # real: return init_chat_model("openai:gpt-4.1-mini", timeout=30, max_retries=2)
    return GenericFakeChatModel(messages=iter([
        "Net-30 means payment is due 30 days after the invoice date."
    ]))


def sse(data: dict, event: str | None = None, id: str | None = None) -> str:
    lines = [f"event: {event}"] if event else []
    if id:
        lines.append(f"id: {id}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"  # a blank line ends the event


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


app = FastAPI()


@app.post("/chat/stream")
async def chat_stream(
    body: ChatIn, request: Request, model: Annotated[BaseChatModel, Depends(get_model)]
) -> StreamingResponse:
    messages = [SystemMessage("You answer accounts payable policy questions."),
                HumanMessage(body.question)]

    async def events():
        n = 0
        try:
            async for chunk in model.astream(messages):
                if await request.is_disconnected():
                    return  # stop paying for tokens nobody will read
                if chunk.content:
                    n += 1
                    yield sse({"text": chunk.content}, event="token", id=str(n))
            yield sse({"chunks": n}, event="done")
        finally:
            pass  # runs on completion, early return or cancellation: log usage here

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # tell nginx not to buffer
    })


def parse_sse(lines) -> list[dict]:
    events, current = [], {}
    for line in lines:
        if line == "":
            if current:
                events.append(current)
            current = {}
        elif not line.startswith(":"):  # ":" lines are comments / keep-alives
            field, _, value = line.partition(": ")
            current[field] = value
    return events


if __name__ == "__main__":
    client = TestClient(app)
    with client.stream("POST", "/chat/stream", json={"question": "What is net-30?"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.iter_lines())
    tokens = [json.loads(e["data"])["text"] for e in events if e["event"] == "token"]
    assert "".join(tokens).startswith("Net-30 means payment")
    assert events[-1]["event"] == "done"
    assert client.post("/chat/stream", json={"question": ""}).status_code == 422
    print("ok")
```

The `finally` block is where you record token usage, close upstream connections and write the audit log. It runs whether the stream completed, returned early or was cancelled.

### What happens when the client disconnects

Under Uvicorn, Starlette watches for the `http.disconnect` message while the response streams. When the browser tab closes, it cancels the streaming task, so your generator receives `CancelledError` at whatever `await` it is parked on, `finally` runs, and the upstream `astream` is closed. You do not need to poll for most cases. Use `request.is_disconnected()` when cancellation cannot reach you: a sync generator running in the threadpool, a CPU-heavy loop between awaits, or servers that report disconnects only when the next write fails.

> **Gotcha:** never swallow `CancelledError` (a bare `except:`, `except BaseException`, or catching it without re-raising). Catch it only to clean up, then re-raise, or the provider call keeps running and billing you for nobody.

## FastAPI's built-in SSE

Current FastAPI ships SSE support in `fastapi.sse`. Before it existed you used the third-party `sse-starlette` package or the hand-rolled version above. Now a path operation that is an async generator, declared with `response_class=EventSourceResponse`, streams whatever it yields:

- yield a `ServerSentEvent(event=..., data=..., id=...)` for full control; `data` is JSON-encoded (strings get quotes), and `raw_data` skips encoding;
- yield a plain dict or Pydantic model and it becomes the `data:` field, validated against the return annotation;
- FastAPI sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`, and sends a `: ping` comment when the generator is idle for 15 seconds.

A related default: a generator path operation **without** a `response_class` streams JSON Lines (`application/jsonl`), one JSON object per line. That is handy for exports of thousands of ledger rows.

## Structured streaming with tool events

Agent UIs show more than tokens: "looking up INV-001", then the result, then the answer. Design the stream as a small protocol with a closed set of event types: `tool_start`, `tool_end`, `token`, `error`, `done`. The client renders by type, and an `error` event lets you fail gracefully after the 200 status line has already been sent.

The example runs one tool-calling round: a non-streamed planning call that returns tool calls, the tool executions (each bracketed by events), then the streamed final answer. Timeouts wrap each `await` rather than the whole generator.

```python
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

IDLE_TIMEOUT_S = 20.0    # max gap between chunks from the provider
TOTAL_TIMEOUT_S = 120.0  # max wall time for one answer


@tool
def get_invoice_status(invoice_number: str) -> str:
    """Look up the payment status of a vendor invoice."""
    return {"INV-001": "paid on 2026-03-14"}.get(invoice_number, "not found")


TOOLS = {t.name: t for t in [get_invoice_status]}


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self  # a real model sends the tool schemas to the provider


async def get_model() -> AsyncIterator[BaseChatModel]:
    # real: yield init_chat_model("openai:gpt-4.1-mini", timeout=30, max_retries=2)
    yield FakeToolModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_invoice_status",
                                           "args": {"invoice_number": "INV-001"},
                                           "id": "call_1"}]),
        AIMessage(content="INV-001 was paid on 14 March 2026."),
    ]))


# a yield dependency holds its slot until the response finishes, so this
# caps concurrent streams per process; extra requests wait, without a thread
ModelDep = Annotated[BaseChatModel, Depends(get_model, max_concurrency=200)]


async def guarded(stream: AsyncIterator, deadline: float) -> AsyncIterator:
    """Re-yield items; raise TimeoutError on an idle gap or the overall deadline."""
    loop = asyncio.get_running_loop()
    it = aiter(stream)
    try:
        while True:
            async with asyncio.timeout_at(min(deadline, loop.time() + IDLE_TIMEOUT_S)):
                try:
                    item = await anext(it)
                except StopAsyncIteration:
                    return
            yield item  # never yield inside the timeout block
    finally:
        await it.aclose()


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


app = FastAPI()


@app.post("/agent/stream", response_class=EventSourceResponse)
async def agent_stream(body: ChatIn, model: ModelDep) -> AsyncIterator[ServerSentEvent]:
    deadline = asyncio.get_running_loop().time() + TOTAL_TIMEOUT_S
    llm = model.bind_tools(list(TOOLS.values()))
    messages = [HumanMessage(body.question)]
    try:
        async with asyncio.timeout_at(deadline):
            ai = await llm.ainvoke(messages)  # planning call: tool calls, not streamed
        messages.append(ai)
        for call in ai.tool_calls:
            yield ServerSentEvent(event="tool_start",
                                  data={"name": call["name"], "args": call["args"]})
            async with asyncio.timeout_at(deadline):
                output = await TOOLS[call["name"]].ainvoke(call["args"])
            messages.append(ToolMessage(output, tool_call_id=call["id"]))
            yield ServerSentEvent(event="tool_end",
                                  data={"name": call["name"], "output": output})
        async for chunk in guarded(llm.astream(messages), deadline):
            if chunk.content:
                yield ServerSentEvent(event="token", data={"text": chunk.content})
    except TimeoutError:
        yield ServerSentEvent(event="error", data={"code": "timeout",
                                                   "message": "model took too long"})
        return
    yield ServerSentEvent(event="done", data={"status": "complete"})


def read_events(client: TestClient, question: str) -> list[tuple[str, dict]]:
    out, event = [], None
    with client.stream("POST", "/agent/stream", json={"question": question}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                out.append((event, json.loads(line.removeprefix("data: "))))
    return out


class SlowModel(FakeToolModel):
    async def ainvoke(self, *args, **kwargs):
        await asyncio.sleep(1)  # simulates a provider that hangs


if __name__ == "__main__":
    client = TestClient(app)
    events = read_events(client, "Was INV-001 paid?")
    kinds = [e for e, _ in events]
    assert kinds[:2] == ["tool_start", "tool_end"] and kinds[-1] == "done"
    assert events[1][1]["output"] == "paid on 2026-03-14"
    answer = "".join(d["text"] for e, d in events if e == "token")
    assert answer == "INV-001 was paid on 14 March 2026."

    TOTAL_TIMEOUT_S = 0.05
    app.dependency_overrides[get_model] = lambda: SlowModel(messages=iter([]))
    assert read_events(client, "Was INV-001 paid?")[-1] == (
        "error", {"code": "timeout", "message": "model took too long"})
    app.dependency_overrides.clear()
    print("ok")
```

Design points:

- **Never put `yield` inside `asyncio.timeout()` or a task group.** The timeout cancels the task that is running the generator; if the generator is suspended at a `yield`, the cancellation lands in the consumer's code instead and the timeout never fires correctly (PEP 789 describes the problem). `guarded` times each `anext()` and yields outside the block.
- **Two budgets:** an idle timeout catches a provider that stalls mid-answer; a total deadline caps cost and holds the latency SLO. Also set the client's own `timeout` and `max_retries` on the model.
- **Errors after the first byte cannot change the status code.** Validate input, authenticate and check rate limits before the generator starts, so those failures are ordinary 4xx responses. Anything after that becomes an `error` event.
- **Tests override the model** with `app.dependency_overrides`, the same mechanism you use for databases and auth.
- LangGraph agents produce the same events via `stream_mode`; see [LangGraph streaming and deployment]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}).

## Backpressure and concurrency

Async generators are pull-based: Starlette asks for the next chunk only after the previous `send` completes, so a slow client slows the generator instead of filling memory. Keep it that way:

- Do not start a background task that pushes chunks into an unbounded `asyncio.Queue`. If you must decouple producer and consumer, use `asyncio.Queue(maxsize=...)`.
- Cap open streams. `Depends(get_model, max_concurrency=200)` on a yield dependency holds a slot for the whole response; extra requests wait without using a thread. To reject instead, check a counter and return 503 with `Retry-After`.
- Add per-user stream limits (one or two open streams) on top of the rate limits from the security note.
- Set proxy timeouts longer than your total deadline, or the proxy cuts the stream first. More in [backpressure]({{ '/backend/backpressure/' | relative_url }}).

## WebSockets for chat

Use a WebSocket when the client must talk mid-stream. The example keeps conversation history per connection, streams tokens from a task, and lets the client cancel the current answer without closing the socket.

```python
import asyncio
from contextlib import suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class SlowFakeModel(GenericFakeChatModel):
    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self._stream(messages, stop=stop, **kwargs):
            await asyncio.sleep(0.01)  # simulate provider latency per token
            yield chunk


def get_model() -> GenericFakeChatModel:
    return SlowFakeModel(messages=iter([
        "Vendor spend for Q1 was 4.2M, up 8 percent on Q4.",
        "The largest increase came from cloud hosting and audit fees, "
        "which together added 260K across the quarter, " * 5,
    ]))


app = FastAPI()


async def generate(ws: WebSocket, model, history: list) -> None:
    parts: list[str] = []
    try:
        async for chunk in model.astream(history):
            parts.append(chunk.content)
            await ws.send_json({"type": "token", "text": chunk.content})
        await ws.send_json({"type": "done"})
    finally:
        history.append(AIMessage("".join(parts)))  # keep partial answers in context


@app.websocket("/ws/chat")
async def chat(ws: WebSocket) -> None:
    if ws.query_params.get("token") != "demo-token":  # real: verify the JWT
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    model = get_model()
    history: list = [SystemMessage("You answer vendor spend questions.")]
    task: asyncio.Task | None = None
    try:
        while True:
            msg = await ws.receive_json()
            if msg["type"] == "message":
                if task and not task.done():
                    await ws.send_json({"type": "error", "code": "busy"})
                    continue
                history.append(HumanMessage(msg["text"]))
                task = asyncio.create_task(generate(ws, model, history))
            elif msg["type"] == "cancel" and task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await ws.send_json({"type": "cancelled"})
    except WebSocketDisconnect:
        pass
    finally:
        if task:
            task.cancel()  # client left: stop generating


if __name__ == "__main__":
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws/chat?token=wrong"):
            pass
        raise AssertionError("expected rejection")
    except WebSocketDisconnect as e:
        assert e.code == status.WS_1008_POLICY_VIOLATION

    with client.websocket_connect("/ws/chat?token=demo-token") as ws:
        ws.send_json({"type": "message", "text": "Q1 vendor spend?"})
        text = ""
        while (msg := ws.receive_json())["type"] == "token":
            text += msg["text"]
        assert msg["type"] == "done" and text.startswith("Vendor spend for Q1")

        ws.send_json({"type": "message", "text": "What drove it?"})
        assert ws.receive_json()["type"] == "token"
        ws.send_json({"type": "cancel"})
        while (msg := ws.receive_json())["type"] == "token":
            pass
        assert msg["type"] == "cancelled"
    print("ok")
```

What this shows:

- **Auth happens before `accept()`.** Browsers cannot set an `Authorization` header on a WebSocket handshake, so pass a short-lived token in the query string or the `Sec-WebSocket-Protocol` header, verify it as in the security note, and close with 1008 if it fails. Keep query-string tokens out of access logs.
- **Generation runs in a task** so the receive loop stays free to handle `cancel`. One answer at a time per socket; a second message gets a `busy` error.
- **The `finally` cancels the task** when the client disconnects, so the provider stream is closed.
- WebSockets pin a client to one process. History kept in memory is lost on deploys; persist it (see [chat history]({{ '/ai-stack/langchain-chat-history/' | relative_url }})) and plan for reconnects.

## Testing streams

- `TestClient.stream(...)` with `iter_lines()` reads SSE line by line; parse events and assert on the sequence, not on exact token boundaries, which vary by provider.
- `TestClient.websocket_connect(...)` drives WebSockets; `WebSocketDisconnect.code` tells you why the server closed.
- `httpx.AsyncClient` with `ASGITransport` buffers the whole response before returning, so it can check the final event list but cannot test incremental delivery or disconnects.
- Test timeouts by overriding the model with a slow fake and shrinking the budget, as in the tool-events demo. Measure time to first token under load against a real server.

## The real provider version (illustrative)

```python
# illustrative: needs OPENAI_API_KEY
from langchain.chat_models import init_chat_model

model = init_chat_model("openai:gpt-4.1-mini", temperature=0, timeout=30,
                        max_retries=2, stream_usage=True)


async def stream_answer(messages):
    full = None
    async for chunk in model.astream(messages):
        full = chunk if full is None else full + chunk  # chunks add up
        if chunk.content:
            yield chunk.content
    # last chunk carries usage when stream_usage=True: record cost per tenant
    usage = full.usage_metadata  # {"input_tokens": ..., "output_tokens": ...}
```

```nginx
# illustrative: nginx in front of the API
location /api/ {
    proxy_pass http://app:8000;
    proxy_http_version 1.1;
    proxy_buffering off;          # X-Accel-Buffering does this per response
    proxy_read_timeout 300s;      # longer than the total deadline
}
```

## Coming from Django

| Django | FastAPI |
|---|---|
| `StreamingHttpResponse` (async iterators since 4.2) | `StreamingResponse`, `EventSourceResponse`, generator routes |
| Django Channels consumers for WebSockets | `@app.websocket` routes, no extra server |
| Channels groups and Redis layer | your own pub/sub (Redis) when several processes must share a stream |
| Celery task plus a polling endpoint | SSE progress events instead of polling |

Under WSGI a streaming response pins a worker thread for the whole stream, which is why Django teams often avoided streaming. Under ASGI an idle stream costs a coroutine, so thousands of open streams per process are normal.

## Common mistakes

- **Sending raw text in `data:`.** A newline inside a token breaks the framing. JSON-encode every payload.
- **Proxy buffering.** Everything arrives at once at the end. Set `X-Accel-Buffering: no` or `proxy_buffering off`, and disable compression for `text/event-stream`.
- **Raising `HTTPException` inside the generator.** The 200 is already sent. Emit an `error` event and end the stream.
- **Assuming a yield dependency is still open while streaming.** Current FastAPI keeps it open until the response finishes (`scope="request"`); some older versions closed it first.
- **Blocking calls in the generator** (a sync SDK, `time.sleep`). They freeze every stream in the process. Use async clients.
- **No caps on concurrent streams.** One user opening 50 tabs can exhaust the provider rate limit for everyone.

## Practice tasks

1. Add `id:` to every event in the built-in SSE route and support `Last-Event-ID` by replaying from an in-memory buffer. Done: a test that reconnects with `Last-Event-ID: 3` receives events 4 onward only.
2. Emit a `usage` event before `done` with input and output token counts (use `usage_metadata` from the aggregated chunks; the fake model returns none, so make it optional). Done: the schema is documented in OpenAPI and the test asserts it is the second-to-last event.
3. Add a per-user limit of two concurrent streams that returns 429 for the third. Done: a test using `asyncio.gather` against `httpx.AsyncClient` shows the third request rejected.
4. Replace the single tool round with a LangGraph agent and map its stream to the same event protocol. Done: the client code does not change.

## Interview questions

**SSE or WebSockets for an LLM chat?**
SSE by default: plain HTTP, works with existing auth, proxies and observability, auto-reconnect in browsers. WebSockets when the client must send while the server streams (cancel, voice, collaborative editing).

**How does FastAPI detect a client disconnect during streaming?**
Starlette listens for `http.disconnect` and cancels the streaming task; the generator gets `CancelledError` at its current await and `finally` runs. `request.is_disconnected()` is a manual poll for code cancellation cannot reach.

**How do you report an error mid-stream?**
The status line is already sent, so emit a typed `error` event with a code and end the stream. Do all validation, auth and rate limiting before the first byte.

**Where do timeouts go?**
On each await: an idle timeout per chunk and an overall deadline, plus the client SDK's own timeout and retries. Never wrap a `yield` in a timeout scope.

**What is backpressure here?**
The consumer's pace controls the producer. Async generators are pull-based, so a slow client slows token consumption. Breaking that with an unbounded queue grows memory per stream.

**How do you test a streaming endpoint?**
`TestClient.stream` and `iter_lines`, parse events, assert the sequence and the final event. Override the model dependency with a fake, including a slow fake for timeout paths. Load test TTFT with a real server.


## Further reading

- FastAPI docs: "Server-Sent Events", "Custom Response - StreamingResponse", "WebSockets", "Testing WebSockets"
- Starlette docs: "Responses" (StreamingResponse)
- WHATWG HTML spec: "Server-sent events"
- LangChain docs: "Streaming" (chat model `astream`, `astream_events`)
- PEP 789: preventing task-cancellation bugs by limiting yield in async generators
- Notes: [real-time communication]({{ '/backend/real-time-communication/' | relative_url }}), [latency and cost]({{ '/llm/latency-and-cost/' | relative_url }})
