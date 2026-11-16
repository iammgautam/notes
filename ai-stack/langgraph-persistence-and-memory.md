---
title: Persistence and memory in LangGraph
section: ai-stack
order: 24
phase: "Phase 4 · LangGraph"
summary: Checkpointers, threads, state history, time travel, durable resume and long-term Store memory in LangGraph 1.2, plus retention rules for finance data.
---
After this note you can give an agent conversation memory, inspect and rewind any run, resume a workflow after a crash without redoing finished steps, keep per-user facts across conversations, and explain how long all of that data lives and who can see it. In a finance platform this is the difference between a demo and something an auditor signs off: every step is recorded, failures resume cleanly, and retention is deliberate.

## Prerequisites

[Tool-calling agents with LangGraph]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}). The conceptual side of memory is in [agent memory]({{ '/llm/agent-memory/' | relative_url }}).

## Two kinds of memory

LangGraph separates memory by scope:

```text
 thread "acme:asha:chat-1"          thread "acme:asha:chat-2"
 +---------------------------+      +---------------------------+
 | checkpoint per superstep  |      | checkpoint per superstep  |
 | (full state: messages,    |      |                           |
 |  intermediate results)    |      |                           |
 +-------------+-------------+      +-------------+-------------+
               |   short-term: one thread only    |
               +---------------+------------------+
                               | runtime.store
                               v
                  Store (long-term, cross-thread)
                  ("acme", "asha", "preferences")
                  ("acme", "policies")
```

| | Checkpointer | Store |
|---|---|---|
| Scope | One thread | Any thread, keyed by namespace |
| Holds | Full graph state after every superstep | JSON documents you choose to save |
| Written | Automatically by the runtime | Explicitly by your nodes or tools |
| Used for | Conversation history, resume, replay, human-in-the-loop | User preferences, learned facts, shared reference data |
| Attach with | `compile(checkpointer=...)` | `compile(store=...)` |

## Checkpointers and threads

A checkpointer saves a snapshot of state at every superstep boundary, keyed by `thread_id`. Pass the thread in config; calling the same thread again loads its latest state and applies your new input through the reducers.

| Checkpointer | Package | Use |
|---|---|---|
| `InMemorySaver` | `langgraph-checkpoint` (bundled) | Tests and notebooks; lost on restart |
| `SqliteSaver` / `AsyncSqliteSaver` | `langgraph-checkpoint-sqlite` | Local tools, single-process services |
| `PostgresSaver` / `AsyncPostgresSaver` | `langgraph-checkpoint-postgres` | Production, multiple workers |

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END, MessagesState

# Illustrative: llm = init_chat_model("anthropic:claude-sonnet-4-5")
llm = FakeListChatModel(responses=["Noted: vendor is Acme.",
                                   "You asked about Acme.",
                                   "Which vendor?"])


def assistant(state: MessagesState) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}


graph = (StateGraph(MessagesState)
         .add_node("assistant", assistant)
         .add_edge(START, "assistant").add_edge("assistant", END)
         .compile(checkpointer=InMemorySaver()))

if __name__ == "__main__":
    a = {"configurable": {"thread_id": "acme:user-17:chat-1"}}
    b = {"configurable": {"thread_id": "acme:user-17:chat-2"}}
    graph.invoke({"messages": [HumanMessage("Look at vendor Acme")]}, a)
    out = graph.invoke({"messages": [HumanMessage("What did I ask?")]}, a)
    assert len(out["messages"]) == 4  # earlier turn loaded from checkpoint
    out = graph.invoke({"messages": [HumanMessage("What did I ask?")]}, b)
    assert len(out["messages"]) == 2  # new thread, empty history

    snap = graph.get_state(a)
    assert snap.next == () and snap.values["messages"][-1].content == "You asked about Acme."
    history = list(graph.get_state_history(a))  # newest first
    assert [h.metadata["step"] for h in history] == [4, 3, 2, 1, 0, -1]
    print("ok")
