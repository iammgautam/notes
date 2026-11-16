---
title: LCEL and runnables
section: ai-stack
order: 16
phase: "Phase 3 · LangChain"
summary: "Compose prompts, models and functions with LCEL: parallel steps, routing, config, streaming and batching, and when to switch to LangGraph."
---
After this note you can read and write LCEL pipelines fluently: compose steps with `|`, fan out lookups in parallel, enrich a dict as it flows, route between branches, pass tags and tenant metadata through every call, stream tokens to a client and batch hundreds of inputs with bounded concurrency. RAG chains, extraction pipelines and every LangGraph node you write later are built from these pieces, and interviewers often ask you to sketch one on a whiteboard.

## Prerequisites

[Models, prompts and structured output]({{ '/ai-stack/langchain-models-prompts-structured-output/' | relative_url }}). Async Python basics help for the streaming section.

## The Runnable protocol

Every LangChain component (prompt, chat model, parser, retriever, tool, compiled graph) is a `Runnable`. That one interface is what makes them composable.

| Method | Does |
|---|---|
| `invoke(input, config)` | One input, one output |
| `batch(inputs, config)` | Many inputs concurrently, results in input order |
| `stream(input, config)` | Yields output chunks as they are produced |
| `ainvoke`, `abatch`, `astream` | Async versions |
| `astream_events(input, version="v2")` | Every start, chunk and end event from every inner step |
| `input_schema`, `output_schema` | Pydantic models describing the expected shapes |

The pipe operator `a | b` builds a `RunnableSequence`: the output of `a` becomes the input of `b`. The sequence is itself a runnable, so it has all the methods above. When one side of `|` is a runnable, the other side is coerced: a plain function becomes a `RunnableLambda` and a dict of runnables becomes a `RunnableParallel`.

```python
from operator import itemgetter

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableParallel, RunnableSequence

prompt = ChatPromptTemplate.from_template("Account {account} is {variance} against budget. Explain in one line.")
model = FakeListChatModel(responses=["Audit fee booked twice in October."])
explain = prompt | model | StrOutputParser()


def to_prompt_vars(row: dict) -> dict:
    variance = row["actual"] - row["budget"]
    return {"account": row["account"], "variance": f"{variance:+,} INR"}


chain = to_prompt_vars | explain  # the function is coerced to a RunnableLambda

with_account = {"account": itemgetter("account"), "explanation": chain} | RunnableLambda(
    lambda d: f"{d['account']}: {d['explanation']}"
)

if __name__ == "__main__":
    assert isinstance(explain, RunnableSequence)
    assert sorted(explain.input_schema.model_json_schema()["required"]) == ["account", "variance"]

    row = {"account": "6100 Audit fees", "actual": 480_000, "budget": 240_000}
    assert to_prompt_vars(row)["variance"] == "+240,000 INR"
    assert chain.invoke(row) == "Audit fee booked twice in October."

    assert isinstance(with_account.first, RunnableParallel)  # the dict became a RunnableParallel
    print(with_account.invoke(row))
```

`itemgetter("account")` from the standard library is the idiomatic way to pull one key out of the input dict.

## Parallel steps and growing a dict

Most real chains carry a dict forward and add to it: the transaction, then the vendor record, then the policy, then the model's verdict. Two primitives do this:

- `RunnableParallel(a=..., b=...)` runs each value on the same input concurrently and returns a dict of results.
- `RunnablePassthrough.assign(key=...)` keeps the input dict and adds keys. Several keys in one `assign` run in parallel; chained `assign` calls run in order, so later keys can use earlier ones.

```text
  input: txn dict
     |
     v
  assign(vendor, mtd) ......... two lookups in parallel
     |
     v
  assign(policy) .............. needs vendor, so runs after
     |
     v
  parallel(verdict, facts)
     |-- verdict: prompt -> model -> parser
     |-- facts:   plain Python, no model call
     v
  output: verdict and facts dict
```

