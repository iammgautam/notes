---
title: Multi-provider routing
section: llm
order: 47
phase: "Phase 8 · Production"
summary: Put LLM vendors behind one interface with a universal message format, per-role config, fallbacks, cascades, gateways and normalized streaming.
---

Depending on one LLM provider means inheriting its outages, rate limits, deprecations and price changes. A routing layer puts application code behind one internal interface and decides, per request, which provider and model serves it. Done well, it buys availability, cost control and the freedom to adopt a better model with a config change. Done badly, it becomes a lowest-common-denominator wrapper that hides quality differences between models and makes incidents harder to debug.

## How it works

### The abstraction boundary

```text
 app code ──► llm.complete(role="rag_answer", messages, tools, schema)
                   │
   ┌───────────────┴──────────────────────────────────────────┐
   │ router                                                   │
   │  1. resolve role → ordered candidates (config)           │
   │  2. check tenant budget and rate limits                  │
   │  3. pick candidate: health, quota, cost, latency         │
   │  4. adapter: universal format → vendor request           │
   │  5. timeouts, retries, breaker per (provider, model)     │
   │  6. normalize response, usage, finish reason, stream     │
   │  7. log role, provider, model, tokens, cost, latency     │
   └───────────────┬──────────────────────────────────────────┘
        ┌──────────┼───────────┬────────────────┐
        ▼          ▼           ▼                ▼
     OpenAI    Anthropic     Gemini     self-hosted (vLLM,
                                        OpenAI-compatible)
```

Three rules keep this boundary healthy:

- Application code names a **role** (`query_rewrite`, `rag_answer`), never a model.
- The router owns candidate choice, retries and fallbacks, in one layer.
- Adapters only translate formats and errors; no policy.

### A universal message format

Every vendor has the same idea (a list of role-tagged messages, optional tools, a completion) and different details. The differences you must normalize, as of 2025–2026:

| Concern | OpenAI Chat Completions | Anthropic Messages | Google Gemini |
|---|---|---|---|
| System prompt | A `system`/`developer` message | Top-level `system` field | `systemInstruction` |
| Roles | system, user, assistant, tool | user, assistant | user, model |
| Content | String or typed parts | String or content blocks | `parts` list |
| Tool call | `tool_calls[]`, arguments as a JSON string | `tool_use` block, `input` as an object | `functionCall` part, `args` object |
| Tool result | `role: "tool"` + `tool_call_id` | `tool_result` block in a user turn | `functionResponse` part |
| Output cap | Optional | `max_tokens` required | `maxOutputTokens` |
| Stop reasons | `stop`, `length`, `tool_calls`, `content_filter` | `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `refusal` | `STOP`, `MAX_TOKENS`, `SAFETY`, … |
| Usage | `prompt_tokens`, `completion_tokens` | `input_tokens`, `output_tokens`, cache fields | `usageMetadata` counts |

OpenAI's newer Responses API uses a different, item-based shape, so even "the OpenAI format" is two formats. Vendors also differ in structured-output parameters, accepted JSON Schema keywords, sampling parameters (some reasoning models reject `temperature`) and tokenizers, so the same text has different token counts per vendor.

Design the internal format around what you need, not around one vendor:

- **Messages**: roles `system`, `user`, `assistant`, `tool`; content as a list of typed parts (text, image, tool call, tool result).
- **Tools**: JSON Schema parameters, kept to the subset all your vendors accept.
- **Finish reasons**: a closed set such as `stop`, `length`, `tool_calls`, `filtered`, `unknown`.
- **Usage**: input, cached input, cache write, output and reasoning tokens.
- **Errors**: your own types (`RateLimited`, `Overloaded`, `ContextTooLong`, `ContentFiltered`, `BadRequest`, `AuthError`, `ProviderError`).
- **Capabilities**: a record per model (context window, max output, tools, parallel tool calls, structured outputs, vision, caching style, reasoning controls). The router filters candidates by what the role requires, so you keep features like prompt caching instead of hiding them.

> **Gotcha:** Reasoning blocks are vendor-specific (sometimes encrypted or signed) and server-side conversation state does not move between providers. When failing over mid-conversation, send the plain transcript.

### Route by role, not by model

Keep routing in versioned config that maps each role to evaluated candidates, parameters and timeouts. Model names below are placeholders:

```yaml
# llm_routes.yaml: loaded at startup, changed only behind the role's eval
roles:
  query_rewrite:
    candidates:
      - { provider: openai,    model: small-model-a, prompt: rewrite_v3 }
      - { provider: anthropic, model: small-model-b, prompt: rewrite_v3 }
    max_tokens: 200
    timeout_s: 8
  rag_answer:
    candidates:
      - { provider: anthropic, model: large-model-b, prompt: answer_v7b }
      - { provider: openai,    model: large-model-a, prompt: answer_v7a }
    requires: [streaming, prompt_caching]
    max_tokens: 1200
    timeout_s: 60
    eval_set: rag_answer_golden
  ticket_triage:
    cascade:
      small: { provider: self_hosted, model: open-8b-instruct }
      large: { provider: anthropic,   model: mid-model-b }
      accept: schema_valid_and_label_prob_gte_0.85
