---
title: Observability and tracing
section: llm
order: 51
phase: "Phase 8 · Production"
summary: Trace every LLM call, tool call and retrieval step, log what matters, follow OpenTelemetry GenAI conventions, set SLOs, and turn traces into eval data.
---

LLM systems fail quietly. A wrong answer, a retrieval that missed the right document, or an agent that looped six times all return HTTP 200. Request logs and CPU graphs cannot tell you what the model saw, which model answered, why it took nine seconds or what it cost. Tracing can: one trace per user request, with a span for every LLM call, retrieval, rerank and tool call, carrying the prompt version, model, tokens, latency, cost and finish reason. The same traces feed dashboards, SLOs, cost attribution, debugging and, most valuably, the evaluation datasets that keep quality from regressing. General observability practice is in [Observability]({{ '/production/observability/' | relative_url }}); this note covers what is specific to LLM systems.

## How it works

### The shape of a trace

```text
trace 4bf9…  POST /ask  tenant=acme  feature=rag_answer      2,910 ms
├─ guardrail.input           classifier              35 ms
├─ chat small-model          query rewrite          410 ms  in=310 out=24
├─ retrieve                  hybrid, top_k=20       120 ms
│  ├─ embeddings embed-model                         40 ms
│  └─ db.query pgvector + full-text                  70 ms
├─ rerank                    cross-encoder, top 6   150 ms
├─ chat large-model          answer               2,050 ms  TTFT 640 ms
│     in=5,820 (cached 2,000) out=312  finish=tool_calls
├─ execute_tool get_order    order-service           95 ms
└─ chat large-model          final answer           (streamed)
```

A trace is a tree of spans sharing one trace id. Each span has a name, start time, duration, status, parent and key-value attributes. Context propagates through `contextvars` in-process and through the W3C `traceparent` header across services, so a Celery task or downstream service joins the same trace.

### What to trace

| Span | Key attributes |
|---|---|
| Request (root) | Route, tenant, user (pseudonymous), feature, session or conversation id |
| LLM call | Provider, requested and response model, prompt id and version, params, tokens, TTFT, finish reason, cost, retries, fallback rung |
| Embedding | Model, input count, tokens |
| Retrieval | Index and version, query (or hash), filters, top_k, returned ids and scores |
| Rerank | Model, candidates in and out, scores |
| Tool call | Tool name, call id, argument summary, outcome, duration, idempotency key |
| Agent step and run | Step number, decision, total steps, total tokens and cost, termination reason |
| Guardrail | Check name, verdict, score |
| Cache lookup | Layer, hit or miss |

### What to log on every LLM call

- **Prompt identity:** template id and version, plus a hash of the rendered prompt. Store full content separately under access control (see PII below).
- **Model:** provider, requested model and the model the response reports.
- **Parameters:** temperature, `max_tokens`, reasoning effort, tool names offered, response schema name.
- **Tokens:** input, cached input, output, reasoning.
- **Latency:** TTFT, total, queue or retry time.
- **Cost:** computed from usage and a versioned price table.
- **Outcome:** finish reason, error type, retry count, fallback rung, validation result.
- **Context:** trace and span id, tenant, feature, response id from the provider (for support tickets).

A single structured log line per call, with the same fields as the span, is a useful complement: cheap to keep at 100%, easy to aggregate in SQL (see [Latency and cost engineering]({{ '/llm/latency-and-cost/' | relative_url }})).

### OpenTelemetry GenAI semantic conventions

OpenTelemetry defines semantic conventions for generative AI so traces from different SDKs, frameworks and vendors look alike. As of 2025–2026 they are still marked as in development, and some names have changed between versions, so pin the version your instrumentation emits. At a high level:

- **Span names** follow `{operation} {model}`, such as `chat large-model` or `embeddings embed-model`.
- **Operation names** (`gen_ai.operation.name`) include `chat`, `text_completion`, `embeddings`, `execute_tool` and `invoke_agent`.
- **Request attributes:** `gen_ai.request.model`, `gen_ai.request.temperature`, `gen_ai.request.max_tokens`.
- **Response attributes:** `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`.
- **Usage:** `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`.
- **Provider:** `gen_ai.system` in earlier versions, renamed `gen_ai.provider.name` later.
- **Tools:** `gen_ai.tool.name` and `gen_ai.tool.call.id` on tool execution spans.
- **Metrics:** `gen_ai.client.token.usage` and `gen_ai.client.operation.duration` on the client side; server-side metrics such as time to first token for serving engines.
- **Content** (prompts and completions) is opt-in, because it is large and sensitive; its representation has changed across versions.

Instrumentation libraries such as OpenLLMetry and OpenInference emit these spans for popular SDKs and frameworks automatically. Manual instrumentation with the OpenTelemetry API is short (illustrative):

