---
title: Chat history and memory in LangChain
section: ai-stack
order: 19
phase: "Phase 3 · LangChain"
summary: Keep multi-turn context with message lists, trimming and summarization, persist it per thread with checkpointers, and handle finance chat privacy.
---
After this note you can give a LangChain assistant a memory that fits in the context window, survives restarts, stays isolated per user and thread, and meets the privacy and retention rules a finance client will ask about. You will also know which history APIs are deprecated in 1.x, so you do not build a new service on them. In the job this is the "conversational assistant over finance data" feature: a controller asks a follow-up ("and the oldest one?") and the system has to know what "one" refers to.

## Prerequisites

[RAG with LangChain]({{ '/ai-stack/langchain-rag/' | relative_url }}) and [LCEL and runnables]({{ '/ai-stack/langchain-lcel-and-runnables/' | relative_url }}). Conceptual background: [agent memory]({{ '/llm/agent-memory/' | relative_url }}).

## Mental model: history is a list you resend

Chat models are stateless. Every call receives the full list of messages it should consider, and nothing else. "Memory" is your code deciding which messages to load, how to shrink them, and where to save the new ones.

```text
 POST /chat {thread_id, text}
      │
      ▼
 authorize thread ──► load state (checkpointer, by thread_id)
                            │ messages: [system, h1, a1, h2, a2, ...]
                            ▼
                      shrink: trim (view) or summarize (rewrite)
                            │
                            ▼
                      chat model ──► reply
                            │
                            ▼
                      save new messages ──► checkpoint store
```

Three separate decisions hide in that picture:

| Decision | Options |
|---|---|
| What the model sees this turn | All messages, a trimmed window, a summary plus recent turns |
| Where state lives | Process memory, SQLite, Postgres, Redis |
| Who may read it and for how long | Ownership check, redaction, retention, deletion |

## Messages as state

The state of a conversation is a list of `SystemMessage`, `HumanMessage`, `AIMessage` and `ToolMessage` objects. Tool calls matter: an `AIMessage` with `tool_calls` must be followed by the matching `ToolMessage` results, or most providers reject the request. Any shrinking strategy has to keep those pairs together.

Token cost grows with every turn because you resend everything. A 30-turn reconciliation session with tool results can reach 20,000 tokens per call, which is slow, expensive and dilutes attention. So you shrink.

## Trimming with trim_messages

`trim_messages` returns a window of the history that fits a token budget. It is a pure function: the stored history is untouched, so you can trim differently per model.

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

llm = FakeListChatModel(responses=[
    "INV-104 from Acme is 12 days overdue.",
    "Acme has 3 open invoices totaling INR 4,20,000.",
    "The oldest is INV-098, posted on the 3rd.",
])
history = [SystemMessage("You are a payables assistant for entity IN01. "
                         "Answer from tool results only.")]
sent = []  # what the model actually received each turn

def chat(user_text: str) -> str:
    history.append(HumanMessage(user_text))
    window = trim_messages(
        history,
        max_tokens=80,                       # budget for history, not the reply
        token_counter=count_tokens_approximately,
        strategy="last",                     # keep the most recent messages
        include_system=True,                 # never drop the system prompt
        start_on="human",                    # don't start on an orphan AI reply
    )
    sent.append(window)
    reply = llm.invoke(window)
    history.append(reply)
    return reply.content

if __name__ == "__main__":
    for q in ["Is INV-104 paid?", "How many open invoices does Acme have?",
              "Which one is oldest?"]:
        chat(q)
    last = sent[-1]
    assert isinstance(last[0], SystemMessage) and isinstance(last[1], HumanMessage)
    assert count_tokens_approximately(last) <= 80
    assert len(last) == 4 and last[1].content == "How many open invoices does Acme have?"
    assert len(history) == 7  # trimming is a view; full history is untouched
    print([type(m).__name__ for m in last])