```

Note the per-candidate prompt version. Prompts tuned for one model often underperform on another, so a fallback candidate may need its own variant. Swapping a role's model is then a reviewed config change gated by the role's eval set (see [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }})).

### Fallbacks and failover

- Fail over on errors another candidate can fix: 429, 5xx, overloaded, timeouts, connection resets.
- Do not fail over on errors that will recur everywhere: malformed requests, schema errors in your tool definitions, auth failures (page someone). `ContextTooLong` is the exception: route it to a larger-context candidate or trim and retry.
- Every candidate must pass the role's eval. A fallback that silently answers worse can be more harmful than a clean error.
- The best fallback is the **same model on another platform** (for example a vendor's model through its own API and through a cloud platform such as Bedrock, Vertex AI or Azure). Behavior and prompts carry over; only the endpoint changes.
- Fallback in a stream is only safe before the first token reaches the client.
- Alert when a role's fallback share rises; quiet fallbacks hide outages and quality regressions.

### Choosing a candidate: cost, latency, quality

| Strategy | How it works | Use when | Trade-off |
|---|---|---|---|
| Static tiers | Small models for rewrite, classify, extract; large for final answers | Always; the first big win | Needs per-role evals |
| Cascade | Small model first, escalate when a check fails | Outputs are checkable | Escalated requests pay twice and wait longer |
| Learned router | Classifier predicts whether the cheap model suffices | High volume, labeled preferences | Training data, drift |
| Latency-aware | Prefer the healthy candidate with best rolling p95 TTFT | Interactive traffic | Can oscillate; add hysteresis |
| Weighted split | Send x% to a new candidate | Canary or A/B a model change | Needs online metrics |
| Hedged request | Send a backup after a deadline, keep the first answer | Tail latency matters more than cost | Extra tokens on hedged calls |

**Cascades** need a confidence signal. Ranked by usefulness:

| Signal | Example | Reliability |
|---|---|---|
| Validation | Schema valid, citations present, tool args valid | High, cheap |
| Label probability | Logprob of the chosen class (where exposed) | Good for classification |
| Verifier | Rules or a small judge model check the answer | Medium, adds a call |
| Agreement | Sample n times, check consistency | Good, costs n× |
| Self-reported confidence | "Rate your confidence 1–10" | Poorly calibrated |

Cascade economics: expected cost per request is `c_small + p_escalate × c_large`. That beats always using the large model while `p_escalate < 1 − c_small / c_large`. With a small model at 1/10 the price, the cascade wins on cost until 90% of requests escalate; the real limit is usually latency, since escalated requests take both calls. Start with static tiers plus validation-based cascades; learned routers (FrugalGPT, RouteLLM) earn their keep only at high volume.

### Rate limits across providers

- **Dimensions:** requests and tokens per minute (input and output), sometimes concurrency, scoped per model and per organization or project.
- **Signals:** HTTP 429 with `retry-after`, plus vendor headers for remaining quota and reset times (for example OpenAI's `x-ratelimit-remaining-tokens`). Some vendors separate "you are over quota" from "we are overloaded" (Anthropic returns 529 `overloaded_error` for the latter); the second is provider-wide, so fail over rather than wait.
- **Admission:** keep a client-side token bucket per (provider, model, key) set slightly below the quota. Reserve estimated tokens (input plus `max_tokens`) before sending and reconcile after; an oversized `max_tokens` can waste quota at admission.
- **Routing:** prefer candidates with headroom instead of waiting for 429s. Separate interactive and batch traffic into different keys or projects. Enforce per-tenant limits upstream so one tenant cannot drain the shared quota (token bucket code is in [Reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }})).

### Streaming normalization

All major vendors stream over Server-Sent Events, but the event shapes differ:

- **Chat Completions** sends unnamed `data:` chunks, each with `choices[0].delta`, ending with `data: [DONE]`. Usage arrives in a final chunk only if you request it with `stream_options: {"include_usage": true}`.
- **Anthropic** sends named events: `message_start` (with input usage), `content_block_start`, `content_block_delta` (`text_delta` or `input_json_delta`), `content_block_stop`, `message_delta` (stop reason and output usage), `message_stop`, plus `ping` and `error`.
- **Gemini** streams partial `GenerateContentResponse` objects.

Tool-call arguments arrive as partial JSON fragments keyed by an index; concatenate per call and parse only when complete. Errors can arrive mid-stream after an HTTP 200. Normalize at the adapter and expose one SSE contract to your frontend: `text_delta`, `tool_call`, `done` (finish reason, usage), `error`.

## Gateways: build or buy

| Option | Examples | Good for | Watch out for |
|---|---|---|---|
| In-process library | LiteLLM SDK, your own adapters | One service, full control, no extra hop | Policy duplicated across services |
| Self-hosted gateway | LiteLLM proxy, Portkey gateway | Many services: central keys, budgets, logs, routing | Another critical hop to operate; feature lag |
| Hosted aggregator | OpenRouter | Quick access to many models, one bill | Data passes a third party; compliance review |
| Cloud model platform | Bedrock, Vertex AI, Azure | Vendor models under your cloud contract and region | Per-platform feature lag |

LiteLLM is the common open-source choice: `litellm.completion()` takes and returns the OpenAI format across many providers, and its proxy is an OpenAI-compatible server where deployments sharing one `model_name` are load-balanced and fallen back among, with virtual keys, budgets and logging. Illustrative config (key names vary by version):

```yaml
model_list:
  - model_name: answer-large          # what clients ask for
    litellm_params:
      model: anthropic/<model-id>
      api_key: os.environ/ANTHROPIC_API_KEY
  - model_name: answer-large          # second deployment, same name
    litellm_params:
      model: openai/<model-id>
      api_key: os.environ/OPENAI_API_KEY