```

Things to notice:

- You send only the new message. The checkpointer supplies the rest, and `add_messages` appends.
- `get_state(config)` returns a `StateSnapshot`: `values`, `next` (nodes that would run next; empty when finished), `config` (including the `checkpoint_id`), `metadata` (`source`, `step`, `parents`), `created_at`, `parent_config`, `tasks` (the node runs from this checkpoint, with their results once finished) and `interrupts`.
- `get_state_history(config)` walks every checkpoint newest first. Step `-1` is the input checkpoint. Two turns of a one-node graph produced six checkpoints, which is why storage grows faster than people expect.
- Thread IDs are just strings. Build them from tenant, user and conversation, and authorize access to a thread in your API before passing it to the graph. The checkpointer does no access control.

## Time travel and update_state

Every checkpoint has an ID, so you can go back to any step, change state there, and continue. This creates a **fork**: the new branch gets new checkpoints, and the original ones stay in history. That is useful for debugging ("what if extraction had read 420, not 4200?") and for corrections that must leave an audit trail.

```python
from typing_extensions import TypedDict
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END


class Expense(TypedDict, total=False):
    raw: str
    amount: float
    gl_account: str
    posted: str


def extract(state: Expense) -> dict:  # pretend this is an LLM extraction
    return {"amount": float(state["raw"].split("$")[1])}


def categorize(state: Expense) -> dict:
    return {"gl_account": "6100" if "flight" in state["raw"] else "6900"}


def post(state: Expense) -> dict:
    return {"posted": f"{state['gl_account']}:{state['amount']:.2f}"}


graph = (StateGraph(Expense)
         .add_node("extract", extract).add_node("categorize", categorize)
         .add_node("post", post)
         .add_edge(START, "extract").add_edge("extract", "categorize")
         .add_edge("categorize", "post").add_edge("post", END)
         .compile(checkpointer=InMemorySaver()))

if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "exp-311"}}
    assert graph.invoke({"raw": "flight to BLR $4200"}, cfg)["posted"] == "6100:4200.00"

    # 1. Find the checkpoint taken right after extract ran
    after_extract = next(s for s in graph.get_state_history(cfg)
                         if s.next == ("categorize",))
    assert after_extract.values["amount"] == 4200.0

    # 2. Fix the extracted amount there; this creates a fork, not an overwrite
    fork_cfg = graph.update_state(after_extract.config, {"amount": 420.0})

    # 3. Resume from the fork: categorize and post re-run, extract does not
    out = graph.invoke(None, fork_cfg)
    assert out["posted"] == "6100:420.00"
    assert graph.get_state(cfg).values["posted"] == "6100:420.00"  # head moved

    # The original run is still in history for audit
    posted = [s.values.get("posted") for s in graph.get_state_history(cfg)]
    assert "6100:4200.00" in posted and "6100:420.00" in posted
    print("ok")
```

`update_state` applies the values through the reducers, as if a node had returned them. By default it is attributed to the node that ran last at that checkpoint, which decides what runs next; pass `as_node="extract"` to be explicit. `invoke(None, config)` means "continue from this checkpoint" rather than "start a new run". The same mechanism powers human review: pause, let a person edit state, resume. That is the subject of [human in the loop]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}).

> **Gotcha:** replaying from an old checkpoint re-executes every node after it, including side effects. If `post` writes to the ledger, a replay posts again. Make side-effecting nodes idempotent (an idempotency key from thread and invoice ID) before you rely on time travel in production.

## Durable execution: resume after failure

Because state is saved after each superstep, a crash loses at most the superstep in progress. Re-invoking the thread with `None` input continues from the last checkpoint. Nodes that finished are not re-run, and if some nodes in a parallel superstep succeeded before a sibling failed, their writes were saved as pending writes and are reused too.

The block simulates a month-end close job whose FX provider is down, then a fresh process (new connection, new graph object) resuming it from SQLite.

```python
import os
import tempfile

from typing_extensions import TypedDict
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END


class Close(TypedDict, total=False):
    period: str
    extracted: int
    fx_rate: float
    report: str


calls = {"extract": 0, "fx": 0}
fx_api_up = False


def extract(state: Close) -> dict:  # slow and expensive: must not re-run
    calls["extract"] += 1
    return {"extracted": 1250}


def fetch_fx(state: Close) -> dict:
    calls["fx"] += 1
    if not fx_api_up:
        raise ConnectionError("fx provider down")
    return {"fx_rate": 83.1}


