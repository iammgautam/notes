---
title: Control flow in LangGraph
section: ai-stack
order: 22
phase: "Phase 4 · LangGraph"
summary: Routing, loops with hard limits, Command, Send map-reduce, parallel branches, subgraphs and node retries in LangGraph 1.2, with runnable finance demos.
---
After this note you can route on state, build loops that always terminate, fan out work in parallel and merge it safely, compose graphs from subgraphs, and make flaky nodes retry or fail over. These are the building blocks behind every "agentic workflow" bullet in a finance AI role: approval routing, reconciliation retries, batch invoice summarization and resilient calls to rate and ledger APIs.

## Prerequisites

[LangGraph core concepts]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }}): state, reducers, nodes, edges and supersteps.

## Conditional edges and routing functions

A routing function reads state and returns where to go next. It can return a node name, `END`, a list of node names (run them in parallel), or `Send` objects (next sections). Keep routing functions pure: no I/O, no LLM calls. Do the expensive work in a node, write the result to state, and route on that.

The optional third argument to `add_conditional_edges` maps return values to node names, so routing logic can use business labels while node names stay technical.

```python
from typing import Literal

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END


class Invoice(TypedDict, total=False):
    vendor: str
    amount: float
    approver: str


def validate(state: Invoice) -> dict:
    return {}  # schema checks would go here


def route_by_amount(state: Invoice) -> Literal["auto", "manager", "cfo"]:
    if state["amount"] < 1_000:
        return "auto"
    return "manager" if state["amount"] < 50_000 else "cfo"


builder = StateGraph(Invoice)
builder.add_node("validate", validate)
builder.add_node("auto_approve", lambda s: {"approver": "system"})
builder.add_node("manager_queue", lambda s: {"approver": "manager"})
builder.add_node("cfo_queue", lambda s: {"approver": "cfo"})
builder.add_edge(START, "validate")
builder.add_conditional_edges(
    "validate",
    route_by_amount,
    {"auto": "auto_approve", "manager": "manager_queue", "cfo": "cfo_queue"},
)
for n in ("auto_approve", "manager_queue", "cfo_queue"):
    builder.add_edge(n, END)
graph = builder.compile()

if __name__ == "__main__":
    assert graph.invoke({"vendor": "Acme", "amount": 420.0})["approver"] == "system"
    assert graph.invoke({"vendor": "Acme", "amount": 8_000.0})["approver"] == "manager"
    assert graph.invoke({"vendor": "Acme", "amount": 90_000.0})["approver"] == "cfo"
    print("ok")
```

## Command: update and route in one return

When the node that computes a decision is also the one that knows where to go, return a `Command` instead of a dict. `update` is applied like a normal return value; `goto` picks the next node. Annotate the return type with `Command[Literal[...]]` so the graph can be drawn and the node needs no outgoing edges.

```python
from typing import Literal

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command


class Invoice(TypedDict, total=False):
    vendor: str
    amount: float
    risk: str
    approver: str


BLOCKED_VENDORS = {"Shell Co 7"}


def assess(state: Invoice) -> Command[Literal["auto_approve", "review"]]:
    if state["vendor"] in BLOCKED_VENDORS or state["amount"] >= 10_000:
        return Command(update={"risk": "high"}, goto="review")
    return Command(update={"risk": "low"}, goto="auto_approve")


builder = StateGraph(Invoice)
builder.add_node("assess", assess)  # no outgoing edges: Command decides
builder.add_node("auto_approve", lambda s: {"approver": "system"})
builder.add_node("review", lambda s: {"approver": "controller"})
builder.add_edge(START, "assess")
builder.add_edge("auto_approve", END)
builder.add_edge("review", END)
graph = builder.compile()

if __name__ == "__main__":
    out = graph.invoke({"vendor": "Shell Co 7", "amount": 50.0})
    assert out == {"vendor": "Shell Co 7", "amount": 50.0,
                   "risk": "high", "approver": "controller"}
    assert graph.invoke({"vendor": "Acme", "amount": 50.0})["approver"] == "system"
    assert "assess -.-> review" in graph.get_graph().draw_mermaid()
    print("ok")
```

| Use | When |
|---|---|
| Conditional edge | Routing is a pure function of state; you want routing visible in one place |
| `Command` | The node computes the decision and the update together |
| `Command(graph=Command.PARENT)` | A subgraph node hands control to a node in the parent (agent handoffs) |
| `Command(resume=...)` | Resuming after `interrupt()`, covered in [human in the loop]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}) |

