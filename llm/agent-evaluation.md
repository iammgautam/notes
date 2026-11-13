---
title: Evaluating agents
section: llm
order: 30
phase: "Phase 5 · Evaluation"
summary: Grade agents on outcomes and trajectories in sandboxed environments, measure tool-call accuracy, cost and pass^k reliability, and keep a regression suite.
---

An agent is a loop that chooses its own actions, so evaluating it is harder than grading a single response. Many different paths can be correct, actions have side effects, a run can take dozens of model calls, and the same task can succeed on one try and fail on the next. A useful agent eval answers four questions: did the task get done (outcome), did it get done the right way (trajectory), what did it cost (efficiency), and does it get done every time (reliability). This note assumes the general toolkit from [Evaluating LLM systems]({{ '/llm/llm-evaluation/' | relative_url }}) and the loop design from [Agents]({{ '/llm/agents/' | relative_url }}).

## Core ideas

```text
 task + initial state
        │
        ▼
 ┌──────────────┐  tool call    ┌──────────────────┐
 │    agent     │ ────────────► │ sandboxed tools  │
 │ (model+loop) │ ◄──────────── │ + simulated user │
 └──────┬───────┘  observation  └──────────────────┘
        │ trace: every call, argument, result, token count
        ▼
 ┌───────────────┬──────────────────┬───────────────────┐
 │ outcome:      │ trajectory:      │ efficiency:       │
 │ final state   │ right tools,     │ steps, tokens,    │
 │ == expected?  │ args, order,     │ latency, $ per    │
 │               │ nothing banned   │ successful task   │
 └───────────────┴──────────────────┴───────────────────┘
```

### Final outcome vs trajectory

| | Final-outcome evaluation | Trajectory evaluation |
|---|---|---|
| Grades | The end state: database rows, files, tests passing, the answer | The path: tool choices, arguments, order, step count |
| Strength | Accepts any valid path; matches what users care about | Explains failures; catches unsafe or wasteful runs that still succeed |
| Weakness | Says nothing about why; rewards lucky or unsafe successes | Over-specified references penalize valid alternatives |
| Use for | The primary success metric | Diagnostics, safety constraints, efficiency, tool-use regressions |

Grade the outcome by **checking state, not the agent's claims**. "Your refund has been processed" means nothing; a refund row with the right amount in the sandbox database does. Benchmarks such as τ-bench grade customer-service agents by comparing the final database state with the expected one, and SWE-bench grades coding agents by running tests the agent never saw.

Trajectory grading comes in several strictness levels:

| Style | Rule | When |
|---|---|---|
| Exact match | Same calls, same order, nothing extra | Short, fully determined flows (rarely right) |
| In-order subsequence | Required calls appear in order; extra calls allowed | Most tool-use regression tests |
| Unordered set | Required calls appear in any order | Independent lookups |
| Constraints | "X before Y", "never Z", "at most N calls of W" | Safety and policy rules |
| Judge over the trace | LLM grades the trace against a rubric | Open-ended steps: did it ask a clarifying question when the request was ambiguous? |

## What to measure

| Metric | Definition | Notes |
|---|---|---|
| Task success rate | Share of trials whose final state passes the checker | Primary metric; report per task category |
| Tool selection | Precision and recall of the right tools, including "no tool" when none is needed | Drops when you add similar tools |
| Argument accuracy | Required calls with correct arguments | Exact for ids, normalized for dates and amounts, judged for free text such as search queries |
| Argument grounding | Every id in an argument appeared in the user message or an earlier tool result | Catches hallucinated ids |
| Policy violations | Forbidden tools, destructive actions without confirmation, skipped verification | Any violation fails the trial |
| Steps and tokens | Model calls and tokens per trial | Loops and dithering show up here first |
| Cost per successful task | Total cost divided by successes | Cheap-but-failing agents look good on cost per run |
| Latency | Wall time per trial, p50 and p95 | Includes tool latency |
| Recovery | Success rate when a tool returns an error or times out | Inject faults deliberately |

Report cost per *successful* task. An agent that costs $0.10 per run and succeeds 50% of the time costs $0.20 per success, worse than one at $0.15 per run with 90% success ($0.17).

## Environments

Agents act, so their evals need a world to act in. Options, from cheap to faithful:

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| Mocked tools | Functions return canned results keyed by arguments | Fast, deterministic, free | Drifts from real APIs; agent cannot explore |
| Stateful simulation | Real tool code against a fixture database or in-memory service | Real side effects; final-state checks | Build and upkeep cost |
| Record and replay | Record real tool responses once, replay them | Real data, deterministic | Breaks when the agent takes a new path |
| Sandbox | Real tools in an isolated container or VM: shell, filesystem, browser, test DB | Highest fidelity for coding and computer use | Slow startup, cost, needs reset per trial |
| Staging services | Real APIs in a test account | Catches integration bugs | Flaky and slow; real side effects |

