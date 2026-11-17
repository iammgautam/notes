---
title: Tools and agents in LangChain
section: ai-stack
order: 17
phase: "Phase 3 · LangChain"
summary: Define typed tools, run the tool loop by hand, build agents with create_agent and middleware, and design read-only finance tools safe to expose.
---
After this note you can define typed tools, run the tool-calling loop yourself, build a LangChain 1.x agent with `create_agent`, add middleware for audit, guardrails, limits and human approval, and design finance tools (allow-listed SQL, pandas aggregation) that a model cannot abuse. This is the "agents with tool orchestration" line in the job description; the security half is what a regulated finance team will probe.

## Prerequisites

[LCEL and runnables]({{ '/ai-stack/langchain-lcel-and-runnables/' | relative_url }}). For the provider-level mechanics of tool calling, see [tool calling]({{ '/llm/tool-calling/' | relative_url }}).

## How tool calling works

The model never runs your code. Its `AIMessage.tool_calls` name a function and arguments. Your code validates the arguments, runs the function and sends the result back as a `ToolMessage` with the same `tool_call_id`. An agent is this loop with a stopping rule.

```text
  messages                 model                    your code
  --------                 -----                    ---------
  question  ------------>  AIMessage
                           tool_calls: name,  ----> validate args
                           args, id                 run function
                                                    ToolMessage(id)
  + AIMessage + ToolMsgs <-------------------------------+
            ------------>  final AIMessage (no tool_calls) -> done
```

## Defining tools

`@tool` turns a typed function into a `BaseTool`: name from the function, description from the docstring, argument schema from the type hints. For richer validation, pass a Pydantic `args_schema` to `@tool` or `StructuredTool.from_function`. Arguments are validated and coerced before your function runs, so `"2026-10-01"` arrives as a `date` and a malformed vendor id never reaches your code.

```python
from datetime import date
from typing import Literal

from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, ValidationError


class SpendArgs(BaseModel):
    vendor_id: str = Field(pattern=r"^V\d{3}$", description="Vendor master id, e.g. V001")
    start: date = Field(description="First posting date, inclusive")
    end: date = Field(description="Last posting date, inclusive")
    basis: Literal["posted", "paid"] = Field("posted", description="Invoices posted or cash paid")


def vendor_spend_impl(vendor_id: str, start: date, end: date, basis: str = "posted") -> str:
    days = (end - start).days + 1
    return f"{vendor_id} {basis} spend over {days} days: INR 400,000"


vendor_spend = StructuredTool.from_function(
    func=vendor_spend_impl,
    name="vendor_spend",
    description="Total spend with one vendor between two dates. Read-only.",
    args_schema=SpendArgs,
)

if __name__ == "__main__":
    params = convert_to_openai_tool(vendor_spend)["function"]["parameters"]
    assert params["properties"]["start"]["format"] == "date"
    assert params["properties"]["basis"]["enum"] == ["posted", "paid"]

    out = vendor_spend.invoke({"vendor_id": "V001", "start": "2026-10-01", "end": "2026-10-31"})
    assert out == "V001 posted spend over 31 days: INR 400,000"

    try:
        vendor_spend.invoke({"vendor_id": "Acme", "start": "2026-10-01", "end": "2026-10-31"})
    except ValidationError as exc:
        print("rejected:", exc.errors()[0]["loc"])
    else:
        raise AssertionError("expected ValidationError")
```

### Descriptions are prompts

The model picks tools from names and descriptions alone. Say **when** to use the tool, name the **id format** ("master data id such as V001, not the name"), state **limits** (read-only, max rows) and keep tools **narrow**: five clear tools beat one `query_anything` tool with a mode flag. `Literal` and `Field(description=...)` put enums and units into the schema.

## bind_tools and running tools yourself

`model.bind_tools(tools)` attaches the tool schemas to every call. `tool_calls` on the response is a list of dicts with `name`, `args` and `id`, already parsed from the provider's format. `tool.invoke(call)` with a whole tool call returns a `ToolMessage` with the right `tool_call_id`. Write this loop once and `create_agent` stops being magic.

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def vendor_terms(vendor_id: str) -> str:
    """Payment terms and currency for one vendor.

    Use for questions about due dates, terms or currency. vendor_id is the
    master data id such as V001, not the vendor's name.
    """
    return {"V001": "NET30, INR", "V002": "NET45, USD"}.get(vendor_id, "unknown vendor")


TOOLS = {t.name: t for t in [vendor_terms]}

