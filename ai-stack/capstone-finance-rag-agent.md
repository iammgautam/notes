---
title: "Capstone: a finance RAG agent end to end"
section: ai-stack
order: 28
phase: "Phase 5 · Putting it together"
summary: A buildable finance assistant with FastAPI, LangGraph, policy RAG, pandas ledger tools, access control, streaming and evals, plus how to present it.
---

This capstone ties every earlier note into one project you can build in about five weeks and demo in an interview. It answers policy questions ("who approves an invoice above 5 lakh?") through RAG and numeric questions ("why did vendor spend jump in March?", "which invoices are unreconciled?") through pandas tools over a ledger, per user, with streaming, an audit trail and evals that check numbers exactly.

Each must-have in the job description maps to a part of the project you can point at:

| JD must-have | Where the capstone proves it |
|---|---|
| Core Python, testing | Pure functions, dataclasses, typed tools, assert-based tests |
| FastAPI | `/ask` and `/ask/stream`, auth dependency, `TestClient` tests |
| LangChain + LangGraph | Tools with `ToolRuntime`, a `StateGraph` with a tool loop |
| RAG and grounding | Policy search with metadata filters, citations, evidence |
| Pandas, SQL | Spend and reconciliation analytics, SQL pushdown in production |
| LLM evaluation | Exact-match numeric evals, grounding check, RAGAS for policy answers |
| Security, regulated data | Entity-level row security, scoped retrieval, audit log |

## Prerequisites

The LangGraph tool loop from [tool-calling agents]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}), SSE from [streaming LLM APIs]({{ '/ai-stack/fastapi-streaming-llm-apis/' | relative_url }}), and retrieval from [RAG with LangChain]({{ '/ai-stack/langchain-rag/' | relative_url }}). The previous note is [LangGraph streaming and deployment]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}).

## What you are building

Two personas are enough to exercise every control:

- **Alice, AP clerk, entity IN01.** Sees IN01 ledger rows and AP policies.
- **Bob, controller, entity US01.** Sees US01 rows, AP policies and treasury policies.

Questions the system must answer correctly:

1. "Why did Acme Cloud spend jump in March?" A month-over-month delta with the invoices that drive it.
2. "Which invoices are unreconciled?" Count, total and ids, scoped to the user's entities.
3. "Who needs to approve a 7 lakh invoice?" A policy answer with a citation like `[POL-AP-01]`.

Non-goals for version one: writing back to the ERP, free-form text-to-SQL, multi-agent routing. Each is a good "what I'd do next" answer, and each adds risk you should not carry into a first demo.

## Architecture

```text
 client (web UI, Slack bot)
   |  POST /ask | /ask/stream    Authorization: Bearer <jwt>
   v
+-------------------------- FastAPI service --------------------------+
|  current_user (Depends) -> User(id, entities, audiences)            |
|  run_args: thread_id = "<user>:<thread>", context = Ctx(user)       |
|     |                                                               |
|     v            LangGraph StateGraph(MessagesState)                |
|  START -> agent --has tool calls?--> tools --+                      |
|            ^  \--no--> END                   |                      |
|            +---------------------------------+                      |
|                                                                     |
|  ToolNode (audit hook on every call)                                |
|    search_policies ------> vector store, filter: audience           |
|    vendor_spend_change --> pandas over visible(user) ledger         |
|    unreconciled_invoices -> pandas over visible(user) ledger        |
|                                                                     |
|  checkpointer: thread memory      tracing: LangSmith                |
+---------------------------------------------------------------------+
```

FastAPI authenticates the caller and builds a `User`. The graph runs with that user in its runtime context, which tools read but the model cannot see or change. The model picks tools; tools compute every number in pandas and return JSON; the model writes the sentence. The response carries the answer plus tool outputs as evidence.

## Repo layout

