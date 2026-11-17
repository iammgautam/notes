---
title: Human-in-the-loop with LangGraph
section: ai-stack
order: 25
phase: "Phase 4 · LangGraph"
summary: Pause a graph with interrupt(), resume it with Command, and put approve, edit and reject gates in front of payments and journal postings.
---

After this note you can stop a LangGraph run at any point, show a person exactly what the agent is about to do, and continue with their answer days later, from a different process. On a finance data platform this is a requirement: an agent may draft a journal entry or prepare a vendor payment, but a named human approves it before anything touches the general ledger or the bank. Interviewers ask about it because it separates demo agents from production ones.

## Prerequisites

Checkpointers and thread ids from [LangGraph persistence and memory]({{ '/ai-stack/langgraph-persistence-and-memory/' | relative_url }}), and the tool loop from [Tool-calling agents in LangGraph]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}).

## The mental model: a checkpointed pause

Calling `interrupt(value)` inside a node raises a special exception. LangGraph catches it, saves a checkpoint for the thread, and returns to the caller with `value` under the `__interrupt__` key. Nothing waits in memory and no worker is blocked. To continue, you invoke the graph again with the same `thread_id` and `Command(resume=answer)`. LangGraph loads the checkpoint, runs the interrupted node again from its first line, and this time `interrupt()` returns `answer` instead of raising.

```text
 client                  graph (thread_id=je-42)            checkpointer
 ------                  -----------------------            ------------
 invoke(input) ------->  draft_entry -> review
                                        interrupt(entry)
                                          | raises    --->  save state +
 <-- {"__interrupt__": [entry]} ----------+                 pending item

      ... reviewer opens the approval screen; hours pass ...

 invoke(Command(resume=decision)) ---> load checkpoint  <------+
                         review runs again from line 1
                         interrupt(...) now returns decision
                         -> post_entry -> END
 <-- final state -------
```

Three consequences drive every design decision below:

1. **You need a checkpointer and a thread id.** The thread id is the identity of the approval request. Use something you already have, such as the journal request id.
2. **The resume value is whatever `interrupt()` returns.** Any JSON-serializable value works: `True`, a string, or a structured decision.
3. **The node restarts.** Code above the `interrupt()` call runs twice. Side effects belong after it, or in the next node.

## An approval gate for journal entries

The graph below drafts a journal entry, pauses for review, and posts only after approval. The reviewer can approve, edit specific fields, or reject with a reason. `SqliteSaver` writes checkpoints to a file, and the two `with` blocks simulate two separate HTTP requests, or two processes, sharing nothing but the database.

