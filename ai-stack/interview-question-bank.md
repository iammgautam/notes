---
title: Interview question bank
section: ai-stack
order: 29
phase: "Phase 5 · Putting it together"
summary: About 70 crisp Q&As across Python, FastAPI, pandas, LangChain, LangGraph, RAG, LLMs, evals, security, cost and SQL, plus scenarios and behavioral prompts.
---

This bank covers what a Python AI engineer interview for a finance data platform asks, in the order rounds usually go: Python and APIs, data, LLM frameworks, RAG and evaluation, then production concerns and scenarios. Answers are short on purpose: say the answer in one or two sentences, then offer the deeper reason or a trade-off if the interviewer wants more. Questions marked *Likely* come up in most interviews for this role.

## Prerequisites

Phases 1 to 4 of this section, and ideally the [capstone]({{ '/ai-stack/capstone-finance-rag-agent/' | relative_url }}), which many answers point to as your example.

## Python core

- **What goes wrong with `def f(x, items=[])`?** *Likely.* The default list is created once at definition time and shared across calls. Use `items=None` and create the list inside.
- **Lookup cost in list, set and dict?** `x in list` is `O(n)`; set and dict lookups are `O(1)` on average because they hash. Dicts keep insertion order.
- **Dataclass or Pydantic model?** *Likely.* Pydantic at trust boundaries (API input, LLM output, config) because it validates and coerces; dataclasses for internal value objects where validation is overhead.
- **Inheritance or composition?** Composition by default; inherit only for a true "is-a" with shared behavior. For interfaces, `typing.Protocol` gives structural typing without forcing a base class.
- **How do you design error handling in a service?** A small hierarchy of domain exceptions, `raise NewError(...) from e` to keep the cause, catch narrowly, translate to HTTP status at the edge, and never swallow errors silently.
- **Give a real decorator use.** Retry with backoff around a provider call, timing or tracing, auth checks. Use `functools.wraps` to keep the wrapped function's name and docstring.
- **Why generators?** Lazy, constant memory. Reading a 10 GB extract line by line or streaming LLM tokens are both generators (sync or async).
- **What does the GIL mean for you?** *Likely.* One thread runs Python bytecode at a time, so threads help I/O but not CPU work. Use processes for CPU, asyncio for many concurrent I/O calls. Free-threaded builds exist since 3.13 but are not yet the default.
- **What breaks an async service?** *Likely.* A blocking call (sync DB driver, `requests`, `time.sleep`, heavy pandas) inside `async def` stalls every request on that loop. Use async clients, `asyncio.to_thread`, or a sync route.
- **How do type hints help if Python ignores them at runtime?** Static checkers (mypy, pyright) catch bugs before running; Pydantic and FastAPI do enforce them at runtime. `Literal`, `TypedDict` and `Annotated` express intent precisely.

The runnable block below proves three of these answers: the default-argument fix, bounded concurrency for LLM calls, and pandas 3 chained assignment.

```python
import asyncio
import warnings

import pandas as pd


def add_line(line, lines=None):  # None sentinel, never lines=[]
    lines = [] if lines is None else lines
    lines.append(line)
    return lines


async def classify_all(invoices: list[str], limit: int = 5):
    sem = asyncio.Semaphore(limit)  # at most `limit` provider calls in flight
    active = peak = 0

    async def one(inv: str):
        nonlocal active, peak
        async with sem:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)  # stands in for an LLM call
            active -= 1
            return inv, "travel" if "hotel" in inv else "other"

    results = await asyncio.gather(*(one(i) for i in invoices))
    return results, peak


if __name__ == "__main__":
    assert add_line("a") == ["a"] and add_line("b") == ["b"]

    results, peak = asyncio.run(classify_all([f"inv-{i} hotel" for i in range(20)]))
    assert len(results) == 20 and peak <= 5

    df = pd.DataFrame({"vendor": ["Acme", "Zen"], "amount": [100, 200]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df[df["amount"] > 150]["amount"] = 0  # chained: never writes in pandas 3
    assert df["amount"].tolist() == [100, 200]
    assert caught[0].category.__name__ == "ChainedAssignmentError"
    df.loc[df["amount"] > 150, "amount"] = 0  # one .loc call writes
    assert df["amount"].tolist() == [100, 0]
    assert df["vendor"].dtype == "str"  # pandas 3 default string dtype
```