def report(state: Close) -> dict:
    return {"report": f"{state['period']}: {state['extracted']} rows @ {state['fx_rate']}"}


def build(checkpointer):
    return (StateGraph(Close)
            .add_node("extract", extract).add_node("fetch_fx", fetch_fx)
            .add_node("report", report)
            .add_edge(START, "extract").add_edge("extract", "fetch_fx")
            .add_edge("fetch_fx", "report").add_edge("report", END)
            .compile(checkpointer=checkpointer))


if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "close-2026-10"}}
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "checkpoints.db")

        # Process 1: crashes in fetch_fx
        with SqliteSaver.from_conn_string(db) as saver:
            try:
                build(saver).invoke({"period": "2026-10"}, cfg, durability="sync")
            except ConnectionError:
                pass

        # Process 2: new connection, new graph object, same thread_id
        fx_api_up = True
        with SqliteSaver.from_conn_string(db) as saver:
            graph = build(saver)
            assert graph.get_state(cfg).next == ("fetch_fx",)  # where it stopped
            out = graph.invoke(None, cfg)  # None = resume, not restart
            assert out["report"] == "2026-10: 1250 rows @ 83.1"
            assert calls == {"extract": 1, "fx": 2}  # extract never re-ran

            saver.delete_thread("close-2026-10")  # retention: drop history
            assert graph.get_state(cfg).values == {}
    print("ok")
```

The `durability` argument trades speed for safety:

| Mode | Checkpoint written | Trade-off |
|---|---|---|
| `"async"` (default) | In the background while the next step runs | Fast; a hard process kill can lose the last step |
| `"sync"` | Before the next step starts | Safest; adds write latency per step |
| `"exit"` | Only when the run ends or fails | Fastest; no mid-run resume after a hard crash |

Use `"sync"` for long, expensive workflows such as close or reconciliation jobs, `"async"` for chat. Combine with the node-level `RetryPolicy` from [control flow]({{ '/ai-stack/langgraph-control-flow/' | relative_url }}): retries handle blips inside one run, checkpoints handle crashes and deploys between runs.

### Production checkpointer (illustrative)

```python
# pip install langgraph-checkpoint-postgres  (uses psycopg 3)
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = "postgresql://agent_rw@db:5432/agents?sslmode=require"

with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()  # creates tables; run once from a migration step
    graph = builder.compile(checkpointer=checkpointer)
    graph.invoke(inputs, {"configurable": {"thread_id": thread_id}})
```

In a FastAPI service, create the async variant (`AsyncPostgresSaver` from `langgraph.checkpoint.postgres.aio`) once in the app lifespan over a connection pool, not per request. Keep the checkpoint database separate from the ledger: different retention, different access.

## Keeping short-term memory small

A thread's `messages` list grows forever, and every turn sends it to the model. Three tools, in order of preference:

- **Trim at call time.** Pass only recent messages to the model; state keeps everything. `start_on="human"` avoids starting the window on an orphaned `ToolMessage`.
- **Delete from state.** Return `RemoveMessage(id=...)` for old messages (or `RemoveMessage(id=REMOVE_ALL_MESSAGES)` followed by replacements) so they leave the checkpoint too.
- **Summarize.** Replace old turns with a summary message; `create_agent` ships `SummarizationMiddleware` for this.

```python
from langchain_core.messages import trim_messages
from langchain_core.messages.utils import count_tokens_approximately


def assistant(state: MessagesState) -> dict:  # fragment: trims per call
    recent = trim_messages(state["messages"], strategy="last", max_tokens=4000,
                           token_counter=count_tokens_approximately,
                           start_on="human")
    return {"messages": [llm.invoke(recent)]}
```

## Long-term memory with a Store

The Store is a key-value document store organized by **namespace** tuples, such as `(tenant, user, "preferences")`. Nodes reach it through `runtime.store`; tools through `ToolRuntime.store`. `search(prefix)` lists documents under a namespace prefix, optionally with a `filter` on fields or a semantic `query`.

```python
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore


@dataclass
class Ctx:
    tenant_id: str
    user_id: str