```

The parameters that matter:

- `token_counter`: `count_tokens_approximately` (about 4 characters per token) is fast and good enough for budgets. Passing the chat model itself uses its tokenizer, which is exact but can be slow or need network calls.
- `strategy="last"` keeps recent turns; `"first"` keeps the start, which is rarely what a chat needs.
- `start_on="human"` and `end_on` keep the window valid for providers that require the first non-system message to be from the user, and stop it from starting with a `ToolMessage` whose call was trimmed away.
- `allow_partial=True` cuts inside a message. Avoid it for tool results with numbers; a half table is worse than none.

Trimming forgets. If the user set a constraint ten turns ago ("only entity IN01, FY25"), it falls out of the window. That is the case for summarization, or better, for moving such constraints into structured state.

## Summarization

Summarization replaces older messages with a short model-written summary and keeps the last few verbatim. Unlike trimming, it rewrites the stored state. In LangChain 1.x you get it as agent middleware, `SummarizationMiddleware`, on `create_agent`.

## RunnableWithMessageHistory is deprecated

Most LangChain tutorials from 2024 wrap a chain in `RunnableWithMessageHistory` with a `get_session_history(session_id)` factory. In `langchain-core` 1.8 the constructor emits a deprecation warning ("deprecated since 1.3.3, use LangGraph's built-in persistence instead", removal planned for 2.0). `BaseChatMessageHistory` and `InMemoryChatMessageHistory` are deprecated since 1.6.4, also with removal in 2.0. The old `ConversationBufferMemory` family lives only in `langchain-classic`.

Legacy, for recognition only (do not use in new code):

{% raw %}
```python
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory

sessions = {}
chain_with_history = RunnableWithMessageHistory(
    prompt | llm,
    lambda session_id: sessions.setdefault(session_id, InMemoryChatMessageHistory()),
    input_messages_key="question",
    history_messages_key="history",
)
chain_with_history.invoke({"question": "Is INV-104 paid?"},
                          config={"configurable": {"session_id": "s1"}})
```
{% endraw %}

Why it was replaced: it stored only messages (no tool state, no approvals, no custom fields), wrote history in a listener after the run, and could not resume a run that paused halfway. LangGraph checkpointers store the full graph state after every step, which also unlocks human-in-the-loop and time travel later.

## The current way: create_agent with a checkpointer

`create_agent` returns a compiled LangGraph graph. Give it a `checkpointer` and pass `thread_id` in the config; each invoke then loads that thread's state, appends the new input, runs, and saves. You send only the new message, never the whole history.

{% raw %}
```python
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

# Real models (illustrative): create_agent("openai:gpt-4.1-mini", ...),
# SummarizationMiddleware(model="openai:gpt-4.1-nano", ...)
model = FakeListChatModel(responses=[f"answer {i}" for i in range(10)])
summarizer = FakeListChatModel(responses=["User reviewed Acme invoices "
                                          "INV-098 and INV-104 for IN01."])
saver = InMemorySaver()
agent = create_agent(
    model,
    tools=[],
    system_prompt="You are a payables assistant for entity IN01.",
    checkpointer=saver,
    middleware=[SummarizationMiddleware(
        model=summarizer,
        trigger=("messages", 6),  # real apps: ("tokens", 3000) or ("fraction", 0.7)
        keep=("messages", 2),     # most recent messages kept verbatim
    )],
)

def ask(thread_id: str, text: str) -> dict:
    cfg = {"configurable": {"thread_id": thread_id}}
    return agent.invoke({"messages": [HumanMessage(text)]}, cfg)  # send only the new turn

if __name__ == "__main__":
    for q in ["Is INV-104 paid?", "Open invoices for Acme?", "Oldest one?"]:
        ask("u42:thread-1", q)
    other = ask("u7:thread-9", "Hotel limit per night?")
    assert len(other["messages"]) == 2           # threads are isolated

    state = ask("u42:thread-1", "Draft a reminder email to Acme.")
    msgs = state["messages"]
    assert msgs[0].content.startswith("Here is a summary of the conversation")
    assert len(msgs) < 8                         # older turns were replaced

    # Earlier checkpoints still hold the raw, unsummarized turns.
    cfg = {"configurable": {"thread_id": "u42:thread-1"}}
    assert any(m.content == "Is INV-104 paid?"
               for c in saver.list(cfg)
               for m in c.checkpoint["channel_values"].get("messages", []))

    saver.delete_thread("u42:thread-1")          # retention / erasure
    assert agent.get_state(cfg).values == {}
    print([type(m).__name__ for m in msgs])
```
{% endraw %}

Two things to notice. The system prompt is not stored in state; `create_agent` adds it on every model call, so you can change it without migrating threads. And summarization only rewrites the latest state: every earlier checkpoint still contains the raw turns. If summarization is part of your data-minimization story, you also need checkpoint pruning or deletion.

Use a small, cheap model for the summarizer, set the trigger in tokens relative to the main model's window, and tell it what must survive: entity, period, document ids, amounts and open actions. The default summary prompt is generic.

## Per-session storage keyed by thread id

A thread is one conversation. The checkpointer stores state per `thread_id` and knows nothing about users, so ownership is your job:

- Generate thread ids on the server (UUIDs), never from user input.
- Keep a `threads` table (`thread_id`, `user_id`, `entity`, `created_at`, `last_active_at`) in your main database, and check it on every request.
- Return "not found" for someone else's thread, not "forbidden", so ids cannot be probed.

`InMemorySaver` loses everything on restart. `SqliteSaver` (from `langgraph-checkpoint-sqlite`) is fine for a single-node service or tests. This block persists to a SQLite file, restarts, checks ownership and redacts before storage:

{% raw %}
```python
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")   # Indian tax id
ACCOUNT = re.compile(r"\b\d{9,18}\b")             # bank account numbers