```python
from operator import itemgetter

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableParallel, RunnablePassthrough

VENDORS = {
    "V001": {"name": "Acme Cloud", "category": "IT infrastructure"},
}
POLICIES = {"IT infrastructure": "Cloud spend above INR 500,000 a month needs CFO approval."}
POSTED = [("V001", 210_000), ("V001", 190_000)]  # this month's posted payments


def load_vendor(txn: dict) -> dict:
    return VENDORS.get(txn["vendor_id"], {"name": "Unknown", "category": "Other"})


def month_to_date(txn: dict) -> int:
    return sum(amount for vendor_id, amount in POSTED if vendor_id == txn["vendor_id"])


def policy_for(state: dict) -> str:
    return POLICIES.get(state["vendor"]["category"], "No category policy.")


def facts(state: dict) -> dict:
    return {"vendor": state["vendor"]["name"], "mtd_after": state["mtd"] + state["txn"]["amount"]}


gather = RunnablePassthrough.assign(
    vendor=itemgetter("txn") | RunnableLambda(load_vendor),
    mtd=itemgetter("txn") | RunnableLambda(month_to_date),
) | RunnablePassthrough.assign(policy=policy_for)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You review payments against policy. Answer in one sentence."),
    ("human", "Payment: {txn}. Vendor: {vendor}. Month to date: {mtd}. Policy: {policy}. Any issue?"),
])
model = FakeListChatModel(responses=["Yes: this payment takes October spend to INR 520,000, above the CFO limit."])

review = gather | RunnableParallel(verdict=prompt | model | StrOutputParser(), facts=facts)

if __name__ == "__main__":
    txn = {"txn_id": "T-9001", "vendor_id": "V001", "amount": 120_000}

    state = gather.invoke({"txn": txn})
    assert set(state) == {"txn", "vendor", "mtd", "policy"}
    assert state["mtd"] == 400_000 and "CFO" in state["policy"]

    out = review.invoke({"txn": txn})
    print(out)
    assert out["facts"] == {"vendor": "Acme Cloud", "mtd_after": 520_000}
    assert "CFO" in out["verdict"]
```

The `facts` branch shows a habit worth keeping: compute numbers in Python and give them to the model, rather than asking the model to add amounts. It also gives you a deterministic field to assert on in tests.

## Routing: RunnableBranch vs a routing function

You often need different handling per input: unknown vendor, amount over a limit, everything else. `RunnableBranch` takes `(condition, runnable)` pairs plus a default. A routing function does the same thing with ordinary `if` statements: when a `RunnableLambda` returns a runnable, LCEL invokes that runnable with the same input.

```python
from langchain_core.runnables import RunnableBranch, RunnableLambda

APPROVAL_LIMIT = 500_000


def is_unknown(state: dict) -> bool:
    return state["vendor"]["name"] == "Unknown"


def over_limit(state: dict) -> bool:
    return state["mtd"] + state["txn"]["amount"] > APPROVAL_LIMIT


reject = RunnableLambda(lambda s: "REJECT: vendor not in master data")
escalate = RunnableLambda(lambda s: "ESCALATE: needs CFO approval")  # real code: prompt | model | parser
approve = RunnableLambda(lambda s: "APPROVE")

branch = RunnableBranch(
    (is_unknown, reject),
    (over_limit, escalate),
    approve,  # default, no condition
)


def route(state: dict):
    if is_unknown(state):
        return reject
    if over_limit(state):
        return escalate
    return approve


routed = RunnableLambda(route)

if __name__ == "__main__":
    cases = {
        "REJECT: vendor not in master data": {"txn": {"amount": 10}, "vendor": {"name": "Unknown"}, "mtd": 0},
        "ESCALATE: needs CFO approval": {"txn": {"amount": 120_000}, "vendor": {"name": "Acme"}, "mtd": 400_000},
        "APPROVE": {"txn": {"amount": 5_000}, "vendor": {"name": "Acme"}, "mtd": 100_000},
    }
    for expected, state in cases.items():
        assert branch.invoke(state) == expected
        assert routed.invoke(state) == expected
    print("both routers agree on", len(cases), "cases")
```

