---
title: Agent frameworks
section: llm
order: 26
phase: "Phase 4 · Agents"
summary: What LangChain, LangGraph, LlamaIndex, DSPy, Pydantic AI and the OpenAI and Claude agent SDKs give you, how to choose one, and when to use none.
---

An agent framework packages the parts every LLM application rebuilds: a model client abstraction, tool schemas generated from functions, the [agent loop]({{ '/llm/agents/' | relative_url }}), structured output parsing, state and persistence, human approval, multi-agent handoffs and tracing. The loop itself is about 50 lines. What frameworks really sell is everything around it: durable state, interrupts, integrations and observability. What they cost is a layer between you and the prompt, version churn, and state formats you can't easily leave. Choose one the way you would choose an ORM or a task queue: by the problems it removes and the escape hatches it leaves.

## What a framework actually gives you

| Concern | Hand-rolled | What a framework adds |
|---|---|---|
| Model access | Provider SDK | One interface over providers, retries, streaming |
| Tools | JSON Schema by hand | Schemas from type hints and docstrings, argument validation |
| Loop | `while` plus a step budget | Prebuilt ReAct agents, parallel tool execution |
| Output | Parse and validate | Typed outputs, automatic re-ask on validation failure |
| State | Your tables | Checkpointers, threads, resume after crash |
| Human-in-the-loop | Custom pause and resume | Interrupt primitives tied to checkpoints |
| Multi-agent | Your routing code | Handoffs, supervisors, agents as tools |
| Tracing | OpenTelemetry spans | Built-in traces, often tied to a vendor dashboard |

They sit at very different abstraction levels:

```text
 low ┌─────────────────────────────────────────────────────────────┐
     │ provider SDKs, instructor        call + validate           │
     │ Pydantic AI, OpenAI Agents SDK   typed agent, tools, loop  │
     │ LangGraph                        state graph + checkpoints │
     │ DSPy                             signatures + optimizers   │
     │ LangChain, LlamaIndex            integrations, RAG chains  │
     │ CrewAI, AutoGen                  role-based multi-agent    │
     │ Claude Agent SDK                 full harness: files,      │
high │                                  shell, permissions        │
     └─────────────────────────────────────────────────────────────┘
```

Lower means more code and more control. Higher means faster demos and harder debugging of the prompt that actually reached the model.

## The frameworks

All snippets are illustrative; signatures shift between versions.

### LangChain

The oldest and broadest ecosystem: a common chat-model interface (`init_chat_model`, `.bind_tools()`, `.with_structured_output()`), composable runnables, and hundreds of integrations for providers, vector stores, loaders and retrievers. Recent versions build their agent abstraction on LangGraph. Its weakness has been churn and indirection: major upgrades broke imports and patterns, and a stack trace passes through many wrappers before the HTTP call. Many teams use it only for integrations and keep control flow elsewhere.

### LangGraph

A low-level orchestration runtime from the LangChain team, usable on its own. You model the agent as a graph:

- **State**: a typed dict. Each key can have a reducer that says how updates merge, for example `add_messages` appends messages instead of overwriting the list.
- **Nodes**: functions that take the state and return a partial update.
- **Edges**: fixed edges or conditional edges (a router function), including cycles. Cycles are what make it an agent rather than a DAG.
- **Checkpointer**: saves the state after every step, keyed by `thread_id`. In-memory for tests, Postgres or SQLite for real use.
- **Interrupts**: `interrupt(payload)` pauses inside a node. The run resumes later, even in another process, with `Command(resume=value)`.
- **Time travel**: past checkpoints can be inspected, edited and replayed from.

```python
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command

class State(TypedDict):
    messages: Annotated[list, add_messages]   # reducer: append, don't replace
    plan: str
    approved: bool

def planner(state: State) -> dict:
    return {"plan": llm.invoke(state["messages"]).content}

def approval(state: State) -> dict:
    answer = interrupt({"question": "Approve this plan?", "plan": state["plan"]})
    return {"approved": answer == "yes"}

builder = StateGraph(State)
builder.add_node("planner", planner)
builder.add_node("approval", approval)
builder.add_node("execute", execute)
builder.add_edge(START, "planner")
builder.add_edge("planner", "approval")
builder.add_conditional_edges("approval", lambda s: "execute" if s["approved"] else END)
builder.add_edge("execute", END)
graph = builder.compile(checkpointer=InMemorySaver())  # Postgres saver in production

config = {"configurable": {"thread_id": "ticket-4812"}}
graph.invoke({"messages": [("user", "Refund order A-1")]}, config)  # pauses
graph.invoke(Command(resume="yes"), config)                          # continues
```