{% raw %}
```python
import os
import tempfile
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

LEDGER: list[dict] = []  # stand-in for the GL system you would call over HTTP
EDITABLE = {"debit_account", "credit_account", "amount", "memo"}


class JournalState(TypedDict, total=False):
    request: str
    requested_by: str
    entry: dict
    status: str
    entry_id: str
    reviewer: str
    review_note: str


def draft_entry(state: JournalState) -> dict:
    # In production an LLM with structured output proposes this entry.
    entry = {
        "debit_account": "6100-Travel",
        "credit_account": "2000-AccountsPayable",
        "amount": 1250.00,
        "memo": state["request"],
    }
    return {"entry": entry, "status": "awaiting_review"}


def check(decision: dict) -> str | None:
    if decision.get("type") not in {"approve", "edit", "reject"}:
        return "type must be approve, edit or reject"
    if not decision.get("reviewer"):
        return "reviewer is required"
    unknown = set(decision.get("changes", {})) - EDITABLE
    if unknown:
        return f"cannot edit fields: {sorted(unknown)}"
    return None


def review(state: JournalState) -> Command[Literal["post_entry", "__end__"]]:
    request = {
        "action": "post_journal_entry",
        "entry": state["entry"],
        "allowed_decisions": ["approve", "edit", "reject"],
    }
    while True:
        decision = interrupt(request)
        problem = check(decision)
        if problem is None:
            break
        request = {**request, "error": problem}  # ask again; never raise on human input

    reviewer = decision["reviewer"]
    if decision["type"] == "approve":
        return Command(update={"reviewer": reviewer}, goto="post_entry")
    if decision["type"] == "edit":
        edited = {**state["entry"], **decision["changes"]}
        return Command(update={"entry": edited, "reviewer": reviewer}, goto="post_entry")
    return Command(
        update={"status": "rejected", "reviewer": reviewer, "review_note": decision["reason"]},
        goto=END,
    )


def post_entry(state: JournalState) -> dict:
    entry_id = f"JE-{len(LEDGER) + 1:04d}"
    LEDGER.append({"id": entry_id, "approved_by": state["reviewer"], **state["entry"]})
    return {"status": "posted", "entry_id": entry_id}


def build(checkpointer):
    g = StateGraph(JournalState)
    g.add_node(draft_entry)
    g.add_node(review)
    g.add_node(post_entry)
    g.add_edge(START, "draft_entry")
    g.add_edge("draft_entry", "review")
    g.add_edge("post_entry", END)
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    db_path = os.path.join(tempfile.mkdtemp(), "checkpoints.db")
    cfg = {"configurable": {"thread_id": "je-request-42"}}

    # Request 1: the run pauses at the review node.
    with SqliteSaver.from_conn_string(db_path) as saver:
        graph = build(saver)
        out = graph.invoke({"request": "Q3 offsite travel", "requested_by": "ravi"}, cfg)
        pending = out["__interrupt__"][0]
        assert pending.value["entry"]["amount"] == 1250.00
        assert graph.get_state(cfg).next == ("review",)
        assert LEDGER == []  # nothing posted yet

    # Request 2, maybe hours later in another process: same db, same thread_id.
    with SqliteSaver.from_conn_string(db_path) as saver:
        graph = build(saver)
        bad = {"type": "edit", "reviewer": "asha", "changes": {"approved_by": "asha"}}
        out = graph.invoke(Command(resume=bad), cfg)
        assert out["__interrupt__"][0].value["error"] == "cannot edit fields: ['approved_by']"

        good = {"type": "edit", "reviewer": "asha", "changes": {"amount": 1205.00}}
        out = graph.invoke(Command(resume=good), cfg)
        assert out["status"] == "posted" and out["entry_id"] == "JE-0001"
        assert LEDGER[0]["amount"] == 1205.00 and LEDGER[0]["approved_by"] == "asha"

        # A rejected request never reaches post_entry.
        cfg2 = {"configurable": {"thread_id": "je-request-43"}}
        graph.invoke({"request": "Duplicate vendor invoice", "requested_by": "ravi"}, cfg2)
        out = graph.invoke(
            Command(resume={"type": "reject", "reviewer": "asha", "reason": "duplicate"}), cfg2
        )
        assert out["status"] == "rejected" and len(LEDGER) == 1
    print("ok")
```
{% endraw %}

What to notice:

- `review` returns `Command(goto=...)`, so the routing decision and the state update happen together. The `Command[Literal[...]]` annotation tells LangGraph which edges exist, for rendering and validation.
- Human input is a trust boundary too. `check` allowlists editable fields, so a reviewer can change the amount or memo but not inject an `approved_by` key.
- Invalid input triggers a second `interrupt()` carrying an `error`, instead of an exception. That matters: once a resume value is recorded, an exception later in the node leaves the thread replaying that same bad value on every retry. Validate and ask again; never raise on human input.
- The reviewer's name lands in state, so `graph.get_state_history(cfg)` gives you who decided what and when. Most auditors still want a dedicated approvals table, but the checkpoint trail is a useful second record.
- `graph.get_state(cfg).interrupts` lists pending interrupts without running anything. `invoke(..., version="v2")` returns a `GraphOutput` with `.value` and `.interrupts` instead of the `__interrupt__` key.
- Parallel branches can interrupt in the same step; resume them with a dict keyed by interrupt id, `Command(resume={intr.id: answer, ...})`.

## Resuming re-runs the node

This is the behavior that causes real bugs. The counter below proves it:

{% raw %}
```python
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

calls = {"before": 0, "after": 0}


class PayState(TypedDict, total=False):
    amount: float
    approved: bool


def gate(state: PayState) -> dict:
    calls["before"] += 1   # runs on the first pass AND again on resume
    ok = interrupt(f"Approve payment of {state['amount']}?")
    calls["after"] += 1    # runs once, after the human answers
    return {"approved": ok}


g = StateGraph(PayState)
g.add_node(gate)
g.add_edge(START, "gate")
g.add_edge("gate", END)
graph = g.compile(checkpointer=InMemorySaver())

if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "pay-1"}}
    graph.invoke({"amount": 980.0}, cfg)
    out = graph.invoke(Command(resume=True), cfg)
    assert out["approved"] is True
    assert calls == {"before": 2, "after": 1}
    print("ok")
```
{% endraw %}