Prefer the routing function. It is plain Python, easy to unit test, and shows up as one named step in traces. `RunnableBranch` is fine for two or three static conditions. Once routing depends on accumulated state or loops back (re-ask on a failed check, escalate then resume), you want LangGraph conditional edges instead; see [LangGraph control flow]({{ '/ai-stack/langgraph-control-flow/' | relative_url }}).

## Config: tags, metadata, callbacks, concurrency

The second argument to every runnable method is a `RunnableConfig` dict. It propagates automatically to every nested step, which is how tracing and per-request context work without threading arguments through your code.

| Key | Use |
|---|---|
| `run_name` | Name of this run in traces |
| `tags` | Labels for filtering (`month-end`, `extraction-v2`) |
| `metadata` | Request context: tenant, user, request id |
| `callbacks` | Handlers that receive start, token, end and error events |
| `max_concurrency` | Upper bound on parallel work in `batch` and `RunnableParallel` |
| `configurable` | Runtime values for configurable fields and LangGraph `thread_id` |
| `recursion_limit` | Max steps, mostly relevant for graphs |

`with_config(...)` bakes defaults into a runnable; values passed at call time are merged on top.

```python
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate


class AuditHandler(BaseCallbackHandler):
    """Records every model call with the request's tags and tenant."""

    def __init__(self):
        self.calls = []

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self.calls.append({"tags": tags, "tenant": metadata.get("tenant")})


prompt = ChatPromptTemplate.from_template("Explain the variance on account {account}.")
model = FakeListChatModel(responses=["Timing difference on the accrual."])
chain = (prompt | model | StrOutputParser()).with_config(run_name="variance_explainer", tags=["month-end"])

if __name__ == "__main__":
    handler = AuditHandler()
    rows = [{"account": "6100"}, {"acct": "typo"}, {"account": "7200"}]
    config = {
        "metadata": {"tenant": "acme-in", "request_id": "req-81"},
        "callbacks": [handler],
        "max_concurrency": 2,
    }
    results = chain.batch(rows, config=config, return_exceptions=True)

    assert results[0] == "Timing difference on the accrual."
    assert isinstance(results[1], KeyError)  # failed row returned, batch kept going
    assert len(handler.calls) == 2
    assert all("month-end" in c["tags"] and c["tenant"] == "acme-in" for c in handler.calls)
    print(handler.calls)
```

Two batch details matter in production. `max_concurrency` is your client-side rate limit; set it below the provider's limit divided by the number of workers. `return_exceptions=True` keeps one bad row from failing the other 499; without it, the first exception aborts the batch.

> **In production:** set `metadata` with tenant and request id at the API boundary and pass the config down. LangSmith, your own callbacks and logs then all agree on which tenant a model call belonged to.

## Streaming and astream_events

`stream` on a chain yields the final step's chunks. Because `StrOutputParser` streams too, `prompt | model | StrOutputParser()` yields text pieces you can forward to a client as server-sent events (the FastAPI side is in [Streaming LLM APIs with FastAPI]({{ '/ai-stack/fastapi-streaming-llm-apis/' | relative_url }})).

`astream_events(version="v2")` is the debugging and UI tool: it yields every event from every inner step (`on_chain_start`, `on_prompt_end`, `on_chat_model_stream`, `on_parser_end`, `on_chain_end`), each with a `name`, `tags`, `metadata` and `data`. Filter by event type or by `include_names` / `include_tags` to pick out exactly the tokens you want, for example only the final answer model in a chain that calls two models.