# real: llm = init_chat_model("openai:gpt-4.1-mini", temperature=0).bind_tools([vendor_terms])
llm = FakeToolModel(messages=iter([
    AIMessage("", tool_calls=[
        {"name": "vendor_terms", "args": {"vendor_id": "V001"}, "id": "call_a"},
        {"name": "vendor_terms", "args": {"vendor_id": "V002"}, "id": "call_b"},
    ]),
    AIMessage("V001 is NET30 in INR; V002 is NET45 in USD."),
])).bind_tools([vendor_terms])


def run_turn(question: str, max_steps: int = 5) -> list:
    messages = [HumanMessage(question)]
    for _ in range(max_steps):
        ai = llm.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            return messages
        for call in ai.tool_calls:  # parallel calls arrive in one message
            messages.append(TOOLS[call["name"]].invoke(call))  # ToolCall in, ToolMessage out
    raise RuntimeError("tool loop did not finish")


if __name__ == "__main__":
    msgs = run_turn("Terms for V001 and V002?")
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage", "AIMessage"]
    assert [m.tool_call_id for m in msgs if isinstance(m, ToolMessage)] == ["call_a", "call_b"]
    assert msgs[2].content == "NET30, INR"
    print(msgs[-1].text)
```

Real providers also accept `tool_choice` (`"auto"`, `"any"` or a tool name) and, on OpenAI, `parallel_tool_calls=False`.

## Agents with create_agent

`create_agent` packages that loop as a LangGraph graph: a `model` node, a `tools` node and an edge that returns to the model while there are tool calls. It replaces `AgentExecutor`, `initialize_agent` and LangGraph's `create_react_agent`.

| Argument | Purpose |
|---|---|
| `model` | A chat model or a `"provider:model"` string |
| `tools` | Tools or plain functions |
| `system_prompt` | Instructions sent on every model call |
| `middleware` | Hooks around model and tool calls |
| `response_format` | Pydantic class for a typed final answer in `structured_response` |
| `context_schema` | Type of per-run context passed as `context=` |
| `checkpointer` | Persists state per `thread_id`; required for human approval |

Input and output are a state dict with `messages`. With `response_format=SpendSummary`, `out["structured_response"]` is a validated `SpendSummary`.

## Safe finance tools

An agent with a `run_sql(query: str)` tool is a SQL injection endpoint with a language model in front of it, and finance adds entity-level access rules and audit. The pattern that holds up in review:

- **Allow-list, do not generate.** The model picks a query name from a `Literal`; the SQL is yours and parameterized.
- **Identity from context.** Entity and user come from the authenticated request via `ToolRuntime.context`, hidden from the model's schema, so the model cannot ask for another entity.
- **Read-only connection.** A read-only role (here SQLite `PRAGMA query_only`) means even a bug cannot write.
- **Bounded output.** Cap rows and return compact CSV.
- **Aggregate in pandas.** The model picks group-by columns and a metric from allow-lists; pandas does the arithmetic.

```python
import sqlite3
from dataclasses import dataclass
from typing import Annotated, Literal

import pandas as pd
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

ROWS = [
    ("IN01", "V001", "Acme Cloud", "CC-IT", "2026-09-12", 190_000.0),
    ("IN01", "V001", "Acme Cloud", "CC-IT", "2026-10-03", 210_000.0),
    ("IN01", "V002", "Globex Audit", "CC-FIN", "2026-10-15", 240_000.0),
    ("US01", "V001", "Acme Cloud", "CC-IT", "2026-10-04", 999_999.0),  # other entity
]
db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE gl_entries (entity_id, vendor_id, vendor, cost_center, posted_on, amount)")
db.executemany("INSERT INTO gl_entries VALUES (?, ?, ?, ?, ?, ?)", ROWS)
db.execute("PRAGMA query_only = ON")  # this connection can no longer write
ledger = pd.read_sql("SELECT * FROM gl_entries", db)


@dataclass
class Caller:
    user_id: str
    entity_id: str  # from the auth token, never from the model


QUERIES = {
    "spend_by_month": (
        "SELECT substr(posted_on, 1, 7) AS month, SUM(amount) AS total FROM gl_entries "
        "WHERE entity_id = :entity_id AND vendor_id = :vendor_id GROUP BY month ORDER BY month"
    ),
    "largest_entries": (
        "SELECT posted_on, amount FROM gl_entries WHERE entity_id = :entity_id "
        "AND vendor_id = :vendor_id ORDER BY amount DESC LIMIT 5"
    ),
}
MAX_ROWS = 50