## Loops with exit conditions

A loop is an edge back to an earlier node plus a routing function that decides when to stop. Put the exit condition in code, never only in a prompt. The example reconciles ledger lines against bank lines, widening the amount tolerance tenfold on each attempt and giving up after three.

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.errors import GraphRecursionError

LEDGER = {"L1": 100.00, "L2": 250.00, "L3": 999.00}
BANK = {"B1": 100.00, "B2": 249.40, "B3": 990.00}


class Recon(TypedDict, total=False):
    tolerance: float
    attempts: int
    matched: dict[str, str]
    unmatched: list[str]


def match(state: Recon) -> dict:
    tol, matched = state["tolerance"], {}
    free = dict(BANK)
    for lid, amt in LEDGER.items():
        hit = next((b for b, v in free.items() if abs(v - amt) <= tol), None)
        if hit:
            matched[lid] = hit
            free.pop(hit)
    return {"matched": matched, "attempts": state.get("attempts", 0) + 1,
            "unmatched": [l for l in LEDGER if l not in matched]}


def widen(state: Recon) -> dict:
    return {"tolerance": state["tolerance"] * 10}


def should_retry(state: Recon) -> str:
    if not state["unmatched"] or state["attempts"] >= 3:
        return END  # exit condition lives in the graph, not in a prompt
    return "widen"


builder = StateGraph(Recon)
builder.add_node("match", match)
builder.add_node("widen", widen)
builder.add_edge(START, "match")
builder.add_conditional_edges("match", should_retry, ["widen", END])
builder.add_edge("widen", "match")
graph = builder.compile()

if __name__ == "__main__":
    out = graph.invoke({"tolerance": 0.01}, {"recursion_limit": 10})
    assert out["attempts"] == 3 and out["tolerance"] == 1.0
    assert out["matched"] == {"L1": "B1", "L2": "B2"} and out["unmatched"] == ["L3"]

    try:  # 3 attempts need 5 steps; a limit of 4 trips the guard
        graph.invoke({"tolerance": 0.01}, {"recursion_limit": 4})
        raise AssertionError("expected GraphRecursionError")
    except GraphRecursionError:
        pass
    print("ok")
```

`recursion_limit` caps the number of supersteps in one run and raises `GraphRecursionError` when hit. It is your circuit breaker for a loop whose exit condition never fires.

> **Gotcha:** in LangGraph 1.2 the default limit is 10,007 supersteps (configurable through the `LANGGRAPH_DEFAULT_RECURSION_LIMIT` environment variable). Older tutorials say 25. An agent looping on a model call can burn thousands of paid calls before the default trips, so set `recursion_limit` explicitly on every production invocation.

To end gracefully instead of raising, add a `remaining_steps: RemainingSteps` key (from `langgraph.managed`) to state. LangGraph fills it with the steps left, and a routing function can send the run to a "summarize what you have" node when it gets low.

## Parallel branches and merging

Two edges out of one node (or out of `START`) run both targets in the same superstep. `add_edge(["a", "b"], "c")` makes `c` wait for both. Any key written by more than one branch needs a reducer; without one, LangGraph cannot choose a winner and raises `InvalidUpdateError`.

```python
import operator
from typing import Annotated

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.errors import InvalidUpdateError


class State(TypedDict, total=False):
    ledger: list[float]
    bank: list[float]
    sources: Annotated[list[str], operator.add]  # both branches write it
    difference: float


def fetch_ledger(state: State) -> dict:
    return {"ledger": [100.0, 250.0], "sources": ["ledger"]}


def fetch_bank(state: State) -> dict:
    return {"bank": [100.0, 249.4], "sources": ["bank"]}


def compare(state: State) -> dict:
    return {"difference": round(sum(state["ledger"]) - sum(state["bank"]), 2)}


builder = StateGraph(State)
builder.add_node("fetch_ledger", fetch_ledger)
builder.add_node("fetch_bank", fetch_bank)
builder.add_node("compare", compare)
builder.add_edge(START, "fetch_ledger")  # two edges from START = fan-out
builder.add_edge(START, "fetch_bank")
builder.add_edge(["fetch_ledger", "fetch_bank"], "compare")  # wait for both
builder.add_edge("compare", END)
graph = builder.compile()


class NoReducer(TypedDict, total=False):
    sources: list[str]