```python
from opentelemetry import trace

tracer = trace.get_tracer("llm.gateway")

def chat(req):
    with tracer.start_as_current_span(f"chat {req.model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", req.model)
        span.set_attribute("gen_ai.request.max_tokens", req.max_tokens)
        span.set_attribute("app.prompt.version", req.prompt_version)
        resp = adapter.complete(req)
        span.set_attribute("gen_ai.response.model", resp.model)
        span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
        span.set_attribute("gen_ai.response.finish_reasons", [resp.finish_reason])
        return resp
```

Put your own attributes (tenant, feature, prompt version, cost) under an application namespace such as `app.` so they never collide with future convention names.

## Tools

| Tool | Deployment | Integration | Strengths |
|---|---|---|---|
| Langfuse | Open source; self-host or cloud | SDKs and decorators, OpenTelemetry ingestion, framework integrations | Traces, prompt management with versions, datasets, evals, cost tracking |
| LangSmith | Managed by LangChain; self-hosted option for enterprise | SDK (`@traceable`), native LangChain and LangGraph tracing, OpenTelemetry | Traces, datasets and experiments, annotation queues, online evaluators |
| Arize Phoenix | Open source; local or self-hosted | OpenTelemetry with OpenInference instrumentation | Local-first tracing, evals, retrieval analysis |
| Helicone | Open source; cloud or self-host | Proxy: change the base URL, add a header | Minimal code change, request logs, cost, caching; sits in the request path |

General observability platforms also ingest OpenTelemetry GenAI spans, so you can keep LLM traces next to the rest of your system. Choosing: self-hosting matters when prompts contain regulated data; OpenTelemetry-native ingestion keeps you portable; a proxy-based tool is fastest to adopt but adds a hop to every call; prompt management and dataset features matter if the same tool will drive your eval loop.

## Dashboards and SLOs

Dashboards per feature and model:

| Category | Signals |
|---|---|
| Latency | TTFT p50/p95/p99, E2E p95, tokens per second, queue time |
| Errors | Error rate by class (timeout, 429, 5xx, validation, refusal), fallback share, breaker state |
| Cost | Cost per request (mean and p95), per tenant, per day; tokens per request; cached-token ratio |
| Output health | `finish_reason = length` rate, schema-validation failures, empty outputs, refusal rate |
| Agents | Steps per run, tool error rate, runs hitting caps |
| Quality | Thumbs-down rate, online eval scores, escalations to humans |

Example SLOs, measured over 28 days:

- 99% of chat requests have TTFT under 2 seconds.
- 99.5% of requests succeed (no error, no empty or invalid output).
- p95 cost per request for `rag_answer` stays under a budget, tracked like an SLO with alerts.