```text
finance-agent/
  pyproject.toml
  README.md
  docker-compose.yml        # app + postgres with pgvector
  app/
    main.py                 # create_app, routes, lifespan
    auth.py                 # JWT -> User
    settings.py             # pydantic-settings, env only
    agent/
      graph.py              # build_graph
      tools.py              # thin wrappers that apply scope
      prompts.py
    analytics/
      ledger.py             # pure pandas: spend_change, unreconciled
      schema.py             # pandera schema for ledger extracts
    rag/
      ingest.py             # load, chunk, embed, upsert policies
  evals/
    golden.jsonl            # questions with exact expected numbers
    run_evals.py
  tests/
    test_ledger.py          # exact numeric tests, no LLM
    test_api.py             # TestClient + scripted fake model
  .github/workflows/ci.yml  # lint, tests, evals on every PR
```

The split that matters is `analytics/` versus `agent/`. Analytics functions take a DataFrame, return plain dicts and know nothing about LLMs, so you test them with exact numbers. Tools only apply the user's scope and serialize.

## The compact core

The whole system fits in one file when you swap real services for fakes: `GenericFakeChatModel` replays scripted tool calls and answers, `DeterministicFakeEmbedding` feeds an `InMemoryVectorStore`, and an in-memory DataFrame stands in for the ledger. Everything else is the real code path: the real `StateGraph`, the real `ToolNode`, the real FastAPI app tested through `TestClient`.

