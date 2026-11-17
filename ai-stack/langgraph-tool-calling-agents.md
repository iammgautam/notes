---
title: Tool-calling agents with LangGraph
section: ai-stack
order: 23
phase: "Phase 4 · LangGraph"
summary: Build a ReAct agent by hand with ToolNode, guard SQL and pandas tools, return structured answers, and know when to use create_agent instead.
---
After this note you can build the tool-calling loop yourself from a model node, `ToolNode` and `tools_condition`, write tools that are safe to hand to a model over finance data (read-only SQL, fixed pandas aggregations, tenant scope the model cannot change), force a validated final answer, and explain why `create_react_agent` is deprecated and what replaced it. "Agents with LangChain/LangGraph over finance data" in the job description means exactly this loop plus the guards around it.

## Prerequisites

[Control flow in LangGraph]({{ '/ai-stack/langgraph-control-flow/' | relative_url }}) and [LangChain tools and agents]({{ '/ai-stack/langchain-tools-and-agents/' | relative_url }}). For provider-level tool calling, see [tool calling]({{ '/llm/tool-calling/' | relative_url }}).

## The ReAct loop as a graph

A tool-calling agent is a two-node cycle. The model node sends the conversation plus tool schemas to the LLM. If the reply contains `tool_calls`, the tools node executes them and appends one `ToolMessage` per call; control returns to the model. If the reply has no tool calls, the run ends.

```text
          +---------------------------------------+
          |                                       |
          v              tool_calls?              |
 START -> agent --tools_condition--> tools -------+
 (model)    |            (ToolNode: run calls,
            |             append ToolMessages)
            +-- no tool calls --> END
```

Three prebuilt pieces in `langgraph.prebuilt` do the plumbing:

- `MessagesState`: state with a `messages` list and the `add_messages` reducer.
- `ToolNode(tools)`: reads the last `AIMessage`, runs each tool call (in parallel when there are several), and returns `ToolMessage`s matched by `tool_call_id`.
- `tools_condition`: routing function returning `"tools"` if the last message has tool calls, else `END`.

## Tools that are safe over finance data

The model chooses which tool to call and with what arguments. Treat those arguments as untrusted input from the internet. Three layers of defense:

1. **Narrow tools.** Prefer `spend_by(group_by, period)` over "run any pandas code". `Literal` parameters become JSON-schema enums, so the model cannot ask for an unsupported operation, and there is no `eval` or `exec` anywhere.
2. **Enforce in the engine, not the prompt.** For the SQL tool, the database itself refuses writes (`PRAGMA query_only`), and an authorizer callback denies every table except `gl_entries`, plus `PRAGMA`, `ATTACH` and DDL. The Python `sqlite3` driver also refuses multiple statements in one `execute`. Rows are capped at 50.
3. **Scope from context, never from arguments.** The tenant comes from `ToolRuntime.context`, which your API sets from the authenticated user. The parameter is hidden from the schema the model sees, so a prompt injection cannot ask for another tenant's ledger.

Errors are returned as text, not raised, so the model can read "not authorized" and correct its call. Save this block as `finance_tools.py`; the next two blocks import it.

