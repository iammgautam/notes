---
title: Agent memory and state
section: llm
order: 24
phase: "Phase 4 · Agents"
summary: Short-term and long-term agent memory, write policies, checkpointing long-running agents, tenant isolation and privacy, with a runnable memory store.
---

Models are stateless. Everything an agent "remembers" is something your code chose to put back into the context window on the next call. Memory engineering is therefore two problems: what to store, and what to load into a limited, expensive context each turn. Get it wrong one way and the agent forgets the user's name between sessions; get it wrong the other way and it carries a stale preference forever, leaks one customer's facts into another's conversation, or replays a refund after a crash. Treat memory as a data system with schemas, write policies, retention and access control, not as a vector store you append to.

## Core ideas

### Kinds of memory

The CoALA framework (Sumers et al., 2023) borrows a useful vocabulary from cognitive science:

| Kind | What it holds | Example | Typical store |
|---|---|---|---|
| Working (short-term) | The current conversation and run | Last turns, tool results, plan | The context window, run checkpoint |
| Semantic | Facts about the user or world | "Prefers Python", "plan: Enterprise" | Rows keyed by slot |
| Episodic | Past experiences | "Last month we fixed a Celery retry storm by..." | Embedded summaries with metadata |
| Procedural | How to do things | Rules, learned instructions, few-shot examples | Prompts, config, versioned in git |

Separately from memory, **run state** is the in-flight state of one agent run (messages, step count, budget used, pending approval). It needs durability, not recall.

```text
          ┌──────────── context window, rebuilt every call ───────────┐
          │ procedural rules │ profile facts │ recalled episodes      │
          │ summary of older turns │ recent turns verbatim            │
          └───────▲──────────────────▲──────────────────▲─────────────┘
                  │ load             │ load by key      │ retrieve top-k
  ┌───────────────┴──┐   ┌───────────┴────────┐   ┌─────┴──────────────┐
  │ run checkpoints  │   │ facts (Postgres)   │   │ episodes (pgvector)│
  └───────▲──────────┘   └───────────▲────────┘   └─────▲──────────────┘
          │ every step               │ write policy: extract, dedup,
          └────────── agent loop ────┴─────── resolve, expire ┘
```

## Short-term memory

The conversation buffer works until the context gets long, slow and expensive. Then you shape it:

| Strategy | How | Trade-off |
|---|---|---|
| Full buffer | Send everything | Simple; cost grows quadratically over a run |
| Trimming | Keep system prompt, the original task, and the last N tokens | Cheap; silently drops early facts |
| Running summary | When over a threshold, fold the oldest turns into a summary; keep recent turns verbatim | Keeps gist; one extra call; detail loss |
| Tool result clearing | Replace consumed tool results with stubs and an ID to re-fetch | Large savings in tool-heavy agents |
| Retrieval over history | Store all turns; retrieve relevant old ones per turn | Precise recall; more machinery |

Rules that prevent the classic bugs:

- **Budget in tokens, not messages.** One tool result can be larger than fifty chat turns.
- **Never separate a tool call from its result.** A trimmed history starting with an orphan tool result is a provider 400.
- **Pin what must survive:** the original goal, user constraints, IDs the agent is working on.
- **Summarize with a purpose-built prompt:** decisions made, facts learned, open questions, entity IDs. Generic "summarize this conversation" drops the details that matter.
- **Trigger early** (around 70–80% of your budget) and at turn boundaries, so a summarization call never lands mid-tool-call.
- **Keep originals** in the database. A summary of a summary drifts; the transcript is the source of truth.

Long contexts also degrade quality, not just cost: models use information at the start and end of a long prompt better than the middle (Liu et al., 2023). See [Context engineering]({{ '/llm/context-engineering/' | relative_url }}).

## Long-term memory

Long-term memory survives across sessions. It is read in three ways:

- **Always loaded.** A small profile (preferences, role, timezone) in every prompt. Keep it to a few hundred tokens and stable, so it sits in the cached prefix.
- **Retrieved per turn.** Embed the current message, fetch top-k episodes or notes, inject them with clear labels. This is RAG over the user's own history ([RAG architecture]({{ '/llm/rag-architecture/' | relative_url }})).
- **Memory as tools.** Give the agent `search_memory` and `save_memory` and let it decide, as in MemGPT's paging between context and external storage. Flexible, but recall now depends on the model remembering to look.

Procedural memory deserves the most caution. A learned rule ("always answer this customer in German") changes behavior on every future run. Store procedural changes as reviewable proposals, or limit them to per-user preferences.

## Vector memory vs structured memory

| | Structured (rows keyed by slot) | Vector (embedded text) |
|---|---|---|
| Best for | Preferences, settings, stable facts | Episodes, notes, fuzzy "like last time" recall |
| Update | Overwrite by key, keep history | Hard: find and merge near-duplicates |
| Delete | Exact | Needs IDs; derived summaries may still hold it |
| Precision | Exact lookup | Approximate; can surface stale or off-topic items |
| Conflicts | Visible (same key, two values) | Hidden (two contradictory chunks both retrieved) |

Most systems want both, and Postgres holds both in one table: a `key` column for slot facts, an `embedding` column for episodes, and metadata for scoping and expiry. Do not store facts that live in a system of record (order status, plan tier) as memory at all; fetch them live with a tool, or memory will contradict the database.

## Memory writing policies

Uncontrolled writes are how memory goes bad. Decide explicitly:

- **What to remember:** explicit requests ("remember that..."), stable preferences, durable facts about the user's context, outcomes of completed tasks. Not: transient chatter, data owned by other systems, secrets, sensitive categories without consent, or anything sourced from untrusted content such as a fetched web page.
- **When to write:** in the hot path (the agent calls `save_memory`: immediate, adds latency, depends on the model) or in the background (an extraction job after the conversation: consistent, batchable, delayed). Background extraction is the safer default.
- **How to extract:** an LLM call with [structured output]({{ '/llm/structured-outputs/' | relative_url }}) returning candidates with `key`, `value`, `kind`, `source` (stated or inferred), `confidence` and evidence (message ID).
- **Dedup:** exact key match for slot facts; embedding similarity above a tuned threshold for episodes, then merge.
- **Conflict resolution:** for each candidate decide add, update, delete or no-op. Newest wins per key, but keep the old value with a validity range; what the user stated beats what was inferred; the system of record beats memory; when stakes are high, ask the user.
- **Forgetting:** TTLs per kind (a "current project" fact expires, a language preference does not), recency decay in ranking, caps per user, and a purge job that actually deletes. Generative Agents ranked memories by recency, importance and relevance, a scheme still worth copying.

## State persistence and checkpointing

Long-running agents crash, get redeployed and wait hours for approvals. Persist run state after every step so any worker can resume.

```sql
CREATE TABLE agent_runs (
    id            uuid PRIMARY KEY,
    tenant_id     uuid NOT NULL,
    status        text NOT NULL,  -- running | awaiting_input | done | failed
    state         jsonb NOT NULL, -- messages, step, tokens, pending call
    state_version int  NOT NULL,  -- bump when the state shape changes
    lease_until   timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- A worker claims a runnable or abandoned run (expired lease = dead worker).
SELECT id FROM agent_runs
WHERE status = 'running' AND (lease_until IS NULL OR lease_until < now())
ORDER BY updated_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

The order of writes within a step decides what a crash can break:

1. Persist the model's response, including its tool calls and their IDs.
2. Execute each tool with an idempotency key derived from `run_id` and the call ID.
3. Persist the results and advance the step.

A crash between 2 and 3 means the resumed worker re-executes the calls; the idempotency key turns the replay into a no-op for side effects ([Idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }})). Renew the lease with a heartbeat during long steps, version the state format so a deploy can read old checkpoints, and keep per-step rows if you want replay and debugging. LangGraph's checkpointers (including a Postgres saver) implement the same idea keyed by `thread_id`.

## Multi-user and tenant isolation

A memory leak across users is a data breach, and agents make it easy: one shared index, one missing filter.

- **Namespace every read and write** by `(tenant_id, user_id)` taken from the session, never from the model.
- **Filter before ranking.** A vector search that takes the global top-k and then filters by tenant returns too few results for small tenants. With pgvector HNSW, the `WHERE` clause is applied to index candidates; use iterative scans (`hnsw.iterative_scan`, pgvector 0.8+), partial indexes or per-tenant partitions ([pgvector]({{ '/postgresql/pgvector/' | relative_url }})).
- **Row-level security as a backstop:**

```sql
ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_memory
    USING (tenant_id = current_setting('app.tenant_id')::uuid);