def redact(text: str) -> str:
    text = PAN.sub("[PAN]", text)
    return ACCOUNT.sub(lambda m: "[ACCT-" + m.group()[-4:] + "]", text)

# In production this is a Postgres table: thread_id, user_id, entity, created_at.
THREAD_OWNERS: dict[str, str] = {}

def open_thread(user_id: str) -> str:
    thread_id = str(uuid.uuid4())                  # opaque, server-generated
    THREAD_OWNERS[thread_id] = user_id
    return thread_id

def ask(agent, user_id: str, thread_id: str, text: str) -> dict:
    if THREAD_OWNERS.get(thread_id) != user_id:    # authorize every access
        raise PermissionError("thread not found")
    cfg = {"configurable": {"thread_id": thread_id}}
    return agent.invoke({"messages": [HumanMessage(redact(text))]}, cfg)

def build_agent(conn: sqlite3.Connection):
    model = FakeListChatModel(responses=["Noted."])
    return create_agent(model, tools=[], checkpointer=SqliteSaver(conn))

if __name__ == "__main__":
    db = Path(tempfile.mkdtemp()) / "chats.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    tid = open_thread("u42")
    ask(build_agent(conn), "u42", tid, "Pay ABCDE1234F at account 123456789012")
    conn.close()                                   # simulate a restart

    conn = sqlite3.connect(db, check_same_thread=False)
    agent = build_agent(conn)
    out = ask(agent, "u42", tid, "What did I just ask?")
    assert len(out["messages"]) == 4               # history survived the restart
    assert out["messages"][0].content == "Pay [PAN] at account [ACCT-9012]"

    stored = b"".join(p.read_bytes() for p in db.parent.iterdir())
    assert b"[ACCT-9012]" in stored and b"123456789012" not in stored

    try:
        ask(agent, "u7", tid, "Show me that thread")
        raise AssertionError("other user read the thread")
    except PermissionError:
        pass
    print(out["messages"][0].content)
```
{% endraw %}

For multiple API replicas, use Postgres. Illustrative (needs `langgraph-checkpoint-postgres` and a database):

```python
from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string("postgresql://chat_app@db:5432/chat") as saver:
    saver.setup()  # creates checkpoint tables; run once from a migration step
    agent = create_agent(model, tools, checkpointer=saver)