```python
import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

import pandas as pd
from langchain.tools import ToolRuntime, tool


@dataclass
class Ctx:  # set by your API from the authenticated user, never by the model
    tenant_id: str


LEDGER = pd.DataFrame(
    [("acme", "6100", "2026-09", "Acme Travel", 1200.0),
     ("acme", "6100", "2026-10", "Acme Travel", 800.0),
     ("acme", "6200", "2026-10", "CloudCo", 4500.0),
     ("acme", "6200", "2026-10", "CloudCo", 500.0),
     ("globex", "6200", "2026-10", "CloudCo", 99_000.0)],
    columns=["tenant", "account", "period", "vendor", "amount"],
)
ALLOWED_TABLES = {"gl_entries"}


def readonly_connection(rows: pd.DataFrame) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    rows.to_sql("gl_entries", conn, index=False)
    conn.execute("PRAGMA query_only = ON")  # engine refuses writes

    def authorizer(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_READ:  # arg1 is the table name
            return sqlite3.SQLITE_OK if arg1 in ALLOWED_TABLES else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY  # PRAGMA, ATTACH, INSERT, DDL, ...

    conn.set_authorizer(authorizer)
    return conn


# One connection per tenant, standing in for a tenant-scoped DB session
CONNS = {t: readonly_connection(df.drop(columns="tenant"))
         for t, df in LEDGER.groupby("tenant")}


@tool
def run_sql(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """Run ONE read-only SQLite SELECT on gl_entries(account, period, vendor,
    amount). Returns at most 50 rows as JSON."""
    conn = CONNS[runtime.context.tenant_id]
    try:
        rows = conn.execute(query).fetchmany(50)
    except sqlite3.Error as exc:  # denied, syntax error, multiple statements
        return f"SQL error: {exc}. Rewrite as a single SELECT on gl_entries."
    return json.dumps(rows)


@tool
def spend_by(group_by: Literal["vendor", "account"], runtime: ToolRuntime[Ctx],
             period: str | None = None) -> str:
    """Total GL spend grouped by vendor or account, optionally for one
    period (YYYY-MM)."""
    df = LEDGER[LEDGER["tenant"] == runtime.context.tenant_id]
    if period is not None:
        df = df[df["period"] == period]
    totals = df.groupby(group_by)["amount"].sum().round(2)
    return json.dumps(totals.to_dict())


if __name__ == "__main__":
    acme = SimpleNamespace(context=Ctx("acme"))  # test stand-in for ToolRuntime
    total = run_sql.func("SELECT SUM(amount) FROM gl_entries", runtime=acme)
    assert json.loads(total) == [[7000.0]]  # globex rows are invisible
    for evil in ["DELETE FROM gl_entries",
                 "SELECT * FROM sqlite_master",
                 "SELECT 1; DROP TABLE gl_entries"]:
        assert run_sql.func(evil, runtime=acme).startswith("SQL error"), evil
    oct_spend = json.loads(spend_by.func("vendor", runtime=acme, period="2026-10"))
    assert oct_spend == {"Acme Travel": 800.0, "CloudCo": 5000.0}
    schema = spend_by.tool_call_schema.model_json_schema()  # what the model sees
    assert "runtime" not in schema["properties"]
    assert schema["properties"]["group_by"]["enum"] == ["vendor", "account"]
    print("ok")
```

> **In production:** on PostgreSQL the same layers are a dedicated role with `SELECT` on a few views only, `SET TRANSACTION READ ONLY`, a `statement_timeout`, and row-level security keyed on a session variable such as `app.tenant_id` set from the request. Point the tool at a read replica so a heavy query cannot slow down posting. See [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

## Building the loop by hand

`GenericFakeChatModel` replays scripted messages but has no `bind_tools`, so a three-line subclass adds a no-op one. The script makes the "model" do what real models do: call two tools at once, try a forbidden write, recover, and answer. The routing, tool execution, error feedback and tenant injection are all real.

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from finance_tools import Ctx, run_sql, spend_by


class ScriptedToolModel(GenericFakeChatModel):
    """Replays scripted AIMessages. A real model picks tools itself."""

    def bind_tools(self, tools, **kwargs):
        return self


def call(name: str, args: dict, id: str) -> dict:
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


# Real model (illustrative):
# from langchain.chat_models import init_chat_model
# llm = init_chat_model("openai:gpt-4.1-mini", temperature=0)
llm = ScriptedToolModel(messages=iter([
    AIMessage("", tool_calls=[  # two calls in one turn run in parallel
        call("spend_by", {"group_by": "vendor", "period": "2026-10"}, "c1"),
        call("run_sql", {"query": "DELETE FROM gl_entries"}, "c2"),
    ]),
    AIMessage("", tool_calls=[call("run_sql", {"query":
        "SELECT COUNT(*) FROM gl_entries WHERE period = '2026-10'"}, "c3")]),
    AIMessage("October spend: CloudCo 5000.00, Acme Travel 800.00 "
              "across 3 GL entries."),
]))

TOOLS = [run_sql, spend_by]
llm_with_tools = llm.bind_tools(TOOLS)
SYSTEM = SystemMessage(
    "You answer questions about GL spend. Get every number from a tool. "
    "If a tool returns an error, fix the call; never guess figures.")


def agent(state: MessagesState) -> dict:
    return {"messages": [llm_with_tools.invoke([SYSTEM, *state["messages"]])]}


builder = StateGraph(MessagesState, context_schema=Ctx)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition)  # "tools" or END
builder.add_edge("tools", "agent")
graph = builder.compile()

if __name__ == "__main__":
    out = graph.invoke(
        {"messages": [HumanMessage("Who did we spend most with in October?")]},
        {"recursion_limit": 12},  # caps model+tool turns for this request
        context=Ctx(tenant_id="acme"),
    )
    msgs = out["messages"]
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]
    assert '"CloudCo": 5000.0' in tool_msgs[0].content  # acme only
    assert tool_msgs[1].content.startswith("SQL error")  # write was refused
    assert tool_msgs[2].content == "[[3]]"
    assert msgs[-1].content.startswith("October spend")
    assert len(msgs) == 7  # human, ai, 2 tools, ai, tool, ai
    print("ok")
