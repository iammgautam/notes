---
title: LangGraph core concepts
section: ai-stack
order: 21
phase: "Phase 4 · LangGraph"
summary: StateGraph, state schemas, reducers, nodes and edges in LangGraph 1.x, built up to a runnable finance query router you can stream and draw.
---
After this note you can explain why an agent needs a graph instead of a chain, define typed state with the right reducers, wire nodes and edges into a compiled graph, and run, stream and draw it. Every LangGraph feature the job description implies (tool-calling agents, approvals, memory, multi-agent flows over finance data) is built from the four pieces covered here: state, nodes, edges and the compiled graph.

## Prerequisites

You know LCEL and runnables from [LangChain LCEL and runnables]({{ '/ai-stack/langchain-lcel-and-runnables/' | relative_url }}) and finished [LangChain observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }}). Examples target `langgraph` 1.2 and `langchain-core` 1.8.

## Why graphs and not chains

An LCEL chain is a pipeline: data flows left to right through `prompt | model | parser`, once. That covers extraction, classification and one-shot RAG. It breaks down when the flow needs to:

- **loop**: call the model, run a tool, call the model again until it stops asking for tools;
- **branch on intermediate results**: route a query by category, escalate a large invoice;
- **keep state across steps**: a running list of messages, matched ledger lines, retry counters;
- **stop and resume**: wait for a controller to approve a journal entry, or pick up after a crash.

LangGraph models the workflow as a state machine. You declare a shared state, functions (nodes) that read it and return updates, and edges that say what runs next. Cycles are allowed, so the agent loop is just an edge pointing back.

| Need | LCEL chain | LangGraph |
|---|---|---|
| Fixed linear steps | Natural fit | Works, more ceremony |
| Loops and retries | Awkward (custom runnable) | Edges back to a node |
| Branching | `RunnableBranch`, hard to read | Conditional edges, `Command` |
| Shared mutable state | Pass dicts through `assign` | Typed state with reducers |
| Pause, resume, replay | Not available | Checkpointers, interrupts |

A compiled graph is itself a `Runnable`, so it still has `invoke`, `stream` and `batch` and plugs into anything that accepts a runnable, including another graph.

## The mental model

LangGraph runs in **supersteps**. In each superstep, every node that is scheduled runs (in parallel if there are several), each returns a partial update, and the updates are merged into state through each key's **reducer**. Then the edges decide which nodes run in the next superstep. The run ends when nothing is scheduled.

```text
            state (typed dict)
   +-------------------------------+
   | question, category, answer,   |
   | steps                         |
   +-------------------------------+
      |  read                ^ merge via reducers
      v                      |
   START --> classify --+--> invoice_lookup --+--> END
                        +--> gl_explain ------+
                        +--> other -----------+
           superstep 1        superstep 2
```

Three rules follow from this, and most LangGraph bugs come from ignoring one:

1. Nodes never mutate state in place. They return a dict of the keys they want to change.
2. How a returned value combines with the existing value is decided by the key's reducer, not by the node.
3. Nodes in the same superstep do not see each other's updates until the superstep ends.

## State schemas

State is the contract between nodes. Three schema styles are supported:

| Schema | Validation | Defaults | Use when |
|---|---|---|---|
| `TypedDict` | None (type hints only) | No | Default choice, fastest |
| `dataclass` | None | Yes | You want default values |
| Pydantic `BaseModel` | On graph input | Yes | Input comes from untrusted callers |

```python
from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class InvoiceState(TypedDict, total=False):  # keys optional
    invoice_id: str
    amount: float


@dataclass
class InvoiceStateDC:
    invoice_id: str
    flags: list[str] = field(default_factory=list)


class InvoiceStateModel(BaseModel):
    invoice_id: str
    amount: float = Field(gt=0)
```

> **Gotcha:** with a Pydantic schema, `graph.invoke({"amount": -1})` raises `ValidationError`, but a node returning `{"amount": -5}` is not validated, and the final output is a plain dict, not a model instance. Validate tool and LLM output inside the node that produces it.

`StateGraph` also accepts `input_schema` and `output_schema` when the public contract should be narrower than the internal state, for example accept only `question` and return only `answer` while internal keys like retrieved chunks stay hidden.

## Reducers

A key without a reducer is **last write wins**. A key annotated with `Annotated[type, reducer]` combines the old and new value with `reducer(old, new)`:

- `Annotated[list[str], operator.add]` concatenates lists. Good for audit trails and fan-in results.
- `Annotated[list[AnyMessage], add_messages]` appends messages, but a message whose `id` already exists **replaces** the old one, and `RemoveMessage(id=...)` deletes one. This is what makes edits and trimming possible.
- `MessagesState` is a prebuilt `TypedDict` with exactly that `messages` key. Subclass it to add your own keys.

The block below also shows **run-scoped context**: values like tenant and role that every node needs but that should not live in state (they are not checkpointed and nodes cannot change them). Declare a `context_schema`, accept a `runtime: Runtime[Ctx]` parameter, and pass `context=` at invoke time.

```python
import operator
from dataclasses import dataclass
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.runtime import Runtime


class State(MessagesState):  # gives you: messages + add_messages reducer
    total: float  # no reducer: last write wins
    gl_lines: Annotated[list[dict], operator.add]  # concatenated


@dataclass
class Ctx:  # run-scoped, read-only, never checkpointed
    tenant_id: str
    role: str


def load(state: State, runtime: Runtime[Ctx]) -> dict:
    line = {"tenant": runtime.context.tenant_id, "acct": "6100", "amt": 100.0}
    return {"total": 100.0, "gl_lines": [line],
            "messages": [AIMessage("Loaded 1 line", id="status")]}


def adjust(state: State, runtime: Runtime[Ctx]) -> dict:
    if runtime.context.role != "controller":
        return {"messages": [AIMessage("Adjustment skipped: role", id="status")]}
    line = {"tenant": runtime.context.tenant_id, "acct": "6100", "amt": -20.0}
    return {"total": state["total"] - 20.0, "gl_lines": [line],
            "messages": [AIMessage("Adjusted by -20", id="status")]}


graph = (
    StateGraph(State, context_schema=Ctx)
    .add_node("load", load)
    .add_node("adjust", adjust)
    .add_edge(START, "load")
    .add_edge("load", "adjust")
    .add_edge("adjust", END)
    .compile()
)

if __name__ == "__main__":
    out = graph.invoke({"messages": [HumanMessage("Post accrual")]},
                       context=Ctx(tenant_id="acme", role="controller"))
    assert out["total"] == 80.0  # overwritten
    assert [l["amt"] for l in out["gl_lines"]] == [100.0, -20.0]  # appended
    # same message id -> replaced in place, not appended
    assert [m.content for m in out["messages"]] == ["Post accrual", "Adjusted by -20"]

    out = graph.invoke({"messages": [HumanMessage("Post accrual")]},
                       context=Ctx(tenant_id="acme", role="analyst"))
    assert out["total"] == 100.0
    assert out["messages"][-1].content == "Adjustment skipped: role"
    print("ok")
```

## Nodes, edges and compile

A **node** is any callable (function, lambda or runnable) that takes the state and returns a partial update. Optional extra parameters are injected by name and type: `config: RunnableConfig` for tags and the thread id, `runtime: Runtime[Ctx]` for context, the store and a stream writer. Async nodes (`async def`) work the same way and run when you call `ainvoke` or `astream`.

**Edges** come in two kinds. `add_edge("a", "b")` always goes from `a` to `b`. `add_conditional_edges("a", route_fn)` calls `route_fn(state)` and goes wherever it points. `START` and `END` are the virtual entry and exit nodes.

`compile()` checks the structure (edges to unknown nodes, no edge from `START`) and returns a `CompiledStateGraph`. This is also where you attach a checkpointer, a store and static interrupts, covered in later notes.

### A runnable finance router

A support assistant for a finance platform gets free-text questions. The first node classifies the question with an LLM; a routing function sends it to a specialist node. The fake model returns canned labels so the block runs without keys.