```python
"""Finance RAG agent, compact core: FastAPI + LangGraph + RAG + pandas tools."""
import json
import re
from dataclasses import dataclass

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (AIMessage, AIMessageChunk, SystemMessage,
                                     ToolMessage)
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langchain_core.vectorstores import InMemoryVectorStore
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel

# ---- data: a tiny AP ledger and three policy documents -----------------
LEDGER = pd.DataFrame({
    "invoice_id": ["INV-1", "INV-2", "INV-3", "INV-4", "INV-5", "INV-6"],
    "entity": ["IN01", "IN01", "IN01", "IN01", "US01", "IN01"],
    "vendor": ["Acme Cloud", "Acme Cloud", "Acme Cloud", "Zen Office",
               "Acme Cloud", "Zen Office"],
    "posted": pd.to_datetime(["2026-02-10", "2026-03-05", "2026-03-20",
                              "2026-03-11", "2026-03-15", "2026-02-14"]),
    "amount": [120_000, 125_000, 98_000, 15_000, 50_000, 14_000],  # int units
    "reconciled": [True, True, False, True, False, True],
})

POLICIES = [
    Document(id="POL-AP-01", page_content="Invoices above 500,000 need two "
             "approvers: the cost center owner and the AP manager.",
             metadata={"audience": "ap"}),
    Document(id="POL-AP-02", page_content="Unreconciled invoices older than "
             "30 days are escalated to the controller at month end.",
             metadata={"audience": "ap"}),
    Document(id="POL-TR-01", page_content="Treasury wire limits: a single "
             "signatory may release wires up to 2,000,000.",
             metadata={"audience": "treasury"}),
]
STORE = InMemoryVectorStore(DeterministicFakeEmbedding(size=256))
STORE.add_documents(POLICIES)


# ---- identity: who is asking, and what they may see ---------------------
@dataclass(frozen=True)
class User:
    id: str
    entities: frozenset[str]
    audiences: frozenset[str]  # which policy sets they may read


@dataclass
class Ctx:  # LangGraph runtime context: per-run, never sent to the model
    user: User


TOKENS = {  # stand-in for JWT validation
    "t-alice": User("alice", frozenset({"IN01"}), frozenset({"ap"})),
    "t-bob": User("bob", frozenset({"US01"}), frozenset({"ap", "treasury"})),
}
AUDIT: list[dict] = []


# ---- pure pandas logic: deterministic, unit-testable, no LLM ------------
def spend_change(df: pd.DataFrame, vendor: str, month: str) -> dict:
    rows = df[df["vendor"] == vendor]
    by_month = rows.groupby(rows["posted"].dt.strftime("%Y-%m"))["amount"].sum()
    prev = str(pd.Period(month, freq="M") - 1)
    cur_total = int(by_month.get(month, 0))
    prev_total = int(by_month.get(prev, 0))
    drivers = rows[rows["posted"].dt.strftime("%Y-%m") == month]
    return {"vendor": vendor, "month": month, "total": cur_total,
            "prev_month": prev, "prev_total": prev_total,
            "delta": cur_total - prev_total,
            "invoices": drivers.sort_values("amount", ascending=False)
                               [["invoice_id", "amount"]].to_dict("records")}


def unreconciled(df: pd.DataFrame) -> dict:
    open_ = df[~df["reconciled"]]
    return {"count": len(open_), "total": int(open_["amount"].sum()),
            "invoice_ids": open_["invoice_id"].tolist()}


def visible(user: User) -> pd.DataFrame:
    """Row-level security: the only door to the ledger."""
    return LEDGER[LEDGER["entity"].isin(user.entities)]


# ---- tools: thin wrappers that apply the caller's scope -----------------
@tool
def vendor_spend_change(vendor: str, month: str, runtime: ToolRuntime[Ctx]) -> str:
    """Spend for a vendor in a month (YYYY-MM) vs the previous month, with
    the invoices that make up the month. Use for 'why did spend change'."""
    return json.dumps(spend_change(visible(runtime.context.user), vendor, month))


@tool
def unreconciled_invoices(runtime: ToolRuntime[Ctx]) -> str:
    """Count, total and ids of invoices not yet reconciled."""
    return json.dumps(unreconciled(visible(runtime.context.user)))


@tool
def search_policies(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """Search finance policies. Cite results by their [id]."""
    allowed = runtime.context.user.audiences
    docs = STORE.similarity_search(
        query, k=3, filter=lambda d: d.metadata["audience"] in allowed)
    return "\n".join(f"[{d.id}] {d.page_content}" for d in docs)


TOOLS = [vendor_spend_change, unreconciled_invoices, search_policies]
SYSTEM = ("You answer finance questions for the signed-in user. Use tools for "
          "every number and copy numbers exactly; never do arithmetic yourself. "
          "Cite policy ids in brackets. If tools return nothing, say so.")


def record(request) -> None:  # one audit row per tool call, before it runs
    call = request.tool_call
    AUDIT.append({"user": request.runtime.context.user.id,
                  "tool": call["name"], "args": call["args"]})


def audit(request, execute):  # ToolNode hook for graph.invoke
    record(request)
    return execute(request)


async def aaudit(request, execute):  # same hook for graph.astream
    record(request)
    return await execute(request)


# ---- the LangGraph agent ------------------------------------------------
def build_graph(model, checkpointer=None):
    llm = model.bind_tools(TOOLS)

    def agent(state: MessagesState):
        return {"messages": [llm.invoke([SystemMessage(SYSTEM), *state["messages"]])]}

    g = StateGraph(MessagesState, context_schema=Ctx)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(TOOLS, wrap_tool_call=audit, awrap_tool_call=aaudit))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)  # tool calls? -> tools
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer or InMemorySaver())


# ---- the FastAPI service ------------------------------------------------
class Ask(BaseModel):
    question: str
    thread_id: str


def current_user(authorization: str | None = Header(default=None)) -> User:
    user = TOKENS.get((authorization or "").removeprefix("Bearer "))
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


def run_args(body: Ask, user: User) -> dict:
    # Namespace threads by user so nobody can resume someone else's chat.
    cfg = {"configurable": dict(thread_id=f"{user.id}:{body.thread_id}")}
    return dict(input={"messages": [("user", body.question)]},
                config=cfg, context=Ctx(user))


def create_app(graph) -> FastAPI:
    app = FastAPI(title="finance-agent")

    @app.post("/ask")
    def ask(body: Ask, user: User = Depends(current_user)):
        out = graph.invoke(**run_args(body, user))
        evidence = [m.content for m in out["messages"] if isinstance(m, ToolMessage)]
        return {"answer": out["messages"][-1].content, "evidence": evidence}

    @app.post("/ask/stream")
    async def ask_stream(body: Ask, user: User = Depends(current_user)):
        async def sse():
            async for chunk, meta in graph.astream(**run_args(body, user),
                                                   stream_mode="messages"):
                if meta["langgraph_node"] == "agent" and chunk.content:
                    yield "data: " + json.dumps({"token": chunk.content}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    return app


# ---- fakes and evals ----------------------------------------------------
class FakeToolModel(GenericFakeChatModel):
    """Replays scripted AIMessages; streams text word by word."""

    def bind_tools(self, tools, **kwargs):  # scripted replies ignore tools
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = next(self.messages)  # the base fake drops tool calls here
        parts = [msg.content] if msg.tool_calls else re.split(r"(\s)", msg.content)
        for part in parts:
            chunk = ChatGenerationChunk(message=AIMessageChunk(
                content=part, tool_calls=msg.tool_calls))
            if run_manager:
                run_manager.on_llm_new_token(part, chunk=chunk)
            yield chunk


def scripted(*replies: AIMessage) -> FakeToolModel:
    return FakeToolModel(messages=iter(replies))


def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", text)}


def grounded(answer: str, evidence: list[str]) -> bool:
    """Every number in the answer must appear in some tool output."""
    return numbers(answer) <= numbers(" ".join(evidence))


if __name__ == "__main__":
    # 1. Golden numeric evals on the pure functions (exact match, no LLM).
    in01 = LEDGER[LEDGER["entity"] == "IN01"]
    assert spend_change(in01, "Acme Cloud", "2026-03")["delta"] == 103_000
    assert unreconciled(in01) == {"count": 1, "total": 98_000,
                                  "invoice_ids": ["INV-3"]}

    # 2. End to end: Alice (IN01 only) asks why Acme spend jumped.
    answer = ("Acme Cloud spend rose from 120,000 in 2026-02 to 223,000 in "
              "2026-03 (+103,000), driven by INV-2 (125,000) and INV-3 (98,000).")
    graph = build_graph(scripted(
        call("vendor_spend_change", vendor="Acme Cloud", month="2026-03"),
        AIMessage(content=answer)))
    client = TestClient(create_app(graph))
    alice = {"Authorization": "Bearer t-alice"}
    r = client.post("/ask", headers=alice,
                    json={"question": "Why did Acme Cloud spend jump in March?",
                          "thread_id": "t1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert '"total": 223000' in body["evidence"][0]
    assert "INV-5" not in body["evidence"][0]  # US01 row never left the tool
    assert grounded(body["answer"], body["evidence"])
    assert not grounded("Spend rose by 110,000.", body["evidence"])
    assert AUDIT[-1]["user"] == "alice"
    assert AUDIT[-1]["args"] == dict(vendor="Acme Cloud", month="2026-03")

    # 3. Access control: no token -> 401; Alice's policy search skips treasury.
    r = client.post("/ask", json={"question": "x", "thread_id": "t"})
    assert r.status_code == 401
    graph = build_graph(scripted(
        call("search_policies", query="wire release limits"),
        AIMessage(content="No policy you can access covers wire limits.")))
    r = TestClient(create_app(graph)).post(
        "/ask", headers=alice, json={"question": "Wire limits?", "thread_id": "t2"})
    assert "POL-TR-01" not in r.json()["evidence"][0]
    assert "POL-AP-01" in r.json()["evidence"][0]

    # 4. Streaming: SSE tokens from the agent node only, then [DONE].
    graph = build_graph(scripted(
        call("unreconciled_invoices"),
        AIMessage(content="1 invoice is unreconciled: INV-3 (98,000).")))
    with TestClient(create_app(graph)).stream(
            "POST", "/ask/stream", headers=alice,
            json={"question": "Unreconciled invoices?", "thread_id": "t3"}) as r:
        lines = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    tokens = [json.loads(ln[6:])["token"] for ln in lines[:-1]]
    assert "".join(tokens) == "1 invoice is unreconciled: INV-3 (98,000)."
    assert lines[-1] == "data: [DONE]" and len(tokens) > 1
    assert AUDIT[-1]["tool"] == "unreconciled_invoices"
    print("capstone core: all checks passed")
```

