---
title: "Observability and evaluation: LangSmith, RAGAS, DeepEval"
section: ai-stack
order: 20
phase: "Phase 3 · LangChain"
summary: Trace LangChain apps with callbacks and LangSmith, measure RAG quality with RAGAS, DeepEval or your own pandas harness, and gate releases in CI.
---
After this note you can instrument a LangChain app so every request shows its latency, tokens, cost and inputs; build a golden dataset; score a RAG system with RAGAS metrics, DeepEval or a small harness of your own; and fail a pull request when quality regresses. The job description lists "LLM evaluation and hallucination reduction" as a responsibility and LangSmith, RAGAS and DeepEval as nice-to-haves. In interviews this is where you show that you measure instead of eyeballing.

## Prerequisites

[RAG with LangChain]({{ '/ai-stack/langchain-rag/' | relative_url }}) and [chat history]({{ '/ai-stack/langchain-chat-history/' | relative_url }}). Background: [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) and [observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).

## Mental model: traces explain one request, evals judge many

Observability answers "what happened in this request and why was it slow or wrong". Evaluation answers "is version B better than version A across the questions we care about". They feed each other:

```text
  production traffic ──► traces (LangSmith / callbacks)
         ▲                    │ bad answers, thumbs-down,
         │                    │ slow runs, new question types
         │                    ▼
      deploy            golden dataset (inputs + references)
         ▲                    │
         │                    ▼
   CI gate passes ◄── offline eval: run system, score metrics,
                      compare with baseline
```

## Callbacks: the hook under everything

Every LangChain runnable emits events: `on_chain_start`, `on_chat_model_start`, `on_llm_end`, `on_retriever_end`, `on_tool_start`, `on_llm_error` and so on. A callback handler subclasses `BaseCallbackHandler` and overrides the ones it needs. Each event carries a `run_id` and `parent_run_id`, which is how LangSmith rebuilds the tree.

Where you attach a handler matters:

- `config={"callbacks": [h]}` at invoke time propagates to every child run. Use this for request-scoped tracing.
- A constructor argument (`ChatOpenAI(callbacks=[h])`) applies only to that object.

Handler errors are logged and swallowed by default (`raise_error = False`), so a broken metrics handler does not take down a request. In async code, use `AsyncCallbackHandler` so handlers do not block the event loop.

This handler records latency, tokens and cost per model call, along with the request's tags and metadata:

{% raw %}
```python
import time
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler, get_usage_metadata_callback
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.outputs import LLMResult
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

PRICE_PER_1M = {"input": 0.40, "output": 1.60}  # example rates in USD, not a quote

class CostLatencyHandler(BaseCallbackHandler):
    """Collects one record per model call: latency, tokens, cost, tags, metadata."""

    def __init__(self) -> None:
        self.started: dict[UUID, tuple[float, list, dict]] = {}
        self.records: list[dict] = []

    def on_chat_model_start(self, serialized, messages, *, run_id, tags=None,
                            metadata=None, **kwargs):
        self.started[run_id] = (time.perf_counter(), tags or [], metadata or {})

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        t0, tags, metadata = self.started.pop(run_id)
        usage = response.generations[0][0].message.usage_metadata or {}
        tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        self.records.append({
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_usd": (tin * PRICE_PER_1M["input"] + tout * PRICE_PER_1M["output"]) / 1e6,
            "tags": tags,
            "entity": metadata.get("entity"),
        })

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.started.pop(run_id, None)
        self.records.append({"error": type(error).__name__})

def fake_retrieve(q: str) -> dict:
    time.sleep(0.01)
    return {"question": q, "context": "Vendor payment terms are net 45 days."}

model = GenericFakeChatModel(messages=iter([AIMessage(
    content="Net 45 days [S1].",
    usage_metadata={"input_tokens": 812, "output_tokens": 9, "total_tokens": 821},
    response_metadata={"model_name": "gpt-4.1-mini"},  # what a real provider sets
)]))
prompt = ChatPromptTemplate.from_template("Context: {context}\nQuestion: {question}")
chain = (RunnableLambda(fake_retrieve).with_config(run_name="retrieve")
         | prompt | model | StrOutputParser()).with_config(run_name="policy_rag")

if __name__ == "__main__":
    handler = CostLatencyHandler()
    with get_usage_metadata_callback() as usage_cb:   # built-in token totals
        answer = chain.invoke(
            "What are vendor payment terms?",
            config={"callbacks": [handler],           # propagates to every child run
                    "tags": ["rag", "prod"],
                    "metadata": {"entity": "IN01", "prompt_version": "v7"}},
        )
    rec = handler.records[0]
    assert answer == "Net 45 days [S1]."
    assert rec["input_tokens"] == 812 and rec["entity"] == "IN01"
    assert "rag" in rec["tags"] and rec["latency_ms"] >= 0
    assert usage_cb.usage_metadata["gpt-4.1-mini"]["total_tokens"] == 821
    print(rec)
```
{% endraw %}