LangGraph is the strongest choice for durable, resumable workflows with approvals, which you would otherwise build from a job queue and a state table. The cost is the graph model and a checkpoint format the framework owns.

### LlamaIndex

A data framework for RAG: readers for many file types and SaaS sources, node parsers (chunkers), indexes such as `VectorStoreIndex`, retrievers, rerankers, query engines that combine retrieval with synthesis, and event-driven Workflows for orchestration.

Use it when ingestion and retrieval variety is the hard part (see [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }})). Watch the defaults: the embedding model, chunk size and prompt templates are chosen for you until you set them.

### DSPy

"Programming, not prompting", from Stanford (Khattab et al.). You declare **signatures** (typed inputs and outputs) and compose **modules** (`Predict`, `ChainOfThought`, `ReAct`). An **optimizer** then compiles the program against a metric and a small training set. It searches over instructions and few-shot demonstrations, and some optimizers fine-tune weights. Optimizers include `BootstrapFewShot`, `MIPROv2` and `GEPA`.

```python
from typing import Literal
import dspy

dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))   # "provider/model" id

class Triage(dspy.Signature):
    """Classify a support ticket and extract the order id if present."""
    ticket: str = dspy.InputField()
    category: Literal["billing", "shipping", "bug", "other"] = dspy.OutputField()
    order_id: str | None = dspy.OutputField()

def metric(example, pred, trace=None):
    return example.category == pred.category and example.order_id == pred.order_id

trainset = [dspy.Example(ticket=t, category=c, order_id=o).with_inputs("ticket")
            for t, c, o in labeled_rows]
optimizer = dspy.BootstrapFewShot(metric=metric, max_bootstrapped_demos=4)
compiled = optimizer.compile(dspy.ChainOfThought(Triage), trainset=trainset)
compiled.save("triage.json")
```

DSPy pays off when you have a metric, labeled examples, and expect to swap models: you re-compile instead of re-tuning prompts by hand. It is a poor fit for open-ended chat with no metric.

### Pydantic AI

A thin, typed agent layer from the Pydantic team: an `Agent` bound to a model string, dependencies injected into tools through `RunContext`, and Pydantic-validated outputs with automatic retry on validation errors.

```python
from dataclasses import dataclass
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

@dataclass
class Deps:
    tenant_id: str          # from the authenticated request, never from the model
    orders: "OrderRepo"

class Answer(BaseModel):
    reply: str
    order_ids: list[str]

agent = Agent("openai:gpt-4o", deps_type=Deps, output_type=Answer,
              instructions="Answer questions about the customer's orders.")

@agent.tool
async def search_orders(ctx: RunContext[Deps], status: str | None = None) -> list[dict]:
    """Search this customer's orders, newest first."""
    return await ctx.deps.orders.search(ctx.deps.tenant_id, status=status)

result = agent.run_sync("Where is my last order?", deps=Deps("t_42", repo))
print(result.output.reply)
```

Dependency injection keeps identity out of tool arguments, the main defense in [tool calling]({{ '/llm/tool-calling/' | relative_url }}), and a test model can be swapped in.

### OpenAI Agents SDK

A small set of primitives: `Agent` (instructions, tools, output type), `Runner` (the loop), **handoffs** (transfer the conversation to another agent), input and output **guardrails**, sessions, and built-in **tracing**. It is the production successor to OpenAI's experimental Swarm. It works best with OpenAI models, and other providers plug in through adapters. Tracing exports to OpenAI's dashboard by default, and you can add your own processors or disable it.