-- The app role must not own the table, or use FORCE ROW LEVEL SECURITY.
```

- **Shared memory** (team knowledge, org conventions) lives in its own namespace with explicit write permissions.
- **Test isolation** with an automated check that writes to tenant A and asserts tenant B cannot recall it.

## Privacy of memories

Memories are personal data, and often the most sensitive kind: inferred from conversations, never typed into a form.

- **Erasure must be complete.** Deleting a user means facts, episodes, embeddings, derived summaries, run checkpoints, caches, traces, and any eval datasets built from their conversations, with backups expiring on a known schedule. Keep a map of every store that derives from conversation data. See [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).
- **Transparency:** let users see, edit and delete what is remembered, and tell them when memory is updated.
- **Minimize:** redact secrets and identifiers before storage; do not save what you will not use.
- **Memory poisoning:** prompt injection that writes to memory becomes persistent. Never save memories derived from untrusted content, treat recalled memories as data rather than instructions, and require confirmation for anything that changes future behavior. See [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

## In practice: a memory store

Runnable, stdlib only: a short-term buffer with a summarization trigger that never orphans a tool result; a long-term fact store with tenant scoping, dedup, conflict resolution (stated beats inferred), TTLs and erasure; and crash-safe checkpointing with idempotent side effects. Keyword overlap stands in for embedding similarity, and a fake summarizer for the LLM call.

```python
import json
import os
import re
import tempfile
import time

WORD = re.compile(r"[a-z0-9]+")


def n_tokens(text):  # rough; use the model's tokenizer in production
    return len(text) // 4 + 1


class ShortTerm:
    """Recent turns verbatim plus a running summary of older turns."""

    def __init__(self, summarize, budget=120, keep_last=4):
        self.summarize, self.budget, self.keep_last = summarize, budget, keep_last
        self.summary, self.messages = "", []

    def tokens(self):
        return n_tokens(self.summary) + sum(n_tokens(m["content"]) for m in self.messages)

    def add(self, role, content):
        self.messages.append({"role": role, "content": content})
        if self.tokens() > self.budget and len(self.messages) > self.keep_last:
            cut = len(self.messages) - self.keep_last
            while cut > 0 and self.messages[cut]["role"] == "tool":
                cut -= 1  # keep each tool result with the call that produced it
            if cut:
                old, self.messages = self.messages[:cut], self.messages[cut:]
                self.summary = self.summarize(self.summary, old)


