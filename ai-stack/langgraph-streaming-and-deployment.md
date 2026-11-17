---
title: Streaming and deploying LangGraph apps
section: ai-stack
order: 27
phase: "Phase 4 · LangGraph"
summary: Stream LangGraph tokens and progress to FastAPI over SSE, then run it in production with Postgres checkpoints, limits, tracing and cost controls.
---

After this note you can stream an agent's tokens and progress to a browser, wrap a graph in a FastAPI service that copes with concurrent users, hung calls and mid-stream failures, and choose between self-hosting and LangChain's managed server. The job description lists latency, cost and reliability next to LangGraph; this is where those get decided.

## Prerequisites

[Multi-agent systems with LangGraph]({{ '/ai-stack/langgraph-multi-agent/' | relative_url }}) for the graphs being served, and [Streaming LLM APIs with FastAPI]({{ '/ai-stack/fastapi-streaming-llm-apis/' | relative_url }}) for SSE basics.

## Stream modes

`graph.stream()` and `graph.astream()` yield events while the graph runs. `stream_mode` picks which events:

| Mode | Each event is | Use it for |
|---|---|---|
| `values` | Full state after each step | Debugging, small states |
| `updates` | `{node_name: update}` per node | Progress ("searching... done") |
| `messages` | `(token_chunk, metadata)` for every LLM call | Typing effect in the UI |
| `custom` | Whatever a node passes to `get_stream_writer()` | Progress bars, retrieved doc ids |
| `checkpoints`, `tasks` | Checkpoint saved, task started or finished | Tooling |
| `debug` | Checkpoints plus tasks, verbose | Deep debugging |

Pass a list to get several modes as `(mode, chunk)` tuples. Add `subgraphs=True` to include events from inside subgraphs, as `(namespace, mode, chunk)`.

```python
from typing import Annotated, TypedDict

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# Real code: llm = init_chat_model("openai:gpt-4.1-mini")
llm = GenericFakeChatModel(messages=iter([AIMessage("Acme Logistics exceeds the limit.")]))


class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    matches: int


def search_invoices(state: State) -> dict:
    write = get_stream_writer()
    for page in (1, 2):
        write({"stage": "search_invoices", "page": page})  # -> "custom" mode
    return {"matches": 14}


def answer(state: State) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}  # tokens -> "messages" mode


team = StateGraph(State)
team.add_node(search_invoices)
team.add_node(answer)
team.add_edge(START, "search_invoices")
team.add_edge("search_invoices", "answer")
team.add_edge("answer", END)

app = StateGraph(State)
app.add_node("finance_team", team.compile())  # a subgraph, to show namespaces
app.add_edge(START, "finance_team")
app.add_edge("finance_team", END)
graph = app.compile()

if __name__ == "__main__":
    seen = {"updates": [], "messages": [], "custom": []}
    for namespace, mode, chunk in graph.stream(
        {"messages": [("user", "Who is over the limit?")]},
        stream_mode=["updates", "messages", "custom"],
        subgraphs=True,
    ):
        seen[mode].append((namespace, chunk))

    assert [chunk["page"] for _, chunk in seen["custom"]] == [1, 2]
    tokens = [token.content for _, (token, meta) in seen["messages"]]
    assert "".join(tokens) == "Acme Logistics exceeds the limit." and len(tokens) > 1
    assert {meta["langgraph_node"] for _, (_, meta) in seen["messages"]} == {"answer"}

    inner = [ns for ns, _ in seen["custom"]][0]
    assert inner[0].startswith("finance_team:")  # emitted inside the subgraph
    top_level = [list(chunk) for ns, chunk in seen["updates"] if ns == ()]
    assert top_level == [["finance_team"]]
    print("ok")
```

Points that matter in practice:

- `messages` mode streams tokens even though the node calls `llm.invoke()`, not `llm.stream()`. LangGraph hooks the model's callbacks, so nodes stay simple.
- Every token carries metadata with `langgraph_node`. Filter on it: in a multi-agent graph you want the writer's tokens on screen, not the supervisor's routing JSON.
- Namespaces look like `("finance_team:<task_id>",)`. The empty tuple is the top-level graph.
- `stream(..., version="v2")` yields dicts with `type`, `ns` and `data` keys instead of tuples, which is easier to forward to clients as-is.

## astream and astream_events

`astream` is the async twin of `stream`; use it in async endpoints. `astream_events(version="v2")` is older and lower level: it emits every callback event from every runnable (`on_chat_model_stream`, `on_tool_start`, `on_chain_end`), useful for tool start and end events or plain LCEL chains.