## FastAPI

- **How does FastAPI use type hints?** *Likely.* Parameters and Pydantic models drive validation, serialization, OpenAPI docs and dependency injection from one declaration.
- **`def` or `async def` for a route?** *Likely.* `async def` when everything inside awaits; plain `def` when you call blocking code, because FastAPI runs it in a threadpool. Blocking inside `async def` is the classic bug.
- **What is `Depends` for?** Per-request dependencies: auth, DB sessions, settings. They compose, are cached within a request, support `yield` for cleanup, and are swapped in tests with `app.dependency_overrides`.
- **Middleware or dependency?** Middleware for every request (request ids, timing, CORS); dependencies for route-specific needs like auth and sessions, which also appear in OpenAPI.
- **How do you stream LLM output?** *Likely.* An async generator yielding SSE `data:` lines. Recent FastAPI ships `EventSourceResponse` (`response_class=` on a route that `yield`s); `StreamingResponse` with `text/event-stream` works everywhere. Stop on client disconnect and disable proxy buffering.
- **`BackgroundTasks` or Celery?** `BackgroundTasks` runs after the response in the same process, with no retries or persistence. Anything that must survive a restart (re-indexing, batch scoring) goes to a real queue.
- **What is `lifespan`?** One async context manager for startup and shutdown: open DB pools, HTTP clients and checkpointers, close them on exit. It replaces `on_event`.
- **How do you test it?** `TestClient` for sync tests, `httpx.AsyncClient` with `ASGITransport` for async ones, dependency overrides for auth and fake models.

## Pandas

- **What changed in pandas 3?** *Likely.* Copy-on-write is always on: chained assignment never modifies the original and emits `ChainedAssignmentError`, and `SettingWithCopyWarning` is gone. Strings default to the `str` dtype instead of `object`.
- **`loc` versus `iloc`?** `loc` selects by label and boolean mask, `iloc` by integer position. Assign with a single `df.loc[mask, col] = value`.
- **`agg` versus `transform`?** *Likely.* `agg` reduces to one row per group; `transform` returns a result aligned to the original rows, e.g. each invoice's share of its vendor's monthly spend.
- **How do you reconcile two sources?** *Likely.* Outer `merge` on the key with `indicator=True`: `left_only` and `right_only` are unmatched, `both` rows with an amount difference are mismatches. Add `validate="one_to_one"` to catch duplicate keys.
- **Why did my merge multiply rows?** Duplicate keys on both sides produce a many-to-many join. Check with `validate=` and deduplicate first.
- **How do you handle money in pandas?** Integer minor units (`int64`) or `Decimal`, never raw floats for totals. Round only for display, and reconcile sums against a control total.
- **A pandas job is slow. What do you check?** Row-wise `apply` (vectorize instead), object columns (use `category` or `str`), reading CSV (use Parquet with pyarrow), and data size: past memory, push work to SQL, Polars or PySpark.

## LangChain

- **What is LangChain 1.x made of?** *Likely.* `langchain-core` (messages, runnables, tool interfaces), provider packages (`langchain-openai`, `langchain-anthropic`), and `langchain` itself (`init_chat_model`, `create_agent`, middleware). Legacy chains moved to `langchain-classic`.
- **What replaced `AgentExecutor`?** *Likely.* `langchain.agents.create_agent`, a LangGraph-based loop customized with middleware (human approval, PII, summarization, retries, call limits). LangGraph's `create_react_agent` is deprecated in its favor.
- **What is a Runnable?** One interface (`invoke`, `batch`, `stream` and async variants) for models, prompts, retrievers and parsers. `|` composes them; `RunnableParallel` fans out.
- **How do you get structured output?** *Likely.* `model.with_structured_output(PydanticModel)`, which uses the provider's JSON-schema mode or tool calling. Pass `include_raw=True` to inspect failures; validate anyway.
- **How does the model see a tool?** Name, description and argument schema built from the function's signature and docstring. Arguments typed as `ToolRuntime` are injected at run time and hidden from the model.
- **You built RAG without LangChain. Why adopt it?** *Likely.* Swappable providers behind one message and tool format, LangGraph persistence, streaming and human-in-the-loop, and LangSmith tracing. Keep business logic in plain functions so the framework stays a thin layer.