router_settings:
  routing_strategy: latency-based-routing
litellm_settings:
  fallbacks: [{"answer-large": ["answer-small"]}]
```

Self-hosted engines (vLLM, SGLang, Ollama) expose OpenAI-compatible endpoints too, so a gateway can front your own models. Keep a thin typed interface of your own on top so the gateway is replaceable.

## In practice

### Fallback chain and cascade

Fake providers make the routing logic testable without network calls. An open breaker skips a candidate without paying its timeout; fatal errors propagate instead of failing over; a cascade escalates only when the acceptance check fails and accounts for both calls' cost.

```python
from dataclasses import dataclass

class Retryable(Exception): ...     # 429, 5xx, overloaded, timeout
class Fatal(Exception): ...         # bad request, auth: switching won't help

@dataclass
class Result:
    text: str
    model: str
    cost: float

class FakeModel:
    def __init__(self, name: str, cost: float, script: list[str] | None = None):
        self.name, self.cost, self.script, self.calls = name, cost, script or [], 0

    def complete(self, prompt: str) -> Result:
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "retryable":
            raise Retryable(self.name)
        if step == "fatal":
            raise Fatal(self.name)
        text = f"{self.name}:{prompt}" if step == "ok" else step
        return Result(text, self.name, self.cost)

class Breaker:                       # minimal: no half-open state here
    def __init__(self, threshold: int = 3):
        self.failures, self.threshold = 0, threshold

    def is_open(self) -> bool:
        return self.failures >= self.threshold

    def record(self, ok: bool) -> None:
        self.failures = 0 if ok else self.failures + 1

def with_fallback(chain, prompt, breakers) -> Result:
    errors = []
    for m in chain:
        if breakers[m.name].is_open():
            errors.append(f"{m.name}:open")
            continue
        try:
            r = m.complete(prompt)
        except Retryable:
            breakers[m.name].record(False)
            errors.append(f"{m.name}:retryable")
            continue
        breakers[m.name].record(True)
        return r
    raise RuntimeError("all candidates failed: " + ", ".join(errors))