In a FastAPI service, push these records to Prometheus or your log pipeline, labeled by route, model and entity. Token usage comes from `AIMessage.usage_metadata`, which the major provider integrations fill in. When streaming, check that usage still arrives (for `ChatOpenAI`, the `stream_usage` option controls it); otherwise your cost numbers silently drop to zero.

## Tags, metadata and run names

Tags and metadata ride along in the config and are inherited by child runs. **Tags** are short filter labels (`prod`, `rag`, `experiment-b`; LangChain adds its own such as `seq:step:3`). **Metadata** holds facts about the request: `entity`, `prompt_version`, `model`, `retriever_k`, a pseudonymous user id, never raw account numbers. **run_name** (via `with_config`) makes a trace read "retrieve → policy_rag" instead of "RunnableLambda → RunnableSequence". Consistent metadata is what makes "p95 latency for IN01 on prompt v7" a single filter later.

## LangSmith tracing

LangSmith is LangChain's hosted (or self-hosted) tracing and evaluation platform. Tracing turns on with environment variables; no code changes for LangChain or LangGraph runs. Illustrative:

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY="<from your secrets manager>"
export LANGSMITH_PROJECT=finance-rag-staging
export LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com  # EU region or self-hosted URL
export LANGSMITH_HIDE_INPUTS=true            # send run structure, not prompt text
export LANGSMITH_HIDE_OUTPUTS=true
export LANGSMITH_TRACING_SAMPLING_RATE=0.2   # trace 20 percent of requests
```

Code outside LangChain, such as a SQL lookup against the ledger or a pandas step, joins the same trace when you decorate it with `@traceable(run_type="tool", name="gl_balance_lookup")` from `langsmith`.

For finance clients, decide early: self-hosted or region-pinned SaaS, masking (`Client(hide_inputs=fn)` takes a function that redacts instead of hiding everything), and trace retention that matches chat retention.

## Datasets and evaluators in LangSmith

A dataset is a list of examples with `inputs` and reference `outputs`. An evaluator is a function that scores one run. `Client.evaluate` runs your target over the dataset, applies evaluators, and stores the result as an experiment you can compare side by side with earlier ones. Illustrative:

{% raw %}
```python
from langsmith import Client

client = Client()
ds = client.create_dataset("finance-policy-qa-v1")
client.create_examples(dataset_id=ds.id, examples=[
    {"inputs": {"question": "Who approves invoices above INR 5,00,000?", "entity": "IN01"},
     "outputs": {"source": "vendor-payments.md",
                 "answer": "The cost center owner and the finance controller."}},
])

def cites_expected_source(outputs: dict, reference_outputs: dict) -> bool:
    return reference_outputs["source"] in outputs["sources"]