class LongTerm:
    """Slot facts scoped by (tenant, user): newest wins, stated beats inferred."""

    def __init__(self, clock=time.time):
        self.clock, self.rows = clock, {}

    def alive(self, row):
        return row["expires"] is None or row["expires"] > self.clock()

    def write(self, tenant, user, key, value, source="stated", ttl=None):
        k, now = (tenant, user, key), self.clock()
        old = self.rows.get(k)
        if old and self.alive(old):
            if old["value"].lower() == value.lower():
                return "noop"
            if old["source"] == "stated" and source == "inferred":
                return "kept"
        self.rows[k] = {"value": value, "source": source, "at": now,
                        "expires": now + ttl if ttl else None,
                        "history": old["history"] + [old["value"]] if old else []}
        return "updated" if old else "added"

    def recall(self, tenant, user, query, k=3):
        q, now, hits = set(WORD.findall(query.lower())), self.clock(), []
        for (t, u, key), row in self.rows.items():
            if (t, u) != (tenant, user) or not self.alive(row):
                continue  # scope and expiry filters before ranking
            overlap = len(q & set(WORD.findall(f"{key} {row['value']}".lower())))
            if overlap:
                recency = 1 / (1 + (now - row["at"]) / 86_400)
                hits.append((overlap + recency, key, row["value"]))
        return [(key, v) for _, key, v in sorted(hits, reverse=True)[:k]]

    def forget_user(self, tenant, user):  # erasure request
        for k in [k for k in self.rows if k[:2] == (tenant, user)]:
            del self.rows[k]


def save_checkpoint(path, state):
    with open(f"{path}.tmp", "w") as f:
        json.dump(state, f)
    os.replace(f"{path}.tmp", path)  # atomic: never a half-written checkpoint