@tool
def run_finance_query(
    query_name: Literal["spend_by_month", "largest_entries"],
    vendor_id: Annotated[str, Field(pattern=r"^V\d{3}$", description="Vendor master id, e.g. V001")],
    runtime: ToolRuntime[Caller],
) -> str:
    """Run one pre-approved, read-only ledger query for the caller's entity.

    spend_by_month: monthly totals for a vendor. largest_entries: its top 5 postings.
    Returns CSV.
    """
    params = {"entity_id": runtime.context.entity_id, "vendor_id": vendor_id}
    return pd.read_sql(QUERIES[query_name], db, params=params).head(MAX_ROWS).to_csv(index=False)


@tool
def aggregate_spend(
    group_by: Annotated[list[Literal["vendor", "cost_center", "month"]], Field(min_length=1, max_length=2)],
    metric: Literal["sum", "mean", "count"],
    runtime: ToolRuntime[Caller],
) -> str:
    """Aggregate the caller's ledger amounts by one or two columns. Returns up to 20 CSV rows."""
    df = ledger[ledger["entity_id"] == runtime.context.entity_id]
    df = df.assign(month=df["posted_on"].str[:7])
    out = df.groupby(group_by, as_index=False)["amount"].agg(metric)
    return out.sort_values("amount", ascending=False).head(20).to_csv(index=False)


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id}


model = FakeToolModel(messages=iter([
    AIMessage("", tool_calls=[call("run_finance_query", {"query_name": "DELETE FROM gl_entries", "vendor_id": "V001"}, "c1")]),
    AIMessage("", tool_calls=[
        call("run_finance_query", {"query_name": "spend_by_month", "vendor_id": "V001"}, "c2"),
        call("aggregate_spend", {"group_by": ["cost_center"], "metric": "sum"}, "c3"),
    ]),
    AIMessage("Acme Cloud: INR 190,000 in September, 210,000 in October. CC-IT is the largest cost center."),
]))
agent = create_agent(
    model,
    tools=[run_finance_query, aggregate_spend],
    system_prompt="You are a read-only finance analyst. Use tools for every number you state.",
    context_schema=Caller,
)

if __name__ == "__main__":
    # the model never sees runtime in the schema
    assert "runtime" not in run_finance_query.tool_call_schema.model_json_schema()["properties"]

    out = agent.invoke(
        {"messages": [HumanMessage("Acme spend by month, and spend by cost center?")]},
        context=Caller(user_id="u-17", entity_id="IN01"),
    )
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]

    assert tool_msgs[0].status == "error" and "query_name" in tool_msgs[0].content  # not in allow-list
    assert tool_msgs[1].content == "month,total\n2026-09,190000.0\n2026-10,210000.0\n"  # IN01 only
    assert tool_msgs[2].content.splitlines()[1] == "CC-IT,400000.0"
    assert "999999" not in "".join(m.content for m in tool_msgs)

    try:
        db.execute("DELETE FROM gl_entries")
    except sqlite3.OperationalError as exc:
        print("write blocked:", exc)
    else:
        raise AssertionError("connection should be read-only")
```

The first call asked for a query not on the list: validation returned an error `ToolMessage` and no SQL ran. In production, put these tools on a PostgreSQL role with `SELECT` on reporting views only, plus row-level security by entity (see [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }})).

## Middleware

Middleware adds cross-cutting behavior without forking the loop. Hooks run at `before_agent`, `before_model`, `after_model` and `after_agent`; `wrap_model_call` and `wrap_tool_call` wrap each call like a decorator. Write them as decorated functions or subclass `AgentMiddleware`. The first middleware in the list is the outermost wrapper.

Built-ins in the installed `langchain` 1.5:

| Middleware | Finance use |
|---|---|
| `HumanInTheLoopMiddleware` | Approve journal postings or payments before they run |
| `PIIMiddleware` | Redact, mask or block emails, card numbers and custom patterns |
| `SummarizationMiddleware` | Compress long analyst sessions near the context limit |
| `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware` | Stop runaway loops, cap cost per run or thread |
| `ModelRetryMiddleware`, `ToolRetryMiddleware` | Back off on transient provider or database errors |
| `ModelFallbackMiddleware` | Switch provider on outage |
| `ToolErrorMiddleware` | Turn chosen exceptions into error `ToolMessage`s |

Also available: `LLMToolSelectorMiddleware` (pre-select tools when you have dozens) and `ContextEditingMiddleware` (clear old tool outputs). There is no single "guardrails" class; guardrails are a `before_model` or `after_model` hook that inspects state and can end the run with `jump_to`.

### Tool errors: three layers

1. **Invalid arguments** come back to the model as an error `ToolMessage` so it can fix the call (seen above).
2. **Expected failures:** raise `ToolException` and set `handle_tool_error = True`; the message goes to the model.
3. **Everything else** fails the run unless `ToolErrorMiddleware` converts it. Deliberate: unknown exceptions may carry hostnames or SQL.

```python
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    before_model,
    wrap_tool_call,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool

AUDIT_LOG = []


@wrap_tool_call
def audit_tool_calls(request, handler):
    """Record every tool call and its outcome, like an audit trail."""
    result = handler(request)
    AUDIT_LOG.append((request.tool_call["name"], request.tool_call["args"], getattr(result, "status", None)))
    return result


@before_model(can_jump_to=["end"])
def read_only_scope(state, runtime):
    """Refuse requests to move money before the model is even called."""
    last = state["messages"][-1]
    if isinstance(last, HumanMessage) and any(w in last.text.lower() for w in ("transfer", "pay ", "wire")):
        return {"messages": [AIMessage("I can only answer read-only questions.")], "jump_to": "end"}
    return None


@tool
def fx_rate(currency: str) -> str:
    """Month-end FX rate to INR for an ISO currency code such as USD."""
    rates = {"USD": "83.10"}
    if currency not in rates:
        raise ToolException(f"No month-end rate for {currency}. Ask the user for the rate.")
    return rates[currency]


fx_rate.handle_tool_error = True


@tool
def bank_balance(account: str) -> str:
    """Current balance of a bank account from the treasury system."""
    raise ConnectionError("treasury-db timeout at 10.2.3.4:5432")


def on_error(exc: Exception, request) -> str | None:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return f"{request.tool_call['name']} is temporarily unavailable. Tell the user; do not guess."
    return None  # anything else propagates


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def build_agent(replies):
    return create_agent(
        FakeToolModel(messages=iter(replies)),
        tools=[fx_rate, bank_balance],
        middleware=[
            audit_tool_calls,
            read_only_scope,
            ToolErrorMiddleware(on_error),
            ModelCallLimitMiddleware(run_limit=6),
            ToolCallLimitMiddleware(run_limit=10),
        ],
    )


if __name__ == "__main__":
    agent = build_agent([
        AIMessage("", tool_calls=[
            {"name": "fx_rate", "args": {"currency": "EUR"}, "id": "c1"},
            {"name": "bank_balance", "args": {"account": "HDFC-001"}, "id": "c2"},
        ]),
        AIMessage("I could not get the EUR rate or the HDFC balance; please share the rate."),
    ])
    out = agent.invoke({"messages": [HumanMessage("EUR rate and HDFC-001 balance?")]})
    errors = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in errors] == ["error", "error"]
    assert "No month-end rate for EUR" in errors[0].content
    assert "temporarily unavailable" in errors[1].content
    assert all("10.2.3.4" not in m.content for m in out["messages"])  # no internal detail leaked
    assert [name for name, _, _ in AUDIT_LOG] == ["fx_rate", "bank_balance"]

    blocked = build_agent([]).invoke({"messages": [HumanMessage("Transfer 5 lakh to Acme")]})
    assert blocked["messages"][-1].text == "I can only answer read-only questions."
```

The keyword match keeps the example small; real guardrails use a classifier or structured intent check with the same hook and early exit.

## Human approval for write actions

Gate tools that change something (post a journal, release a payment) with `HumanInTheLoopMiddleware`. The agent pauses before the tool runs, the checkpointer saves state, and the run resumes when a reviewer approves, edits or rejects the call, possibly hours later from another process. Mechanics: [LangGraph human in the loop]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}).

{% raw %}
```python
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def post_journal_entry(debit: str, credit: str, amount: float, memo: str) -> str:
    """Post a balanced journal entry to the general ledger."""
    return f"JE-1001 posted: Dr {debit} / Cr {credit} {amount:,.2f}"


je_args = {"debit": "6100", "credit": "2100", "amount": 45000.0, "memo": "Oct audit accrual"}
model = FakeToolModel(messages=iter([
    AIMessage("", tool_calls=[{"name": "post_journal_entry", "args": je_args, "id": "je1"}]),
    AIMessage("Posted JE-1001 for the October audit accrual."),
]))
agent = create_agent(
    model,
    tools=[post_journal_entry],
    middleware=[HumanInTheLoopMiddleware(interrupt_on={"post_journal_entry": {"allowed_decisions": ["approve", "reject"]}})],
    checkpointer=InMemorySaver(),  # use a Postgres or SQLite checkpointer in production
)
config = {"configurable": {"thread_id": "close-2026-10-IN01"}}