```

Details worth knowing:

- The system prompt is added at call time, not stored in state, so it never duplicates across turns and you can change it without migrating saved threads.
- `ToolNode`'s default error handling returns a message to the model only for invalid arguments (a `ToolInvocationError`); any other exception propagates and fails the run. Pass `handle_tool_errors=True` (or an exception tuple or a handler function) to turn more failures into `ToolMessage`s. Returning error strings from your own tools, as above, is the most explicit option.
- Every `AIMessage` with `tool_calls` must be followed by a `ToolMessage` for each call ID before the next model call, or most providers reject the request. `ToolNode` guarantees this; hand-written tool nodes often do not.
- Writing the loop yourself is worth it when you need extra state (retrieved documents, an approval flag), a non-ReAct step (validate SQL with a second model before running it), or per-node retries and timeouts.

## Structured final answers

Free text is fine for chat. A finance API that feeds a dashboard or a reconciliation job needs a typed answer with the figures and their sources. Two ways to get one:

**In a hand-built graph**, route "no more tool calls" to a final node that calls the model with a schema. It costs one extra model call per run but keeps the loop unchanged (illustrative):

```python
def respond(state: ReportState) -> dict:  # ReportState adds `final: SpendAnswer`
    answer = llm.with_structured_output(SpendAnswer).invoke(state["messages"])
    return {"final": answer}

def route(state: ReportState) -> Literal["tools", "respond"]:
    return "tools" if state["messages"][-1].tool_calls else "respond"
```

**With `create_agent`**, pass `response_format`. `ToolStrategy(Schema)` exposes the schema as one more tool; when the model calls it, the arguments are validated and the run ends with `structured_response` set. Invalid arguments are sent back to the model to fix (`handle_errors=True` by default). `ProviderStrategy(Schema)` uses the provider's native JSON-schema mode instead. Passing the bare schema class lets `create_agent` pick provider-native when the model supports it and fall back to the tool strategy otherwise.

```python
from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from finance_tools import Ctx, run_sql, spend_by


class ScriptedToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class SpendAnswer(BaseModel):
    """Final answer about vendor spend."""
    top_vendor: str
    amount: float = Field(ge=0)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    sources: list[str] = Field(description="Tools the figures came from")


llm = ScriptedToolModel(messages=iter([
    AIMessage("", tool_calls=[{"name": "spend_by", "id": "c1",
        "args": {"group_by": "vendor", "period": "2026-10"}}]),
    AIMessage("", tool_calls=[{"name": "SpendAnswer", "id": "c2",
        "args": {"top_vendor": "CloudCo", "amount": 5000.0,
                 "period": "2026-10", "sources": ["spend_by"]}}]),
]))

agent = create_agent(
    llm,  # or a string such as "openai:gpt-4.1-mini"
    tools=[run_sql, spend_by],
    system_prompt="Answer GL spend questions using tools only.",
    response_format=ToolStrategy(SpendAnswer),
    context_schema=Ctx,
    middleware=[ModelCallLimitMiddleware(run_limit=8)],
)

if __name__ == "__main__":
    out = agent.invoke({"messages": [HumanMessage("Top vendor in October?")]},
                       context=Ctx(tenant_id="acme"))
    answer = out["structured_response"]
    assert isinstance(answer, SpendAnswer)
    assert answer.top_vendor == "CloudCo" and answer.amount == 5000.0
    assert '"CloudCo": 5000.0' in out["messages"][2].content  # real tool ran
    print(answer.model_dump_json())