def load_checkpoint(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def run_task(path, steps, sent, crash_at=None):
    state = load_checkpoint(path) or {"run_id": "r1", "next": 0, "log": []}
    while state["next"] < len(steps):
        i = state["next"]
        key = f"{state['run_id']}:{i}"
        if key not in sent:  # a unique constraint on the key in production
            sent[key] = steps[i]
        if i == crash_at:
            raise RuntimeError("worker died after the side effect, before the checkpoint")
        state["log"].append(steps[i])
        state["next"] = i + 1
        save_checkpoint(path, state)
    return state


def fake_summarize(summary, old):  # an LLM call in production
    firsts = " ".join(m["content"].split(".")[0] + "." for m in old)
    return f"{summary} {firsts}".strip()[-160:]


if __name__ == "__main__":
    st = ShortTerm(fake_summarize)
    for i in range(12):
        st.add("user", f"Question {i}. Some filler words to use up the token budget.")
        st.add("assistant", f"Answer {i}. More filler so the summarizer has to run.")
    assert st.summary and st.tokens() <= st.budget
    assert st.messages[-1]["content"].startswith("Answer 11")  # recent turns verbatim
    assert len(st.messages) < 24 and all("Question 0." not in m["content"] for m in st.messages)

    tiny = ShortTerm(fake_summarize, budget=10, keep_last=1)
    for role, text in [("user", "x" * 40), ("assistant", "calling search_docs"), ("tool", "result " * 10)]:
        tiny.add(role, text)
    assert [m["role"] for m in tiny.messages] == ["assistant", "tool"]  # pair kept together

    now = [1_000_000.0]
    lt = LongTerm(clock=lambda: now[0])
    assert lt.write("acme", "u1", "pref.language", "Python") == "added"
    assert lt.write("acme", "u1", "pref.language", "python") == "noop"
    assert lt.write("acme", "u1", "pref.language", "Go", source="inferred") == "kept"
    assert lt.write("acme", "u1", "pref.language", "Go") == "updated"  # user changed their mind
    assert lt.rows[("acme", "u1", "pref.language")]["history"] == ["Python"]
    lt.write("acme", "u1", "task.current", "migrating billing to Postgres 16", ttl=7 * 86_400)
    lt.write("globex", "u1", "pref.language", "Rust")  # same user id, different tenant
    assert lt.recall("acme", "u1", "Which language for code examples?") == [("pref.language", "Go")]
    assert lt.recall("acme", "u1", "current billing task")[0][0] == "task.current"
    now[0] += 8 * 86_400  # eight days later the short-lived fact has expired
    assert lt.recall("acme", "u1", "current billing task") == []
    lt.forget_user("acme", "u1")
    assert lt.recall("acme", "u1", "language") == []
    assert lt.recall("globex", "u1", "language") == [("pref.language", "Rust")]

    with tempfile.TemporaryDirectory() as d:
        path, sent = os.path.join(d, "run.json"), {}
        steps = ["plan", "email customer", "update ticket", "report"]
        try:
            run_task(path, steps, sent, crash_at=2)
        except RuntimeError:
            pass
        assert load_checkpoint(path)["next"] == 2  # resume at the step that crashed
        state = run_task(path, steps, sent)  # another worker picks the run up
        assert state["log"] == steps and list(sent.values()) == steps  # each effect once
    print("memory demo: ok")
```

## Pitfalls

- **Append-only vector memory.** Symptom: the agent cites a preference the user changed months ago; recall returns three contradictory chunks. Fix: slot facts with overwrite and history; dedup and expiry for episodes.
- **Memory shadowing the system of record.** Symptom: "your plan is Pro" after an upgrade. Fix: fetch live data with tools; never memorize it.
- **Summaries that drop identifiers.** Symptom: after compaction the agent asks for an order number it was given. Fix: pin IDs and constraints; use a structured summary prompt.
- **Global top-k, then filter.** Symptom: small tenants get empty or irrelevant recall. Fix: filter inside the search.
- **Replay after crash.** Symptom: duplicate emails or refunds after a deploy. Fix: idempotency keys on every side effect, results persisted before advancing.
- **Incomplete deletion.** Symptom: a deleted user's facts reappear via a cached summary or an eval set. Fix: inventory every derived store; test erasure end to end.

## Checklist

- Short-term: token budget, pinned goal and IDs, summarize early, tool pairs intact, originals kept.
- Long-term: slot facts in rows, episodes with embeddings, small always-loaded profile.
- Write policy: background extraction, stated beats inferred, dedup, TTLs, no untrusted sources.
- Run state checkpointed every step with leases, versioning and idempotent tools.
- Every query scoped by tenant and user from the session; RLS underneath; isolation tested.
- Users can view, edit and delete memories; erasure reaches every derived store.

## Interview questions

**What is the difference between short-term and long-term memory in an agent?** Short-term is the working context of the current conversation or run, shaped by trimming and summarization. Long-term survives across sessions in external stores (facts, episodes, procedures) and is loaded or retrieved into the context when relevant.

**How do you keep a long conversation within the context window?** Budget in tokens; keep the system prompt, goal and recent turns verbatim; fold older turns into a running summary at around 70–80% of the budget; clear consumed tool results; keep tool call-result pairs intact; store the full transcript for retrieval.

**Vector store or relational table for memory?** Both. Stable facts and preferences are slot rows you can overwrite, audit and delete exactly. Episodes and notes are embedded for fuzzy recall. In Postgres, one table with a key, an embedding column and scoping metadata covers both.

**The user says "I moved to Berlin" after saying "I live in Paris". What happens?** The extractor proposes an update to the `location` slot. Newest stated value wins, the old one is kept with a validity range for audit, and later recall returns only Berlin. An inferred guess would not override a stated fact.

**How does an agent resume after a crash mid-run?** Run state is checkpointed every step: model response persisted before tools run, results persisted before advancing. A worker claims the run via an expired lease, loads the checkpoint and continues. Tools carry idempotency keys from run and call IDs, so replays cause no duplicate side effects.

**How do you prevent one customer's memories leaking to another?** Scope every read and write by tenant and user from the session, filter inside the vector search, enforce row-level security in the database, and run automated cross-tenant isolation tests.

## Further reading

- MemGPT: Towards LLMs as Operating Systems (Packer et al., 2023).
- Generative Agents: Interactive Simulacra of Human Behavior (Park et al., 2023).
- Cognitive Architectures for Language Agents (Sumers et al., 2023).
- Lost in the Middle: How Language Models Use Long Contexts (Liu et al., 2023).
- LangGraph documentation on persistence (checkpointers) and long-term memory stores.