if __name__ == "__main__":
    paused = agent.invoke({"messages": [HumanMessage("Accrue 45,000 for the October audit")]}, config)
    request = paused["__interrupt__"][0].value["action_requests"][0]
    print("needs approval:", request["name"], request["args"])
    assert request["args"] == je_args
    assert not any(isinstance(m, ToolMessage) for m in paused["messages"])  # nothing posted yet

    done = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert done["messages"][-2].content == "JE-1001 posted: Dr 6100 / Cr 2100 45,000.00"
    assert done["messages"][-1].text.startswith("Posted JE-1001")
```
{% endraw %}

Rejecting with a `message` skips the tool and returns an error `ToolMessage`, so the agent can propose a different entry.

## Coming from Django

| Django / DRF | LangChain agents |
|---|---|
| A DRF view the client calls | A tool the model calls |
| Serializer validation on request data | Pydantic validation of tool arguments |
| `request.user` and object permissions | `ToolRuntime.context` with caller and entity |
| Django middleware around views | Agent middleware around model and tool calls |
| Admin approval workflow on a model | `HumanInTheLoopMiddleware` plus a checkpointer |

Treat every tool like a public endpoint with an untrusted, creative client: validate, authorize from the session, rate limit, log.

## Common mistakes

- **Generic SQL or Python tools.** `run_sql(query)` hands the model your database. Use named queries.
- **Identity in tool arguments.** A prompt injection can then ask for another entity. Use `ToolRuntime.context`.
- **Vague descriptions.** "Gets data" makes the model guess.
- **Huge tool outputs.** 10,000 rows blow the context and the bill. Cap and aggregate.
- **Every exception into the prompt.** Raw traces leak hosts and schema. Convert only known failures.
- **No limits.** A confused model loops until the recursion limit. Set call limits.

## Practice tasks

1. **Typed tool.** Write `open_invoices(vendor_id, as_of: date)` with a validator that rejects future dates. Done: tests for valid input, bad vendor id and future date.
2. **Entity isolation.** Add a third entity to the finance agent's data. Done: a parametrized test proves each caller sees only its own entity through both tools.
3. **Audit trail.** Make the `wrap_tool_call` hook write user id (from context), tool, args and status to SQLite. Done: one row per call, including rejected ones.
4. **Approval API.** Wrap the journal agent in FastAPI: `POST /runs` returns pending approvals, `POST /runs/{id}/decision` resumes. Done: a `TestClient` test covers approve and reject.

## Interview questions

**How does tool calling work end to end?**
Schemas go with the request; the model returns `tool_calls` (name, args, id); your code validates, runs the tool, appends a `ToolMessage` with the matching id and calls the model again until it answers without tool calls.

**What does `create_agent` add over a hand-written loop?**
Parallel tool execution, validation errors returned to the model, streaming, checkpointing, approval interrupts, structured final output and middleware, because it is a LangGraph graph.

**How do you let an agent query finance data safely?**
Identity from the authenticated request via runtime context, never tool arguments. Named, parameterized queries from an allow-list on a read-only role, row-level security as a second layer, row caps, aggregation in code and an audit record per call. Free-form text-to-SQL only against a sandboxed replica.

**How should tool errors be handled?**
Validation errors go back to the model so it can correct itself. Expected failures raise `ToolException` with a safe message. Unknown exceptions fail the run or are converted by `ToolErrorMiddleware` to a generic message, never a raw trace.

**What is agent middleware and give two finance uses?**
Hooks around model and tool calls. `HumanInTheLoopMiddleware` to approve postings, `PIIMiddleware` to redact customer emails, call limits to cap cost.

**How do you implement human approval for a payment tool?**
`HumanInTheLoopMiddleware` with `interrupt_on` for that tool and a durable checkpointer. The run pauses with the proposed call, the reviewer's decision resumes it with `Command(resume=...)` on the same `thread_id`.

## Further reading

- LangChain docs: "Tools"
- LangChain docs: "Agents" (`create_agent`, structured output, runtime context)
- LangChain docs: "Middleware" (built-in middleware and custom middleware)
- LangChain docs: "Human-in-the-loop"
- LangChain API reference: `StructuredTool`, `ToolRuntime`, `ToolException`
- LangGraph docs: "Interrupts" and "Persistence"
