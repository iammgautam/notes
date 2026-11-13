---
title: Agent loops and failure modes
section: llm
order: 22
phase: "Phase 4 · Agents"
summary: How the agent loop works, how to bound it with budgets and stop conditions, how agents fail and recover, and when a fixed workflow is the better design.
---

An agent is a loop: the model reads the conversation and tool results so far, decides the next action, your code executes it, and the result goes back into the context until the model says it is done. A workflow you wrote has a fixed number of steps and a predictable cost; an agent picks its own control flow, so its cost, latency and failure modes are open-ended. Engineering an agent is mostly engineering the loop around the model: budgets, stop conditions, error handling, checkpoints and human gates. The individual model calls are the easy part, and [Tool calling]({{ '/llm/tool-calling/' | relative_url }}) covers the dispatcher inside each step.

## How it works

### Observe, think, act

```text
            ┌──────────────────────────────────────────────┐
            ▼                                              │
  context ──► model ──► decision                           │
 (goal, history,          │                                │
  tool results)  ┌────────┼──────────────┬─────────┐       │
                 ▼        ▼              ▼         ▼       │
           final answer  tool call(s)  ask human  budget   │
                 │        │              │        exceeded │
               done   validate+execute  persist,    │      │
                          │             pause    forced    │
                          │                      answer    │
                          └──── observation ───────────────┘
```

Each iteration is one model call. **Observe**: the full context (system prompt, goal, every prior call and result) is sent again, because the model is stateless. **Think**: the model reasons, explicitly in text or internally in a reasoning model. **Act**: it either answers or emits tool calls. Your code executes the calls, appends results, and decides whether to go round again.

The loop, state and stop decision belong to your code. The model proposes; the loop disposes. Frameworks wrap this same loop: LangGraph caps it with `recursion_limit`, the OpenAI Agents SDK with `max_turns`.

### Bounded autonomy

An unbounded loop is an unbounded bill. Every run needs hard limits enforced in code:

| Limit | Starting point (tune from traces) | Catches |
|---|---|---|
| Steps (model calls) | 10–25 interactive; more for background jobs | Loops, wandering |
| Tokens or cost per run | A few times your p95 run | Context bloat, runaway runs |
| Wall-clock time | 60–120 s when a user is waiting | Slow tools, long chains |
| Calls per tool | e.g. 5 searches per run | Over-calling |
| Identical calls | 2 before intervening | Repetition loops |
| Spend per tenant per day | Product decision | Abuse, bugs across many runs |

Hitting a limit should not throw. Make one last call with tools disabled (`tool_choice: none`) asking for the best answer from what was gathered, mark the run with the limit it hit, and show the user it is partial.

### Stop conditions

| Condition | Detected by | Terminal status | User sees |
|---|---|---|---|
| Model answers with no tool calls | Model | `done` | The answer |
| Model calls a `finish` or `submit` tool | Model | `done` | Structured result |
| Step, token or time budget hit | Code | `step_limit`, `token_limit`, `timeout` | Partial answer, flagged |
| Repetition or no progress | Code | `loop_detected` | Partial answer, flagged |
| Needs approval or clarification | Code or model | `awaiting_input` | Approval UI or a question |
| Unrecoverable error | Code | `failed` | Error plus partial work |
| User cancels | User | `cancelled` | Nothing further runs |

An explicit `finish` tool is often better than "no tool calls means done". It forces a structured final result you can validate (did the agent cite sources, fill every field?), and it stops a chatty model from ending the run by narrating ("Let me check that next...") without acting.

## Failure modes

| Failure | Symptom | Metric to watch | Mitigation |
|---|---|---|---|
| Repetition loop | Same call, same args | Duplicate-call rate | Signature counting, nudge, then stop |
| Oscillation | A, B, A, B | Steps per run p95 | Detect cycles in recent call history |
| Hallucinated tool | Call to a nonexistent tool | Unknown-tool rate | Error listing available tools; fewer, clearer tools |
| Wrong arguments | Invented IDs, bad formats | Validation error rate per tool | Strict schemas, errors as data, lookup tools |
| Giving up | "I can't do that" when it could | Task success on evals | Clearer tool descriptions, recovery hints in errors |
| Over-calling | 15 searches for one fact | Calls per tool per run | Per-tool caps, better result quality |
| Context bloat | Cost and latency grow each step; quality drops | Input tokens per step | Compact old tool results, cap result size |
| Premature success | "Refund issued" when the tool failed | Claim vs database mismatch | Verify outcomes in code |
| Goal drift | Pursues a side quest | Steps on irrelevant tools | Restate the goal; plan-and-execute |
| Injection via tool output | Follows instructions in a fetched page | Red-team evals | Treat results as data; restrict writes after untrusted reads |