```python
import operator
from typing import Annotated, Literal

from typing_extensions import TypedDict
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END

Category = Literal["invoice_lookup", "gl_explain", "other"]


class State(TypedDict, total=False):
    question: str
    category: Category
    answer: str
    steps: Annotated[list[str], operator.add]  # every node appends


# Real model (illustrative): llm = init_chat_model("openai:gpt-4.1-mini", temperature=0)
llm = FakeListChatModel(responses=["invoice_lookup", "gl_explain", "the weather"])

classify_prompt = ChatPromptTemplate.from_messages([
    ("system", "Label the finance question as invoice_lookup, gl_explain "
               "or other. Reply with the label only."),
    ("human", "{question}"),
])
classifier = classify_prompt | llm

INVOICES = {"INV-1042": {"vendor": "Acme Corp", "amount": 1250.00, "status": "paid"}}
GL_ACCOUNTS = {"6100": "Travel and entertainment", "2000": "Accounts payable"}


def classify(state: State) -> dict:
    label = classifier.invoke({"question": state["question"]}).content.strip()
    if label not in ("invoice_lookup", "gl_explain"):
        label = "other"  # never trust a free-text label
    return {"category": label, "steps": ["classify"]}


def invoice_lookup(state: State) -> dict:
    inv_id = next((w.strip("?.,") for w in state["question"].split()
                   if w.startswith("INV-")), None)
    inv = INVOICES.get(inv_id)
    answer = (f"{inv_id}: {inv['vendor']}, {inv['amount']:.2f}, {inv['status']}"
              if inv else "Invoice not found.")
    return {"answer": answer, "steps": ["invoice_lookup"]}


def gl_explain(state: State) -> dict:
    code = next((w.strip("?.,") for w in state["question"].split()
                 if w.strip("?.,").isdigit()), None)
    name = GL_ACCOUNTS.get(code, "unknown account")
    return {"answer": f"GL {code} is {name}.", "steps": ["gl_explain"]}


def fallback(state: State) -> dict:
    return {"answer": "I can help with invoices and GL accounts only.",
            "steps": ["fallback"]}


def route(state: State) -> Category:
    return state["category"]


builder = StateGraph(State)
builder.add_node("classify", classify)
builder.add_node("invoice_lookup", invoice_lookup)
builder.add_node("gl_explain", gl_explain)
builder.add_node("other", fallback)
builder.add_edge(START, "classify")
builder.add_conditional_edges("classify", route)
for node in ("invoice_lookup", "gl_explain", "other"):
    builder.add_edge(node, END)
graph = builder.compile()


if __name__ == "__main__":
    out = graph.invoke({"question": "What is the status of INV-1042?"})
    assert out["category"] == "invoice_lookup"
    assert out["answer"] == "INV-1042: Acme Corp, 1250.00, paid"
    assert out["steps"] == ["classify", "invoice_lookup"]

    updates = list(graph.stream({"question": "What is GL 6100?"},
                                stream_mode="updates"))
    assert [list(u) for u in updates] == [["classify"], ["gl_explain"]]
    assert updates[-1]["gl_explain"]["answer"] == "GL 6100 is Travel and entertainment."

    out = graph.invoke({"question": "Will it rain tomorrow?"})
    assert out["category"] == "other"

    mermaid = graph.get_graph().draw_mermaid()
    assert "classify -.-> invoice_lookup" in mermaid
    print(mermaid)
```

Note that `add_conditional_edges("classify", route)` has no path map. LangGraph reads the `Literal` return annotation of `route` to learn the possible destinations, which is what lets it draw the graph. In production, replace the label parsing with `llm.with_structured_output(RouteModel)` so the model must return one of the allowed values.

## Invoke and stream

`invoke` returns the final state. `stream` yields as the graph runs; `stream_mode` picks what you get:

| `stream_mode` | Each chunk is | Use for |
|---|---|---|
| `"values"` | Full state after each superstep | Debugging, simple UIs |
| `"updates"` | `{node_name: partial_update}` | Progress events, audit logs |
| `"messages"` | `(token_chunk, metadata)` from LLM calls | Token streaming to a client |
| `"custom"` | Whatever a node sends via `runtime.stream_writer` | "Fetched 120 GL rows" progress |
| `"debug"`, `"tasks"`, `"checkpoints"` | Internal events | Deep debugging |

Pass a list (`stream_mode=["updates", "messages"]`) to get `(mode, chunk)` tuples. Streaming to a browser over FastAPI is covered in [LangGraph streaming and deployment]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}).

## Visualizing a graph

`graph.get_graph()` returns a drawable description. `draw_mermaid()` returns Mermaid text you can paste into GitHub, Notion or a design doc. This is the router above (styling lines omitted):

```text
graph TD;
  __start__([<p>__start__</p>]):::first
  classify(classify)
  invoice_lookup(invoice_lookup)
  gl_explain(gl_explain)
  other(other)
  __end__([<p>__end__</p>]):::last
  __start__ --> classify;
  classify -.-> gl_explain;
  classify -.-> invoice_lookup;
  classify -.-> other;
  gl_explain --> __end__;
  invoice_lookup --> __end__;
  other --> __end__;
```