def cascade(small, large, prompt, accept, breakers) -> tuple[Result, bool]:
    first = with_fallback(small, prompt, breakers)
    if accept(first.text):
        return first, False
    second = with_fallback(large, prompt, breakers)
    return Result(second.text, second.model, first.cost + second.cost), True

if __name__ == "__main__":
    br = {n: Breaker() for n in ("a", "b", "s", "l", "x")}
    a, b = FakeModel("a", 1.0, ["retryable"]), FakeModel("b", 1.2)
    r = with_fallback([a, b], "q", br)
    assert r.model == "b" and br["a"].failures == 1

    a.script = ["retryable", "retryable"]
    with_fallback([a, b], "q", br)
    with_fallback([a, b], "q", br)
    assert br["a"].is_open()
    before = a.calls
    with_fallback([a, b], "q", br)
    assert a.calls == before            # skipped: no timeout paid

    try:
        with_fallback([FakeModel("x", 1.0, ["fatal"]), b], "q", br)
        raise AssertionError("fatal errors must not fail over")
    except Fatal:
        pass

    small, large = FakeModel("s", 0.1, ["LOW", "yes"]), FakeModel("l", 1.0)
    accept = lambda text: text != "LOW"  # stand-in for schema/confidence checks
    r, escalated = cascade([small], [large], "q1", accept, br)
    assert escalated and r.model == "l" and abs(r.cost - 1.1) < 1e-9
    r, escalated = cascade([small], [large], "q2", accept, br)
    assert not escalated and r.text == "yes" and r.cost == 0.1
    print("ok")
```

### Normalizing two streaming formats

Both vendors' streams for the same answer (some text, then one tool call with arguments split across fragments) normalize to identical internal events, including a closed set of finish reasons.

{% raw %}
```python
import json

FINISH = {
    "openai": {"stop": "stop", "length": "length",
               "tool_calls": "tool_calls", "content_filter": "filtered"},
    "anthropic": {"end_turn": "stop", "stop_sequence": "stop",
                  "max_tokens": "length", "tool_use": "tool_calls",
                  "refusal": "filtered"},
}

def norm_finish(provider: str, raw: str | None) -> str:
    return FINISH[provider].get(raw, "unknown")   # never default to "stop"

def from_openai(chunks):
    """Chat Completions-style chunks (JSON-decoded `data:` payloads)."""
    calls: dict[int, dict] = {}
    finish, usage = None, None
    for ch in chunks:
        if ch.get("usage"):
            u = ch["usage"]
            usage = {"input": u["prompt_tokens"], "output": u["completion_tokens"]}
        for choice in ch.get("choices", []):
            d = choice.get("delta", {})
            if d.get("content"):
                yield ("text", d["content"])
            for tc in d.get("tool_calls", []):
                c = calls.setdefault(tc["index"], {"id": None, "name": None, "args": ""})
                fn = tc.get("function", {})
                c["id"] = tc.get("id") or c["id"]
                c["name"] = fn.get("name") or c["name"]
                c["args"] += fn.get("arguments", "")
            finish = choice.get("finish_reason") or finish
    for i in sorted(calls):
        c = calls[i]
        yield ("tool_call", {"id": c["id"], "name": c["name"],
                             "args": json.loads(c["args"])})
    yield ("done", {"finish": norm_finish("openai", finish), "usage": usage})

def from_anthropic(events):
    blocks: dict[int, dict] = {}
    finish, usage = None, {"input": 0, "output": 0}
    for ev in events:
        t = ev["type"]
        if t == "message_start":
            usage["input"] = ev["message"]["usage"]["input_tokens"]
        elif t == "content_block_start":
            blocks[ev["index"]] = dict(ev["content_block"], args="")
        elif t == "content_block_delta":
            d = ev["delta"]
            if d["type"] == "text_delta":
                yield ("text", d["text"])
            elif d["type"] == "input_json_delta":
                blocks[ev["index"]]["args"] += d["partial_json"]
        elif t == "content_block_stop":
            b = blocks[ev["index"]]
            if b["type"] == "tool_use":
                yield ("tool_call", {"id": b["id"], "name": b["name"],
                                     "args": json.loads(b["args"] or "{}")})
        elif t == "message_delta":
            finish = ev["delta"].get("stop_reason")
            usage["output"] = ev["usage"]["output_tokens"]
        elif t == "error":
            yield ("error", ev["error"]["message"])
            return
    yield ("done", {"finish": norm_finish("anthropic", finish), "usage": usage})