client.evaluate(
    lambda inputs: rag.invoke(inputs),     # the chain from the RAG note
    data="finance-policy-qa-v1",
    evaluators=[cites_expected_source],
    experiment_prefix="k4-threshold-0.2",
    max_concurrency=4,
)
```
{% endraw %}

Evaluators can be code (regex, citation check) or LLM-as-judge. LangSmith also runs online evaluators on sampled production traces, and annotation queues let a finance SME label answers that become new examples. `@pytest.mark.langsmith` logs pytest cases as an experiment.

## RAGAS metrics

RAGAS is an open-source library of RAG metrics, most of them computed with an LLM judge. Know the four core ones precisely, because interviewers ask what each one can and cannot detect:

| Metric | Inputs | How it is computed | Low score means |
|---|---|---|---|
| Faithfulness | question, response, retrieved contexts | An LLM splits the response into atomic claims and checks each against the contexts. Score = supported claims / total claims. | The answer states things the sources do not support |
| Answer (response) relevancy | question, response | An LLM writes N questions (default 3) that the response would answer; score = mean cosine similarity between their embeddings and the real question's. Noncommittal answers score 0. | The answer drifts, is incomplete or hedges |
| Context precision | question, retrieved contexts, reference (or response) | Each retrieved chunk is judged relevant or not; score = mean of precision@k over the ranks k that hold relevant chunks. Rank-aware. | Relevant chunks exist but sit below irrelevant ones |
| Context recall | retrieved contexts, reference | The reference answer is split into claims; score = claims attributable to the contexts / total claims. | Retrieval missed information needed for the answer |

Two consequences: faithfulness says nothing about correctness (a faithful answer from the wrong policy version scores 1.0), and context recall needs a human-written reference, so it only runs offline on a golden set.

A worked precision example: with 3 retrieved chunks where only the third is relevant, precision@3 = 1/3, so context precision = 0.33. Move that chunk to rank 1 and it becomes 1.0. That is why a reranker shows up in context precision first.

Illustrative (RAGAS 0.2/0.3 style API; names have changed across releases, for example `answer_relevancy` became `ResponseRelevancy`, so pin the version and check its docs):

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas import EvaluationDataset, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (Faithfulness, LLMContextPrecisionWithReference,
                           LLMContextRecall, ResponseRelevancy)

dataset = EvaluationDataset.from_list([{
    "user_input": "What are vendor payment terms?",
    "retrieved_contexts": ["Vendor payment terms are net 45 days from the invoice date."],
    "response": "Net 45 days from the invoice date [S1].",
    "reference": "Net 45 days.",
}])
result = evaluate(
    dataset=dataset,
    metrics=[Faithfulness(), ResponseRelevancy(),
             LLMContextPrecisionWithReference(), LLMContextRecall()],
    llm=LangchainLLMWrapper(ChatOpenAI(model="gpt-4.1", temperature=0)),
    embeddings=LangchainEmbeddingsWrapper(OpenAIEmbeddings(model="text-embedding-3-small")),
)
scores = result.to_pandas()   # one row per sample, one column per metric
```

## DeepEval

DeepEval packages similar metrics as pytest-style assertions: `FaithfulnessMetric`, `AnswerRelevancyMetric`, `ContextualPrecisionMetric`, `ContextualRecallMetric`, `HallucinationMetric` (checks against a supplied ground-truth `context`, not the retrieved one) and `GEval` for custom rubrics. Each has a threshold and returns a reason. Illustrative:

```python
from deepeval import assert_test
from deepeval.metrics import FaithfulnessMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

amounts_match = GEval(
    name="Amounts match sources",
    criteria="Every amount, date and percentage in the output appears in the retrieval context.",
    evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.RETRIEVAL_CONTEXT],
    threshold=0.8,
)

def test_payment_terms():
    case = LLMTestCase(
        input="What are vendor payment terms?",
        actual_output=rag.invoke({"question": "What are vendor payment terms?",
                                  "entity": "IN01"})["answer"],
        retrieval_context=["Vendor payment terms are net 45 days from the invoice date."],
    )
    assert_test(case, [FaithfulnessMetric(threshold=0.9), amounts_match])
```