Dotted arrows are conditional edges. `draw_ascii()` needs the optional `grandalf` package, and `draw_mermaid_png()` renders through a remote Mermaid service by default, so prefer the text form inside a regulated network.

## Coming from Django

| Django habit | LangGraph equivalent |
|---|---|
| View function handling one request | A node handling one step |
| `request.user`, tenant middleware | `context_schema` + `Runtime.context` |
| Model instance passed between services | State dict, serialized between steps |
| `F()` expressions merging concurrent writes | Reducers merging parallel node updates |
| URLconf resolving the next handler | Edges and routing functions |
| `django-fsm` transitions | Conditional edges on a state machine |
| System checks at startup | `compile()` validating edges |

The big difference: a Django view finishes in one request. A LangGraph run can span many model calls, pause for a human for days, and resume on another worker, which is why state must be serializable and nodes must be side-effect aware.

## Common mistakes

- **Mutating state in a node** (`state["steps"].append(...)`). The change may be lost or doubled. Return `{"steps": ["x"]}` with an `operator.add` reducer.
- **Forgetting the reducer on a list key.** Two nodes that both write `messages` without `add_messages` will overwrite each other; in parallel branches LangGraph raises `InvalidUpdateError`.
- **Putting secrets and request context in state.** State gets checkpointed and traced. Pass tenant, user and role through `context`, and fetch credentials inside the node.
- **Trusting LLM labels for routing.** Validate against an allowed set or use structured output, and always have a fallback node.
- **Expecting Pydantic state to validate node output.** It validates graph input only.
- **Storing large blobs in state** (whole PDFs, DataFrames). Every checkpoint serializes them. Store an ID or a path and load in the node that needs it.

## Practice tasks

1. **Extend the router.** Add a `vendor_spend` category and node backed by a dict of vendor totals. Done: three asserts pass for the new route, and the Mermaid output shows four conditional edges.
2. **Structured routing.** Replace the label parsing with `with_structured_output` on a Pydantic `Route` model, using a fake model subclass that returns a tool call. Done: an unknown label is impossible by construction, and a test covers the fallback.
3. **Custom progress stream.** In `invoice_lookup`, emit `{"progress": "looked up INV-1042"}` through `runtime.stream_writer`. Done: `graph.stream(..., stream_mode=["updates", "custom"])` yields the custom event between the two node updates.
4. **Narrow the public contract.** Add `input_schema` (question only) and `output_schema` (answer and category only). Done: `invoke` output no longer contains `steps`, and an input with extra keys is ignored.

## Interview questions

**Why use LangGraph instead of an LCEL chain?**
Chains are one-pass pipelines. Agents need loops, branches on intermediate results, shared state across steps, and the ability to pause and resume. LangGraph gives you those as first-class concepts, and a compiled graph is still a runnable.

**What is a reducer and why does it exist?**
A function that merges a node's returned value into the existing value of a state key. Without one, the last write wins. Reducers make parallel branches safe and let message lists append, replace by id and delete.

**What does `add_messages` do that `operator.add` does not?**
It replaces a message with the same `id` instead of duplicating it, handles `RemoveMessage`, and coerces dicts and tuples into message objects.

**What is a superstep?**
One round of execution in which all scheduled nodes run, their updates are merged through reducers, and a checkpoint is taken. Nodes in the same superstep do not see each other's writes.

**TypedDict, dataclass or Pydantic for state?**
TypedDict by default. Dataclass when you need defaults. Pydantic when graph input comes from untrusted callers, remembering that only input is validated.

**Where do tenant and user identity go?**
In run context (`context_schema`, `Runtime.context`), not state. Context is per run, read-only and not checkpointed, so it cannot leak into stored history or be changed by a node.

**How do you visualize and review a graph?**
`graph.get_graph().draw_mermaid()` produces Mermaid text. Commit it next to the code so reviewers see control-flow changes in diffs.

## Further reading

- LangGraph docs: "Overview" and "Thinking in LangGraph"
- LangGraph docs: "Graph API" (state, reducers, nodes, edges)
- LangGraph docs: "Use the graph API" how-to guide
- LangGraph docs: "Streaming"
- LangGraph reference: `StateGraph` and `CompiledStateGraph`
