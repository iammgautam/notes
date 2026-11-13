---
title: Agent architectures
section: llm
order: 23
phase: "Phase 4 · Agents"
summary: ReAct, plan-and-execute, reflection, tree search, routers, orchestrator-workers, multi-agent patterns and state graphs, and when each is worth its cost.
---

An agent architecture is how you arrange model calls, tools and control flow. The named patterns (ReAct, plan-and-execute, Reflexion, supervisor, swarm) sound like different systems, but almost all are compositions of three primitives: an LLM call with tools, a loop around it, and a graph of steps that decides what runs next. The real design question is how much control flow you hand to the model. Every step toward model control buys flexibility and costs predictability, tokens and debuggability, so start at the simple end and move only when evals show you need to.

## Core ideas

```text
  code decides the path                          model decides the path
  ◄──────────────────────────────────────────────────────────────────►
  single     chain /    plan-and-   ReAct     orchestrator   multi-
  call       router     execute     loop      + workers      agent
  ──────────────────────────────────────────────────────────────────
  cheap, predictable, testable        flexible, costly, harder to debug
```

Three questions place any design on this line:

1. **Who chooses the next step?** Your code (workflow), a plan the model wrote up front, or the model at every step.
2. **How many contexts are there?** One growing context, or several isolated ones (sub-agents) whose results get merged.
3. **Where does feedback come from?** Tool results, a critic model, tests and validators, or a human.

[Agent loops and failure modes]({{ '/llm/agents/' | relative_url }}) covers the loop itself: budgets, stop conditions, recovery. This note covers the shapes you build from it.

## Single-agent patterns

### ReAct

ReAct (reason + act) interleaves a reasoning step, an action and an observation, repeated until the model answers. The original paper used text prompts (`Thought:`, `Action:`, `Observation:`); today native tool calling and reasoning models implement the same loop, and it is the default shape of every agent framework.

- **Strengths:** adapts to whatever each observation reveals; simple to build.
- **Weaknesses:** myopic (one step at a time, no global plan), one sequential model call per action, a context that grows every step, prone to loops and drift.
- **Use when:** the toolset is small and the number of steps is unknown but modest.

### Plan-and-execute

A planner call writes an explicit list of steps. An executor runs them, often with a cheaper model or plain code. A re-planner revisits the plan when a step fails or reveals something new.

- **ReWOO** plans all tool calls up front with placeholders for results (`#E1`, `#E2`), executes them, then calls a solver once, so the big model never re-reads intermediate observations.
- **LLMCompiler** goes further and emits a dependency graph so independent calls run in parallel.

Strengths: fewer calls to the expensive model, a plan a user can approve, parallelism, and goal drift is easier to spot. Weakness: plans go stale. A plan written before seeing data is a guess, so you need a re-plan trigger (step failed, result contradicts an assumption, plan exhausted without an answer).

### Reflection and self-critique

Generate, critique, revise. **Self-Refine** uses the same model as generator and critic in a loop. **Reflexion** adds memory across attempts: after a failed trial (a failing test, a wrong answer from an environment), the agent writes a verbal reflection on why it failed and the next attempt reads it.

The deciding factor is the feedback source. Reflection works well when the critique is grounded in an external signal: unit tests, a schema validator, a SQL `EXPLAIN`, a fact check against retrieved sources. Pure self-critique of reasoning without new information gains little and can talk a model out of a correct answer (Huang et al., 2023, "Large Language Models Cannot Self-Correct Reasoning Yet"). Cap iterations at two or three; quality gains flatten quickly while cost grows linearly. Anthropic calls the production form of this the evaluator-optimizer workflow.

### Tree of thoughts and search

Tree of Thoughts treats reasoning as search: generate several candidate next steps, score each state with the model or a heuristic, and expand the promising ones with BFS, DFS or beam search. LATS combines this with Monte Carlo tree search and environment feedback.

These multiply calls by 10–100x. They pay off on problems with verifiable intermediate states (puzzles, planning, code that can be executed) and rarely in request-response products. Reasoning models now perform much of this exploration internally at a lower cost per answer; see [Reasoning models]({{ '/llm/reasoning-models/' | relative_url }}). Cheap cousins are often enough: sample N answers and vote (self-consistency), or generate N candidates and pick with a verifier.

## Routing and orchestration

### Router

One classification call (or a small classifier model) picks a path: a specialized prompt, toolset, model or sub-agent. It is the cheapest form of specialization: refunds go to a billing prompt with billing tools, everything else to general support. Routers also do model routing, sending easy requests to a small model ([Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})). Use structured output for the decision and log it, since a misroute is a common silent failure.