```python
import asyncio

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate.from_template("Explain account {account}.")
model = GenericFakeChatModel(messages=iter([AIMessage("Audit fee booked twice in October.")] * 2))
chain = (prompt | model | StrOutputParser()).with_config(run_name="variance_explainer")


async def answer_tokens(account: str) -> list[str]:
    tokens = []
    async for event in chain.astream_events({"account": account}, version="v2"):
        if event["event"] == "on_chat_model_stream":
            tokens.append(event["data"]["chunk"].text)
        elif event["event"] == "on_chain_end" and event["name"] == "variance_explainer":
            assert event["data"]["output"] == "".join(tokens)
    return tokens


if __name__ == "__main__":
    pieces = list(chain.stream({"account": "6100"}))
    assert len(pieces) > 1 and "".join(pieces) == "Audit fee booked twice in October."

    tokens = asyncio.run(answer_tokens("6100"))
    print(tokens)
    assert "".join(tokens) == "Audit fee booked twice in October."
```

One catch: a plain function step in the middle of a chain consumes its whole input before passing anything on, so tokens stop streaming at that step. Put post-processing after the parser as a generator function, or move it out of the streamed path.

## Batching for throughput

`batch` is how you run extraction over 2,000 invoices or classify a month of bank lines. It uses a thread pool (`abatch` uses asyncio) bounded by `max_concurrency`. Rules that keep it safe:

- Bound concurrency explicitly. The default can exceed provider rate limits on big batches.
- Use `return_exceptions=True` and write failures to a retry table with the input id.
- Chunk very large jobs (for example 200 rows per `batch` call) so progress is checkpointed; a Celery task per chunk works well.
- For overnight jobs, check whether the provider's asynchronous batch API (cheaper, slower) fits better than real-time calls.

## When LCEL is enough and when to switch to LangGraph

| Need | LCEL | LangGraph |
|---|---|---|
| Fixed pipeline: retrieve, prompt, generate, parse | Yes | Overkill |
| Parallel lookups then one model call | Yes | Overkill |
| Static branching on input fields | Yes (routing function) | Also fine |
| Loops: re-ask until validation passes, tool loops | Awkward | Yes |
| Pause for human approval and resume later | No | Yes (`interrupt` plus checkpointer) |
| State shared across many steps and turns | Clumsy dicts | Typed state |
| Retry or time out a single step | `with_retry` on that step | Retry policy per node |

LCEL describes a directed acyclic pipeline. The moment you need a cycle, durable state or a pause, use LangGraph, and keep using LCEL chains inside the graph's nodes.

## Coming from Django

| Celery canvas / Django | LCEL |
|---|---|
| `chain(a.s(), b.s())` | `RunnableSequence` built with the pipe operator |
| `group(...)` | `RunnableParallel` or `batch` |
| `chord(group, callback)` | `RunnableParallel` followed by a step that combines results |
| `link_error` | `with_fallbacks` |
| `autoretry_for`, `retry_backoff` | `with_retry(retry_if_exception_type=..., wait_exponential_jitter=True)` |
| Request object carrying user and tenant | `RunnableConfig` metadata propagated to nested calls |
| Signals (`task_prerun`, `task_failure`) | Callback handlers (`on_chain_start`, `on_chain_error`) |

The big difference from Celery: LCEL runs in-process. It does not persist intermediate results or survive a worker restart. Put durable, long-running steps in Celery or LangGraph with a checkpointer, and use LCEL for the in-request pipeline.

## Common mistakes

- **Lambdas everywhere.** Anonymous lambdas show up in traces as `RunnableLambda` with no useful name. Use named functions for anything non-trivial.
- **Losing input keys.** A step that returns only its own result drops the rest of the dict. Use `RunnablePassthrough.assign` to add keys instead of replacing the dict.
- **Sync functions in async chains.** A blocking DB call in a `RunnableLambda` blocks the event loop under `ainvoke`. Give `RunnableLambda` an async function (or both, via `afunc`) for I/O.
- **Unbounded batch.** `batch` over 5,000 inputs without `max_concurrency` hits 429s and triggers retries that make it worse.
- **Using `RunnableBranch` for a state machine.** Once branches loop back or need memory, the chain becomes unreadable. Move to LangGraph.
- **Streaming broken by a middle step.** A non-streaming function between model and client buffers everything. Check with `astream_events` where chunks stop.
- **Config lost in your own threads.** Config propagates through context variables, so a nested `.invoke` inside a `RunnableLambda` is traced automatically. Work you hand to your own `ThreadPoolExecutor` loses it: accept `config` as the function's second parameter and pass it explicitly there.

