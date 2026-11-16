---
title: Multi-agent systems with LangGraph
section: ai-stack
order: 26
phase: "Phase 4 · LangGraph"
summary: When splitting work across agents pays off, and how to build supervisors, handoffs and nested teams in LangGraph with shared and private state.
---

After this note you can decide whether a problem needs more than one agent, build a supervisor that routes work between specialists, hand control from one agent to another with `Command`, and nest teams as subgraphs that keep their own private state. Finance work splits along natural lines: a researcher reads policy documents, a SQL analyst queries the warehouse with a read-only role, a writer produces the report. The interview question is rarely "build a multi-agent system". It is "why not one agent?", and you need a good answer.

## Prerequisites

`Command(goto=...)` routing from [LangGraph control flow]({{ '/ai-stack/langgraph-control-flow/' | relative_url }}) and `create_agent` from [Tool-calling agents in LangGraph]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}). The previous note, [Human-in-the-loop with LangGraph]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}), covers approvals, which work the same way inside multi-agent graphs.

## When multiple agents help, and when they hurt

In this note an agent is a bundle: a prompt, a set of tools, a model, and its own message history. A multi-agent system has several bundles plus something (code or a model) that decides who acts next.

| Splitting helps when | Splitting hurts when |
|---|---|
| One agent has 20+ tools and picks the wrong ones | The steps always run in the same order (use a workflow) |
| Intermediate work (failed queries, raw rows) bloats context | Each hop adds an LLM call you pay for in latency and tokens |
| Roles need different permissions (read-only SQL vs payments) | Context is lost at every handoff, like a game of telephone |
| Roles need different models (cheap router, strong writer) | Errors compound: three 90%-reliable hops are about 73% |
| Teams own and evaluate components separately | Agents loop, defer to each other, or argue |

> **Rule of thumb:** start with one agent and well-designed tools, or a fixed workflow. Split when evals show tool-selection errors or context overflow, and split along permission boundaries first, because that is a security win, not just a quality one.

## Three shapes

```text
 supervisor               handoffs (swarm)          hierarchical
 ----------               ----------------          ------------
    [supervisor]          [triage] --> [sql]            [top]
    /    |     \              ^          |             /     \
 [res] [sql] [writer]         +--------[writer]   [team A]   [team B]
 workers report back      the active agent picks  each team is a
 to the hub each turn     the next one directly   subgraph with its
                                                  own router
```

| Shape | Who decides next | Cost per hop | Good for |
|---|---|---|---|
| Supervisor | A router model, every turn | Router call + worker call | Open-ended requests, central control |
| Handoffs | The active agent, via a tool | Worker call only | Conversational flows, triage desks |
| Hierarchical | Routers at each level | Highest | Large systems with separate teams |
| Agents as tools | The orchestrator's tool calls | Orchestrator + subagent | Isolating noisy subtasks |

## Supervisor: a router node choosing specialists

The supervisor is a node that asks a model for a structured `Route` decision and returns `Command(goto=...)`. Workers are plain nodes, or whole subgraphs, that append a named message and return to the supervisor. The example has three specialists: a policy researcher, an analytics team (itself a subgraph that writes and runs SQL), and a writer. Fake models replace real ones so it runs without API keys, but the SQL really executes against SQLite.