if __name__ == "__main__":
    out = graph.invoke({})
    assert sorted(out["sources"]) == ["bank", "ledger"]
    assert out["difference"] == 0.6

    bad = (StateGraph(NoReducer)
           .add_node("a", lambda s: {"sources": ["ledger"]})
           .add_node("b", lambda s: {"sources": ["bank"]})
           .add_edge(START, "a").add_edge(START, "b").compile())
    try:
        bad.invoke({})
        raise AssertionError("expected InvalidUpdateError")
    except InvalidUpdateError:
        pass
    print("ok")
```

The order in which `operator.add` concatenates parallel results is not something to rely on. Sort, or key results by ID, before using them. If one branch fails, the whole superstep fails; with a checkpointer, the successful branches' writes are saved and not re-run on resume (see [persistence and memory]({{ '/ai-stack/langgraph-persistence-and-memory/' | relative_url }})).

## Send: map-reduce over a dynamic list

Parallel edges are fixed at build time. When the number of branches depends on data (one per invoice in a batch), return a list of `Send(node, payload)` from a routing function. Each `Send` runs the target node once with its own payload as input, all in the same superstep. A reducer collects the results, and the next node runs once after every worker finishes.

```text
              +--> summarize(INV-1) --+
              +--> summarize(INV-2) --+
 START -fan_out                        +--> reduce --> END
              +--> ...                 |   (summaries via operator.add)
              +--> summarize(INV-n) --+
```

```python
import operator
from typing import Annotated

from typing_extensions import TypedDict
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send


class BatchState(TypedDict, total=False):
    invoices: list[dict]
    summaries: Annotated[list[dict], operator.add]  # workers append here
    report: str


class WorkerInput(TypedDict):  # private input for one worker
    invoice: dict


# Illustrative: llm = init_chat_model("openai:gpt-4.1-mini")
llm = FakeListChatModel(responses=["Cloud hosting, monthly."])


def fan_out(state: BatchState) -> list[Send]:
    return [Send("summarize", {"invoice": inv}) for inv in state["invoices"]]


def summarize(state: WorkerInput) -> dict:
    inv = state["invoice"]
    text = llm.invoke(f"Summarize in 5 words: {inv['lines']}").content
    return {"summaries": [{"id": inv["id"], "summary": text,
                           "amount": inv["amount"]}]}


def reduce(state: BatchState) -> dict:
    total = sum(s["amount"] for s in state["summaries"])
    return {"report": f"{len(state['summaries'])} invoices, total {total:.2f}"}


builder = StateGraph(BatchState)
builder.add_node("summarize", summarize)
builder.add_node("reduce", reduce)
builder.add_conditional_edges(START, fan_out, ["summarize"])
builder.add_edge("summarize", "reduce")  # runs once, after all workers
builder.add_edge("reduce", END)
graph = builder.compile()

if __name__ == "__main__":
    invoices = [{"id": f"INV-{i}", "amount": 100.0 * i, "lines": "AWS usage"}
                for i in range(1, 6)]
    out = graph.invoke({"invoices": invoices}, {"max_concurrency": 3})
    assert len(out["summaries"]) == 5
    assert {s["id"] for s in out["summaries"]} == {f"INV-{i}" for i in range(1, 6)}
    assert out["report"] == "5 invoices, total 1500.00"
    print("ok")
```

Two production details. The total comes from the source amounts, not from LLM text, so a hallucinated number cannot reach the report. And `max_concurrency` in the config caps how many workers run at once, which is how you stay under a provider's rate limit when the batch has 2,000 invoices.

## Subgraphs and state mapping

A compiled graph can be a node in another graph. There are two ways to connect them:

- **Shared keys**: if the subgraph's schema shares keys with the parent, pass the compiled subgraph straight to `add_node`. Shared keys flow in and out automatically.
- **Different schema**: call the subgraph inside a wrapper node, mapping parent state to child input and child output back to a parent update.

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START


class CheckState(TypedDict, total=False):  # shares keys with the parent
    invoice: dict
    flags: list[str]


def duplicate_check(state: CheckState) -> dict:
    return {"flags": ["duplicate"] if state["invoice"]["id"] == "INV-7" else []}


checks = (StateGraph(CheckState).add_node("duplicate_check", duplicate_check)
          .add_edge(START, "duplicate_check").compile())


class FxState(TypedDict, total=False):  # no keys in common with the parent
    amount: float
    currency: str
    amount_inr: float


RATES = {"USD": 83.0, "INR": 1.0}
fx = (StateGraph(FxState)
      .add_node("convert", lambda s: {"amount_inr": s["amount"] * RATES[s["currency"]]})
      .add_edge(START, "convert").compile())


class Parent(TypedDict, total=False):
    invoice: dict
    flags: list[str]
    amount_inr: float


def to_inr(state: Parent) -> dict:  # map parent -> child -> parent
    inv = state["invoice"]
    result = fx.invoke({"amount": inv["amount"], "currency": inv["currency"]})
    return {"amount_inr": result["amount_inr"]}


graph = (StateGraph(Parent)
         .add_node("checks", checks)  # compiled graph used directly as a node
         .add_node("to_inr", to_inr)
         .add_edge(START, "checks").add_edge("checks", "to_inr")
         .compile())

if __name__ == "__main__":
    out = graph.invoke({"invoice": {"id": "INV-7", "amount": 10.0, "currency": "USD"}})
    assert out["flags"] == ["duplicate"] and out["amount_inr"] == 830.0
    chunks = list(graph.stream({"invoice": {"id": "INV-8", "amount": 5.0,
                                            "currency": "INR"}},
                               stream_mode="updates", subgraphs=True))
    namespaces = {ns for ns, _ in chunks}  # () is the parent graph
    assert () in namespaces
    assert any(ns and ns[0].startswith("checks:") for ns in namespaces)
    print("ok")
```