## Practice tasks

1. **Variance explainer.** Build `to_prompt_vars | prompt | model | parser` for GL variance rows and run it over a pandas DataFrame with `batch` (`df.to_dict("records")`). Done: results written back as a new column, with failed rows marked instead of crashing.
2. **Payment review chain.** Extend the payment example with a third parallel lookup (duplicate check: same vendor and amount in the last 7 days) and a routing function with a `DUPLICATE` branch. Done: tests for all four routes.
3. **Tenant audit callback.** Write a callback handler that writes one row per model call (tenant, run name, tags, latency) to SQLite using `on_chat_model_start` and `on_llm_end`. Done: a batch of 10 inputs produces 10 rows with non-zero latency.
4. **Stream only the final answer.** Build a chain with two model calls (rewrite the question, then answer) and use `astream_events` with `include_names` so only the second model's tokens are streamed. Done: a test proves no tokens from the rewrite step leak.
5. **Rewrite as a graph.** Take the payment review chain and add "if the verdict mentions a policy breach, re-run with a stricter prompt at most twice". Done: a short note on why this was awkward in LCEL, plus the LangGraph version after reading [LangGraph core concepts]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }}).

## Interview questions

**What is a Runnable and why does LangChain use one interface for everything?**
A common protocol with `invoke`, `batch`, `stream`, their async versions and `astream_events`. Prompts, models, parsers, retrievers, tools and graphs all implement it, so any of them compose with `|` and get batching, streaming, retries, fallbacks and tracing for free.

**What does `|` produce, and what gets coerced?**
A `RunnableSequence`. Plain functions become `RunnableLambda`, dicts become `RunnableParallel`, so `{"a": r1, "b": r2} | step` works.

**`RunnableParallel` vs `RunnablePassthrough.assign`?**
Parallel returns only the new keys. `assign` returns the input dict plus the new keys, running multiple keys in parallel. Use `assign` to grow state through a pipeline.

**How do you route between chains?**
A function wrapped in `RunnableLambda` that returns the runnable to run, or `RunnableBranch` with condition pairs. Functions are easier to test. Stateful or looping routing belongs in LangGraph conditional edges.

**How does tenant or request context reach nested model calls?**
Through `RunnableConfig`: `metadata`, `tags` and `callbacks` passed at the top propagate to every child run, and LangSmith and callback handlers see them.

**How do you stream tokens from a chain to a browser?**
`astream` on a chain ending in `StrOutputParser` for simple cases; `astream_events(version="v2")` filtered on `on_chat_model_stream` when you need tokens from a specific inner model. Forward chunks as server-sent events from an async FastAPI route.

**How do you run LCEL over thousands of records safely?**
`batch` or `abatch` with `max_concurrency` under the provider's rate limit, `return_exceptions=True`, chunked jobs with progress tracking, and failures recorded for retry.

**When would you stop using LCEL and move to LangGraph?**
When you need cycles, durable state, human approval with resume, or per-step retry and timeout policies. LCEL is for acyclic in-process pipelines.

## Further reading

- LangChain docs: "Runnables" and "LangChain Expression Language (LCEL)"
- LangChain API reference: `RunnableParallel`, `RunnablePassthrough`, `RunnableLambda`, `RunnableBranch`
- LangChain API reference: `RunnableConfig`
- LangChain docs: "Streaming" (`astream_events`)
- LangChain docs: "Callbacks"
- LangGraph docs: "Graph API overview" for the step beyond LCEL