> **Gotcha:** Never trust the model's claim that an action succeeded. "I've cancelled the subscription" can appear after a tool returned an error. Derive user-visible status from your database, not the final message.

### Context bloat in numbers

Every step re-sends the whole context. With a 3k-token base (system prompt plus tools) and 2k tokens added per step (call plus result), step `n` sends `3k + 2k·(n−1)` tokens. Over 20 steps:

```text
total input = 20·3k + 2k·(0 + 1 + ... + 19) = 60k + 380k = 440k tokens
```

Quadratic growth: doubling the steps roughly quadruples input tokens. At an illustrative $3 per million input tokens that is about $1.30 per run before output tokens. Prompt caching cuts the price of the repeated prefix but not the quality cost of a long, noisy context. Collapsing each tool result to a 200-token stub once the model has used it brings the same run to roughly 130k tokens. See [Context engineering]({{ '/llm/context-engineering/' | relative_url }}).

## Recovery strategies

Recovery is a decision your code makes per error class, not something the model figures out alone.

| Situation | Recovery |
|---|---|
| Transient tool failure (timeout, 503) | Retry in code with backoff, once or twice; then report "unavailable" to the model |
| Validation or not-found error | Return it as the tool result; the model corrects itself |
| Same error twice | Nudge: "This approach failed twice. Try a different tool or ask the user." |
| Stuck or drifting | Re-plan: ask for a fresh plan given the goal and what has been learned |
| Ambiguous request | Ask the user one specific question instead of guessing |
| Weak model on a hard step | Escalate that step to a stronger model ([Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})) |
| Context too large | Compact: summarize old turns, drop used tool results |
| Worker crashed mid-run | Resume from the last checkpoint; idempotency keys make replayed tools safe |
| Budget exhausted | Forced final answer from what was gathered, marked partial |

Persist run state after every step: messages, step count, tokens used, pending approvals. Runs last seconds to hours and must survive deploys and worker restarts. [Agent memory]({{ '/llm/agent-memory/' | relative_url }}) covers checkpointing in depth.

## Human-in-the-loop checkpoints

Put people where a mistake is expensive or the agent lacks information:

- **Before irreversible actions:** payments, refunds above a threshold, outbound emails, deletes, permission changes. Show the exact tool and arguments, not the model's paraphrase. On approval, execute the stored call; on rejection, return the rejection (with any reason) as the tool result so the model adapts.
- **Plan approval:** for long or costly runs, show the plan before executing it. Cheap to review, and it catches misunderstood goals early.
- **Clarification:** let the model call an `ask_user` tool when information is missing, instead of guessing.
- **Escalation:** route to a human operator on repeated failures, low confidence or sensitive topics.
- **Final review:** for outputs that leave the building (customer emails, reports), a human approves the draft.

Implement a gate as pause-and-persist, never a blocking wait. The worker saves state with status `awaiting_input` and exits; the approval API loads the run and resumes it, possibly hours later on another machine. LangGraph's `interrupt` and checkpointers do exactly this. Expire stale approvals, and re-check authorization at execution time, since permissions may have changed while the request waited.

## Workflows vs agents

Most production LLM features should be workflows: code-defined paths with LLM calls at fixed points. Anthropic's "Building effective agents" names the common shapes: prompt chaining, routing, parallelization, orchestrator-workers, and evaluator-optimizer loops. They are covered in [Agent architectures]({{ '/llm/agent-architectures/' | relative_url }}).

| | Fixed workflow | Agent |
|---|---|---|
| Control flow | Your code | The model |
| Cost and latency | Predictable, low variance | Variable, long tail |
| Testing | Unit-test each step | Needs trajectory evals |
| Failure modes | Known, enumerable | Open-ended |
| Handles novel requests | Poorly | Well |
| Best for | Known steps: extract, classify, route, summarize | Steps that depend on intermediate results |

Choose a workflow when you can draw the flowchart, when latency and cost must be predictable, or when errors are expensive and the action space is small. Choose an agent when the number and order of steps genuinely depend on what earlier steps find: open-ended research, debugging, multi-source investigation. Even then, keep the toolset small and the scope narrow. Many successful "agents" are a workflow with one agentic step inside, such as a router followed by a bounded tool loop for the one hard branch.

## In practice: a bounded agent loop