## LangGraph

- **Explain the core model.** *Likely.* A typed state, nodes that return partial updates, edges (fixed or conditional) that pick the next node, and reducers that merge updates. `compile()` makes a runnable graph.
- **What is a reducer?** The rule for merging a node's update into state. `add_messages` appends and replaces by message id; without a reducer a key is overwritten.
- **What does a checkpointer give you?** *Likely.* State saved after every step per `thread_id`: conversation memory, resume after a crash, human approval and time travel. `InMemorySaver` for tests, Postgres in production.
- **How does human-in-the-loop work?** `interrupt(payload)` pauses inside a node and returns the payload to the caller; `Command(resume=value)` on the same thread continues. The node re-runs from its start, so side effects belong after the interrupt or must be idempotent.
- **State, config or context?** State is data that changes during the run; config carries `thread_id` and callbacks; runtime context holds per-run dependencies such as the current user, which tools read through `ToolRuntime`.
- **Which stream modes matter?** `updates` for per-node progress, `messages` for LLM tokens, `values` for full state, `custom` for tool progress events.
- **How do you stop an agent looping?** Set `recursion_limit` explicitly (recent versions default very high), cap tool calls with `ToolCallLimitMiddleware`, and make tools return clear errors instead of empty results.

## RAG and retrieval tuning

- **How do you chunk policy documents?** *Likely.* By structure (sections, clauses), a few hundred tokens with small overlap, and metadata on every chunk: doc id, section, effective date, audience.
- **Why hybrid search?** *Likely.* Dense vectors miss exact tokens like GL codes, vendor names and policy numbers; BM25 catches them. Merge with reciprocal rank fusion, then rerank.
- **What do you tune when retrieval is poor?** Chunking first, then embeddings, k, metadata filters, query rewriting, MMR for diversity, and a cross-encoder reranker. Change one knob at a time against a labeled set.
- **How do you measure retrieval?** Hit rate or recall@k, MRR and nDCG on labeled question-to-chunk pairs; RAGAS context precision and recall for end-to-end views. See [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).
- **How do you ground answers?** Answer only from retrieved context, cite chunk ids, allow "not found", and verify that cited ids were actually retrieved.
- **How do you keep the index fresh?** Incremental ingestion keyed on content hash, effective-date metadata, and a filter that excludes superseded versions.
- **Which vector store?** pgvector when you already run Postgres and need joins and transactional ACLs; a managed service (Azure AI Search, Databricks Vector Search) for built-in hybrid search or larger scale. Filter support matters more than benchmarks.

## LLM engineering

- **What is different about Azure OpenAI?** *Likely.* You call a deployment you created, not a model name; older SDK calls pin an `api-version` while the newer v1 endpoint follows the OpenAI client. Add regional deployment for data residency, Entra ID auth, private endpoints, default content filters, per-deployment TPM and RPM quotas, and provisioned throughput.
- **How do you choose a model?** Run your eval set and compare quality, latency, cost, context length and data-residency fit. Small models route and extract; larger ones reason. Open models fit on-prem constraints.
- **What temperature for tool use and extraction?** Zero or near zero. Outputs are still not perfectly deterministic, so tests assert structure and facts, not exact strings.
- **JSON mode, function calling or JSON schema?** Strict JSON schema constrains decoding to your schema and is the best default where supported; function calling is the portable fallback; plain JSON mode guarantees only valid JSON.
- **What goes into a good prompt?** Role, task, constraints, output format, a few examples, and untrusted content inside clear delimiters. Version prompts like code.
- **How do you manage long conversations?** Trim or summarize old turns, retrieve instead of stuffing, and keep a stable prefix so provider prompt caching applies.
- **How do you handle 429s and timeouts?** *Likely.* Exponential backoff with jitter, honor `Retry-After`, client timeouts, a fallback deployment or model, and a circuit breaker so one slow provider does not exhaust workers.