Run with `deepeval test run tests/evals/test_rag.py`. RAGAS suits notebook and dataframe analysis; DeepEval suits a pytest suite. Both call a judge model, so both cost money and vary between runs.

## Your own harness: pandas plus an LLM judge

You should be able to build the core yourself, and your own checks catch finance failures generic metrics miss: a wrong number, a missing refusal, the wrong policy cited. Deterministic checks first; the judge covers what code cannot.

```python
import re

import pandas as pd
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

REFUSAL = "I could not find this in the policies you have access to."

golden = pd.DataFrame([
    {"id": "q1", "category": "approvals", "should_refuse": False,
     "question": "Who approves invoices above INR 5,00,000?",
     "expected_source": "vendor-payments.md",
     "reference": "The cost center owner and the finance controller."},
    {"id": "q2", "category": "terms", "should_refuse": False,
     "question": "What are vendor payment terms?",
     "expected_source": "vendor-payments.md", "reference": "Net 45 days."},
    {"id": "q4", "category": "out_of_scope", "should_refuse": True,
     "question": "What is the parental leave policy?",
     "expected_source": None, "reference": REFUSAL},
])

# Stand-in for the system under test. In your project: rag.invoke(...).
CANNED = {
    "q1": ("Two approvals: the cost center owner and the finance controller [S1].",
           ["vendor-payments.md"], ["Invoices above INR 5,00,000 need two approvals: "
                                    "the cost center owner and the finance controller."]),
    "q2": ("Terms are net 60 days [S1].",  # wrong number: a hallucination
           ["vendor-payments.md"], ["Vendor payment terms are net 45 days."]),
    "q4": (REFUSAL, [], []),
}

def run_system(row) -> dict:
    answer, sources, contexts = CANNED[row.id]
    return {"answer": answer, "sources": sources, "contexts": contexts}

NUM = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")  # 5,00,000 / 6,000 / 12.5

def numbers_grounded(answer: str, contexts: list[str]) -> bool:
    """Every number in the answer must appear in the retrieved text."""
    answer = re.sub(r"\[S\d+\]", "", answer)  # citation markers are not claims
    # Substring match is enough for a gate; tokenize numbers for production.
    return all(n in " ".join(contexts) for n in NUM.findall(answer))

class Verdict(BaseModel):
    score: float = Field(ge=0, le=1)
    reason: str

JUDGE_SYSTEM = ("You grade answers to finance policy questions. Compare the "
                "answer with the reference. Return only JSON with keys score "
                "(1 = same meaning, 0 = wrong or missing) and reason.")

def judge(llm, question: str, reference: str, answer: str) -> Verdict:
    msg = llm.invoke([SystemMessage(JUDGE_SYSTEM), HumanMessage(
        f"Question: {question}\nReference: {reference}\nAnswer: {answer}")])
    return Verdict.model_validate_json(msg.content)

def evaluate(golden: pd.DataFrame, judge_llm) -> pd.DataFrame:
    rows = []
    for row in golden.itertuples():
        out = run_system(row)
        refused = out["answer"] == REFUSAL
        verdict = judge(judge_llm, row.question, row.reference, out["answer"])
        rows.append({
            "id": row.id,
            "category": row.category,
            "refusal_ok": refused == row.should_refuse,
            "source_hit": refused if row.should_refuse
                          else row.expected_source in out["sources"],
            "numbers_grounded": numbers_grounded(out["answer"], out["contexts"]),
            "judge_score": verdict.score,
            "judge_reason": verdict.reason,
        })
    return pd.DataFrame(rows).set_index("id")

METRICS = ["refusal_ok", "source_hit", "numbers_grounded", "judge_score"]

def regressions(summary: pd.Series, baseline: dict, tolerance: float = 0.02) -> dict:
    return {m: (baseline[m], round(float(summary[m]), 3))
            for m in baseline if summary[m] < baseline[m] - tolerance}

if __name__ == "__main__":
    # Real judge (illustrative): init_chat_model("openai:gpt-4.1", temperature=0)
    fake_judge = FakeListChatModel(responses=[
        '{"score": 1.0, "reason": "Names both approvers."}',
        '{"score": 0.0, "reason": "Says 60 days; reference says 45."}',
        '{"score": 1.0, "reason": "Correct refusal."}',
    ])
    report = evaluate(golden, fake_judge)
    summary = report[METRICS].mean()
    by_category = report.groupby("category")[METRICS].mean()
    baseline = {"refusal_ok": 1.0, "source_hit": 1.0,
                "numbers_grounded": 1.0, "judge_score": 0.9}

    assert not report.loc["q2", "numbers_grounded"]           # caught without an LLM
    assert report.loc["q4", "refusal_ok"]
    assert set(regressions(summary, baseline)) == {"numbers_grounded", "judge_score"}
    print(by_category)
    print(report.loc[report.judge_score < 1, ["judge_reason"]])
```