### Orchestrator-workers

An orchestrator model decomposes a task at runtime into subtasks, dispatches each to a worker (an LLM call or a full sub-agent with its own tools and context), then synthesizes the results. It differs from plan-and-execute in that the subtasks are not known in advance and each worker is itself a model with a fresh context window.

```text
                      ┌──────────────┐
          task ──────►│ orchestrator │◄─────── results (condensed)
                      └──────┬───────┘
             ┌───────────────┼───────────────┐
             ▼               ▼               ▼
        ┌─────────┐     ┌─────────┐     ┌─────────┐
        │ worker A│     │ worker B│     │ worker C│   parallel,
        │ own ctx │     │ own ctx │     │ own ctx │   isolated
        │ + tools │     │ + tools │     │ + tools │   contexts
        └─────────┘     └─────────┘     └─────────┘
```

The key benefit is context isolation: each worker can read 100k tokens of sources and return a 1k-token summary, so the orchestrator's context stays small. Research across many sources is the canonical fit.

## Multi-agent patterns

- **Supervisor.** A coordinating agent calls specialist agents as tools and decides what to do with their outputs. Control always returns to the supervisor. Easy to reason about, and the supervisor is a single point for budgets and logging.
- **Hierarchical.** Supervisors of supervisors: a top agent delegates to team leads, which delegate to workers. Only for very broad domains; each layer adds latency, cost and a place for context to get lost.
- **Handoffs (swarm).** Agents transfer control to each other along with the conversation: triage hands off to billing, billing to refunds. OpenAI's experimental Swarm popularized this and the OpenAI Agents SDK has handoffs as a primitive. Low overhead, natural for customer support, but watch for ping-pong between agents and for the receiving agent missing context the previous one had.
- **Debate and voting.** Several agents answer independently, then critique each other over a few rounds or vote (Du et al., 2023). Improves factuality and reasoning on some benchmarks at N times the cost. Diversity matters: copies of the same model with the same prompt tend to agree on the same mistakes.

Agent-as-tool versus handoff is the practical distinction. As a tool, the sub-agent returns a result and the caller stays in charge. With a handoff, the next agent owns the conversation. Use tools when one agent must keep the global view; use handoffs when specialists talk to the user directly.

## State machines and graphs

Production agents increasingly look like explicit graphs: named nodes (an LLM call, a tool, plain code, a human gate), a typed shared state, and edges that are either fixed or chosen by a function of the state. Cycles are allowed, so a ReAct loop or a reflection loop is just a cycle with a guard. LangGraph is the best-known implementation. Illustrative, with signatures that may vary by version:

```python
import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

class State(TypedDict):
    question: str
    plan: list[str]
    results: Annotated[list[str], operator.add]  # reducer: updates append
    draft: str
    critique: str | None
    revisions: int

g = StateGraph(State)
for name, fn in [("plan", plan), ("execute", execute),
                 ("write", write), ("critique", critique)]:
    g.add_node(name, fn)  # fn(state) -> dict of updates
g.add_edge(START, "plan")
g.add_edge("plan", "execute")
g.add_conditional_edges("execute", lambda s: "execute"
                        if len(s["results"]) < len(s["plan"]) else "write")
g.add_edge("write", "critique")
g.add_conditional_edges("critique", lambda s: END
                        if s["critique"] is None or s["revisions"] >= 2 else "write")
app = g.compile(checkpointer=MemorySaver())  # Postgres saver in production
app.invoke({"question": "..."},
           {"configurable": {"thread_id": "run-42"}, "recursion_limit": 25})
```

What the graph buys you over a free-form loop: every path is visible and testable node by node; deterministic steps stay in code; a checkpointer snapshots state after each step, giving you resume after a crash, human-in-the-loop interrupts and replay; and the recursion limit bounds every cycle. It is the natural way to build a workflow with a few agentic nodes, which is what most production systems turn out to be.

## When multi-agent is worth the cost

Multi-agent systems are expensive. Anthropic reported that its multi-agent research system used roughly 15 times the tokens of a chat interaction, and that this paid off on broad research queries that split into independent threads. The same costs appear everywhere:

| Benefit | When it is real |
|---|---|
| Context isolation | Subtasks read far more than they return |
| Parallel breadth | Subtasks are independent (search five sources) |
| Specialization | Domains need different prompts, tools or models |
| Permission separation | One agent reads untrusted content, another holds write tools |