```python
async for event in graph.astream_events(inputs, config, version="v2"):
    if event["event"] == "on_chat_model_stream":
        print(event["metadata"]["langgraph_node"], event["data"]["chunk"].content)
```

For LangGraph apps prefer `astream` with `messages`, `updates` and `custom`: less chatty, stable shapes.

## Streaming to FastAPI over SSE

The installed FastAPI (0.163) ships `fastapi.sse`: declare `response_class=EventSourceResponse`, `yield` `ServerSentEvent` objects, and FastAPI handles the wire format and sends a keep-alive comment every 15 seconds. Older code used `sse-starlette` or a `StreamingResponse` with hand-built `data:` lines. The endpoint below streams progress, node completions and answer tokens, and reports failures as events.

```python
import asyncio
import itertools
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, TypedDict

from fastapi import FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

log = logging.getLogger("agent-api")

# Real code: init_chat_model("openai:gpt-4.1-mini", timeout=30, max_retries=2)
llm = GenericFakeChatModel(messages=itertools.cycle([
    AIMessage("Acme Logistics spent 72,500 in Q3, above the 50,000 review threshold.")
]))
VECTOR_STORE_UP = True  # flipped in the test to show the error path
STREAM_TOKENS_FROM = {"answer"}  # never stream router or tool-planning output


class ChatState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    context: list[str]


async def retrieve(state: ChatState) -> dict:
    write = get_stream_writer()
    write({"stage": "retrieve", "status": "searching policy documents"})
    if not VECTOR_STORE_UP:
        raise ConnectionError("vector store unavailable")
    return {"context": ["FIN-07: vendor spend above 50,000 per quarter needs CFO review"]}


async def answer(state: ChatState) -> dict:
    return {"messages": [await llm.ainvoke(state["messages"])]}


builder = StateGraph(ChatState)
builder.add_node(retrieve)
builder.add_node(answer)
builder.add_edge(START, "retrieve")
builder.add_edge("retrieve", "answer")
builder.add_edge("answer", END)
graph = builder.compile(checkpointer=InMemorySaver())  # compiled once, shared

api = FastAPI()


class RunRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=4_000)


@api.post("/runs/stream", response_class=EventSourceResponse)
async def stream_run(body: RunRequest) -> AsyncIterator[ServerSentEvent]:
    config = {
        "configurable": {"thread_id": body.thread_id},
        "recursion_limit": 20,
        "run_name": "policy-qa",
        "metadata": {"thread_id": body.thread_id},
    }
    inputs = {"messages": [("user", body.question)]}
    try:
        async with asyncio.timeout(60):  # whole-run budget
            async for mode, chunk in graph.astream(
                inputs, config, stream_mode=["messages", "updates", "custom"]
            ):
                if mode == "messages":
                    token, meta = chunk
                    if token.content and meta["langgraph_node"] in STREAM_TOKENS_FROM:
                        yield ServerSentEvent(event="token", data={"text": token.content})
                elif mode == "custom":
                    yield ServerSentEvent(event="progress", data=chunk)
                elif "__interrupt__" in chunk:  # an approval gate fired
                    pending = [i.value for i in chunk["__interrupt__"]]
                    yield ServerSentEvent(event="interrupt", data=pending)
                else:  # "updates": {node_name: update}
                    yield ServerSentEvent(event="node", data={"node": next(iter(chunk))})
    except TimeoutError:
        yield ServerSentEvent(event="error", data={"message": "run timed out"})
    except Exception:
        log.exception("run failed thread=%s", body.thread_id)  # traceback stays server-side
        # The 200 and headers are already sent, so report the failure as an event.
        yield ServerSentEvent(event="error", data={"message": "run failed"})
    yield ServerSentEvent(event="done", data={"thread_id": body.thread_id})


def read_events(client: TestClient, payload: dict) -> list[tuple[str, dict]]:
    events, name = [], None
    with client.stream("POST", "/runs/stream", json=payload) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
    return events


if __name__ == "__main__":
    client = TestClient(api)
    events = read_events(client, {"thread_id": "t-1", "question": "Who needs CFO review?"})
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "progress" and kinds[-1] == "done"
    assert ("node", {"node": "retrieve"}) in events
    text = "".join(data["text"] for kind, data in events if kind == "token")
    assert text.startswith("Acme Logistics spent 72,500")

    VECTOR_STORE_UP = False
    events = read_events(client, {"thread_id": "t-2", "question": "Again?"})
    assert [kind for kind, _ in events][-2:] == ["error", "done"]
    assert client.post("/runs/stream", json={"thread_id": "", "question": "x"}).status_code == 422
    print("ok")
```