```

Async FastAPI routes use `AsyncPostgresSaver` with `ainvoke`. The [LangGraph persistence note]({{ '/ai-stack/langgraph-persistence-and-memory/' | relative_url }}) covers checkpoint history, `get_state_history` and long-term memory stores.

## Privacy and retention for finance chats

Finance chats collect exactly what regulators care about: vendor bank details, PAN and GST numbers, salaries, unpublished results. Treat the checkpoint store as a system of record with personal data in it.

- **Redact before the graph sees it.** `PIIMiddleware` (for example `PIIMiddleware("pan", detector=r"...", strategy="redact")`) redacts what the model receives, but the raw user input is written to the first checkpoint before middleware runs. Redact at the API boundary, as above, if raw values must never be stored. Keep a mapping to the real value in a separate vault only if a tool needs it.
- **Minimize tool results.** A tool that returns a full vendor master row puts bank details into history. Return only the fields the answer needs.
- **Retention.** Define it per data class (for example 90 days for chat, longer for an audit log). A scheduled job deletes expired threads with `delete_thread(thread_id)`. Summaries and older checkpoints are covered because deletion is per thread.
- **Erasure versus audit.** Right-to-erasure requests delete the chat; regulatory audit needs a record of what the assistant advised. Keep a separate, minimal audit log (who, when, question hash, sources cited, answer) under its own retention, instead of keeping chats forever.
- **Encryption.** Encrypt the database at rest, and for stricter cases use LangGraph's `EncryptedSerializer` so checkpoint blobs are encrypted with a key from your KMS.
- **Telemetry.** Traces contain the same messages. Mask inputs and outputs in LangSmith or self-host it, as covered in the [observability note]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }}).

More on regulation in [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).

## Coming from Django

| Django | LangChain / LangGraph |
|---|---|
| `django.contrib.sessions`, keyed by session key | Checkpointer, keyed by `thread_id` |
| Session backend (db, cache, signed cookies) | Saver backend (memory, SQLite, Postgres) |
| `request.session["cart"]` | Graph state (messages plus custom keys) |
| `SESSION_COOKIE_AGE` and `clearsessions` | Retention job calling `delete_thread` |
| Object-level permission check in a view | Thread ownership check before `invoke` |
| Middleware that scrubs request bodies before logging | Redaction at the API boundary, `PIIMiddleware` |

The difference from sessions: Django stores what you put in the session; a checkpointer stores a snapshot after every graph step, so the data volume and the copies of sensitive text are larger than you expect.

## Common mistakes

- **Sending the full history from the client.** The client can forge earlier turns, including fake assistant messages. Keep history server-side, keyed by a thread id you issued.
- **Trusting the thread id.** Without an ownership check, any user who guesses a UUID reads another user's chat. Check on every request.
- **Trimming that breaks tool pairs.** A window that starts with a `ToolMessage` gets rejected by the provider. Use `start_on="human"`.
- **Building new code on `RunnableWithMessageHistory`.** It is deprecated and will be removed in 2.0. Use a checkpointer.
- **Assuming summarization deletes data.** Earlier checkpoints keep raw turns. Delete or prune for minimization.
- **Using `InMemorySaver` in production.** State disappears on deploy and is not shared across workers.
- **Putting constraints only in chat.** "Entity IN01, FY25" should be structured state or a filter from auth, not a sentence that can be trimmed or summarized away.

## Practice tasks

1. **Budget sweep.** Run the trimming demo with `max_tokens` from 40 to 200. Done: a printed table of budget versus number of messages sent, and you can explain each jump.
2. **Constraint survival.** Write a summarizer prompt that must preserve entity, period and document ids. Done: a test where the summary (from a fake model you script) is checked by a function that rejects summaries missing any of the three.
3. **FastAPI chat endpoint.** `POST /threads` creates a thread for the current user; `POST /threads/{id}/messages` calls the agent. Done: `TestClient` tests for a normal turn, another user's thread (404), and history across two requests.
4. **Retention job.** Store `last_active_at` per thread and write a function that deletes threads idle for more than N days. Done: a test with a fixed clock that deletes one thread and keeps another.
5. **Redaction coverage.** Extend `redact` to GSTIN and IFSC codes and add `PIIMiddleware` for model output. Done: a parametrized test with 10 positive and 10 negative strings.

## Interview questions

**How does an LLM "remember" a conversation?**
It does not. Each call receives the messages you send. Memory is your code loading prior messages for the thread, shrinking them to fit, and saving the new ones.

**Trimming versus summarization?**
Trimming selects a recent window and leaves storage alone; it is cheap and lossy at the old end. Summarization calls a model to compress older turns into a summary and rewrites state; it keeps gist at the cost of a model call and possible errors. Many systems do both: summarize past a threshold, trim as a safety net.

**What replaced `RunnableWithMessageHistory` in LangChain 1.x?**
LangGraph persistence. `create_agent(..., checkpointer=...)` with a `thread_id` in config stores full graph state after each step. `RunnableWithMessageHistory` is deprecated since langchain-core 1.3.3, with removal planned for 2.0.

**How do you isolate conversations between users?**
Server-generated thread ids, a thread ownership table checked on every request, and the user's entity applied as a retrieval filter from auth, not from chat content.

**A user asks you to delete their chat data. What do you delete?**
All checkpoints for their threads (`delete_thread`), any long-term memory entries, cached responses keyed by their content, and traces in the observability tool. You keep only the minimal audit record your regulator requires, under its own retention policy.

**Where can sensitive data leak in a chat system?**
Checkpoint history, summaries, tool results, traces and logs, caches, and the model provider. Mitigate with redaction at the boundary, minimal tool outputs, masked tracing, retention jobs and a provider agreement with no training on your data and defined data residency.

**Why does the system prompt not live in the checkpoint?**
`create_agent` injects it on each model call, so you can change instructions without rewriting stored threads, and summarization never compresses it away.

## Further reading

- LangChain docs: "Short-term memory" and "Middleware" (built-in `SummarizationMiddleware`, `PIIMiddleware`)
- LangGraph docs: "Persistence" (checkpointers, threads, `delete_thread`)
- LangChain reference: `trim_messages`, `count_tokens_approximately`
- LangChain v1 migration guide (what moved to `langchain-classic`)