Sandbox requirements for anything that executes code or touches state:

- **Fresh state per trial:** restore a snapshot or rebuild the container. State leaking between trials makes results order-dependent.
- **No network by default**, or an egress allowlist; no production secrets mounted.
- **Resource limits and timeouts** on CPU, memory, wall time and steps.
- **Determinism where possible:** fixed clock, seeded data, pinned dependencies.
- **Hidden graders:** tests and checkers the agent cannot read or modify. Coding agents under pressure will edit tests or special-case expected outputs, so also verify which files the diff touches.

See [Coding and computer-use agents]({{ '/llm/coding-and-computer-use-agents/' | relative_url }}) for sandbox design in more depth.

### Simulated users

Many agents are conversational: they need information the user has not given yet. A **simulated user** is an LLM playing the customer, with a goal, private facts it reveals only when asked, and a persona. τ-bench uses this design. The simulator needs its own validation: read transcripts and check that it stays in character, does not dump every fact in the first message, does not give up early and is not unrealistically cooperative.

```python
# Illustrative: `agent.respond` and `llm.complete` stand in for your own clients.
SIMULATOR = """You are a customer talking to a support agent.
Goal: get a refund for order A-1001, a blender that arrived broken.
Private facts (reveal only when asked): order id A-1001, email ana@example.com.
Style: brief, a little impatient. Never invent facts beyond these.
When the goal is achieved or clearly refused, reply with exactly: DONE"""


def flip_roles(history: list[dict]) -> list[dict]:
    swap = {"user": "assistant", "assistant": "user"}
    return [{"role": swap[m["role"]], "content": m["content"]} for m in history]


def simulate(agent, llm, opening: str, max_turns: int = 10) -> list[dict]:
    history, user_msg = [], opening
    for _ in range(max_turns):
        history.append({"role": "user", "content": user_msg})
        reply = agent.respond(history)               # executes tools in the sandbox
        history.append({"role": "assistant", "content": reply})
        user_msg = llm.complete(system=SIMULATOR, messages=flip_roles(history))
        if user_msg.strip() == "DONE":
            break
    return history
```

## Reliability: pass@k vs pass^k

A single trial per task hides the most important property of an agent in production: consistency. Two metrics answer opposite questions:

- **pass@k:** the probability that *at least one* of `k` trials succeeds. Right for settings where you can generate several attempts and verify them cheaply, such as code with tests. Introduced with an unbiased estimator in the Codex paper: `pass@k = 1 - C(n - c, k) / C(n, k)` for `n` trials with `c` successes.
- **pass^k:** the probability that *all* `k` trials succeed. Right for agents that face customers, where each conversation is one shot and a 1-in-5 failure is a support ticket. Introduced by τ-bench; estimated as `C(c, k) / C(n, k)`.

If each trial succeeds independently with probability `p`, `pass@k = 1 - (1 - p)^k` rises toward 1 while `pass^k = p^k` falls fast. An agent at `p = 0.8` has `pass^8 ≈ 0.17`: it handles a given task correctly eight times in a row only one time in six. Run each task several times and report both, averaged over tasks.

```python
from math import comb


def pass_at_k(n: int, c: int, k: int) -> float:
    """P(at least one of k trials succeeds), unbiased from n trials with c successes."""
    assert 0 <= c <= n and 1 <= k <= n
    return 1.0 if n - c < k else 1 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """P(all k trials succeed): reliability."""
    assert 0 <= c <= n and 1 <= k <= n
    return comb(c, k) / comb(n, k)


def suite(tasks: list[tuple[int, int]], k: int) -> tuple[float, float]:
    at = sum(pass_at_k(n, c, k) for n, c in tasks) / len(tasks)
    hat = sum(pass_hat_k(n, c, k) for n, c in tasks) / len(tasks)
    return at, hat


if __name__ == "__main__":
    assert pass_at_k(10, 8, 1) == pass_hat_k(10, 8, 1) == 0.8
    assert pass_at_k(10, 8, 3) == 1.0                       # only 2 failures exist
    assert abs(pass_hat_k(10, 8, 3) - 56 / 120) < 1e-12     # about 0.47
    assert abs(0.8**8 - 0.1678) < 1e-4

    tasks = [(10, 10), (10, 9), (10, 5), (10, 0)]           # (trials, successes) per task
    at1, hat1 = suite(tasks, 1)
    at4, hat4 = suite(tasks, 4)
    assert abs(at1 - 0.6) < 1e-12 and abs(hat1 - 0.6) < 1e-12
    assert abs(at4 - (1 + 1 + (1 - 5 / 210) + 0) / 4) < 1e-12        # about 0.74
    assert abs(hat4 - (1 + 126 / 210 + 5 / 210 + 0) / 4) < 1e-12     # about 0.41
    print(f"pass@1={at1:.2f}  pass@4={at4:.2f}  pass^4={hat4:.2f}")
```