Things the demo encodes:

- **Errors after the first byte are events.** Once streaming starts, the status code is fixed at 200. Log the exception with the thread id and send a generic `error` event; never send stack traces to the client.
- **Interrupts are events too.** If a gate from [Human-in-the-loop with LangGraph]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}) fires, the client gets the pending payload and calls a separate resume endpoint.
- **Disconnects cancel the run.** The generator is canceled at its next `yield`, which cancels the graph; completed steps are already checkpointed. For runs longer than a page view, run in a worker and let clients attach to its stream, as LangGraph Server's background runs do.
- **Browsers.** `EventSource` only does GET; for POST use `fetch` with a stream reader. Behind nginx, disable proxy buffering or tokens arrive in bursts.

## Concurrency and thread safety

- **Compile once, share everywhere.** A compiled graph holds no per-run state; that lives in the checkpointer under the thread id. Build it at startup, not per request.
- **One active run per thread.** Two concurrent runs on one `thread_id` load the same checkpoint and race, and one run's messages get lost. Reject the second with a 409, queue it, or take a Postgres advisory lock on the thread id. LangGraph Server calls this `multitask_strategy` (`reject`, `interrupt`, `rollback`, `enqueue`).
- **No mutable globals in nodes.** Module-level lists like the demos' `LEDGER` are shared by every request. Real state goes in graph state, the store, or your database.
- **Match sync and async.** Async endpoints need `astream`/`ainvoke` and an async checkpointer: `SqliteSaver` raises `NotImplementedError` on async calls, so use `AsyncSqliteSaver` or `AsyncPostgresSaver`. Sync nodes run in a thread pool under `astream`, but a blocking call inside an `async def` node stalls the whole worker.

## Timeouts, retries, errors and recursion limits

Put a limit at every level, because each fails differently:

| Level | Setting | What it stops |
|---|---|---|
| Model call | `timeout=`, `max_retries=`, `max_tokens=` on the chat model | Hung or rambling provider calls |
| Node | `add_node(..., retry_policy=RetryPolicy(...))` | Transient tool and API failures |
| Node | `add_node(..., timeout=...)` (async nodes) | A hung tool call |
| Run | `recursion_limit` in config | Agent loops |
| Request | `asyncio.timeout()` around the stream | Total wall-clock time |
| Agent | `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware` | Call budgets in `create_agent` |

{% raw %}
```python
import asyncio
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

calls = {"load": 0, "fx": 0}
FX_API_UP = False


class RunState(TypedDict, total=False):
    invoices: int
    rate: float
    drafts: int


def load_invoices(state: RunState) -> dict:
    calls["load"] += 1
    return {"invoices": 3}


def fx_rate(state: RunState) -> dict:
    calls["fx"] += 1
    if not FX_API_UP:
        raise ConnectionError("fx api down")
    return {"rate": 83.2}


g = StateGraph(RunState)
g.add_node(load_invoices)
g.add_node(
    fx_rate,
    retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.01, retry_on=ConnectionError),
)
g.add_edge(START, "load_invoices")
g.add_edge("load_invoices", "fx_rate")
g.add_edge("fx_rate", END)
pipeline = g.compile(checkpointer=InMemorySaver())


def redraft(state: RunState) -> dict:
    return {"drafts": state.get("drafts", 0) + 1}


loop = StateGraph(RunState)
loop.add_node(redraft)
loop.add_edge(START, "redraft")
loop.add_conditional_edges("redraft", lambda s: "redraft", ["redraft", END])  # never done
critic_loop = loop.compile()


async def slow_lookup(state: RunState) -> dict:
    await asyncio.sleep(5)  # a hung HTTP call
    return {"rate": 0.0}


t = StateGraph(RunState)
t.add_node(slow_lookup, timeout=0.1)  # seconds, per attempt
t.add_edge(START, "slow_lookup")
t.add_edge("slow_lookup", END)
timed = t.compile()

if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "close-2026-10"}}
    try:
        pipeline.invoke({}, cfg)
    except ConnectionError:
        pass
    assert calls == {"load": 1, "fx": 3}  # retried 3 times, then failed

    FX_API_UP = True
    out = pipeline.invoke(None, cfg)  # resume from the last good checkpoint
    assert out["rate"] == 83.2
    assert calls == {"load": 1, "fx": 4}  # load_invoices did not run again

    try:
        critic_loop.invoke({}, {"recursion_limit": 8})
        raise AssertionError("should have stopped")
    except GraphRecursionError:
        pass

    try:
        asyncio.run(timed.ainvoke({}))
        raise AssertionError("should have timed out")
    except NodeTimeoutError:
        pass
    print("ok")
```
{% endraw %}