What each piece demonstrates, so you can explain it line by line:

- **`spend_change` and `unreconciled` are pure pandas.** The model never adds numbers; it copies `"delta": 103000` from a tool. Amounts are integers to avoid float drift (use integer minor units or `Decimal` in production).
- **`visible(user)` is the only door to the ledger.** A new tool cannot forget the entity filter. Alice never sees INV-5 because the US01 row is dropped before any computation.
- **Identity travels in `context`, not tool arguments.** A `user_id` parameter could be set by the model or a prompt injection. `ToolRuntime[Ctx]` is injected by `ToolNode` and hidden from the schema the model sees.
- **Retrieval filters inside the search.** The `filter` runs before top-k, so a treasury chunk cannot take a slot and then be dropped.
- **One audit hook.** `wrap_tool_call` and `awrap_tool_call` record who called which tool with which arguments, for `invoke` and `astream`.
- **Threads are namespaced by user.** `"alice:t1"` and `"bob:t1"` are different checkpoints.
- **Streaming filters by node.** `stream_mode="messages"` yields token chunks with metadata; keeping `langgraph_node == "agent"` skips tool payloads.
- **`grounded()` is a cheap hallucination check.** Every number in the answer must appear in a tool output. It is crude (it misses "1.5 lakh" versus 150,000) but catches the costliest finance failure: a confident, invented total.

