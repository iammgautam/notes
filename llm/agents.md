---
title: Agent loops and failure modes
section: llm
order: 10
summary: How a ReAct-style agent loop works, how it fails in production (loops, hallucinated tools, runaway cost), and when a fixed workflow is the better design.
---

An agent is a loop: the model looks at the conversation and tool results so far, decides the next action, your code executes it, and the result goes back into the context until the model decides it is done. That flexibility is the point and the problem. A workflow you wrote has a fixed number of steps and a predictable cost; an agent decides its own control flow, so its cost, latency and failure modes are open-ended. Engineering an agent is mostly about bounding that loop.

## How it works

### The ReAct-style loop

```text
          ┌────────────────────────────────────────────┐
          ▼                                            │
 context ──► model ──► decision                        │
                          │                            │
            ┌─────────────┼───────────────┐            │
            ▼             ▼               ▼            │
       final answer   tool call(s)   ask human         │
            │             │               │            │
          return     validate+execute  pause, persist  │
                          │               state        │
                          └── observation ─────────────┘
```

"ReAct" (reason + act) interleaves reasoning with actions: think about what is needed, call a tool, observe the result, repeat. Modern APIs implement this natively through tool calling, and reasoning-capable models do the "think" step internally. The loop itself is your code.

### Planning

Two common shapes:

- **Implicit planning:** the model picks one step at a time. Simple and adaptive, but it can wander.
- **Plan-then-execute:** one call writes an explicit plan (a list of steps), then the loop executes steps, optionally re-planning when a step fails. Plans are inspectable, easier to show users, and easier to approve before execution.

Multi-agent setups (an orchestrator delegating to specialist sub-agents) are the same loop nested. They help when sub-tasks need isolated context windows; they multiply cost and make debugging harder.

### State

The model's context is the agent's working memory, and it grows every step. Keep three kinds of state distinct:

| State | Lives in | Example |
|---|---|---|
| Conversation context | The message list sent to the model | Tool calls and results so far |
| Run state | Your database | Step count, tokens used, status, pending approval |
| Domain state | Your normal tables | The refund that was created |

Persist run state after each step. Agent runs take seconds to minutes and must survive worker restarts, deploys and human approval delays measured in hours.

## In practice

### A bounded loop

```python
from dataclasses import dataclass

@dataclass
class Budget:
    max_steps: int = 12
    max_tokens: int = 200_000
    max_seconds: float = 120.0

async def run_agent(run: AgentRun, llm, tools, budget: Budget) -> AgentResult:
    seen_calls: dict[str, int] = {}
    while True:
        if run.steps >= budget.max_steps:
            return run.finish("step_limit", await summarize_partial(llm, run))
        if run.tokens_used >= budget.max_tokens or run.elapsed() > budget.max_seconds:
            return run.finish("budget_exceeded", await summarize_partial(llm, run))

        resp = await llm.complete(messages=run.messages, tools=tools.schemas(run.user))
        run.record(resp)                                  # tokens, latency, step++
        if not resp.tool_calls:
            return run.finish("done", resp.text)

        for call in resp.tool_calls:
            sig = f"{call.name}:{call.arguments}"
            seen_calls[sig] = seen_calls.get(sig, 0) + 1
            if seen_calls[sig] > 2:
                run.add_tool_result(call, error="Repeated identical call. "
                                    "Try a different approach or answer with what you have.")
                continue
            if tools.requires_approval(call):
                await run.pause_for_approval(call)        # persist and return
                return run.finish("awaiting_approval", None)
            run.add_tool_result(call, await tools.execute(run.ctx, call))
        await run.save()
```

What this enforces:

- **Hard limits** on steps, tokens and wall time, with a graceful partial answer instead of an exception.
- **Loop detection** on identical calls. Also consider detecting oscillation (A, B, A, B) and lack of progress.
- **Per-user tool sets** (`tools.schemas(run.user)`): the model never sees tools the user may not use.
- **Approval gates** that persist and resume rather than block a worker.
- **Checkpointing** after each step.

### Human-in-the-loop

Put a human gate in front of irreversible or high-impact actions: payments, refunds over a threshold, emails to customers, deletes, permission changes. Present the exact proposed action and arguments, not the model's description of it. On approval, execute the stored call exactly; on rejection, feed the rejection back as a tool result so the model can adjust.

Run the loop as a background job (Celery or a Postgres-backed queue), stream progress events to the client, and resume from the persisted state when the approval arrives.

### Observability

Trace every run as a tree: run, steps, model calls, tool calls, with inputs, outputs, tokens, latency and errors. You will need to replay "why did the agent do that" for a specific run id. Track per-run aggregates: steps, tokens, cost, terminal status (`done`, `step_limit`, `budget_exceeded`, `error`, `awaiting_approval`). A rising share of `step_limit` is your earliest signal that something regressed.

## Failure modes

| Failure | Symptom | Mitigation |
|---|---|---|
| Infinite or repetitive loops | Same tool, same args; step limit hit | Duplicate-call detection, step cap, "answer with what you have" nudge |
| Hallucinated tools | Calls to tools that do not exist | Strict tool schemas; return "unknown tool, available: …" |
| Hallucinated arguments | Invented ids, wrong formats | Validation, lookups before actions, enums |
| Premature completion | Claims success without doing the work | Verify outcomes in code, not from model text |
| Context bloat | Cost and latency grow each step; quality drops | Truncate or summarize old tool results; cap result sizes |
| Goal drift | Agent pursues a sub-task forever | Plan-then-execute, re-state the goal in each step's prompt |
| Error spirals | Every retry fails, model keeps trying | Classify errors as retryable or not; stop on non-retryable |
| Injection via tool results | Agent follows instructions in a fetched page | Treat tool output as data; restrict tools after untrusted reads |
| Runaway cost | One run uses 100x median tokens | Token budget per run and per tenant per day |

> **Gotcha:** Never trust the model's claim that an action succeeded. "I've issued the refund" can appear even when the tool returned an error. Derive user-visible status from your database, not from the final message.

## When not to use agents

Prefer a fixed workflow (a pipeline of LLM calls and code with known steps) when:

- The steps are known in advance: extract, classify, route, summarize. Code the steps.
- You need predictable latency and cost per request.
- Errors are expensive and the action space is small.
- You can enumerate branches with a router (one classification call, then a fixed path).

Use an agent when the number and order of steps truly depends on intermediate results: open-ended research, debugging, multi-source investigation, tasks where the user's request is underspecified. Even then, give the agent a small toolset and narrow scope. Many successful "agents" are a workflow with one agentic step inside.

## Checklist

- Hard caps on steps, tokens and wall time; graceful partial answers.
- Duplicate-call and no-progress detection.
- Per-user tool exposure; least-privilege tool handlers.
- Human approval for irreversible actions; resumable persisted runs.
- Trace every step; track terminal status distribution.
- Outcome checks in code, not in the model's final text.
- Start with a workflow; add autonomy only where it pays.

## Interview angle

- **How do you prevent an agent from looping forever?** Step, token and time budgets; duplicate-call detection; error classification; and a forced final answer when limits hit.
- **Agent or workflow?** Workflow when steps are known; agent when control flow depends on intermediate results. Workflows are cheaper, faster and easier to test.
- **How do you add human approval to an agent?** Persist run state, pause on gated tool calls, show the exact action, resume with the stored call on approval or feed the rejection back.