def remember(state: MessagesState, runtime: Runtime[Ctx]) -> dict:
    ns = (runtime.context.tenant_id, runtime.context.user_id, "preferences")
    text = state["messages"][-1].content
    if text.lower().startswith("remember:"):
        runtime.store.put(ns, "report_currency", {"value": text.split(":")[1].strip()})
        return {"messages": [AIMessage("Saved.")]}
    pref = runtime.store.get(ns, "report_currency")
    currency = pref.value["value"] if pref else "USD"
    return {"messages": [AIMessage(f"Reporting in {currency}.")]}


graph = (StateGraph(MessagesState, context_schema=Ctx)
         .add_node("remember", remember)
         .add_edge(START, "remember").add_edge("remember", END)
         .compile(checkpointer=InMemorySaver(), store=InMemoryStore()))

if __name__ == "__main__":
    asha = Ctx(tenant_id="acme", user_id="asha")
    graph.invoke({"messages": [HumanMessage("remember: INR")]},
                 {"configurable": {"thread_id": "t1"}}, context=asha)
    # Different thread, same user: long-term memory carries over
    out = graph.invoke({"messages": [HumanMessage("Show Q3 spend")]},
                       {"configurable": {"thread_id": "t2"}}, context=asha)
    assert out["messages"][-1].content == "Reporting in INR."
    # Different user: isolated namespace, default applies
    out = graph.invoke({"messages": [HumanMessage("Show Q3 spend")]},
                       {"configurable": {"thread_id": "t3"}},
                       context=Ctx(tenant_id="acme", user_id="ravi"))
    assert out["messages"][-1].content == "Reporting in USD."

    store = graph.store
    items = store.search(("acme",))  # prefix search across the tenant
    assert [(i.namespace, i.key) for i in items] == [
        (("acme", "asha", "preferences"), "report_currency")]
    store.delete(("acme", "asha", "preferences"), "report_currency")  # erasure
    assert store.search(("acme",)) == []
    print("ok")
```

The namespace comes from run context, not from the model, so one user cannot read another's memories. For fuzzy recall ("what did I say about travel policy?"), configure an embedding index (illustrative):

```python
from langchain.embeddings import init_embeddings
from langgraph.store.memory import InMemoryStore

