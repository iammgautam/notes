---
title: LLMs for backend engineers
section: llm
order: 1
summary: What an LLM API call actually does, from tokens and context windows to sampling, determinism and the knobs that change cost and behavior.
---

From the outside an LLM is an HTTP endpoint that takes text and returns text. Underneath it is a next-token predictor running on GPUs, billed per token, bounded by a context window, and nondeterministic by default. Every production decision you make (prompt size, timeouts, caching, retries, evaluation) follows from those four facts.

## How it works

### Tokens

Models never see characters. A tokenizer (usually byte-pair encoding or a variant) maps text to integer ids from a vocabulary of roughly 30k–250k entries. Rough conversions for English:

| Unit | Approximate tokens |
|---|---|
| 1 English word | ~1.3 tokens |
| 1,000 characters of prose | ~250 tokens |
| 1 page of text | ~500–700 tokens |
| JSON, code, non-English text | often 1.5–3x more per character |

Consequences you will hit:

- **Billing and limits are in tokens**, and different providers tokenize the same text differently. A prompt that is 10k tokens on one model may be 12k on another.
- **Character-level tasks are hard.** Counting letters, reversing strings or exact string slicing fight the tokenizer.
- **Whitespace and formatting cost money.** Pretty-printed JSON in a prompt can be 30% more tokens than compact JSON.

### The context window

The context window is the maximum number of tokens for input plus output in one request. Modern models range from tens of thousands to over a million tokens. Three things matter more than the headline number:

1. **Output is capped separately**, often far below the context size. `max_tokens` (or its equivalent) bounds generation; if you hit it, the response is truncated mid-sentence or mid-JSON.
2. **Quality degrades with length.** Models retrieve facts from the start and end of long prompts more reliably than from the middle ("lost in the middle"). A 200k-token window does not mean 200k tokens of reliable attention.
3. **Cost and latency scale with input.** Prefill time grows with prompt length, and you pay for every input token on every call. Stuffing the window is the expensive default.

### What one API call does

```text
messages ──► chat template ──► tokenize ──► prefill (whole prompt, parallel)
                                                  │
                     ┌────────────────────────────┘
                     ▼
              decode loop: logits ──► sample one token ──► append
                     │                                       │
                     └──────────── until stop / max_tokens ◄─┘
                     ▼
              detokenize ──► stream chunks / final response
```

The "chat" format is an illusion maintained by a **chat template**: your `system`, `user`, `assistant` and `tool` messages are flattened into one token sequence with special delimiter tokens. The model has no memory between calls; every request resends the full conversation. "Memory" in a product is your code deciding what to put back into the prompt.

### Sampling

At each step the model outputs logits over the vocabulary. Sampling turns those into one token:

- **Temperature** divides logits before softmax. `0` approaches greedy (always the most likely token); `1` uses the model's distribution as-is; higher flattens it and increases randomness.
- **Top-p (nucleus)** keeps the smallest set of tokens whose cumulative probability reaches `p`, then samples among them. `top_p=0.9` drops the long tail.
- **Top-k** keeps only the `k` most likely tokens.
- **Stop sequences** end generation when a string appears.
- **Penalties** (frequency, presence, repetition) discourage repeats. Rarely needed with modern models.

Adjust temperature or top-p, not both. Some newer reasoning-style models ignore or reject these parameters entirely.

| Task | Temperature |
|---|---|
| Extraction, classification, structured output | 0–0.2 |
| RAG answers, summaries | 0.2–0.5 |
| Brainstorming, creative copy | 0.7–1.0 |

### Determinism

`temperature=0` is not a determinism guarantee. Hosted inference batches your request with others, and floating-point reductions on GPUs are not associative, so the batch composition can change logits slightly and flip a near-tie. Mixture-of-experts routing, model updates behind a stable alias and provider-side changes add more drift. Some APIs expose a `seed` parameter; treat it as best effort.

Design for this: make outputs checkable (schemas, validators), make tests tolerant (assert properties, not exact strings), and pin model versions where the provider lets you.

## In practice

A provider-neutral call wrapper should pin everything that affects behavior and record everything you need to debug it later:

```python
from dataclasses import dataclass, field
import time

@dataclass
class LLMRequest:
    model: str                      # pinned version, not a floating alias
    messages: list[dict]
    temperature: float = 0.2
    max_tokens: int = 1024
    stop: list[str] = field(default_factory=list)
    timeout_s: float = 60.0

@dataclass
class LLMResponse:
    text: str
    finish_reason: str              # "stop", "length", "tool_calls", "content_filter"
    input_tokens: int
    output_tokens: int
    latency_ms: int

def complete(client, req: LLMRequest) -> LLMResponse:
    t0 = time.monotonic()
    raw = client.chat(req)                     # your provider adapter
    resp = LLMResponse(
        text=raw.text,
        finish_reason=raw.finish_reason,
        input_tokens=raw.usage.input_tokens,
        output_tokens=raw.usage.output_tokens,
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    if resp.finish_reason == "length":
        # Truncated: never parse this as complete JSON or a final answer.
        raise TruncatedOutput(resp)
    return resp
```

Count tokens before sending, not after a 400 error. Use the provider's tokenizer or token-count endpoint when it exists, and a conservative estimate (characters / 3) when it does not. Budget explicitly:

```python
CONTEXT = 128_000
RESERVED_OUTPUT = 4_000
SAFETY = 0.9

def fits(prompt_tokens: int) -> bool:
    return prompt_tokens <= (CONTEXT - RESERVED_OUTPUT) * SAFETY
```

> **In production:** Log `model`, `input_tokens`, `output_tokens`, `finish_reason`, latency and a prompt hash on every call. It is the only way to answer "why did cost double on Tuesday" or "why did this answer change".

## Pitfalls

- **Ignoring `finish_reason`.** A `length` finish produces truncated JSON that fails to parse, or a confident half-answer. Symptom: sporadic parse errors on long inputs. Fix: check it, raise `max_tokens`, or ask for shorter output.
- **Floating model aliases.** "latest" aliases change underneath you. Symptom: eval scores or output format shift with no deploy. Fix: pin dated versions and upgrade deliberately behind an eval.
- **Assuming the model remembers.** Each call is stateless. Symptom: follow-up questions lose context. Fix: your code owns conversation state and decides what to resend or summarize.
- **Unbounded conversation growth.** Chat history grows until requests fail or cost spikes. Fix: window, summarize or retrieve older turns.
- **Treating `temperature=0` as a cache key guarantee.** Caching on identical inputs is fine; asserting identical outputs in tests is flaky.
- **Mixing tokenizer assumptions across providers.** A budget computed with one tokenizer can overflow on another. Leave headroom.

## Checklist

- Pin model versions; store them with every logged call.
- Set `max_tokens` deliberately and handle `finish_reason == "length"`.
- Pre-count tokens and reserve room for output.
- Use low temperature for anything you parse or evaluate.
- Validate outputs structurally instead of trusting determinism.
- Keep conversation state server-side and trim it intentionally.

## Interview angle

- **Why is `temperature=0` not deterministic?** Batched GPU inference changes floating-point reduction order, which can flip near-tied tokens; MoE routing and silent model updates add drift.
- **What limits the context window in practice?** Attention quality over long inputs, KV cache memory on the server, and cost/latency that scale with input tokens.
- **Where does chat "memory" live?** In your application. The model is stateless; each request carries the full context you choose to send.