```python
import sqlite3
from typing import Annotated, Literal, TypedDict

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from pydantic import BaseModel, Field


class FakeToolModel(GenericFakeChatModel):
    """Scripted stand-in. Real code: init_chat_model("openai:gpt-4.1-mini")."""

    def bind_tools(self, tools, **kwargs):
        return self


class Route(BaseModel):
    """Choose who acts next, or FINISH when the answer is ready."""

    next: Literal["researcher", "analytics_team", "writer", "FINISH"]
    reason: str = Field(description="One line, for the trace")


def route(to: str, why: str) -> AIMessage:  # the tool call a real model would emit
    return AIMessage(content="", tool_calls=[
        {"name": "Route", "args": {"next": to, "reason": why}, "id": f"route-{to}"}
    ])


supervisor_llm = FakeToolModel(messages=iter([
    route("researcher", "need the spend policy"),
    route("analytics_team", "need Q3 totals by vendor"),
    route("writer", "have policy and numbers"),
    route("FINISH", "report written"),
])).with_structured_output(Route)
researcher_llm = FakeToolModel(messages=iter([AIMessage(
    "Policy FIN-07: quarterly spend above 50,000 with one vendor needs CFO review.")]))
sql_llm = FakeToolModel(messages=iter([AIMessage(
    "SELECT vendor, SUM(amount) FROM spend WHERE quarter = 'Q3' "
    "GROUP BY vendor ORDER BY 2 DESC")]))
writer_llm = FakeToolModel(messages=iter([AIMessage(
    "Q3: Acme Logistics at 72,500 exceeds the FIN-07 threshold and needs CFO review.")]))

DB = sqlite3.connect(":memory:", check_same_thread=False)
DB.execute("CREATE TABLE spend (vendor TEXT, quarter TEXT, amount REAL)")
DB.executemany("INSERT INTO spend VALUES (?, ?, ?)", [
    ("Acme Logistics", "Q3", 40_000), ("Acme Logistics", "Q3", 32_500),
    ("Northwind Paper", "Q3", 8_200), ("Acme Logistics", "Q2", 15_000),
])
DB.execute("PRAGMA query_only = ON")  # the analyst can read, never write


# ---------- analytics team: a subgraph with private keys ----------
class AnalyticsState(TypedDict, total=False):
    messages: Annotated[list, add_messages]  # shared with the parent (same key)
    sql: str                                 # private to this team
    rows: list                               # private to this team


def write_sql(state: AnalyticsState) -> dict:
    return {"sql": sql_llm.invoke(state["messages"]).content}


def run_sql(state: AnalyticsState) -> dict:
    return {"rows": DB.execute(state["sql"]).fetchall()}


def report_rows(state: AnalyticsState) -> dict:
    lines = ", ".join(f"{vendor}: {total:,.0f}" for vendor, total in state["rows"])
    return {"messages": [AIMessage(f"Q3 spend by vendor: {lines}", name="sql_analyst")]}


team = StateGraph(AnalyticsState)
team.add_node(write_sql)
team.add_node(run_sql)
team.add_node(report_rows)
team.add_edge(START, "write_sql")
team.add_edge("write_sql", "run_sql")
team.add_edge("run_sql", "report_rows")
team.add_edge("report_rows", END)
analytics_team = team.compile()


# ---------- parent graph: supervisor + specialists ----------
class TeamState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    turns: int


SUPERVISOR_PROMPT = (
    "You coordinate a finance team: researcher (policies), analytics_team (SQL over "
    "vendor spend), writer (final answer). Pick one worker per turn, FINISH when done."
)
MAX_TURNS = 6


def supervisor(state: TeamState) -> Command[
    Literal["researcher", "analytics_team", "writer", "__end__"]
]:
    turns = state.get("turns", 0)
    if turns >= MAX_TURNS:  # hard stop in code, whatever the model says
        return Command(goto=END)
    decision = supervisor_llm.invoke([SystemMessage(SUPERVISOR_PROMPT), *state["messages"]])
    goto = END if decision.next == "FINISH" else decision.next
    return Command(goto=goto, update={"turns": turns + 1})


def researcher(state: TeamState) -> dict:
    reply = researcher_llm.invoke(state["messages"])
    return {"messages": [AIMessage(reply.content, name="researcher")]}


def writer(state: TeamState) -> dict:
    reply = writer_llm.invoke(state["messages"])
    return {"messages": [AIMessage(reply.content, name="writer")]}


g = StateGraph(TeamState)
g.add_node(supervisor)
g.add_node(researcher)
g.add_node("analytics_team", analytics_team)  # a compiled graph is a node
g.add_node(writer)
g.add_edge(START, "supervisor")
for worker in ("researcher", "analytics_team", "writer"):
    g.add_edge(worker, "supervisor")  # workers always report back
graph = g.compile()

if __name__ == "__main__":
    out = graph.invoke({"messages": [("user", "Which vendors need CFO review for Q3?")]})
    speakers = [m.name for m in out["messages"] if m.type == "ai"]
    assert speakers == ["researcher", "sql_analyst", "writer"]
    assert "72,500" in out["messages"][2].content
    assert out["turns"] == 4
    assert "sql" not in out and "rows" not in out  # team-private keys stay inside
    print(out["messages"][-1].content)
```

What to notice:

- **The supervisor only routes.** Structured output with a `Literal` restricts it to real node names, so a made-up worker fails validation in the supervisor instead of surfacing as a routing error deeper in the run.
- **Loop limits live in code.** `MAX_TURNS` ends the run even if the model never says FINISH. Set `recursion_limit` in the run config as a second backstop.
- **Workers sign their messages** with `name=`. The router and your traces can then tell who said what in a shared history.
- **Permissions come from the database, not the prompt.** `PRAGMA query_only` is the SQLite version. In Postgres, give the analyst a role with `SELECT` on a few reporting views and a `statement_timeout`.
- **Check whether you need a router at all.** This request always runs research, SQL, then writing. If that holds for your traffic, replace the supervisor with three fixed edges: one fewer LLM call per hop and a deterministic test.

## Shared and private state