The same suite reads as 60%, 74% or 41% depending on the question. For a customer-facing agent, pass^k is the honest number.

## Trajectory grading

A grader that checks required calls as an in-order subsequence, with argument matchers, forbidden tools, a step budget and an outcome check from the sandbox:

```python
from dataclasses import dataclass, field
from typing import Any

ANY = object()  # matcher: argument must be present, any value


@dataclass
class Call:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


def value_ok(want: Any, got: Any) -> bool:
    return want is ANY or (want(got) if callable(want) else want == got)


def args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(k in actual and value_ok(w, actual[k]) for k, w in expected.items())


def grade_trajectory(calls: list[Call], expected: list[Call], *, final_state_ok: bool,
                     forbidden: frozenset[str] = frozenset(), max_steps: int = 8) -> dict:
    used: set[int] = set()
    positions, tool_hits = [], 0
    for exp in expected:
        same_tool = [i for i, c in enumerate(calls) if c.tool == exp.tool and i not in used]
        tool_hits += bool(same_tool)
        hit = next((i for i in same_tool if args_match(exp.args, calls[i].args)), None)
        if hit is not None:
            used.add(hit)
            positions.append(hit)
    n = len(expected) or 1
    r = {
        "tool_recall": tool_hits / n,
        "arg_accuracy": len(positions) / n,
        "in_order": positions == sorted(positions),
        "extra_calls": len(calls) - len(used),
        "over_budget": len(calls) > max_steps,
        "violations": [c.tool for c in calls if c.tool in forbidden],
    }
    r["passed"] = (final_state_ok and not r["violations"] and r["in_order"]
                   and len(positions) == len(expected) and not r["over_budget"])
    return r


if __name__ == "__main__":
    expected = [
        Call("lookup_order", {"order_id": "A-1001"}),
        Call("issue_refund", {"order_id": "A-1001", "amount": lambda x: 0 < x <= 49.99}),
    ]
    banned = frozenset({"delete_account"})
    lookup = Call("lookup_order", {"order_id": "A-1001"})
    refund = Call("issue_refund", {"order_id": "A-1001", "amount": 49.99})

    good = grade_trajectory([Call("search_kb", {"q": "refund"}), lookup, refund], expected,
                            final_state_ok=True, forbidden=banned)
    assert good["passed"] and good["extra_calls"] == 1

    swapped = grade_trajectory([refund, lookup], expected, final_state_ok=True)
    assert not swapped["passed"] and not swapped["in_order"]       # refunded before checking

    overpaid = grade_trajectory([lookup, Call("issue_refund", {"order_id": "A-1001",
                                 "amount": 59.99})], expected, final_state_ok=False)
    assert overpaid["tool_recall"] == 1.0 and overpaid["arg_accuracy"] == 0.5

    claimed = grade_trajectory([lookup, refund], expected, final_state_ok=False)
    assert not claimed["passed"]                                   # said done, state unchanged

    rogue = grade_trajectory([lookup, refund, Call("delete_account")], expected,
                             final_state_ok=True, forbidden=banned)
    assert rogue["violations"] == ["delete_account"] and not rogue["passed"]

    looping = grade_trajectory([lookup] * 9 + [refund], expected, final_state_ok=True)
    assert looping["over_budget"] and not looping["passed"]
    print("trajectory grader ok")
```

`final_state_ok` comes from the environment, for example `db.refunds_for("A-1001") == [49.99]`. Keep the expected trajectory minimal: list only the calls that must happen and the arguments that matter, and let constraints and the outcome check do the rest. A reference that pins every call breaks the first time the model finds a shorter valid path.

## Evaluating multi-turn agents

- **Conversation level:** goal completion (final state), turns to completion, policy adherence across the whole conversation (identity verified before the refund), and a rubric judge over the transcript for things like "asked only for information it did not already have".
- **Turn level:** replay a fixed transcript prefix and grade only the next turn. This is cheap, close to deterministic, and good for regressions such as "after the user corrects the order id, the agent uses the new one".
- **Consistency:** the agent does not contradict earlier turns or re-ask for facts already given; long conversations stress [agent memory]({{ '/llm/agent-memory/' | relative_url }}) and context limits.
- **Handoffs:** when it should escalate to a human, does it, with a usable summary?

Run multi-turn tasks several times with the simulator at a nonzero temperature. Variation in user behavior is part of what you are testing, and pass^k across simulated conversations is the closest offline proxy for production reliability.

## Regression suites

A regression suite is a frozen set of tasks, each with an instruction, an initial environment snapshot, a success checker, optional trajectory constraints and tags.