> **Gotcha:** `stream_mode="messages"` makes LangGraph stream the model even when the node calls `invoke`, and `GenericFakeChatModel` drops tool calls when streaming. Hence the `_stream` override. Real models stream tool calls as `tool_call_chunks`.

### Why an explicit graph and not `create_agent`

LangChain 1.x replaced `AgentExecutor` and `initialize_agent` with `langchain.agents.create_agent`, which builds the same loop on LangGraph and adds middleware (`HumanInTheLoopMiddleware`, `PIIMiddleware`, `SummarizationMiddleware`, `ModelFallbackMiddleware`). LangGraph's `create_react_agent` is deprecated in its favor. `create_agent(model, tools, context_schema=Ctx, checkpointer=...)` is fine for an MVP; the explicit `StateGraph` is easier to extend and explain once you add an approval node or a grounding check before `END`.

## Milestone plan

| Week | Build | Done when |
|---|---|---|
| 1 | Synthetic ledger (5,000 rows, 3 entities, 40 vendors), pandera schema, `analytics/ledger.py` | 15 exact-match tests pass, including empty months and unknown vendors |
| 2 | Policy ingest: 20 to 30 markdown policies, chunking, PGVector, audience metadata | A 30-question retrieval set reports hit@5; restricted docs never appear for the wrong audience |
| 3 | LangGraph agent, tools with `ToolRuntime`, Postgres checkpointer, `/ask` with JWT auth | Fake-model API tests green; a real-model demo answers all three seed questions |
| 4 | `/ask/stream`, audit table, Docker Compose, LangSmith tracing, retries and timeouts | Tokens stream in a browser; every tool call has an audit row and a trace |
| 5 | Golden set of 50 questions, eval runner in CI, cost and latency report, README, demo recording | CI blocks a PR that drops numeric accuracy below 100% or grounding below 98% |

Use synthetic data: real ledgers are confidential, and you can plant anomalies (a duplicate invoice, a vendor that doubles in March) whose answers you know exactly.

## Swapping fakes for real services

Each swap touches one function. All snippets here are illustrative (they need keys or services).

The model. `init_chat_model` picks the provider from a string; on Azure you pass the deployment name and API version.

```python
# illustrative: needs langchain-openai and Azure OpenAI credentials in env
from langchain.chat_models import init_chat_model

model = init_chat_model(
    "azure_openai:gpt-4.1-mini",
    azure_deployment="gpt41-mini-prod",   # your deployment, not the model name
    api_version="2024-10-21",
    temperature=0,
    timeout=30,
    max_retries=2,
)
# model = init_chat_model("openai:gpt-4.1-mini", temperature=0)
graph = build_graph(model)
```

Embeddings and the vector store. Move to PGVector, keep one audience value per chunk so the filter stays a simple `$in`, and see [pgvector]({{ '/postgresql/pgvector/' | relative_url }}) for index choices.

```python
# illustrative: needs Postgres with pgvector and langchain-postgres
from langchain.embeddings import init_embeddings
from langchain_postgres import PGVector

store = PGVector(
    embeddings=init_embeddings("azure_openai:text-embedding-3-large"),
    collection_name="finance_policies",
    connection="postgresql+psycopg://app@db:5432/finance",
    use_jsonb=True,
)
audience_filter = {"$in": sorted(user.audiences)}
docs = store.similarity_search(query, k=5, filter={"audience": audience_filter})
```

The ledger. Push the entity filter into SQL so the database does row-level security and you never load other entities' rows into memory. Postgres row-level security policies are a second layer on top.