if __name__ == "__main__":
    oa = [
        {"choices": [{"delta": {"content": "Let me check."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
            "function": {"name": "weather", "arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": 'ty": "Paris"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 12}},
    ]
    an = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 50}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Let me check."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "c1", "name": "weather", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"ci'}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": 'ty": "Paris"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]
    a, b = list(from_openai(oa)), list(from_anthropic(an))
    assert a[0] == b[0] == ("text", "Let me check.")
    call = ("tool_call", {"id": "c1", "name": "weather", "args": {"city": "Paris"}})
    assert a[1] == b[1] == call
    assert a[2][1]["usage"] == {"input": 50, "output": 12}
    assert a[2] == b[2]                 # same finish reason and usage
    assert a[2][1]["finish"] == "tool_calls"
    print("ok")
```
{% endraw %}

> **In production:** Log the provider and the model the response reports (not just the one you requested) on every call, and show it in traces. When quality complaints arrive, the first question is "which model answered this?" See [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).

## Pitfalls

- **Lowest-common-denominator abstraction.** Hiding prompt caching or reasoning controls throws away wins. Fix: capability flags.
- **Unevaluated fallbacks.** Symptom: quality dips during a provider incident and nobody knows why. Fix: every candidate passes the role's eval; alert on fallback share.
- **Prompt portability.** A prompt tuned for one model underperforms on another. Fix: per-candidate prompt versions, evaluated separately.
- **Retry storms.** SDK, router and gateway retries multiply. Fix: retry in one layer; set SDK `max_retries` to 0.
- **One tokenizer for all estimates.** Context-length errors appear only on the fallback model. Fix: per-model limits with headroom.
- **Floating model aliases.** "Latest" aliases change behavior under you. Fix: pin versioned model ids; log the response model.
- **Cache loss on failover.** Prompt caches are per provider and model, so failover raises cost and TTFT.
- **Compliance drift.** Each provider is a subprocessor. Failing over to a provider or region outside a tenant's data agreement is an incident. Fix: per-tenant provider and region allowlists enforced by the router (see [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).
- **Hardcoded prices.** Prices change. Fix: a price table in config with effective dates; compute cost from logged usage.

## Checklist

- Route by role; config maps roles to evaluated candidates and prompt versions.
- Adapters normalize messages, tools, finish reasons, usage, errors, streams.
- Fail over on retryable errors only; prefer the same model on another platform.
- Token buckets per (provider, model, key); per-tenant limits; separate batch quota.
- Cascades use objective checks before self-reported confidence.
- Log requested and served model, tokens, cost, latency and fallback path.

## Interview questions

**How do you make an LLM feature resilient to provider outages?**
Ordered, evaluated fallbacks per role; circuit breakers per provider and model so an outage does not cost a timeout per request; layered timeouts; graceful degradation when everything fails. The best first fallback is the same model on another platform.

**What goes into a universal message format?**
Role-tagged messages with typed parts, JSON Schema tools, tool calls and results linked by id, closed finish reasons, normalized usage (cached and reasoning tokens included), your own error types and per-model capability flags.

**When does a model cascade save money?**
When `c_small + p_escalate × c_large < c_large`, i.e. the escalation rate stays below `1 − c_small / c_large`, and the acceptance check is cheap and reliable. It costs latency on escalated requests, so it fits batch and extraction better than latency-critical chat.

**How do you handle rate limits across several providers?**
Client-side buckets per provider, model and key fed by response headers; honor `retry-after`; route to candidates with headroom before 429s; separate batch keys; per-tenant limits. Provider overload means fail over; your own quota exhaustion means queue or shed.

**Why is streaming normalization harder than request normalization?**
Event shapes and usage placement differ, tool arguments arrive as fragments to assemble per call, errors arrive after a 200, and fallback is only possible before the first token reaches the user.

**Would you use a gateway like LiteLLM or build your own?**
One service: a library or thin adapters. Many services and teams: a self-hosted gateway for central keys, budgets, logging and routing, at the cost of another critical hop. Either way, keep your own typed interface.

## Further reading

- FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance (Chen et al., 2023).
- RouteLLM: Learning to Route LLMs with Preference Data (Ong et al., 2024).
- The Tail at Scale (Dean and Barroso, 2013), for hedged requests.
- LiteLLM documentation: SDK, proxy, router and fallbacks.
- OpenAI, Anthropic and Google Gemini API references: streaming events, tool calling and rate limits.