Rules that follow from it:

- Never call an external system above `interrupt()` in the same node. If `gate` had created a payment draft in the bank API before pausing, you would get two drafts.
- Make the side-effecting call idempotent anyway, keyed on something stable like the thread id plus entry id. Retries and double clicks happen. See [Idempotency]({{ '/backend/idempotency/' | relative_url }}).
- If a node calls `interrupt()` more than once, resume values are matched by position. The validation loop above is safe because replaying the same answers produces the same sequence of calls. Do not put calls behind a condition that can change between runs, such as the clock or a database read.
- `interrupt()` works by raising an exception that subclasses `Exception`. A broad `try/except Exception` around it swallows the pause.

## Static breakpoints for debugging

Before `interrupt()` existed, you paused graphs with `interrupt_before` and `interrupt_after`. They still exist, and they are the right tool for stepping through a graph while debugging: stop before a node, inspect state, patch it, continue. Pass them to `compile()` or to a single `invoke()` call.

{% raw %}
```python
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class ReconState(TypedDict, total=False):
    bank_total: float
    ledger_total: float
    adjustments: Annotated[list[float], operator.add]
    status: str


def compute_gap(state: ReconState) -> dict:
    return {"adjustments": [round(state["bank_total"] - state["ledger_total"], 2)]}


def book_adjustment(state: ReconState) -> dict:
    return {"status": f"booked {sum(state['adjustments']):.2f}"}


g = StateGraph(ReconState)
g.add_node(compute_gap)
g.add_node(book_adjustment)
g.add_edge(START, "compute_gap")
g.add_edge("compute_gap", "book_adjustment")
g.add_edge("book_adjustment", END)
graph = g.compile(checkpointer=InMemorySaver(), interrupt_before=["book_adjustment"])

if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "recon-2026-10"}}
    graph.invoke({"bank_total": 10_480.00, "ledger_total": 10_000.00}, cfg)

    snap = graph.get_state(cfg)
    assert snap.next == ("book_adjustment",)
    assert snap.values["adjustments"] == [480.0]

    # 30.00 is a bank fee already booked elsewhere: patch state, then continue.
    graph.update_state(cfg, {"adjustments": [-30.0]})
    assert graph.get_state(cfg).values["adjustments"] == [480.0, -30.0]  # reducer ran

    out = graph.invoke(None, cfg)  # None means "continue from the checkpoint"
    assert out["status"] == "booked 450.00"
    print("ok")
```
{% endraw %}

`update_state` goes through the reducers, exactly like a node return, and is recorded as a new checkpoint attributed to the last node that ran (override with `as_node=`). That is why the list above grows instead of being replaced.

Use static breakpoints for debugging and tests. For production approvals use `interrupt()`: it carries a payload, receives a structured answer, and can fire conditionally, while a static breakpoint stops at a node every time.

## Gating agent tools

In a tool-calling agent the dangerous step is a tool, so the gate belongs around the tool call. There are two ways.

**Call `interrupt()` inside the tool.** Tools run inside a graph node, so this just works when the agent has a checkpointer:

```python
@tool
def send_payment(vendor_id: str, amount: float) -> str:
    """Send a payment to a vendor."""
    answer = interrupt({"tool": "send_payment", "vendor_id": vendor_id, "amount": amount})
    if not answer["approved"]:
        return f"Payment not sent: {answer['reason']}"  # the model sees this
    return bank.pay(vendor_id, amount, idempotency_key=...)
```

**Use `HumanInTheLoopMiddleware` with `create_agent`.** LangChain 1.x moved agent customization into middleware. This middleware checks every tool call the model proposes against an `interrupt_on` policy and pauses before execution. It is the better default because the policy lives in one place, tools stay plain functions, and the reviewer gets a uniform payload.