```python
from agents import Agent, Runner, function_tool

@function_tool
def get_invoice(order_id: str) -> str:
    """Return the invoice for an order."""
    ...

billing = Agent(name="Billing", instructions="Handle refunds and invoices.",
                tools=[get_invoice])
triage = Agent(name="Triage", instructions="Route the user to a specialist.",
               handoffs=[billing])
result = Runner.run_sync(triage, "I was charged twice for order A-1")
print(result.final_output)
```

A handoff gives the conversation to the specialist. The alternative is `agent.as_tool()`: the orchestrator keeps control and treats the specialist like a function call. Pick per use case (see [Agent architectures]({{ '/llm/agent-architectures/' | relative_url }})).

### Claude Agent SDK

The Claude Code harness packaged as a library (`claude-agent-sdk` for Python, `@anthropic-ai/claude-agent-sdk` for TypeScript). It was formerly called the Claude Code SDK. You give it a prompt and options. It drives a full agent with built-in tools (`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, `WebFetch`), permission modes, hooks around tool use, subagents, MCP servers, sessions and context compaction. Custom tools are in-process MCP servers built with `@tool` and `create_sdk_mcp_server`.

```python
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

async def main():
    options = ClaudeAgentOptions(
        cwd="/work/repo",
        allowed_tools=["Read", "Grep", "Glob"],   # auto-approved without prompting
        disallowed_tools=["WebFetch"],
        permission_mode="default",
        max_turns=20,
    )
    async for message in query(prompt="Find where invoice totals are rounded.",
                               options=options):
        print(message)

asyncio.run(main())
```

Use it when the job resembles a developer's: read a codebase, edit files, run commands. It is tied to Claude models and runs with filesystem access, so sandbox it (see [Coding and computer-use agents]({{ '/llm/coding-and-computer-use-agents/' | relative_url }})).

### CrewAI and AutoGen, briefly

- **CrewAI**: agents defined by role, goal and backstory, grouped into a crew that runs tasks sequentially or hierarchically. Flows add event-driven, deterministic orchestration. It is quick for multi-agent prototypes, but the role-play abstraction hides control flow and token spend.
- **AutoGen** (Microsoft Research): multi-agent conversation between agents and teams. Version 0.4 rebuilt it on an asynchronous, event-driven core, the original authors forked it as AG2, and Microsoft later announced a combined Agent Framework with Semantic Kernel. Check maintenance status first.
- Also: Google's Agent Development Kit, Hugging Face smolagents (code as actions), and the Vercel AI SDK and Mastra for TypeScript.

## Comparison

| Framework | Abstraction | Core primitive | Strengths | Lock-in and risk |
|---|---|---|---|---|
| LangChain | High | Chat model, runnables, integrations | Breadth of adapters | Indirection, version churn |
| LangGraph | Low-mid | State graph + checkpointer | Durable state, interrupts, replay | Checkpoint format, graph model |
| LlamaIndex | High (data) | Index, retriever, query engine | Ingestion and RAG primitives | Hidden defaults |
| DSPy | Mid | Signature, module, optimizer | Metric-driven prompt optimization | Needs metric and data; opaque prompts |
| Pydantic AI | Low | Typed agent, deps, tools | Type safety, DI, testability | Smaller ecosystem |
| OpenAI Agents SDK | Low-mid | Agent, handoff, guardrail | Simple handoffs, tracing | Best on OpenAI; default trace sink |
| Claude Agent SDK | High (harness) | `query()` + built-in tools | Files, shell, permissions, hooks | Claude-only; needs sandboxing |
| CrewAI | High | Role agents, crews, flows | Fast multi-agent demos | Opaque control flow |
| AutoGen | Mid-high | Conversing agents, teams | Research patterns | Ecosystem in flux |

## In practice

### What a checkpointer does, in stdlib Python

Checkpointing is the feature most worth paying a framework for, and the one that locks you in. The toy saves state after every node in SQLite, pauses for approval, and resumes from a new connection standing in for another worker.

{% raw %}
```python
import json
import os
import sqlite3
import tempfile


class Interrupt(Exception):
    def __init__(self, payload):
        self.payload = payload


class Graph:
    """Nodes are functions (state, resume) -> (update, next_node). State is
    checkpointed after every node, so a run can pause and resume elsewhere."""

    def __init__(self, nodes, db_path):
        self.nodes, self.db = nodes, sqlite3.connect(db_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (thread TEXT, "
                        "step INT, node TEXT, state TEXT, PRIMARY KEY (thread, step))")

    def save(self, thread, step, node, state):
        with self.db:  # one transaction per checkpoint
            self.db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?)",
                            (thread, step, node, json.dumps(state)))

    def history(self, thread):
        rows = self.db.execute("SELECT step, node, state FROM checkpoints "
                               "WHERE thread = ? ORDER BY step", (thread,))
        return [(step, node, json.loads(state)) for step, node, state in rows]

    def run(self, thread, state=None, resume=None):
        past = self.history(thread)
        if past:
            step, node, state = past[-1]  # continue from the latest checkpoint
        else:
            step, node = 0, "start"
            self.save(thread, step, node, state)
        while node != "end":
            try:
                update, node = self.nodes[node](state, resume)
            except Interrupt as i:
                return {"interrupted": i.payload}  # nothing lost: state is saved
            resume, step, state = None, step + 1, {**state, **update}
            self.save(thread, step, node, state)
        return state