A node with no outgoing edge simply ends the run, so `END` edges are optional. `stream(..., subgraphs=True)` yields `(namespace, chunk)` tuples so you can tell parent events from subgraph events. Subgraphs are how you let separate teams own "validation" and "FX" while the orchestrating graph stays small; they are also the basis of [multi-agent systems]({{ '/ai-stack/langgraph-multi-agent/' | relative_url }}).

## Retries and error handling in nodes

Model calls, FX APIs and ledger databases fail transiently. Attach a `RetryPolicy` to the node instead of writing retry loops by hand. Its fields: `max_attempts` (including the first, default 3), `initial_interval` (0.5 s), `backoff_factor` (2.0), `max_interval`, `jitter` and `retry_on` (an exception class, tuple or predicate). The default `retry_on` retries most exceptions, including `ConnectionError` and HTTP 5xx, but not programming errors such as `ValueError`, `TypeError` or `KeyError`, and not HTTP 4xx.

LangGraph 1.2 also lets you attach an `error_handler` node: when the node still fails after retries, the handler receives the state plus a `NodeError` (failing node name and exception) and can return a `Command` that routes to a fallback. `add_node` also takes a `timeout`, and `set_node_defaults()` applies a retry policy, timeout or handler to every node.

```python
import operator
from typing import Annotated, Literal

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, RetryPolicy
from langgraph.errors import NodeError


class FxUnavailable(Exception):
    """Transient: worth retrying."""


class State(TypedDict, total=False):
    amount_usd: float
    rate: float
    status: str
    log: Annotated[list[str], operator.add]


calls = {"fetch_rate": 0}


def fetch_rate(state: State) -> dict:
    calls["fetch_rate"] += 1
    if calls["fetch_rate"] < 3:  # fails twice, then recovers
        raise FxUnavailable("rates API 503")
    return {"rate": 83.1, "status": "converted", "log": ["rate fetched"]}


def on_failure(state: State, error: NodeError) -> Command[Literal["manual_review"]]:
    return Command(update={"status": "needs_review",
                           "log": [f"{error.node} failed: {error.error}"]},
                   goto="manual_review")


retry = RetryPolicy(max_attempts=3, initial_interval=0.01, jitter=False,
                    retry_on=FxUnavailable)

builder = StateGraph(State)
builder.add_node("fetch_rate", fetch_rate, retry_policy=retry,
                 error_handler=on_failure)
builder.add_node("manual_review", lambda s: {"log": ["queued for a human"]})
builder.add_edge(START, "fetch_rate")
builder.add_edge("fetch_rate", END)
builder.add_edge("manual_review", END)
graph = builder.compile()

if __name__ == "__main__":
    out = graph.invoke({"amount_usd": 10.0})
    assert calls["fetch_rate"] == 3 and out["rate"] == 83.1

    calls["fetch_rate"] = -10  # now it fails on every attempt
    out = graph.invoke({"amount_usd": 10.0})
    assert out["status"] == "needs_review"
    assert out["log"] == ["fetch_rate failed: rates API 503", "queued for a human"]
    print("ok")
```