## Evaluation and hallucination reduction

- **How do you evaluate an LLM application?** *Likely.* A golden set per use case, metrics per layer (retrieval, tools, final answer), LLM-as-judge with a rubric calibrated against human labels, online feedback, and a CI regression gate.
- **What do RAGAS metrics measure?** Faithfulness (claims supported by context), answer relevancy, context precision (retrieved chunks are relevant) and context recall (needed facts were retrieved).
- **How do you reduce hallucinations?** *Likely.* Better retrieval, explicit permission to say "not found", citations that are verified, tools for every number, low temperature, and a post-check that blocks unsupported claims.
- **Where do LLM judges go wrong?** Position and verbosity bias and favoring their own outputs. Use rubrics or pairwise comparisons, a different model family, and spot-check against humans.
- **What do LangSmith and DeepEval give you?** LangSmith traces runs and stores datasets and experiments for side-by-side comparison; DeepEval runs LLM metrics as pytest tests. See [observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }}).

## Security and privacy in regulated finance

- **How do you defend against prompt injection?** *Likely.* Treat retrieved text as data, give tools least privilege, keep identity and secrets out of the prompt, validate outputs, and require human approval for any write.
- **How do you enforce access control in RAG?** *Likely.* Permission metadata on every chunk, filtered at query time from the authenticated identity and synced from the source system. The prompt is never the boundary.
- **How do you handle PII?** Detect and redact or tokenize before the model call, keep PII out of logs and traces, minimize fields sent, and set retention rules.
- **What goes in an audit log?** Who asked, the question, which tools and documents were used with which arguments, model and prompt versions, the answer, and timestamps, stored append-only.
- **Which regulations come up?** Data residency and zero-retention terms with providers, SOX-style controls on financial reporting, and India's DPDP Act for personal data. Know them at the level of what they constrain.

## Performance and cost

- **How do you cut latency?** *Likely.* Stream to cut time to first token, use smaller models for simple steps, run independent tool calls in parallel, trim context, and cache.
- **How do you cut cost?** Route by difficulty, use prompt caching for stable prefixes, shorten prompts, cap agent steps, and batch offline jobs.
- **What do you measure?** Tokens and cost per request, time to first token, p50 and p95 per stage, and cache hit rate, sliced by use case.
- **When is caching dangerous?** Semantic caches can serve a cached total for a question about a different month. Key caches by user scope and data version, and never semantically cache numeric answers.

## SQL

- **Running balance per account?** *Likely.* `SUM(amount) OVER (PARTITION BY account_id ORDER BY posted_at)`; month-over-month change uses `LAG(total) OVER (ORDER BY month)`.
- **Invoices with no matching payment?** An anti-join: `LEFT JOIN payments p ON ... WHERE p.id IS NULL`, or `NOT EXISTS`.
- **Find duplicate invoices?** `GROUP BY vendor_id, invoice_no, amount HAVING COUNT(*) > 1`, or `ROW_NUMBER()` partitioned by those columns to keep the first.
- **Speeding up a slow filter?** Run `EXPLAIN ANALYZE`; add a composite index with equality columns first and the range column last.
- **How do you make text-to-SQL safe?** *Likely.* A read-only role, row-level security, `statement_timeout`, allowlisted views, a forced `LIMIT`, and parsing the SQL before running it. Better: let the model pick metrics and filters, and generate SQL in code. See [AI system design for finance data]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}).

## Scenario questions

Answer every scenario in the same order. It shows method, not luck.

```text
 symptom (wrong total, missed doc, slow, costly)
    |
    v
 reproduce from the trace (request id -> LangSmith run)
    |
    v
 localize: retrieval | tool/data | model/prompt | infra
    |
    v
 fix that layer -> add the case to the golden set -> gate in CI
```