```python
# illustrative: needs a database with an ap_invoices table
import pandas as pd
from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg://readonly@db:5432/finance")


def visible(user, since: str) -> pd.DataFrame:
    sql = text("SELECT invoice_id, entity, vendor, posted, amount, reconciled "
               "FROM ap_invoices WHERE entity = ANY(:entities) AND posted >= :since")
    return pd.read_sql(sql, engine,
                       params={"entities": sorted(user.entities), "since": since})
```

Memory and tracing. Replace `InMemorySaver` with `AsyncPostgresSaver` (package `langgraph-checkpoint-postgres`) opened in the FastAPI lifespan, and turn on LangSmith with environment variables.

```python
# illustrative: needs Postgres and langgraph-checkpoint-postgres
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def lifespan(app):
    async with AsyncPostgresSaver.from_conn_string(settings.db_url) as saver:
        await saver.setup()  # creates checkpoint tables once
        app.state.graph = build_graph(model, checkpointer=saver)
        yield
```

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=...          # from a secret store, never committed
export LANGSMITH_PROJECT=finance-agent
```

Auth. Replace `TOKENS` with JWT validation and build `User` from claims or an entitlements table, keeping `current_user` the single place that does it ([FastAPI security]({{ '/ai-stack/fastapi-security/' | relative_url }})).

## Evals that gate merges

Three layers, cheapest first:

1. **Tool evals.** Exact match on pandas functions. No LLM, runs in milliseconds, must be 100%.
2. **Retrieval evals.** For each policy question, the expected document id must appear in the top 5 (hit@5) and restricted ids must never appear for the wrong audience.
3. **End-to-end evals.** Real model, real tools. Numeric questions check that the expected numbers appear in the answer and `grounded()` passes. Policy questions check the citation id and score faithfulness and context precision with RAGAS or DeepEval, as in [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

A golden file (one JSON object per question, wrapped here for width) keeps expected values flat and exact:

```text
{"id": "q01", "user": "alice", "q": "Why did Acme spend jump in March?",
 "numbers": [223000, 103000], "ids": ["INV-2", "INV-3"]}
{"id": "q02", "user": "alice", "q": "Who approves a 7 lakh invoice?",
 "ids": ["POL-AP-01"], "forbid": ["POL-TR-01"]}
{"id": "q03", "user": "bob", "q": "Which invoices are unreconciled?",
 "numbers": [50000], "forbid": ["INV-3"]}
```

Run layers 1 and 2 on every PR, layer 3 when prompts, tools or models change and nightly. Log runs to LangSmith to diff prompt versions question by question.

## Coming from Django

| Django habit | Capstone equivalent |
|---|---|
| DRF serializer validates input | Pydantic `Ask` model on the route |
| Authentication class sets `request.user` | `Depends(current_user)` returns `User` |
| `get_queryset()` filters by tenant | `visible(user)` filters by entity |
| `StreamingHttpResponse` | `StreamingResponse` with an async generator |
| Django test `Client` | `fastapi.testclient.TestClient` |
| Celery task for ingestion | Scheduled ingest job (Celery, Databricks job) |

## Common mistakes

- **Letting the model total numbers.** Right most of the time is worse than always wrong. Compute in tools, check with `grounded()`.
- **User id as a tool argument.** The model can change it. Read identity from `ToolRuntime.context`.
- **Filtering retrieval after top-k.** Returns too few chunks or none. Filter inside the search.
- **Global thread ids.** Prefix with the user id, or store thread ownership and check it.
- **Live LLM calls in unit tests.** Slow and flaky. Use scripted fakes for wiring and a separate eval job for quality.
- **Answer without evidence.** Everyone asks "where did this number come from?" Return tool outputs and citations.
- **Loading the whole ledger per request.** Fine for a demo, a problem at 10 million rows. Push filters and aggregates to SQL.

## Presenting it in an interview

### README skeleton

```text
# finance-agent
One line: an assistant that answers AP policy and spend questions per user,
with every number computed by code and every answer traceable.