```

Schema validation proves the shape, not the truth. To reduce hallucinated figures, check `amount` against the tool output in code before returning it to the caller, and fail closed if they disagree.

## create_react_agent vs create_agent

`langgraph.prebuilt.create_react_agent` was the prebuilt agent before LangChain 1.0. In `langgraph` 1.2 it still works but emits `LangGraphDeprecatedSinceV10`: "create_react_agent has been moved to `langchain.agents`", with removal planned for LangGraph 2.0. Use `langchain.agents.create_agent` for new code. It returns a compiled LangGraph graph, so checkpointers, streaming and interrupts work the same way.

| `create_react_agent` | `create_agent` |
|---|---|
| `prompt=` | `system_prompt=` (dynamic prompts via middleware) |
| `pre_model_hook` / `post_model_hook` | Middleware: `before_model`, `after_model`, `wrap_model_call` |
| Node names `agent`, `tools` | Node names `model`, `tools` (update stream consumers) |
| `state_schema` (TypedDict or Pydantic) | `state_schema` extending `AgentState` |
| `response_format` (extra model call) | `response_format` with tool or provider strategy, in the loop |

Middleware is where the cross-cutting policy goes: `ModelCallLimitMiddleware` and `ToolCallLimitMiddleware` cap loops, `ModelFallbackMiddleware` switches provider on failure, `PIIMiddleware` redacts, `SummarizationMiddleware` trims long histories, and `HumanInTheLoopMiddleware` pauses before risky tools.

**Which to use:** start with `create_agent` when the flow is "model, tools, repeat" plus policies that middleware covers. Write the graph yourself when the workflow has fixed steps around the agent (classify, retrieve, validate, approve), needs custom state, or the agent is one node inside a larger graph. Both produce the same runtime, so moving between them is cheap.

## Coming from Django

| Django / DRF | Agent equivalent |
|---|---|
| A DRF action with a serializer | A tool with a typed signature and docstring |
| Serializer `ChoiceField` | `Literal[...]` parameter (schema enum) |
| `request.user` scoping a queryset | `ToolRuntime.context` scoping the query |
| Permission classes | Engine-level read-only role, authorizer, RLS |
| Response serializer | `response_format` schema |
| Throttling | Model and tool call limits |

## Common mistakes

- **Tenant or user ID as a tool argument.** The model can be talked into changing it. Inject it from context.
- **"Run arbitrary pandas" tools** built on `eval` or `exec`. Expose a few fixed aggregations instead.
- **Read-only by prompt only.** "Only run SELECT" in the docstring is not a control. Enforce it in the database.
- **Unbounded tool results.** A `SELECT *` that returns 200,000 rows blows the context window and the bill. Cap rows and summarize.
- **Raising from tools on expected failures.** The run dies instead of letting the model retry. Return a clear error string.
- **Teaching `create_react_agent` or `AgentExecutor` as current.** Both are legacy; use `create_agent` or a hand-built graph.
- **No loop cap.** Set `recursion_limit` per invocation or use `ModelCallLimitMiddleware`.

## Practice tasks

1. **Add a vendor master tool.** `vendor_details(vendor)` returns payment terms from a dict, tenant-scoped through `ToolRuntime`. Done: a scripted run calls it alongside `spend_by` in one turn, and an assert checks both `ToolMessage`s.
2. **SQL review node.** Insert a `check_sql` node between `agent` and `tools` that rejects queries without a `WHERE period` filter by returning a `ToolMessage` error with the right `tool_call_id`. Done: a scripted unfiltered query never reaches the database.
3. **Grounded answer check.** After the `create_agent` run, compare `structured_response.amount` with the number in the `spend_by` `ToolMessage`. Done: a script where the model reports 5,500 raises a `ValueError`.
4. **Port a legacy agent.** Rewrite a `create_react_agent` example with `prompt` and `pre_model_hook` using `create_agent`, `system_prompt` and a `before_model` middleware. Done: runs on `langchain` 1.5 with no deprecation warning (`-W error::DeprecationWarning`).

## Interview questions

**Walk me through a ReAct agent in LangGraph.**
`MessagesState`; an agent node that calls a model bound to tools; a `ToolNode` that executes the tool calls and appends `ToolMessage`s; `tools_condition` routing to tools or `END`; an edge from tools back to the agent; a recursion limit.

**How do you stop an agent from writing to the database?**
Not with the prompt. A read-only database role or connection, an allowlist of tables or views, statement timeouts and row caps, and RLS for tenant scope. The tool is a thin, typed wrapper.

**How does the tool know which tenant it is serving?**
From run context injected through `ToolRuntime`, set by the API from the authenticated user. It is not in the tool's schema, so the model cannot see or change it.

**What happens when a tool raises?**
With `ToolNode`'s default, invalid-argument errors go back to the model as a `ToolMessage`; other exceptions fail the run. Configure `handle_tool_errors`, or return error strings from tools for expected failures.

**Is `create_react_agent` still the way to build agents?**
No. It is deprecated since LangGraph 1.0 and moved to `langchain.agents.create_agent`, which adds middleware and in-loop structured output. It still runs in 1.2 with a deprecation warning.

**How do you get a structured final answer?**
`create_agent(response_format=ToolStrategy(Schema))` or `ProviderStrategy`, read from `structured_response`. In a custom graph, a final node with `with_structured_output`. Then verify numbers against tool outputs.

**When would you hand-build the graph instead of using `create_agent`?**
When the agent is one step in a larger workflow, needs custom state or deterministic steps such as SQL review and approval, or needs per-node retries and timeouts.

**How do you test an agent without calling a model?**
A fake chat model that replays scripted `AIMessage`s with tool calls. Assert on the tool calls made, the `ToolMessage` contents and the final state, then run evals against the real model separately.

## Further reading

- LangChain docs: "Agents" and "Structured output"
- LangChain docs: "Middleware" (built-in middleware list)
- LangChain docs: "Tools" (the `ToolRuntime` and context section)
- LangGraph docs: "Build a custom agent" / "Use the graph API"
- LangGraph reference: `ToolNode` and `tools_condition`
- LangChain docs: "LangChain v1 migration guide"