`numbers_grounded` caught the hallucinated "60 days" with no model call. The most damaging finance hallucinations are numbers, dates and named approvers, and code can check them. For the judge: use a stronger model than the one under test at temperature 0, an anchored rubric, a reason with every score, and Pydantic parsing so malformed output fails loudly. Before trusting it, have a finance SME label 50 answers and measure agreement. Always report per category (`groupby`); an average hides a broken "out of scope" slice.

## Regression testing in CI

Split tests by cost. Unit tests use fake models and keyword embeddings, run on every push, and check wiring, the refusal path, citation validation and access filters. The offline eval uses the real model and judge, runs on PRs that touch prompts, retrieval or model config (and nightly), and compares golden-set metrics with a committed baseline, with a tolerance because judge scores vary between runs. Online evaluators then watch sampled production traces. Illustrative pytest:

```python
# tests/evals/test_regression.py
import json
from pathlib import Path

from evals.harness import METRICS, evaluate, load_golden, real_judge

def test_no_quality_regression():
    report = evaluate(load_golden("evals/golden.csv"), real_judge())
    summary = report[METRICS].mean()
    baseline = json.loads(Path("evals/baseline.json").read_text())
    failed = {m: (baseline[m], round(summary[m], 3))
              for m in baseline if summary[m] < baseline[m] - 0.02}
    assert not failed, f"regressed (baseline, now): {failed}"
```

And the workflow, path-filtered so the paid suite only runs when it can change. Illustrative:

{% raw %}
```yaml
name: llm-evals
on:
  pull_request:
    paths: ["app/prompts/**", "app/retrieval/**", "app/llm_config.py", "evals/**"]
jobs:
  offline-eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: pytest tests/evals -q
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          LANGSMITH_API_KEY: ${{ secrets.LANGSMITH_API_KEY }}
```
{% endraw %}

When a change improves a metric on purpose, update `baseline.json` in the same PR so the reviewer sees the new numbers.

## Reducing hallucinations, driven by eval results

Do not apply fixes blindly; read which metric moved and fix that layer. Then rerun the whole suite, since fixes trade off (a stricter threshold lifts faithfulness and raises wrong refusals).

| Eval signal | Likely cause | Fix |
|---|---|---|
| Low faithfulness, good context recall | Model adds facts beyond the sources | "Use only sources" prompt with exact refusal text, citation validation in code, temperature 0, fewer and cleaner chunks |
| Low context recall | Retriever misses the needed chunk | Structure-aware chunking, higher `k`, multi-query, hybrid search |
| Low context precision | Noise ranked above signal | Reranker, metadata filters, MMR, lower `k` |
| Low answer relevancy | Answer hedges or drifts | Answer-first instruction, query rewriting, shorter context |
| Numbers not grounded | Model recalls or computes figures | Compute with SQL or pandas tools, quote figures from sources, numeric gate before returning |
| Answers when it should refuse | Threshold too loose | Out-of-scope examples in the golden set, tune threshold per category |
| Refuses answerable questions | Threshold too strict or recall low | Lower threshold, fix retrieval first |