**1. The agent returns wrong totals.** *Likely.* Triage: pull the trace and compare the tool output with the answer. Diagnose: if the tool was right, the model did arithmetic or misread; if the tool was wrong, check filters, period boundaries, currency and duplicates. Fix: compute totals in tools, add a numeric grounding check. Prevent: exact-match golden questions in CI.

**2. Retrieval misses a recently updated policy.** *Likely.* Triage: check whether the new version was ingested. Diagnose: ingestion job failures, missing hash-based re-index, old and new versions competing, or an effective-date filter that is wrong. Fix: incremental re-index, supersede old versions. Prevent: freshness monitoring and a retrieval test per policy change.

**3. Latency doubled after a release.** *Likely.* Triage: compare per-span timings before and after. Diagnose: more agent steps, longer prompts, a lost prompt-cache prefix, a new reranker, or a provider region change. Fix the stage that grew. Prevent: p95 budgets per stage alerted in CI and production.

**4. Cost tripled this month.** Triage: cost per request by route and user. Diagnose: a looping agent, a prompt that grew, a cache miss, a client retry storm, or a model swap. Fix: step caps, routing, caching. Prevent: per-tenant budgets and anomaly alerts.

**5. A user saw another entity's numbers.** *Likely.* Treat it as an incident: contain (disable the route), then preserve audit logs. Diagnose: a tool that skipped the scope function, a cache keyed without user scope, or a shared thread id. Fix at the data boundary. Prevent: an authorization test per tool and per cache.

**6. The agent loops calling the same tool.** Diagnose: an empty or ambiguous tool result, or a description that invites retries. Fix: clear "no data" results, tool-call limits, an explicit recursion limit. Prevent: evals that assert step counts.

**7. Answers changed after a provider model update.** Diagnose: re-run the golden set against the old and new versions. Fix: pin model versions or deployments, adjust prompts. Prevent: model upgrades go through the same eval gate as code.

**8. Users report citations to policies that do not exist.** Diagnose: the model fabricates ids not in the retrieved set. Fix: validate every cited id against retrieved chunks, regenerate or drop unsupported claims, constrain citations to a list of ids. Prevent: a citation-validity metric in evals.

## Behavioral prompts

Use the situation, task, action, result format, with numbers in the result and one lesson at the end. Prepare a real story for each.

- **A production incident you owned end to end.** They look for calm triage, communication and a lasting fix.
- **A time you mentored someone.** Show how you raised their independence: pairing, reviews that teach, gradually bigger ownership.
- **Pushing back on a risky requirement.** For example, resisting "let the model compute the totals". Show data, an alternative and how you aligned stakeholders.
- **A build-versus-adopt decision.** Custom LLM abstractions versus LangChain is a natural one: criteria, cost of switching, what you kept.
- **Disagreeing with a senior engineer.** Facts, a small experiment, then commit to the decision.
- **Learning a new stack quickly.** Your plan, the first thing you shipped, and how you checked quality.

## Common mistakes

- Reciting framework APIs without saying why a design is safe for finance data.
- Answering scenarios with a fix and no diagnosis or prevention.
- Giving accuracy or latency numbers you did not measure.
- Saying "the prompt tells the model not to" as a security control.

## Practice tasks

1. Answer every *Likely* question aloud in under 45 seconds each. Done: a recording with no answer over a minute.
2. Write a full scenario answer for 1, 3 and 5 using your capstone traces. Done: each names the span, the root cause and the regression test.
3. Turn three SQL answers into queries over your capstone ledger. Done: they run and match the pandas results.
4. Draft two behavioral stories with numbers. Done: each fits in two minutes.

## Further reading

- Python docs: asyncio (Task groups, synchronization primitives), `typing`.
- FastAPI docs: Dependencies, Server-Sent Events, Testing, Lifespan events.
- pandas docs: Copy-on-Write user guide, migration guide for pandas 3.0.
- LangChain docs: Agents, Middleware, Structured output. LangGraph docs: Persistence, Interrupts, Streaming.
- Azure OpenAI docs: deployments, quotas and limits, data privacy.
- RAGAS docs: Metrics. In this site: [AI system design interviews]({{ '/llm/ai-system-design/' | relative_url }}).