The parent's state is the shared blackboard. A subgraph compiled from its own schema shares only the keys whose names match the parent's. In the example, `analytics_team` reads and writes `messages`, while `sql` and `rows` never leave it. The writer does not see raw rows and the supervisor's prompt stays small.

| Need | How |
|---|---|
| Everyone sees it | Key in the parent schema |
| Only one team sees it | Key only in that subgraph's schema |
| Team uses a different schema entirely | Call the subgraph inside a node and map fields in and out yourself |
| One node reads a subset | `add_node(..., input_schema=NodeInput)` |
| Per-agent message history | Separate key per agent (`research_messages`), or agents as tools |

Private state is also a data-handling control. If the analytics team's rows contain vendor bank details, keeping them out of the shared history keeps them out of every later prompt and every trace that renders it.

## Hierarchical teams with subgraphs

The analytics team above is already a level of hierarchy. Scale it up by giving each team its own supervisor inside its subgraph and letting the top-level supervisor route between teams. Practical points:

- Compile subgraphs without a checkpointer. They inherit the parent's, so an `interrupt()` inside a team pauses the whole run and resumes correctly on the parent thread.
- Stream with `subgraphs=True` to see events from inside teams; each event carries a namespace like `("analytics_team:<task_id>",)`. See [Streaming and deploying LangGraph apps]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}).
- Two levels is usually the useful maximum. Each level adds a router call per hop and another place where context gets summarized away.

## Handoffs with Command and tools

In a handoff, the active agent decides who goes next; there is no hub. You have already seen the node version: return `Command(goto="other_node")`. The tool version lets a model trigger the handoff. Agents built with `create_agent` are subgraphs, so the tool returns a `Command` aimed at the parent graph with `graph=Command.PARENT`.

```python
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class DeskState(MessagesState):
    task: str


@tool
def transfer_to_sql_analyst(task: str, runtime: ToolRuntime) -> Command:
    """Hand the conversation to the SQL analyst for spend totals and counts."""
    receipt = ToolMessage("Transferred to sql_analyst", tool_call_id=runtime.tool_call_id)
    return Command(
        goto="sql_analyst",
        graph=Command.PARENT,  # route in the parent graph, not inside this agent
        update={"messages": [*runtime.state["messages"], receipt], "task": task},
    )


handoff_call = {"name": "transfer_to_sql_analyst",
                "args": {"task": "Q3 spend by vendor"}, "id": "h1"}
triage = create_agent(
    FakeToolModel(messages=iter([AIMessage("", tool_calls=[handoff_call])])),
    tools=[transfer_to_sql_analyst],
    system_prompt="Answer policy questions yourself. Transfer numeric questions.",
    name="triage",
)
sql_analyst = create_agent(
    FakeToolModel(messages=iter([AIMessage("Acme Logistics: 72,500. Northwind: 8,200.")])),
    tools=[],  # real version: a read-only run_sql tool
    name="sql_analyst",
)

g = StateGraph(DeskState)
g.add_node("triage", triage, destinations=("sql_analyst",))  # for drawing only
g.add_node("sql_analyst", sql_analyst)
g.add_edge(START, "triage")
g.add_edge("sql_analyst", END)
graph = g.compile()

if __name__ == "__main__":
    out = graph.invoke({"messages": [("user", "What did we spend per vendor in Q3?")]})
    trail = [(m.type, m.name) for m in out["messages"]]
    assert trail == [("human", None), ("ai", "triage"),
                     ("tool", "transfer_to_sql_analyst"), ("ai", "sql_analyst")]
    assert out["task"] == "Q3 spend by vendor"
    print("ok")
```

The update carries the agent's whole history plus a `ToolMessage`. The parent has not seen the triage agent's tool-calling message yet, and every tool call needs a matching result or the next model call fails validation on most providers. `ToolRuntime` is LangChain 1.x's injected argument that gives a tool its call id, state, context and store; the model never sees it in the schema.

## Agents as tools

The lightest multi-agent pattern needs no graph wiring: wrap a specialist in a tool and give it to an orchestrator agent. The orchestrator sees only the specialist's final answer, so failed queries and raw rows stay isolated by construction.

```python
@tool
def ask_sql_analyst(question: str) -> str:
    """Answer one self-contained question about vendor spend totals."""
    result = sql_analyst.invoke({"messages": [{"role": "user", "content": question}]})
    return result["messages"][-1].content


orchestrator = create_agent(
    model,
    tools=[ask_sql_analyst, ask_policy_researcher],
    system_prompt="Delegate numbers to ask_sql_analyst and policy to ask_policy_researcher.",
)
```

LangChain's 1.x multi-agent docs call this the subagents pattern and present it next to handoffs. It gives up direct handoffs (control always returns to the orchestrator) in exchange for one place to read the whole flow.

## Prebuilt packages