store = InMemoryStore(index={
    "embed": init_embeddings("openai:text-embedding-3-small"),
    "dims": 1536,
    "fields": ["text"],  # which document fields get embedded
})
store.put(("acme", "asha", "facts"), "m1", {"text": "Owns the travel cost center"})
hits = store.search(("acme", "asha"), query="who handles travel?", limit=3)
```

`PostgresStore` (in `langgraph-checkpoint-postgres`, backed by pgvector for search) and `SqliteStore` are the persistent options, and both support per-item TTL in minutes via `put(..., ttl=...)`. Decide what deserves long-term memory: stable preferences and facts the user confirmed, not every message.

## Retention and privacy for finance data

Checkpoints are a full copy of state at every step. Whatever you put in state (invoice text, bank details, employee names, tool results) is now stored N times in another database. Treat that as a data store in its own right:

- **Minimize state.** Store IDs and references, not documents or account numbers. Load sensitive data inside the node that needs it.
- **Encrypt.** Pass `serde=EncryptedSerializer.from_pycryptodome_aes()` (from `langgraph.checkpoint.serde.encrypted`; needs `pycryptodome` and reads the key from `LANGGRAPH_AES_KEY`) to the checkpointer, and enable encryption at rest on the database.
- **Expire.** Checkpoints are operational data, not the system of record. Run a scheduled job that calls `delete_thread` for threads past their window, or `prune(thread_ids, strategy="keep_latest")` to keep only the final state. Use Store TTLs for memories.
- **Honor erasure.** Map a user to their thread IDs and namespaces so a deletion request removes checkpoints and Store items together.
- **Separate retention rules.** Statutory record keeping applies to the posted journal entry in the ledger, not to the agent's scratch state. Persist the business record in the ledger with its audit trail, and keep agent history only as long as debugging and dispute handling require.
- **Isolate tenants.** Tenant in every thread ID and namespace, checked by the API, plus database-level separation where contracts require it.

See [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}) for the broader controls.

## Coming from Django

| Django | LangGraph |
|---|---|
| Database-backed sessions | Checkpointer keyed by `thread_id` |
| `django-simple-history` | `get_state_history` |
| Data migration fixing a bad row | `update_state` on a checkpoint (a fork, history kept) |
| Celery `acks_late` + idempotent tasks | Durable execution + idempotent nodes |
| User profile model | Store namespace per user |
| `clearsessions` management command | Scheduled `delete_thread` / `prune` job |

## Common mistakes

- **Forgetting `thread_id`.** With a checkpointer attached, invoke fails without it; with a shared constant, every user shares one conversation.
- **`InMemorySaver` in production.** Memory vanishes on restart and does not work across workers.
- **Passing the full history on every call.** Send only the new message; the checkpointer has the rest.
- **Restarting instead of resuming.** `invoke(inputs, cfg)` after a failure starts again from `START` on top of the saved state and re-runs finished nodes; `invoke(None, cfg)` resumes.
- **Non-idempotent nodes with replay.** Time travel and retries re-run side effects.
- **Never deleting anything.** Checkpoint tables grow by one snapshot per superstep per thread. Plan retention on day one.
- **Model-chosen namespaces.** Build namespaces from authenticated context only.

## Practice tasks

1. **Chat over SQLite.** Swap `InMemorySaver` for `SqliteSaver` in the chat example and restart the process between turns. Done: the second process answers with four messages in state.
2. **Audit view.** Write `audit(graph, thread_id)` that returns `(step, node, keys_written)` tuples from each history snapshot's `tasks` (`task.name`, `task.result`). Done: for the expense graph it lists extract, categorize and post with the keys each wrote.
3. **Correction workflow.** Wrap the time-travel steps in `correct_amount(thread_id, new_amount, reason)` that also writes `{"correction_reason": reason}`. Done: the latest state has the new amount and reason, and the original posting is still in history.
4. **Retention job.** Store `created_at` per thread in a small table and write a function that deletes threads older than N days with `delete_thread`. Done: a test with two threads deletes only the old one.
5. **Memory tool.** Give the agent from the previous note a `save_preference(key, value)` tool that writes to `ToolRuntime.store` under the user's namespace from context. Done: a second thread sees the preference, and a different user does not.

## Interview questions

**What is the difference between a checkpointer and a store?**
The checkpointer saves full graph state per thread at every superstep, automatically; it is short-term memory and the basis for resume, replay and interrupts. The store holds documents across threads in namespaces, written explicitly; it is long-term memory.

**What is a thread?**
The unit of persistence: a `thread_id` in config that groups all checkpoints of one conversation or workflow instance.

**How does LangGraph resume after a crash?**
It loads the last checkpoint for the thread and continues with the nodes in `next`. Finished nodes are not re-run, and successful writes from a partially failed superstep are reused. You trigger it with `invoke(None, config)`.

**What does time travel let you do, and what is the risk?**
Resume from any earlier checkpoint, optionally after `update_state`, creating a fork while keeping the original history. The risk is re-executing side effects; nodes must be idempotent.

**Which durability mode would you choose for a month-end close workflow?**
`"sync"`: every step is persisted before the next starts, so a crash never loses completed expensive work. The latency cost is small compared with the steps.

**How do you keep the conversation from exceeding the context window?**
Trim at call time with `trim_messages`, remove old messages with `RemoveMessage`, or summarize older turns. The checkpoint can keep more than the model sees.

**How do you handle PII and retention in checkpoints?**
Keep sensitive data out of state, encrypt the serializer and the database, expire threads with `delete_thread` or `prune`, use TTLs on store items, and keep the system of record in the ledger rather than in agent history.

**How is multi-tenant isolation enforced?**
Tenant and user in thread IDs and store namespaces, derived from authenticated context and checked by the API before any graph call. The checkpointer and store do not enforce access control themselves.

## Further reading

- LangGraph docs: "Persistence" (threads, checkpoints, state history)
- LangGraph docs: "Durable execution"
- LangGraph docs: "Memory" overview and "Add memory" how-to
- LangGraph docs: "Use time travel"
- LangGraph reference: checkpointers (`InMemorySaver`, `SqliteSaver`, `PostgresSaver`) and `BaseStore`