{% raw %}
```python
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

PAID: list[dict] = []


class FakeToolModel(GenericFakeChatModel):
    """Scripted model. Real code: init_chat_model("openai:gpt-4.1-mini")."""

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def lookup_vendor(vendor_id: str) -> str:
    """Return the vendor's name and bank verification status."""
    return f"{vendor_id}: Acme Logistics, bank verified"


@tool
def send_payment(vendor_id: str, amount: float, invoice_id: str) -> str:
    """Send a payment to a vendor for an invoice."""
    PAID.append({"vendor_id": vendor_id, "amount": amount, "invoice_id": invoice_id})
    return f"paid {amount:.2f} to {vendor_id} for {invoice_id}"


def over_limit(request) -> bool:
    return request.tool_call["args"]["amount"] > 1_000  # small payments skip review


script = iter([
    AIMessage(content="", tool_calls=[
        {"name": "lookup_vendor", "args": {"vendor_id": "V-881"}, "id": "c1"},
    ]),
    AIMessage(content="", tool_calls=[
        {"name": "send_payment",
         "args": {"vendor_id": "V-881", "amount": 4200.0, "invoice_id": "INV-1007"},
         "id": "c2"},
    ]),
    AIMessage(content="Payment for INV-1007 sent after review."),
])

agent = create_agent(
    FakeToolModel(messages=script),
    tools=[lookup_vendor, send_payment],
    middleware=[
        HumanInTheLoopMiddleware(
            interrupt_on={
                "lookup_vendor": False,  # read-only, never gated
                "send_payment": {
                    "allowed_decisions": ["approve", "edit", "reject"],
                    "when": over_limit,
                },
            },
        )
    ],
    checkpointer=InMemorySaver(),
)

if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "ap-run-7"}}
    out = agent.invoke({"messages": [{"role": "user", "content": "Pay INV-1007"}]}, cfg)
    request = out["__interrupt__"][0].value
    assert request["action_requests"][0]["name"] == "send_payment"
    assert request["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]
    assert PAID == []

    edited = {"name": "send_payment",
              "args": {"vendor_id": "V-881", "amount": 4020.0, "invoice_id": "INV-1007"}}
    out = agent.invoke(
        Command(resume={"decisions": [{"type": "edit", "edited_action": edited}]}), cfg
    )
    assert PAID == [{"vendor_id": "V-881", "amount": 4020.0, "invoice_id": "INV-1007"}]
    assert out["messages"][-1].content.startswith("Payment for INV-1007")
    print("ok")
```
{% endraw %}

How the middleware behaves in LangChain 1.5:

- **Decisions.** `approve` runs the call as proposed. `edit` runs `edited_action` instead, and the tool result is prefixed with a note telling the model a human changed the call. `reject` skips the tool and sends the model your `message`, so it can explain or try something else. `respond` skips the tool and returns your text as the tool result, which suits "ask the user" tools.
- **Batching.** The default `interrupt_mode="batched"` raises one interrupt per model turn listing every gated call; answer with `{"decisions": [...]}` in the same order. `interrupt_mode="per_call"` raises one per call, answered by interrupt id, and is slated to become the default in the next major release.
- **Conditional gates.** `when` receives the tool call request and returns `True` to pause, so routine small payments skip the queue.

What changed: older code used `create_react_agent(..., interrupt_before=["tools"])`, a static breakpoint that stopped every tool call, harmless reads included, with no approve or edit protocol. `langgraph.prebuilt.create_react_agent` still imports, but new code should use `langchain.agents.create_agent` with middleware.

## Serving approvals from an API

In a service the flow becomes two endpoints: one starts the run and returns the pending payload, the other resumes it. A sketch of the second, built on the journal graph above:

{% raw %}
```python
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

api = FastAPI()


class DecisionIn(BaseModel):
    type: Literal["approve", "edit", "reject"]
    changes: dict = {}
    reason: str = ""


@api.post("/journal-requests/{request_id}/decision")
def decide(request_id: str, body: DecisionIn, reviewer: str = Depends(current_reviewer)):
    cfg = {"configurable": {"thread_id": request_id}}
    snap = graph.get_state(cfg)
    if not snap.interrupts:
        raise HTTPException(409, "nothing is pending for this request")
    if reviewer == snap.values["requested_by"]:
        raise HTTPException(403, "the requester cannot approve their own entry")
    out = graph.invoke(Command(resume={**body.model_dump(), "reviewer": reviewer}), cfg)
    return {"status": out["status"], "entry_id": out.get("entry_id")}
```
{% endraw %}

The 403 is maker-checker, the segregation-of-duties control every finance team expects. The 409 handles the second click after a decision. Two reviewers deciding at the same instant can still race; serialize resumes per thread with a row lock on your approvals table or a [distributed lock]({{ '/backend/distributed-locking/' | relative_url }}).

## Coming from Django