Runnable, stdlib only. A scripted fake model drives four scenarios: recovery from a bad argument, a repetition loop that is nudged and then stopped, budgets ending a wandering run, and an approval gate that persists, resumes from JSON, and verifies the side effect in code.

```python
import itertools
import json
from dataclasses import asdict, dataclass, field

REFUNDS = []


def search_docs(query):
    return {"hits": [f"Refunds are allowed within 30 days of invoice ({query})."]}


def get_invoice(invoice_id):
    return {"id": invoice_id, "amount_cents": 4900, "days_old": 12}


def issue_refund(invoice_id, amount_cents):
    REFUNDS.append(invoice_id)
    return {"ok": True}


TOOLS = {f.__name__: f for f in (search_docs, get_invoice, issue_refund)}
NEEDS_APPROVAL = {"issue_refund"}


@dataclass
class Budget:
    max_steps: int = 8
    max_tokens: int = 6_000
    max_repeats: int = 2  # identical calls executed before we intervene


@dataclass
class Run:  # everything needed to resume; persisted after every step
    goal: str
    messages: list = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    status: str = "running"
    pending: dict | None = None
    seen: dict = field(default_factory=dict)
    answer: str | None = None


def execute(call):
    if call["name"] not in TOOLS:
        return {"error": f"unknown tool {call['name']!r}; available: {sorted(TOOLS)}"}
    try:
        return TOOLS[call["name"]](**call["args"])
    except TypeError as e:  # wrong argument names: error as data
        return {"error": f"bad arguments: {e}"}


def finish(run, model, status):
    """Out of budget or stuck: one last call with tools disabled."""
    run.answer, run.status = model(run.messages, tools=False)["content"], status
    return run


def run_agent(run, model, budget=Budget()):
    if not run.messages:
        run.messages.append({"role": "user", "content": run.goal})
    while run.status == "running":
        if run.steps >= budget.max_steps:
            return finish(run, model, "step_limit")
        if run.tokens >= budget.max_tokens:
            return finish(run, model, "token_limit")
        reply = model(run.messages, tools=True)
        run.steps += 1
        run.tokens += len(json.dumps(run.messages)) // 4  # whole context re-sent each step
        run.messages.append(reply)
        if not reply.get("tool_calls"):
            run.status, run.answer = "done", reply["content"]
            break
        for call in reply["tool_calls"]:
            sig = call["name"] + json.dumps(call["args"], sort_keys=True)
            run.seen[sig] = run.seen.get(sig, 0) + 1
            if run.seen[sig] > budget.max_repeats:
                result = {"error": "You already made this exact call. Use the earlier "
                                   "result, try something different, or answer."}
            elif call["name"] in NEEDS_APPROVAL:
                run.pending = call  # its result arrives on resume
                continue
            else:
                result = execute(call)
            run.messages.append({"role": "tool", "id": call["id"], "content": result})
        if max(run.seen.values()) > budget.max_repeats + 1:  # ignored the nudge
            return finish(run, model, "loop_detected")
        if run.pending:
            run.status = "awaiting_input"  # persist and exit; no blocking wait
    return run


def resume(run, model, approved):
    call, run.pending, run.status = run.pending, None, "running"
    result = execute(call) if approved else {"error": "User rejected this action. Do not retry it."}
    run.messages.append({"role": "tool", "id": call["id"], "content": result})
    return run_agent(run, model)


class FakeModel:
    """Scripted stand-in for an LLM: returns the next reply, ignoring its input."""
    def __init__(self, replies):
        self.replies = iter(replies)

    def __call__(self, messages, tools):
        if not tools:
            return {"role": "assistant", "content": "Best effort: refunds allowed within 30 days."}
        return next(self.replies)


ids = itertools.count()


def calls(*pairs):
    return {"role": "assistant",
            "tool_calls": [{"id": f"c{next(ids)}", "name": n, "args": a} for n, a in pairs]}


def say(text):
    return {"role": "assistant", "content": text}


if __name__ == "__main__":
    # 1. a wrong argument name comes back as data and the model recovers
    run = run_agent(Run("Can I get a refund for INV-7?"), FakeModel([
        calls(("get_invoice", {"id": "INV-7"})),
        calls(("get_invoice", {"invoice_id": "INV-7"}), ("search_docs", {"query": "refund window"})),
        say("Yes: INV-7 is 12 days old and the window is 30 days."),
    ]))
    assert run.status == "done" and run.steps == 3
    assert "bad arguments" in run.messages[2]["content"]["error"]

    # 2. the same call forever: two executions, a nudge, then a forced stop
    run = run_agent(Run("Refund policy?"),
                    FakeModel([calls(("search_docs", {"query": "refund"})) for _ in range(10)]))
    assert run.status == "loop_detected" and run.steps == 4 and run.answer.startswith("Best effort")

    # 3. never repeats, never finishes: the step budget or the token budget ends it
    def wander():
        return FakeModel(calls(("search_docs", {"query": f"refund {i}"})) for i in itertools.count())
    assert run_agent(Run("Refund policy?"), wander(), Budget(max_steps=5)).status == "step_limit"
    run = run_agent(Run("Refund policy?"), wander(), Budget(max_steps=100, max_tokens=5_000))
    assert run.status == "token_limit" and run.steps < 30  # quadratic growth hits tokens first

    # 4. approval gate: pause, persist as JSON, resume elsewhere, verify in code
    model = FakeModel([calls(("issue_refund", {"invoice_id": "INV-7", "amount_cents": 4900})),
                       say("Refund issued.")])
    run = run_agent(Run("Refund INV-7"), model)
    assert run.status == "awaiting_input" and REFUNDS == []
    saved = json.dumps(asdict(run))  # e.g. a JSONB column on agent_runs
    run = resume(Run(**json.loads(saved)), model, approved=True)
    assert run.status == "done" and REFUNDS == ["INV-7"]  # trust the side effect, not the text
    print("agent loop demo: ok")
```