refunds = []  # stands in for an external side effect


def start(state, _):
    return {"plan": f"refund order {state['order_id']}"}, "approve"


def approve(state, resume):
    if resume is None:  # this node re-runs on resume, so keep it side-effect free
        raise Interrupt({"question": f"Approve: {state['plan']}?"})
    return {"approved": resume == "yes"}, "act" if resume == "yes" else "end"


def act(state, _):
    refunds.append(state["order_id"])
    return {"result": "refunded"}, "end"


if __name__ == "__main__":
    nodes = {"start": start, "approve": approve, "act": act}
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "checkpoints.db")
        out = Graph(nodes, db).run("t1", {"order_id": "A-1"})
        assert out == {"interrupted": {"question": "Approve: refund order A-1?"}}
        assert refunds == []
        # A different process (new connection) resumes hours later.
        final = Graph(nodes, db).run("t1", resume="yes")
        assert final["result"] == "refunded" and refunds == ["A-1"]
        assert [n for _, n, _ in Graph(nodes, db).history("t1")] == [
            "start", "approve", "act", "end"]
        # Time travel: every intermediate state is still inspectable.
        assert Graph(nodes, db).history("t1")[1][2] == {
            "order_id": "A-1", "plan": "refund order A-1"}
        Graph(nodes, db).run("t2", {"order_id": "B-2"})
        assert Graph(nodes, db).run("t2", resume="no")["approved"] is False
        assert refunds == ["A-1"]
    print("ok")