| Cost | Symptom |
|---|---|
| Tokens and latency | Each agent re-reads instructions and context |
| Lost context | A worker lacks a decision the orchestrator made; conflicting outputs |
| Coordination failures | Duplicated work, agents waiting on each other, ping-pong handoffs |
| Debuggability | Failures spread across many traces; evals must score whole trajectories |

A useful test: is the task read-heavy and parallelizable, or write-heavy and tightly coupled? Research, due diligence and broad search split well. Writing one coherent piece of code or one document does not: parallel writers make incompatible decisions, which is the argument in Cognition's "Don't Build Multi-Agents" post. A study of multi-agent failures (Cemri et al., 2025) groups them into specification and system-design issues, inter-agent misalignment, and weak task verification: mostly design problems, not raw model capability. Try a single agent with better tools and context first.

## Comparison

| Pattern | Who controls flow | Relative cost | Best for | Main risk |
|---|---|---|---|---|
| Chain or router | Code | 1–3 calls | Known steps, triage | Rigid, misroutes |
| ReAct | Model, every step | 3–20 calls, sequential | Unknown steps, small toolset | Loops, drift, bloat |
| Plan-and-execute | Model plans, code runs | 1 plan + cheap steps | Decomposable tasks, approvals | Stale plans |
| Reflection | Code loop | 2–3x base | Checkable outputs | Rationalizing without feedback |
| Tree search | Code search | 10–100x | Verifiable search problems | Cost |
| Orchestrator-workers | Model | 1 + N workers + merge | Broad, parallel research | Tokens, synthesis quality |
| Supervisor, hierarchy | Model | Many calls | Distinct domains and tools | Coordination, lost context |
| Handoffs | Model | Low overhead | Support triage | Ping-pong, missing context |
| Debate or voting | Code | N x rounds | High-stakes answers | Cost, correlated errors |

## In practice: a minimal graph runner

Runnable, stdlib only. A tiny LangGraph-style runner: nodes return partial state updates, edges are fixed or computed from state, a step limit bounds every cycle, and a snapshot after each node allows resume. The graph combines a router, plan-and-execute and a capped reflection loop; deterministic functions stand in for model calls.

```python
import json

END = "__end__"


class Graph:
    def __init__(self):
        self.nodes, self.edges = {}, {}

    def add(self, name, fn, then):  # then: node name, END, or fn(state) -> name
        self.nodes[name], self.edges[name] = fn, then
        return self

    def run(self, node, state, max_steps=20, checkpoints=None):
        for _ in range(max_steps):
            state = {**state, **self.nodes[node](state), "trace": state.get("trace", []) + [node]}
            nxt = self.edges[node]
            node = nxt(state) if callable(nxt) else nxt
            if checkpoints is not None:
                checkpoints.append((node, json.dumps(state)))  # a DB row in production
            if node == END:
                return state
        raise RuntimeError(f"step limit {max_steps} hit before {node!r}")


# Stand-ins for model calls (router, planner, executor, writer, critic).
def route(s):
    return {"route": "simple" if len(s["question"].split()) < 6 else "complex"}

def answer(s):
    return {"draft": f"Direct answer: {s['question']}"}

def plan(s):
    return {"plan": ["refund policy", "invoice age", "compare"], "results": []}

def execute(s):  # one plan step per visit: a tool call or a cheap model
    return {"results": s["results"] + [f"found: {s['plan'][len(s['results'])]}"]}

def write(s):
    cited = " with citations" if s.get("critique") else ""
    return {"draft": f"{len(s['results'])} findings{cited}", "revisions": s.get("revisions", -1) + 1}

def critique(s):  # best as a deterministic check; an LLM judge otherwise
    return {"critique": None if "citations" in s["draft"] else "add citations"}


def build(critic=critique):
    return (Graph()
            .add("route", route, lambda s: "answer" if s["route"] == "simple" else "plan")
            .add("answer", answer, END)
            .add("plan", plan, "execute")
            .add("execute", execute,
                 lambda s: "execute" if len(s["results"]) < len(s["plan"]) else "write")
            .add("write", write, "critique")
            .add("critique", critic,
                 lambda s: END if s["critique"] is None or s["revisions"] >= 2 else "write"))


if __name__ == "__main__":
    g = build()
    assert g.run("route", {"question": "Refund window?"})["trace"] == ["route", "answer"]

    cps = []
    out = g.run("route", {"question": "Can I still get a refund for invoice INV-7?"}, checkpoints=cps)
    assert out["trace"] == ["route", "plan", "execute", "execute", "execute",
                            "write", "critique", "write", "critique"]
    assert out["draft"] == "3 findings with citations" and out["revisions"] == 1

    # resume from the snapshot taken after "plan" (e.g. after a crash): same result
    node, saved = cps[1]
    assert node == "execute" and g.run(node, json.loads(saved)) == out

    # a critic that is never satisfied: the revision cap ends the reflection loop
    stubborn = build(critic=lambda s: {"critique": "still not good"})
    out = stubborn.run("route", {"question": "Can I still get a refund for invoice INV-7?"})
    assert out["trace"].count("write") == 3

    # a cycle with no exit: the step limit is the last line of defense
    loop = Graph().add("a", lambda s: {}, "b").add("b", lambda s: {}, "a")
    try:
        loop.run("a", {}, max_steps=6)
        raise AssertionError("expected step limit")
    except RuntimeError as e:
        assert "step limit 6" in str(e)
    print("graph runner demo: ok")
```