- **Run each task 3–5 times** and report success rate, pass^k, mean steps, cost per success and p95 latency, per tag.
- **Diff per task against the baseline:** newly failing tasks first, then tasks whose cost or step count jumped.
- **Treat flaky tasks as signal.** If a task passes 3 of 5 on main, the agent is unreliable there or the environment is nondeterministic. Fix one of them; do not just rerun until green.
- **Trigger on** prompt or tool-schema changes, new tools (tool selection degrades as similar tools accumulate), model version changes and framework upgrades.
- **Budget it.** 100 tasks × 5 trials × 12 steps × 8k input tokens per step is about 48M input tokens per run. Prompt caching of the stable prefix and a smaller smoke subset per PR keep this affordable; run the full suite nightly.

When success drops after a model upgrade, compare trajectories for the newly failing tasks side by side. The cause is usually visible in the first divergent step: a different tool choice, a malformed argument, or the model stopping early with a claim of success.

## Public agent benchmarks

| Benchmark | Tasks | Graded by |
|---|---|---|
| SWE-bench (Verified) | Resolve real GitHub issues in Python repos | Hidden repo tests |
| τ-bench | Retail and airline customer service with a simulated user and policies | Final database state; reports pass^k |
| WebArena | Tasks on self-hosted websites (shopping, forums, code hosting) | Functional checks of site state |
| OSWorld | Desktop tasks across real applications in VMs | Execution-based checkers |
| GAIA | Assistant questions needing browsing and tools | Short exact answers |
| BFCL | Function-calling accuracy across many tool schemas | Argument matching and execution |

Scores depend heavily on the harness (prompts, tools, step limits, retries), so a model's number on one harness does not transfer to another, and none of these is your workload.

## Pitfalls

- **Trusting the agent's final message.** Agents report success they did not achieve. Fix: check environment state.
- **Over-specified trajectories.** Valid shorter paths fail. Fix: required calls plus constraints, not full reference paths.
- **One trial per task.** A 0.8 success rate looks like reliability. Fix: multiple trials; report pass^k.
- **Leaky environments.** State from one trial changes the next. Fix: snapshot restore per trial.
- **Reward hacking.** Edited tests, hardcoded outputs, checkers the agent can read. Fix: hidden graders and diff scope checks.
- **Ignoring cost.** A change that raises success 2 points and doubles steps. Fix: cost per success and step counts in the gate.
- **Unvalidated simulators.** A too-helpful simulated user inflates scores. Fix: read transcripts and calibrate against real conversations.

## Checklist

- Every task has a state-based success checker the agent cannot see.
- Trajectory checks cover required calls, argument matchers, grounding, forbidden tools and a step budget.
- Sandboxes reset per trial, without network or production secrets.
- Each task runs 3–5 times; report success, pass^k, steps, cost per success and latency.
- Faults (tool errors, timeouts) are injected in some tasks.
- Simulated users are validated by reading transcripts.
- The suite runs on every prompt, tool, model and framework change, with per-task diffs.

## Interview questions

**How do you evaluate an agent?** Build tasks with an initial environment state and a state-based success checker, run them in a sandbox several times each, and report success rate, pass^k, tool-call accuracy, policy violations, steps and cost per successful task. Trajectory checks explain failures; outcome checks define success.

**Final-outcome or trajectory evaluation?** Outcome as the primary metric, because many paths are valid. Trajectory checks for diagnostics and for constraints that matter even when the outcome is right, such as verifying identity before a refund or never calling a destructive tool.

**pass@k vs pass^k?** pass@k is the chance at least one of `k` attempts succeeds, right when you can verify and pick among attempts. pass^k is the chance all `k` succeed, right for customer-facing agents where each run counts. At 80% per-trial success, pass^8 is about 17%.

**How would you test a support agent that issues refunds?** A stateful simulation with a fixture database, a simulated customer with private facts, and checkers for the refund row and amount. Add trajectory constraints (lookup before refund, amount within the order total) and a forbidden list. Run each scenario several times.

**Success dropped after a model upgrade. How do you debug it?** Diff per task, then compare old and new trajectories for newly failing tasks and find the first divergent step: tool choice, argument format, early stopping, or changed handling of tool errors.

**How do you evaluate multi-turn behavior without humans?** Simulated users with goals and private facts for conversation-level outcomes, plus turn-level replays of fixed transcript prefixes for cheap regressions. Validate the simulator by reading transcripts.

## Further reading

- Yao et al., "τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains" (2024).
- Jimenez et al., "SWE-bench: Can Language Models Resolve Real-World GitHub Issues?" (2023).
- Zhou et al., "WebArena: A Realistic Web Environment for Building Autonomous Agents" (2023).
- Mialon et al., "GAIA: A Benchmark for General AI Assistants" (2023).
- Chen et al., "Evaluating Large Language Models Trained on Code" (2021), for the pass@k estimator.
- Liu et al., "AgentBench: Evaluating LLMs as Agents" (2023).