```
{% endraw %}

Note what the toy leaves out and a real checkpointer must handle: concurrent resumes of the same thread (use a row lock or a version check), schema changes to the state while threads are paused, retention, and side effects in `act` that must be [idempotent]({{ '/hld/idempotency-and-retries/' | relative_url }}) because a crash between the side effect and the checkpoint re-runs the node.

### When to use no framework

Hand-roll the loop when most of these hold:

- One or two providers, fewer than about 15 tools, a bounded loop.
- You already run a job system (Celery, a Postgres-backed queue) and a database where state can live.
- You need exact control over the request: [prompt caching]({{ '/llm/llm-caching/' | relative_url }}) breakpoints, reasoning parameters, strict tool schemas, provider-specific features on release day.
- Your value is in domain tools and evals, not in integrations.

A production-grade core is a few hundred lines: a model-client interface, a tool registry with Pydantic validation and per-tool authorization, the loop with step, token and time budgets, state rows in Postgres, and OpenTelemetry spans per call. That is often less than the framework glue you would write anyway, and every prompt shows up in your own logs.

Reach for a framework when you need what is expensive to build well: durable interrupts and resume (LangGraph), many data connectors (LlamaIndex, LangChain), metric-driven prompt optimization (DSPy), or a complete file-and-shell agent harness (Claude Agent SDK).

### How to evaluate a framework

Run a one-day spike: build the same three-tool agent in each candidate and run it against your existing [agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }}) set. Then check:

| Question | How to check |
|---|---|
| Can I see the exact request sent to the model? | Log the raw payload; compare with what you expected |
| Can I drop to the raw SDK for one step? | Write one node with the provider SDK directly |
| Are provider features passed through? | Caching, strict schemas, parallel tools, reasoning settings |
| Does state survive a crash and a deploy? | Kill the worker mid-run; resume from another process |
| Does it fit your stores? | Postgres checkpointer, your vector store, your queue |
| Is tracing portable? | OpenTelemetry export, not only a vendor dashboard |
| Is it testable and stable? | Fake model and replay; changelog, semver, deprecation windows |
| Is it safe? | Tool permission hooks, MCP support, secret handling |

### Migration risk

What makes leaving hard:

- **Persisted state.** In-flight threads stored in the framework's checkpoint format. Plan to drain them or write a converter.
- **Hidden prompts.** Agent prompts and output parsers inside the library. Behavior changes when you move, even with the same model.
- **Generated artifacts.** DSPy compiled programs and framework-generated tool schemas.
- **Platform services.** Hosted runtimes, trace stores and prompt hubs.

Mitigate with ports and adapters. Keep prompts and tool definitions as plain data in your repo. Keep business logic in plain functions that the framework calls. Keep your eval harness framework-agnostic and replay recorded traces through it. Pin versions and schedule upgrades deliberately.

## Pitfalls

- **Demo velocity, production drag.** A ten-line prototype becomes hard to debug once you need custom retries or caching. Symptom: monkey-patching framework internals. Fix: own the loop, keep the framework for integrations.
- **Invisible token spend.** Default prompts and multi-agent chatter multiply cost. Track tokens per run, not per call ([Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }})).
- **Re-executed nodes.** On resume, LangGraph re-runs the interrupted node from its start. Side effects before `interrupt()` happen twice. Put side effects after the interrupt, or in their own node, and make them idempotent.
- **Multi-agent by default.** Crews cost more and fail in more places than one agent with good tools. Start with one.
- **Default trace sinks.** Some SDKs send traces to the vendor by default. Check data-handling rules before sending customer data.

## Rules of thumb

- Start with the provider SDK plus Pydantic validation. Add a framework when a concrete need appears.
- Own your prompts, tools, evals and traces. Rent integrations and runtimes.
- Every framework call should be traceable down to the raw model request.

## Interview questions

**When would you not use a framework?** When the loop is simple, providers are few, you already have a queue and database for state, and you need exact control of prompts, caching and provider features.

**LangChain vs LangGraph?** LangChain provides model wrappers, integrations and high-level chains and agents. LangGraph is a low-level runtime for stateful, cyclic graphs with checkpointing and interrupts. Use it when control flow and durability matter.

**What does a LangGraph checkpointer do, and why does human-in-the-loop need it?** It persists graph state after each step under a thread id. An interrupt can then end the process, and a later request, possibly on another worker, resumes from the saved state with the human's input. Without persistence, a pause would have to keep a process alive.

**Explain DSPy's optimizers.** You define signatures and modules plus a metric. The optimizer runs the program on training examples, keeps traces that score well as few-shot demos, and searches over instructions. The result is a compiled program you can re-optimize for a new model instead of hand-tuning prompts.

**Handoff vs agent-as-tool?** A handoff transfers the conversation to another agent, which then talks to the user. Agent-as-tool keeps the orchestrator in charge, calls the specialist like a function and uses its output. Handoffs suit routing. Agents as tools suit decomposition with central control.

**How would you choose a framework for a team?** Spike the same small agent in each candidate and score it on the eval set. Then check request transparency, escape hatches, persistence in your stores, OpenTelemetry tracing and release stability.

**What is sticky when migrating off a framework?** Persisted checkpoints, hidden prompt templates, compiled or generated artifacts, and hosted platform services. Keep prompts, tools and evals in your own code from day one.

## Further reading

- Anthropic, "Building effective agents" (2024).
- Khattab et al., "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines" (2023).
- Wu et al., "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation" (2023).
- LangGraph documentation: persistence and human-in-the-loop.
- OpenAI Agents SDK, Claude Agent SDK and Pydantic AI official documentation.