| Django habit | LangGraph equivalent |
|---|---|
| Model with `status = PENDING_APPROVAL` plus an admin action | `interrupt()` in a node; the checkpoint is the pending row |
| Request id in the URL | `thread_id` in `config["configurable"]` |
| Celery chain that stops and is re-queued later | `Command(resume=...)` on the same thread |
| `django-fsm` transitions | `Command(goto=...)` from the review node |
| `ModelAdmin` form validation | Validate the resume payload before acting on it |
| `simple_history` audit rows | `get_state_history()`, plus your own approvals table |

In Django the "after approval" code lives in a separate view that reloads state by hand. Here the run's state is already saved and the node picks up where it stopped.

## Common mistakes

- **No checkpointer.** The first `invoke` returns the interrupt and looks fine; the resume then fails with `RuntimeError: Cannot use Command(resume=...) without checkpointer`. Compile with a checkpointer and always pass a `thread_id`.
- **New thread id on resume.** A fresh id starts a fresh run. Persist the id with the approval request.
- **Side effects before the interrupt.** They run twice. Move them after `interrupt()` or into the next node.
- **Trusting the resume payload.** It comes from a browser. Validate types, allowlist editable fields, check that the reviewer is allowed to approve that amount.
- **Raising on bad human input.** The recorded resume value is replayed on every later resume of that thread, so the node fails forever. Validate at the API before resuming, and inside the node re-interrupt with an error instead of raising.
- **`InMemorySaver` in production.** Pending approvals vanish on deploy. Use the Postgres checkpointer (see [Streaming and deploying LangGraph apps]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }})).
- **Gating everything.** Reviewers rubber-stamp a queue of trivial approvals. Gate writes, gate above thresholds, let reads through.

## Practice tasks

1. Run the journal graph, then call `get_state_history` on the approved thread and print, for each checkpoint, `next` and the `status` value. Done when you can point at the checkpoint where the reviewer's decision entered state.
2. Add an approval limit: the decision carries `limit`, and `check` rejects an approval when `entry["amount"]` exceeds it. Done when a 5,000 entry approved by a reviewer with a 2,000 limit comes back as a new interrupt with an `error`, and a second resume from a reviewer with a 10,000 limit posts it.
3. Extend the middleware demo so `send_payment` allows only `approve` and `reject`, and the agent issues two payments in one turn. Done when you resume with one approval and one rejection and only one payment lands in `PAID`.
4. Wrap the journal graph in FastAPI: `POST /journal-requests` returns the interrupt payload, `POST /journal-requests/{id}/decision` resumes it. Use `SqliteSaver` and `TestClient`. Done when tests cover approve, edit, reject, the 409 on a double decision, and the 403 for maker-checker.

## Interview questions

**How does `interrupt()` pause a graph without blocking a worker?**
It raises an exception that LangGraph catches. The graph saves a checkpoint containing state and the pending interrupt, then returns. Resuming is a new call that loads the checkpoint, so no process holds anything open while the human decides.

**What happens to the code in a node before the `interrupt()` call when you resume?**
It runs again, because the node restarts from the top. Keep side effects after the interrupt or in a later node, and make them idempotent.

**`interrupt()` or `interrupt_before`?**
`interrupt()` for product features: it carries a payload, receives a structured decision, and can be conditional. Static breakpoints for debugging and tests, where you want to stop at a node every time.

**How would you let a reviewer edit an agent's proposed payment?**
With `HumanInTheLoopMiddleware`, resume with an `edit` decision carrying the new arguments; the tool runs with them and the model is told the call was changed. In a hand-built graph, merge allowlisted changes into state and route on with `Command(goto=...)`.

**Two reviewers click approve at the same time. What happens?**
Both may resume the same thread. Serialize resumes per thread with a lock, reject a resume when nothing is pending, and make the posting call idempotent so a duplicate cannot create a second entry.

**How do you audit approvals?**
Write an approvals row (who, when, decision, before and after values) in the same request that resumes the graph, and keep checkpoint history as a secondary trail. Tag traces with the request id so you can jump from the audit row to the full agent trace.

## Further reading

- LangGraph docs: "Interrupts" and "Human-in-the-loop"
- LangGraph docs: "Persistence" (checkpointers, `get_state`, `update_state`)
- LangChain docs: "Human-in-the-loop" middleware for `create_agent`
- LangGraph reference: `interrupt`, `Command`, `SqliteSaver`
- [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}) and [Agent loops and failure modes]({{ '/llm/agents/' | relative_url }})