Alert on error-budget burn rate rather than raw thresholds: page when the budget is burning fast over a short window (the Google SRE Workbook's example pages at 14.4× burn over an hour), open a ticket for slow burns. A PromQL panel for p95 TTFT by model, assuming you export a histogram:

```text
histogram_quantile(0.95,
  sum by (le, model) (rate(llm_ttft_seconds_bucket[5m])))
```

Quality does not show up in latency or error metrics. Sample traces into online evaluators (see [Online evaluation]({{ '/llm/online-evaluation/' | relative_url }})) and chart their scores alongside the operational signals.

## PII in traces

Prompts contain user messages, retrieved documents and tool results, so traces are a copy of your most sensitive data.

- Capture content opt-in per feature; keep metadata (tokens, latency, model, finish reason) everywhere.
- Redact before export: regexes for emails, phone numbers and card numbers, plus an NER-based detector such as Microsoft Presidio for names and addresses. Redaction is best-effort; treat redacted traces as still sensitive.
- Pseudonymize identifiers: hash user ids with a keyed hash so traces can be joined without exposing identity.
- Separate retention: content for days or weeks, metadata for months.
- Restrict access to content and audit who reads it.
- Make deletion requests reach the trace store, and keep traces in the region your data agreements require (see [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).
- Self-host the trace store when prompts contain regulated data.

## Sampling

- **Metrics and per-call usage rows are never sampled.** Cost and SLO numbers must be computed from complete data.
- **Head sampling** decides at the start of a trace (for example 10%), deterministically from the trace id so every service agrees. Cheap, but it drops the rare interesting traces.
- **Tail sampling** decides after the trace completes: keep all errors, slow traces, expensive traces, traces with negative feedback, plus a random baseline. It needs buffering (an OpenTelemetry Collector tail-sampling processor, or the vendor's backend).
- Sample content more aggressively than metadata, and sample per tenant if one tenant dominates traffic.

## Linking traces to eval datasets

The highest-value use of traces is turning production failures into tests:

```text
production traces ──► feedback: thumbs, edits, escalations
       │                         │
       │ online evaluators       ▼
       └─(sampled judges)──► scores on traces ──► triage queue
                                                       │
                       curated failures (redacted) ◄───┘
                                │
                                ▼
       regression dataset: input, context, expected output
       or criteria, source trace id
                                │
                                ▼
       offline eval on every prompt, model or retrieval change
                                │
                                ▼
                       deploy gate (see LLMOps)
```

- Attach feedback and evaluator scores to the trace id, so you can filter traces by "bad answer" and see exactly what the model saw.
- Promote reviewed failures into a regression dataset with the context needed to replay them (retrieved passages, tool results), and keep the source trace id for provenance.
- Record prompt version and model on every trace so you can compare versions online, and replay the dataset offline before each change (see [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) and [LLMOps lifecycle]({{ '/llm/llmops-lifecycle/' | relative_url }})).

## In practice

### A minimal tracer with spans, redaction and tail sampling

A stdlib tracer that shows the mechanics: nested spans via `contextvars`, GenAI-style attributes, content redaction before export, complete metrics, and a tail-sampling rule that keeps every failed trace even at 0% baseline sampling.

```python
import contextvars
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager

_current = contextvars.ContextVar("current_span", default=None)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CONTENT_KEYS = {"app.prompt", "app.completion"}

class Tracer:
    def __init__(self, sample_rate: float, capture_content: bool = False):
        self.sample_rate, self.capture_content = sample_rate, capture_content
        self.exported: list[dict] = []          # stand-in for an exporter
        self.metrics: Counter = Counter()       # never sampled
        self._pending: dict[str, list[dict]] = {}

    def _sampled(self, trace_id: str) -> bool:
        # deterministic, so every service makes the same decision
        return int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16) / 2**32 < self.sample_rate

    @contextmanager
    def span(self, name: str, **attrs):
        parent = _current.get()
        span = {"trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "parent_id": parent["span_id"] if parent else None,
                "name": name, "attrs": dict(attrs), "status": "ok"}
        token, start = _current.set(span), time.perf_counter()
        try:
            yield span
        except Exception as e:
            span["status"] = "error"
            span["attrs"]["error.type"] = type(e).__name__
            raise
        finally:
            span["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            _current.reset(token)
            self._finish(span)

    def _finish(self, span: dict) -> None:
        a = span["attrs"]
        if "gen_ai.operation.name" in a:
            self.metrics["llm.calls"] += 1
            self.metrics["llm.errors"] += span["status"] == "error"
            self.metrics["llm.output_tokens"] += a.get("gen_ai.usage.output_tokens", 0)
        for k in CONTENT_KEYS & a.keys():
            if self.capture_content:
                a[k] = EMAIL.sub("[email]", a[k])
            else:
                del a[k]
        self._pending.setdefault(span["trace_id"], []).append(span)
        if span["parent_id"] is None:          # root ended: tail-sampling decision
            spans = self._pending.pop(span["trace_id"])
            if self._sampled(span["trace_id"]) or any(s["status"] == "error" for s in spans):
                self.exported.extend(spans)

def fake_chat(tracer: Tracer, model: str, prompt: str, fail: bool = False) -> str:
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model,
             "app.prompt": prompt, "app.prompt.version": "answer_v7"}
    with tracer.span(f"chat {model}", **attrs) as s:
        if fail:
            raise TimeoutError("no first token")
        text = "Refunds take five days."
        s["attrs"].update({"gen_ai.response.model": model + "-2025-01",
                           "gen_ai.usage.input_tokens": len(prompt.split()),
                           "gen_ai.usage.output_tokens": len(text.split()),
                           "gen_ai.response.finish_reasons": ["stop"],
                           "app.completion": text})
        return text

def handle(tracer: Tracer, question: str, fail: bool = False) -> str:
    with tracer.span("POST /ask", **{"app.tenant": "acme", "app.feature": "rag_answer"}):
        with tracer.span("retrieve", **{"app.top_k": 5}) as r:
            r["attrs"]["app.doc_ids"] = ["d1", "d7"]
        return fake_chat(tracer, "large-model", f"Context: d1 d7\nQ: {question}", fail)

if __name__ == "__main__":
    t = Tracer(sample_rate=1.0, capture_content=True)
    handle(t, "Refund for jane@example.com?")
    by_name = {s["name"]: s for s in t.exported}
    root, llm = by_name["POST /ask"], by_name["chat large-model"]
    assert len({s["trace_id"] for s in t.exported}) == 1
    assert by_name["retrieve"]["parent_id"] == root["span_id"] == llm["parent_id"]
    assert "[email]" in llm["attrs"]["app.prompt"]
    assert "jane@" not in json.dumps(t.exported)            # redacted before export
    assert llm["attrs"]["gen_ai.usage.output_tokens"] == 4

    t2 = Tracer(sample_rate=0.0)        # no baseline sampling, no content
    handle(t2, "fine question")
    try:
        handle(t2, "slow question", fail=True)
    except TimeoutError:
        pass
    assert len({s["trace_id"] for s in t2.exported}) == 1   # only the failed trace
    by_status = {s["name"]: s["status"] for s in t2.exported}
    assert by_status["chat large-model"] == "error" and by_status["retrieve"] == "ok"
    assert all(not (CONTENT_KEYS & s["attrs"].keys()) for s in t2.exported)
    assert t2.metrics["llm.calls"] == 2 and t2.metrics["llm.errors"] == 1
    print(json.dumps(llm["attrs"], indent=2))
```

In a real system the exporter is an OpenTelemetry SDK `BatchSpanProcessor` sending OTLP to a Collector, which applies redaction and tail sampling before forwarding to your backend.

## Pitfalls

- **Logging only the requested model.** Aliases and fallbacks mean the serving model differs. Fix: record the response model and fallback rung.
- **No prompt version on traces.** You cannot tell which prompt produced a bad answer or compare versions. Fix: version every template and attach it to every LLM span.
- **Costs computed from sampled traces.** Totals are wrong by the sampling factor, or worse with tail sampling bias. Fix: unsampled usage rows and metrics.
- **Raw PII in a third-party trace store.** Fix: opt-in content capture, redaction before export, self-hosting for regulated data, retention limits.
- **Broken context propagation.** Background tasks and thread pools start new traces, so agent runs appear as fragments. Fix: propagate `traceparent` into task headers; copy `contextvars` into worker threads.
- **Streaming spans that end too early.** The span closes when the generator is created, not when the last token arrives, so latency looks tiny. Fix: end the span in the stream's completion path and record TTFT as an attribute or event.
- **Dashboards without quality signals.** Everything is green while answers get worse. Fix: online evaluator scores and feedback rates next to latency and errors.
- **Traces nobody reads.** Fix: a weekly triage of the worst-scored traces feeding the regression dataset.

## Checklist

- One trace per request; spans for every LLM call, retrieval, rerank, tool call, guardrail and agent step.
- LLM spans carry provider, requested and response model, prompt version, params, tokens (input, cached, output, reasoning), TTFT, cost, finish reason, retries and fallback rung.
- GenAI semantic convention names where they exist, `app.` names for your own; convention version pinned.
- Metrics and usage rows unsampled; traces head-sampled with tail rules for errors, slow, costly and negatively rated requests.
- Content capture opt-in, redacted before export, restricted and short-retention.
- SLOs on TTFT, success rate and cost per request, with burn-rate alerts.
- Feedback and eval scores attached to trace ids; reviewed failures promoted to regression datasets.

## Interview questions

**What would you put on a span for an LLM call?**
Provider, requested and response model, prompt template version, parameters, input, cached, output and reasoning tokens, TTFT and total latency, cost, finish reason, retry count, fallback rung, tenant and feature, and the provider response id. Content only when opted in and redacted.

**What are the OpenTelemetry GenAI semantic conventions?**
A shared vocabulary for spans and metrics from generative AI clients: span names like `chat {model}`, attributes such as `gen_ai.request.model`, `gen_ai.response.finish_reasons` and `gen_ai.usage.input_tokens`, operation names for chat, embeddings, tool execution and agents, and token-usage and duration metrics. They are still evolving, so pin a version.

**How do you sample traces without losing what matters?**
Never sample metrics or cost data. Head-sample a baseline deterministically by trace id, and tail-sample to keep every error, slow, expensive or negatively rated trace, which requires buffering in a collector or backend.

**How do you handle PII in LLM traces?**
Capture content opt-in, redact before export, pseudonymize identifiers, keep content on short retention with restricted access, honor deletion requests and region constraints, and self-host for regulated data.

**Which SLOs would you set for an LLM chat feature?**
TTFT p95 or "99% under 2 seconds", success rate including validation and empty-output failures, and cost per request tracked against a budget, with burn-rate alerts. Pair them with quality signals from online evals and feedback, since operational SLOs miss wrong answers.

**How do traces improve evaluation?**
They give real inputs with full context. Feedback and evaluator scores on traces surface failures; reviewed failures become regression cases with the context to replay them; prompt and model versions on traces enable online comparisons.

## Further reading

- OpenTelemetry semantic conventions for generative AI (opentelemetry.io documentation).
- Dapper, a Large-Scale Distributed Systems Tracing Infrastructure (Sigelman et al., 2010).
- The Site Reliability Workbook (Beyer et al., 2018), chapter "Alerting on SLOs".
- Langfuse, LangSmith and Arize Phoenix documentation on tracing, datasets and evaluations.
- OpenTelemetry Collector documentation: tail sampling processor.