Two recent changes. The default `recursion_limit` was 25 in older LangGraph; the installed 1.2 defaults to 10,007 (env `LANGGRAPH_DEFAULT_RECURSION_LIMIT`), so a looping agent burns thousands of calls before failing. Always set your own. Per-node `timeout=` is new in 1.2 and relies on asyncio cancellation, so it only cuts off async code. The resume step is the payoff of checkpointing: `invoke(None, cfg)` continues from the last completed step without repeating work.

## Deploying: self-host or LangGraph Server

```text
 browser --SSE--> load balancer --> FastAPI (uvicorn workers) --> LLM APIs
                                      | graph compiled per process
                                      +--> Postgres: checkpoints, store
                                      +--> warehouse / pgvector (read-only)
                                      +--> LangSmith (async trace upload)
```

### Self-hosting behind FastAPI with Postgres

Your auth, your routes, LangGraph as a library. Illustrative; it needs Postgres and `langgraph-checkpoint-postgres`:

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

DB_URI = os.environ["CHECKPOINT_DB_URI"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncConnectionPool(
        DB_URI, max_size=20, kwargs={"autocommit": True, "prepare_threshold": 0}
    ) as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # creates checkpoint tables; safe to rerun
        app.state.graph = builder.compile(checkpointer=saver)
        yield


app = FastAPI(lifespan=lifespan)
```

Size the pool for workers times concurrent runs, use PgBouncer with many workers (`prepare_threshold=0` keeps transaction pooling working), and add a retention job: checkpoints grow with every step of every thread.

### LangGraph Server and langgraph.json

LangChain sells a managed runtime for graphs. It launched as LangGraph Platform and newer docs fold it into LangSmith as LangSmith Deployment; check the current name, license and pricing before proposing it. The server (LangGraph Server) exposes threads, runs and assistants over HTTP, with a task queue, background runs, resumable streams, `multitask_strategy`, cron jobs and LangGraph Studio. You describe your app in `langgraph.json` (illustrative):

```json
{
  "dependencies": ["."],
  "graphs": {
    "finance_agent": "./src/finance_agent/graph.py:graph"
  },
  "env": ".env",
  "python_version": "3.12"
}
```

`langgraph dev` (from `langgraph-cli`) runs a local in-memory server; `langgraph build` produces a Docker image. Clients use the SDK (illustrative):

```python
from langgraph_sdk import get_client

client = get_client(url="http://localhost:2024")
thread = await client.threads.create()
async for part in client.runs.stream(
    thread["thread_id"], "finance_agent",
    input={"messages": [{"role": "user", "content": "Q3 spend by vendor?"}]},
    stream_mode=["messages", "updates"],
    multitask_strategy="reject",
):
    print(part.event, part.data)
```

| Choose | When |
|---|---|
| Self-host on FastAPI | You have a platform team, auth and deploy pipeline; data must stay in your VPC; graphs are one feature of a larger API |
| LangGraph Server | Long-running and background agents, many graphs, and you want queues, resumable streams and Studio without building them |

## Observability with LangSmith

Tracing is on by environment variable; LangChain and LangGraph send runs in a background thread. Illustrative:

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=...
export LANGSMITH_PROJECT=finance-agent-prod
export LANGSMITH_TRACING_SAMPLING_RATE=0.25   # sample high-volume traffic
export LANGSMITH_HIDE_INPUTS=true             # or redact selectively in code
```

Put `run_name`, `tags` and `metadata` (tenant id, thread id, app version) in the run config, as the SSE demo does, to filter traces per customer and jump from an error log to its trace. For finance data, decide what may leave your network first: hide or redact inputs and outputs, or self-host LangSmith. Keep Sentry for exceptions; traces answer "why did the agent do that", not "is the service up". See [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).

## Cost and latency control

- **Stream early.** Time to first token matters more than total time; emit `progress` before the first model call.
- **Cheap models for routing and grading,** strong ones for the final answer.
- **Cap everything:** `max_tokens`, `recursion_limit`, `ModelCallLimitMiddleware(run_limit=...)`, turn counters in state.
- **Run independent work in parallel.** Nodes in the same superstep run concurrently; fan out retrieval and SQL instead of chaining them.
- **Trim history** before each model call, and use provider prompt caching for long, stable system prompts.
- **Cache deterministic nodes** with `add_node(..., cache_policy=CachePolicy(ttl=300))` and `compile(cache=...)`.
- **Pick durability per graph.** `durability="exit"` writes one checkpoint at the end, cheap for short Q&A; keep the default `"async"` or use `"sync"` where approvals and resumes matter.
- **Measure per node.** Trace latency and `usage_metadata` tokens by node and tenant; fix the slowest node, not the suspected one. More in [Latency and cost engineering]({{ '/llm/latency-and-cost/' | relative_url }}).

## Coming from Django

| Django | LangGraph service |
|---|---|
| `StreamingHttpResponse` under WSGI workers | `EventSourceResponse` under uvicorn (ASGI); a WSGI worker is blocked per stream |
| Celery task for long work | Background runs in LangGraph Server, or your worker plus checkpointer |
| `select_for_update()` on a row | One active run per thread: advisory lock or 409 |
| Sentry performance traces | LangSmith traces per run, plus Sentry for exceptions |

## Common mistakes

- **Streaming everything.** Router JSON and tool arguments leak into the UI. Filter `messages` by `langgraph_node` or tags.
- **No recursion limit.** With the 1.2 default, a stuck agent runs about ten thousand steps. Set a small limit per graph.
- **Raising HTTP errors mid-stream.** Too late for status codes. Validate before streaming (Pydantic body, auth dependency) and send `error` events after.
- **Sync checkpointer in async code.** `SqliteSaver` with `astream` raises `NotImplementedError`. Use the async savers.
- **Tracing raw financial data to a SaaS by default.** Agree on redaction with security first.

## Practice tasks

1. Change the stream-modes demo to `version="v2"` and rewrite the asserts against `part["type"]`, `part["ns"]` and `part["data"]`. Done when all asserts pass.
2. Add a `supervisor` node that calls a second fake model before `answer`, and prove with a test that none of its tokens reach the SSE stream.
3. Add `POST /runs/{thread_id}/resume` to the SSE app, put an `interrupt()` before `answer`, and stream the rest of the run after resuming. Done when a test sees `interrupt`, then after resume sees tokens and `done`.
4. Make a second request on the same `thread_id` while the first is streaming return 409. Done when an async test with `httpx.AsyncClient` and `ASGITransport` shows one 200 and one 409.
5. Run the self-hosted setup against Postgres in Docker. Done when you can kill the app mid-run, restart it, and resume the thread with `invoke(None, cfg)`.

## Interview questions

**Which stream modes would you use for a chat UI?**
`messages` for tokens, filtered to the answering node; `updates` or `custom` for progress. `values` is too heavy for large states.

**How do you stream a LangGraph agent through FastAPI?**
An async endpoint with `response_class=EventSourceResponse` that iterates `graph.astream(..., stream_mode=[...])` and yields typed events. Validation happens before the stream starts; errors after that become `error` events.

**Is it safe to share one compiled graph across requests?**
Yes. Per-run state lives in the checkpointer under the thread id. What is not safe is two concurrent runs on the same thread, or mutable globals inside nodes.

**What happens when a node fails halfway through a run?**
The exception propagates and the checkpointer keeps the last completed step. With a retry policy, transient errors are retried first. After fixing the cause, `invoke(None, config)` resumes without repeating finished nodes.

**What is the recursion limit and what should you set it to?**
The maximum number of supersteps per run, raising `GraphRecursionError` when hit. Set it per graph from the longest legitimate path plus margin; do not rely on the default.

**Self-host or LangGraph Server?**
Self-host when the agent is one feature of an existing API with its own auth, deployment and data residency rules. The managed server when you need background runs, queues and resumable streams and do not want to build them.

**Where does latency go in an agent, and how do you cut it?**
Mostly in sequential model calls. Fewer hops, smaller routing models, parallel independent nodes, shorter prompts, caching, and streaming for perceived latency.

## Further reading

- LangGraph docs: "Streaming" (stream modes, subgraphs, custom data)
- LangGraph docs: "Persistence" (Postgres and SQLite checkpointers, durability modes)
- LangGraph reference: `RetryPolicy`, `TimeoutPolicy`, `GraphRecursionError`
- FastAPI docs: "Server-Sent Events"
- LangSmith docs: "Tracing" and "Mask inputs and outputs"; LangGraph Server and `langgraph.json` configuration reference
- [Reliability patterns for LLM calls]({{ '/llm/reliability-patterns/' | relative_url }})