## Pitfalls

- **Architecture before evals.** Symptom: a five-agent system nobody can say is better than one prompt. Fix: build an eval set first ([Agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }})) and add complexity only when a simpler baseline measurably fails.
- **Uncapped reflection.** Symptom: cost per request triples with no quality change. Fix: two or three rounds, external feedback, stop when the critic passes.
- **Lossy handoffs between agents.** Symptom: a sub-agent redoes work or contradicts an earlier decision. Fix: pass a structured brief (goal, constraints, decisions so far, expected output format), not "see above".
- **Workers returning raw dumps.** Symptom: the orchestrator's context explodes, defeating isolation. Fix: workers return condensed, structured results with references.
- **Untraceable multi-agent runs.** Symptom: a failure cannot be attributed to an agent. Fix: one trace id per run, spans per agent and tool call ([Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).
- **Framework lock-in for a loop.** A ReAct agent is about 50 lines of code. Adopt a framework for checkpointing, interrupts and graph tooling, not for the loop ([Agent frameworks]({{ '/llm/agent-frameworks/' | relative_url }})).

## Rules of thumb

- Start with one call, then a workflow, then one agent. Go multi-agent only for parallel, read-heavy breadth.
- Keep deterministic steps in code and model nodes for judgment calls.
- Reflection needs a real signal: tests, validators, retrieved evidence.
- Bound every cycle: step limits, revision caps, recursion limits.
- One agent in charge of the global view; sub-agents get explicit briefs and return compact results.

## Interview questions

**ReAct or plan-and-execute?** ReAct when steps depend heavily on each observation and the task is short. Plan-and-execute when the task decomposes up front, you want fewer expensive calls, parallel execution, or a plan users can approve, with re-planning when steps fail.

**When does self-reflection help?** When the critique has new information: test failures, validator errors, retrieved evidence. Without it, a model critiquing its own reasoning often gains little and can talk itself out of correct answers. Cap the rounds.

**When is a multi-agent system worth it?** When subtasks are independent and read-heavy, so isolated contexts and parallelism help, or when domains need separate tools or permissions. Not for tightly coupled work like writing one codebase change, where parallel agents make conflicting decisions. Expect several times the tokens.

**Supervisor, handoff or agent-as-tool?** Agent-as-tool or supervisor when one agent must keep the global view and merge results. Handoffs when a specialist should own the conversation, as in support triage. Handoffs need full context transfer and loop protection.

**Why model an agent as a state graph?** Explicit paths you can test node by node, deterministic steps in code, bounded cycles, and checkpoints that enable resume, human interrupts and replay. It is the practical way to mix workflow and agent behavior.

**Design a support assistant for billing, shipping and technical issues.** A router (or triage agent) classifies intent and hands off to specialists, each with a small toolset and its own prompt. Billing writes go through confirmation gates. Shared state carries customer identity and conversation summary. Unclear or repeated-failure cases escalate to a human. Evaluate routing accuracy and per-specialist task success separately.

**Is tree of thoughts used in production?** Rarely as such: it multiplies calls by 10–100x. Best-of-N with a verifier, self-consistency voting, or a reasoning model usually gets most of the benefit at a fraction of the cost.

## Further reading

- ReAct: Synergizing Reasoning and Acting in Language Models (Yao et al., 2022).
- Reflexion: Language Agents with Verbal Reinforcement Learning (Shinn et al., 2023).
- Tree of Thoughts: Deliberate Problem Solving with Large Language Models (Yao et al., 2023).
- Improving Factuality and Reasoning in Language Models through Multiagent Debate (Du et al., 2023).
- Building effective agents (Anthropic, 2024) and How we built our multi-agent research system (Anthropic, 2025).
- Why Do Multi-Agent LLM Systems Fail? (Cemri et al., 2025).