`error_handler` is a recent addition. On older 1.x versions, or when you want the failure visible as data, use the portable pattern: catch the exception inside the node, write `{"error": str(exc)}` to state, and route on it with a conditional edge. Either way, a retried node must be idempotent: if it posts a journal entry and then times out, the retry must not post it twice. Use an idempotency key derived from the thread and invoice ID.

## Coming from Django

| Celery / Django | LangGraph |
|---|---|
| `chain(a.s(), b.s())` | `add_edge("a", "b")` |
| `group(...)` / `chord(group, callback)` | `Send` fan-out plus a reducer and a join node |
| `autoretry_for`, `retry_backoff` | `RetryPolicy(retry_on=..., backoff_factor=...)` |
| `link_error` callback | `error_handler` node |
| Task time limit | Node `timeout` |
| Nested canvas | Subgraph |

The difference: Celery coordinates through a broker and stores results by task ID. LangGraph keeps one shared, typed state for the whole workflow and checkpoints it, so the join step reads merged results directly.

## Common mistakes

- **Unbounded loops.** No attempt counter in state and no explicit `recursion_limit`. Add both.
- **I/O inside routing functions.** The result of a call made in a router never lands in state, so it cannot be inspected, traced as a step or replayed. Make the call in a node, write the result, route on it.
- **No reducer on a fan-in key.** Results in `InvalidUpdateError` or silently lost results. Annotate every key that parallel branches or `Send` workers write.
- **Using `Send` without a rate cap.** 500 invoices means 500 concurrent model calls. Set `max_concurrency`.
- **Retrying non-idempotent side effects.** Writes need idempotency keys or a check-then-write.
- **Retrying everything.** A 400 from a malformed prompt will fail three times, slower. Restrict `retry_on` to transient errors.

## Practice tasks

1. **Approval with Command.** Merge `validate` and `route_by_amount` into one `Command`-returning node that also writes `approval_reason`. Done: the three original asserts pass and the Mermaid output still shows all three branches.
2. **Graceful loop exit.** Add `remaining_steps: RemainingSteps` to the reconciliation state and route to a `report_partial` node when fewer than 2 steps remain. Done: with `recursion_limit=4` the run ends normally with unmatched lines listed, instead of raising.
3. **Rate-limited map-reduce.** Make `summarize` an `async def` that sleeps 0.1 s, run 20 invoices with `ainvoke` and `max_concurrency=5`. Done: a timing assert shows about 0.4 s, not 2 s or 0.1 s.
4. **Failure isolation in fan-out.** Make one invoice raise inside `summarize`. Catch it in the node and append `{"id": ..., "error": ...}`. Done: the report says "4 invoices, 1 failed" and the run completes.
5. **Subgraph contract test.** Write a test that compiles only the FX subgraph and checks conversions for three currencies, plus a `KeyError` for an unknown one. Done: the parent graph test no longer needs to cover FX edge cases.

## Interview questions

**Conditional edge or `Command`?**
Conditional edge when routing is a pure function of state and you want all routing visible in the graph definition. `Command` when the node that computes the update also decides the destination, and for handoffs to a parent graph.

**How do you guarantee an agent loop terminates?**
An explicit exit condition in a routing function (attempt counter, no remaining work), an explicit `recursion_limit` per invocation, and optionally `RemainingSteps` to finish gracefully. Never rely on the model deciding to stop.

**What is `Send` and when do you need it?**
A way to run a node once per item with a per-item payload, when the number of branches is only known at runtime. It is LangGraph's map step; a reducer plus a join node is the reduce step.

**What happens when two parallel nodes write the same key?**
With a reducer, both updates are merged at the end of the superstep. Without one, `InvalidUpdateError`.

**How do subgraphs share state with the parent?**
Through overlapping keys when added directly as a node, or through explicit mapping in a wrapper node when schemas differ.

**How do you handle flaky dependencies in a node?**
`RetryPolicy` with `retry_on` restricted to transient errors, a timeout, idempotent side effects, and an error handler or error-in-state route to a fallback such as manual review.

**What is the default recursion limit?**
10,007 in LangGraph 1.2 (it was 25 in early versions). Production code should set its own limit sized to the expected number of steps.

## Further reading

- LangGraph docs: "Use the graph API" (branches, loops, `Send`, `Command`)
- LangGraph docs: "Subgraphs"
- LangGraph docs: "Graph API" concepts (recursion limit, `RemainingSteps`)
- LangGraph reference: `RetryPolicy`, `Command`, `Send`
- LangGraph docs: "Durable execution" (how retries interact with checkpoints)