## Demo        gif + three example questions and answers
## Architecture  the diagram above, one paragraph
## Guarantees  numbers from tools only; entity-scoped data; audit per call
## Evals       table: tool 100%, retrieval hit@5, e2e numeric, faithfulness
## Run it      docker compose up; make seed; make evals
## Trade-offs  explicit graph vs create_agent; pandas vs SQL; what's next
```

### A 60-second pitch

"I built a finance assistant on FastAPI and LangGraph. Policy questions go through RAG over PGVector with audience filters, so a clerk never retrieves treasury documents. Numeric questions go to pandas tools over the ledger, and the model never does arithmetic: a grounding check fails any answer whose numbers aren't in tool outputs. User identity rides in LangGraph's runtime context, not in tool arguments, so the model can't widen its own scope. Every tool call is audited, answers stream over SSE, and CI runs exact-match numeric evals plus RAGAS on policy answers."

### Talking points with trade-offs

- **Why tools over text-to-SQL:** a fixed tool set is reviewable and testable; text-to-SQL is flexible but needs a semantic layer and SQL guards (see [AI system design for finance data]({{ '/ai-stack/finance-ai-system-design/' | relative_url }})).
- **Why pandas and not only SQL:** fast to iterate and test; aggregates move to SQL once data outgrows memory.
- **Numbers to quote:** your measured p50 and p95 latency, cost per question and eval pass rates. Never estimate what you could have measured.
- **What you'd add next:** hybrid search and reranking, an `interrupt()` approval before any write action, semantic caching for repeated policy questions, model routing by question type.

## Practice tasks

1. **Add `top_vendors(month, n)`.** Done: an exact-match test and a fake-model API test that checks the evidence.
2. **Add a grounding gate.** A node before `END` runs `grounded()` and, on failure, adds a correction message and loops once. Done: a scripted wrong answer is retried and the final one passes.
3. **Approval before action.** A `flag_invoice` tool that calls `interrupt()` and a `/ask/resume` route that sends `Command(resume=...)`. Done: the audit log shows request, approver and result.
4. **Real retrieval.** PGVector, real embeddings, 30 retrieval questions. Done: hit@5 per audience and zero restricted hits.
5. **Cost and latency report.** Trace 50 golden questions in LangSmith. Done: p50, p95, tokens and cost per question type.

## Interview questions

**How do you stop the model from inventing totals?** Tools compute every number; the prompt says copy, never calculate; a post-check compares answer numbers with tool outputs and retries on mismatch; evals measure numeric accuracy exactly.

**Where does access control live?** In code at the data boundary: the API authenticates, the user travels in runtime context, `visible(user)` filters ledger rows, retrieval filters by audience inside the search. The prompt is not a security boundary.

**Why `ToolRuntime` instead of a `user_id` parameter?** Injected arguments are hidden from the model's tool schema, so neither the model nor an injected instruction can change whose data a tool reads.

**How do you test an agent without paying for tokens?** Scripted fakes replay tool calls and answers, so routing, tools, auth and streaming are tested deterministically. Quality is a separate eval job with a real model.

**How does streaming work end to end?** `graph.astream(..., stream_mode="messages")` yields token chunks with node metadata; the route filters to the agent node and writes SSE `data:` lines; the client reads until `[DONE]`.

**What happens when a tool fails?** By default `ToolNode` turns invalid arguments into an error `ToolMessage` so the model can fix the call, and re-raises other exceptions. Pass `handle_tool_errors=True` (or a handler function) to return them as messages instead. For finance, add timeouts and return a clear "data unavailable" so the model never fills the gap with a guess.

**What would you change for production and scale?** Real JWT auth, PGVector with hybrid search, a Postgres checkpointer behind stateless pods, aggregates pushed to SQL or Databricks, an audit table with retention rules, PII redaction before model calls, LangSmith tracing and CI eval gates.

## Further reading

- LangGraph docs: Graph API overview, Persistence, Streaming.
- LangChain docs: Tools (the `ToolRuntime` section), Agents (`create_agent` and middleware).
- FastAPI docs: Dependencies, Custom response (`StreamingResponse`), Testing.
- pandas user guide: Group by, Time series / date functionality.
- LangSmith docs: Evaluation concepts. RAGAS docs: Metrics.
- In this site: [AI system design for finance data]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}), [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}), [interview question bank]({{ '/ai-stack/interview-question-bank/' | relative_url }}).