More patterns in [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

## Coming from Django

| Django world | LLM world |
|---|---|
| Sentry performance traces, Debug Toolbar | LangSmith traces with nested runs |
| Middleware that times requests | Callback handler on model and chain events |
| `assertNumQueries` | Token and cost budget assertions per chain |
| Fixtures and factory_boy | Golden dataset with references |

The new part is non-determinism: you assert on aggregates with tolerances, not on exact strings.

## Common mistakes

- **No golden set, only vibes.** Start with 30 to 50 real questions with references and expected sources, including out-of-scope ones.
- **One overall number.** Slice by category, entity and question type; regressions hide in averages.
- **Trusting an uncalibrated judge.** Measure judge agreement with human labels first.
- **Reading faithfulness as correctness.** Faithful to the wrong context is still wrong; pair it with context recall and reference-based checks.
- **Exact-match gates on LLM output.** Gate on thresholds with tolerance and keep a committed baseline.
- **Tracing raw financial data to a SaaS.** Mask inputs and outputs, pin the region or self-host, and set trace retention.

## Practice tasks

1. **Latency breakdown.** Extend the handler with `on_chain_start` and `on_chain_end` to record each named step. Done: a dict like `{"retrieve": ms, "policy_rag": ms}` for one request, asserted to contain both keys.
2. **Golden set from CSV.** Move the dataset to a CSV with 15 questions across 4 categories, including 3 out-of-scope. Done: `evaluate` reads it and prints the per-category table.
3. **Context precision by hand.** Implement rank-aware context precision from per-chunk relevance labels (lists of 0 and 1). Done: tests for `[0, 0, 1]` = 0.33 and `[1, 0, 1]` = 0.83.
4. **Judge calibration.** Label 20 answers yourself, script a fake judge with some disagreements, and compute agreement and Cohen's kappa with pandas. Done: a printed agreement rate and a list of disagreements.
5. **CI gate.** Wire the harness into a pytest test with a baseline JSON. Done: the test fails when you change one canned answer's number, and passes after you revert it.

## Interview questions

**What is the difference between faithfulness and answer correctness?**
Faithfulness measures whether the answer's claims are supported by the retrieved context. Correctness compares with a reference answer. A faithful answer can be wrong if retrieval returned the wrong document.

**How is RAGAS context precision computed?**
Each retrieved chunk is judged relevant or not. For every rank that holds a relevant chunk, compute precision at that rank; average those values. It rewards putting relevant chunks first.

**Which metrics need a reference answer?**
Context recall and reference-based context precision. Faithfulness and answer relevancy do not, which is why they can run on production traces.

**How do you add tracing to a LangChain app?**
Set `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT`. LangChain and LangGraph runs are traced automatically; wrap other code with `@traceable`. For in-house telemetry, write a callback handler and pass it in the invoke config.

**How do you keep an LLM judge honest?**
Stronger model, temperature 0, explicit rubric, reasons with scores, structured output, and measured agreement with human labels on a sample. Re-check when you change the judge model.

**How would you catch hallucinated numbers in a finance assistant?**
A deterministic check that every number and date in the answer appears in the cited sources or in tool results, run before the answer is returned and in the eval suite. Calculations go to SQL or pandas tools, not the model.

**How do you prevent quality regressions when someone edits a prompt?**
Prompts are versioned in Git; a path-filtered CI job runs the golden set with the real model and judge, compares with a committed baseline with tolerance, and blocks the merge on regression.

## Further reading

- LangChain docs: "Callbacks" and the `BaseCallbackHandler` reference
- LangSmith docs: "Tracing quick start", "Evaluation concepts", "Run an evaluation with pytest", "Mask inputs and outputs"
- RAGAS docs: "Metrics" (Faithfulness, Response Relevancy, Context Precision, Context Recall)
- DeepEval docs: "Metrics introduction" and "G-Eval"
- Next: [LangGraph core concepts]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }})