Two separate packages wrap these patterns: `langgraph-supervisor` (`create_supervisor`) and `langgraph-swarm` (`create_swarm`, `create_handoff_tool`). They are not part of `langgraph` itself and are not installed in this stack. Both produce ordinary LangGraph graphs, which makes them fine for prototypes, but you give up control over what each agent sees and how messages are passed. Check that their release supports your `langgraph` 1.x version before adopting them; the patterns above are small enough to own.

## Coming from Django

| Django | Multi-agent LangGraph |
|---|---|
| A dispatcher view that calls service functions | Supervisor node routing with `Command(goto=...)` |
| Django apps with their own models | Subgraphs with their own state schema |
| Request-scoped data passed between services | Shared parent state keys |
| An app's internal tables, never serialized in the API | Subgraph-private keys |
| `redirect()` to another view | A handoff tool returning `Command(graph=Command.PARENT)` |
| A read-only `DATABASES` alias plus a DB router | A read-only DB role for the SQL agent |
| Celery canvas `chain` | Fixed edges, when no routing decision is needed |

## Common mistakes

- **Multi-agent by default.** Most finance Q&A is retrieve, maybe query, answer. Prove a single agent fails on your eval set before adding routers.
- **Prompt-only permissions.** "You must not write to the database" is not a control. Give each agent only its tools and the narrowest database role.
- **Unbounded routing.** Supervisors ping-pong between workers. Count turns in state and stop in code.
- **Everything in one shared message list.** Every agent then pays for every other agent's tokens and sees data it should not. Use private keys or agents as tools.
- **Dangling tool calls after a handoff.** Forgetting the `ToolMessage` leaves an unanswered tool call in history, and the next model call errors.
- **Free-text routing.** Parsing "I think the SQL analyst should go next" breaks. Use structured output with a `Literal` of node names.

## Practice tasks

1. Remove the supervisor from the first example and wire `researcher -> analytics_team -> writer` with fixed edges. Done when the same asserts pass (minus `turns`) and you can state how many LLM calls you saved per request.
2. Make the scripted supervisor route to `analytics_team` forever. Done when the run ends at `MAX_TURNS` with no exception, and a second version without `MAX_TURNS` but with `{"recursion_limit": 10}` raises `GraphRecursionError`.
3. Add an `interrupt()` to `run_sql` that shows the generated SQL for approval before it runs. Done when the parent graph, compiled with `InMemorySaver`, pauses with the SQL in the payload and resumes to the writer on approval.
4. Rebuild the example as agents as tools: an orchestrator agent with `ask_researcher`, `ask_sql_analyst` and a final answer. Done when the orchestrator's message history contains only its own tool calls and short tool results, never SQL or raw rows.

## Interview questions

**When would you choose multiple agents over one?**
When one agent's tool set or context gets too large to stay accurate, when roles need different permissions or models, or when teams need to own and evaluate parts separately. Not by default: every hop costs an LLM call and loses context.

**How does a supervisor decide where to go next in LangGraph?**
It is a node that calls a model with structured output listing valid workers, then returns `Command(goto=worker)`. Workers have edges back to it. A counter in state and the recursion limit stop runaway loops.

**Supervisor or handoffs?**
Supervisor for central control and auditability, at the cost of a router call per hop. Handoffs when the active agent knows who should go next, which saves the router call but spreads routing logic across agents.

**How do you keep one agent's data away from another?**
Give the team a subgraph schema with private keys, or call it as a tool so only its final answer comes back. Shared keys are only the ones both schemas declare.

**What does `Command.PARENT` do?**
It sends the `goto` and `update` to the closest parent graph. A handoff tool inside an agent subgraph uses it to transfer control to a sibling agent node.

**How do you stop a SQL agent from modifying data?**
At the database: a read-only role limited to reporting views, a statement timeout, and row limits. Prompt instructions and SQL string checks are extra layers, not the control.

**How do you evaluate a multi-agent system?**
End to end on task success, then per component: routing accuracy against labeled next-worker decisions, each specialist on its own dataset, and cost and latency per request. Traces should show which agent produced each message.

**Do you need langgraph-supervisor or langgraph-swarm?**
No. They are separate convenience packages that build the same graphs. Hand-built versions are short and give you control over state and message passing.

## Further reading

- LangGraph docs: "Multi-agent systems" and "Subgraphs"
- LangGraph docs: "Command" (`goto`, `update`, `graph=Command.PARENT`)
- LangChain docs: "Multi-agent" (subagents, handoffs) for `create_agent`
- `langgraph-supervisor` and `langgraph-swarm` READMEs
- [Agent architectures]({{ '/llm/agent-architectures/' | relative_url }}) and [Agent frameworks]({{ '/llm/agent-frameworks/' | relative_url }})