## Pitfalls

- **Budgets as exceptions.** Symptom: users see a 500 after two minutes of waiting. Fix: forced final answer with tools disabled, flagged as partial.
- **Counting steps but not tokens.** Symptom: a 10-step run costs 50x the median because one tool returned a huge payload. Fix: token budget plus result size caps.
- **Blocking on approval.** Symptom: workers tied up for hours, lost runs on deploy. Fix: persist, exit, resume from the approval endpoint.
- **No trajectory visibility.** Symptom: you cannot explain why a run did something. Fix: trace every run as a tree of steps, model calls and tool calls with inputs, outputs, tokens and latency ([Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).
- **Watching averages.** A rising share of `step_limit` or `loop_detected` terminal statuses is the earliest regression signal after a prompt or model change. Alert on the terminal-status distribution and p95 steps, not mean latency.
- **Agent where a workflow fits.** Symptom: high variance in cost and quality on a task with known steps. Fix: hard-code the steps; keep the model for the judgment calls.

## Checklist

- Caps on steps, tokens, wall time, per-tool calls and tenant spend; forced partial answer at the limit.
- Duplicate-call and cycle detection with a nudge before a stop.
- An explicit `finish` tool when the output must be structured.
- Errors classified: retry transient in code, return the rest as data.
- State persisted every step; approvals and clarifications as pause-and-resume.
- Outcomes verified in code, never from the model's final text.
- Terminal status, steps, tokens and cost tracked per run.
- Start with a workflow; add autonomy only where it pays.

## Interview questions

**What is an agent, mechanically?** A loop in your code: send context to the model, execute any tool calls it returns, append results, repeat until it answers or a limit hits. The model is stateless; the loop owns state, budgets and stopping.

**How do you stop an agent from looping forever?** Hard step, token and time budgets; duplicate-call and cycle detection with a nudge and then a stop; error classification so non-retryable errors are not retried; and a forced final answer with tools disabled when a limit hits.

**Why does agent cost grow faster than linearly with steps?** Each step re-sends the full growing context, so total input tokens grow roughly quadratically in step count. Compact old tool results, cap result sizes, and use prompt caching for the stable prefix.

**How do you add human approval?** Gate specific tools. On a gated call, persist the run with the exact pending call and exit. The approval endpoint loads the run, executes the stored call (or returns the rejection as a tool result), and resumes the loop.

**The agent says it issued a refund but the customer has no refund. What went wrong and how do you prevent it?** The model claimed success despite a failed or never-executed tool call. Derive status from the database or tool results, verify outcomes in code, and show users system-confirmed state.

**When would you not use an agent?** When the steps are known in advance, latency and cost must be predictable, or mistakes are costly and the action space small. A workflow of fixed LLM calls is cheaper, faster and testable.

**How do you know an agent change made things better?** Run trajectory and outcome evals on a fixed task set, compare success rate, steps, tokens and terminal statuses, then watch the same metrics online. See [Agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }}).

## Further reading

- ReAct: Synergizing Reasoning and Acting in Language Models (Yao et al., 2022).
- Building effective agents (Anthropic, 2024).
- A practical guide to building agents (OpenAI, 2025).
- SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering (Yang et al., 2024).
- LangGraph documentation on persistence and human-in-the-loop.
