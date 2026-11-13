---
title: LLMs for backend engineers
section: llm
order: 1
phase: "Phase 1 · Foundations"
summary: What an LLM is, what one API call does end to end, and how tokens, context, sampling and cost shape every later design decision.
---

From the outside an LLM is an HTTP endpoint that takes text and returns text. Underneath it is a next-token predictor running on GPUs, billed per token, bounded by a context window, stateless between calls and nondeterministic by default. Every production decision you make (prompt size, timeouts, caching, retries, evaluation, routing) follows from those five facts. This note is the map: it walks one request end to end, gives you the numbers to reason with, and points to the note that goes deep on each part.

## What an LLM is

A large language model is a function that takes a sequence of token ids and returns a probability distribution over the next token. Generation is a loop around that function: pick a token from the distribution, append it, run the function again. Chat, tool calls, JSON mode and "reasoning" are all built on that loop by formatting the input in a particular way and constraining or post-processing the output.

Three properties follow from how these models are built:

- **Knowledge is frozen in the weights.** A model learns from a fixed corpus up to a cutoff date. It does not look anything up at inference time unless your code puts the information into the prompt. That gap is the reason retrieval-augmented generation exists.
- **Behavior comes from training stages.** Pretraining on trillions of tokens produces a base model that continues documents. Post-training (supervised fine-tuning on instruction data, then preference tuning) turns it into an assistant that follows a chat format, uses tools and declines some requests. See [Pretraining and scaling laws]({{ '/llm/pretraining-and-scaling/' | relative_url }}).
- **It is a probabilistic text generator, not a database or a program.** The same input can produce different outputs, fluent text can be wrong, and the model has no built-in signal for "I don't know." You compensate with grounding, validation and evaluation.

Almost every current model is a decoder-only transformer: an embedding table, a stack of attention and feed-forward blocks, and an output layer that produces one score (a logit) per vocabulary entry. [Transformer architecture]({{ '/llm/transformer-architecture/' | relative_url }}) covers the mechanism, and [The machine learning you need for LLMs]({{ '/llm/ml-primer/' | relative_url }}) covers the vocabulary (parameters, loss, gradients, embeddings) you need to read it.

## The request lifecycle

What happens between `client.chat(...)` and the last byte of the response:

```text
your app ─► SDK ─► HTTPS ─► provider gateway (auth, rate limits, routing)
                                   │
                                   ▼
          messages ─► chat template ─► tokenizer ─► token ids
                                                       │
                                   ┌───────────────────┘
                                   ▼
                  scheduler: queue, batch with other requests
                                   │
                                   ▼
             prefill: whole prompt in one parallel pass,
                      fills the KV cache            ─► TTFT
                                   │
                                   ▼
             decode: logits ─► sample ─► append ──┐
                        ▲                         │ one token
                        └── until stop / max ◄────┘ per step
                                   │
                                   ▼
             detokenize ─► stream chunks (SSE) ─► usage, finish_reason
```

Step by step:

1. **Gateway.** Authentication, per-key rate limits (requests per minute and tokens per minute), and routing to a model deployment. 429 responses and `retry-after` headers come from here.
2. **Chat template.** Your `system`, `user`, `assistant` and `tool` messages are flattened into one string with special delimiter tokens. Tool definitions and response schemas are serialized into that string too, which is why they cost input tokens on every call.
3. **Tokenization.** The string becomes integer ids from a fixed vocabulary. Billing, rate limits and the context window are all counted in these units. See [Tokenization]({{ '/llm/tokenization/' | relative_url }}).
4. **Scheduling.** The server batches your request with others to keep the GPU busy. Batching is why throughput is high, why latency varies with load, and one reason outputs vary between identical requests.
5. **Prefill.** The model processes all prompt tokens in one parallel pass and stores per-token keys and values (the KV cache) so it never recomputes them. Prefill is compute-bound and sets time to first token (TTFT).
6. **Decode.** One forward pass per output token, each reading all the weights and the whole KV cache. Decode is memory-bandwidth-bound and sets time per output token (TPOT). See [Inference basics]({{ '/llm/inference-basics/' | relative_url }}).
7. **Sampling.** Each step turns logits into one token according to temperature, top-p and related settings. See [Decoding and sampling]({{ '/llm/decoding-and-sampling/' | relative_url }}).
8. **Stop.** Generation ends on an end-of-turn token, a stop sequence, a tool-call boundary or `max_tokens`. The reason comes back as `finish_reason` (OpenAI-style) or `stop_reason` (Anthropic-style), and your code must check it.
9. **Response.** Tokens are detokenized and streamed as server-sent events, followed by usage counts that are your billing record.

## Tokens, context and sampling at a glance

### Tokens

Models never see characters. A tokenizer (usually byte-pair encoding or a variant) maps text to integer ids from a vocabulary of roughly 30k to 260k entries. Rough conversions for English:

| Unit | Approximate tokens |
|---|---|
| 1 English word | ~1.3 tokens |
| 1,000 characters of prose | ~250 tokens |
| 1 page of text | ~500–700 tokens |
| JSON, code, non-English text | often 1.5–3x more per character |

Consequences you will hit:

- **Billing and limits are in tokens**, and providers tokenize the same text differently. A prompt that is 10k tokens on one model can be 12k on another.
- **Character-level tasks are hard.** Counting letters, reversing strings or exact slicing fight the tokenizer.
- **Formatting costs money.** Pretty-printed JSON in a prompt can be 30% more tokens than compact JSON.

### The context window

The context window is the maximum number of tokens for input plus output in one request. As of 2025–2026, mainstream models offer from about 128k tokens to over a million. Three things matter more than the headline number:

1. **Output is capped separately**, usually far below the context size. `max_tokens` (or its equivalent) bounds generation; when you hit it, the response is cut mid-sentence or mid-JSON.
2. **Quality degrades with length.** Models use information at the start and end of long prompts more reliably than information in the middle. A 200k-token window does not mean 200k tokens of reliable attention.
3. **Cost and latency scale with input.** Prefill time grows with prompt length, and you pay for every input token on every call. Stuffing the window is the expensive default. See [Context engineering]({{ '/llm/context-engineering/' | relative_url }}).

### Sampling

At each step the model outputs logits over the whole vocabulary, and a sampler picks one token:

- **Temperature** divides logits before softmax. Near `0` it approaches greedy (always the top token); `1` uses the model's distribution as is; higher values flatten it.
- **Top-p** keeps the smallest set of tokens whose cumulative probability reaches `p`; **top-k** keeps the `k` most likely.
- **Stop sequences** end generation when a string appears; **penalties** discourage repeats.

| Task | Temperature |
|---|---|
| Extraction, classification, structured output | 0–0.2 |
| RAG answers, summaries | 0.2–0.5 |
| Brainstorming, creative copy | 0.7–1.0 |

Adjust temperature or top-p, not both. Some reasoning models restrict or ignore these parameters.

### Statelessness

The chat format is maintained by the chat template and by your code. The model has no memory between calls: every request resends the system prompt, the full conversation and every tool definition. "Memory" in a product is your code deciding what to put back into the prompt: the last N turns, a running summary, retrieved facts. Tool calling works the same way. The model emits a structured request, your code executes it, and you send the result back as a new message in the next call. See [Tool calling]({{ '/llm/tool-calling/' | relative_url }}) and [Agent memory]({{ '/llm/agent-memory/' | relative_url }}).

Two consequences: cost grows faster than linearly with conversation length because history is resent, and any state you care about belongs in your database, not "in the model."

### Determinism

`temperature=0` is not a determinism guarantee. Hosted inference batches your request with others, and floating-point reductions on GPUs are not associative, so batch composition can change logits slightly and flip a near-tie. Mixture-of-experts routing, model updates behind a stable alias and provider-side changes add more drift. Some APIs expose a `seed` parameter; treat it as best effort.

Design for this: make outputs checkable (schemas, validators), make tests assert properties rather than exact strings, and pin model versions where the provider lets you.

## Cost and latency

Pricing is per token, with output tokens priced several times higher than input tokens on most APIs. Two formulas cover most planning:

```text
cost    = input_tokens × input_price + output_tokens × output_price
latency ≈ queueing + TTFT(prompt length) + output_tokens × TPOT
```

What follows from them:

- **Output length drives latency.** 500 output tokens at 50–100 tokens/s is 5–10 seconds regardless of how fast prefill is. Ask for shorter outputs, stream, or split the work.
- **Input length drives cost** in RAG and agent systems, because prompts carry retrieved context, tool schemas and history, and are resent on every step. A 10-step agent loop pays for its system prompt ten times.
- **Discounts exist for repeated prefixes and for patient work.** As of 2025–2026, the major providers offer prompt caching (a reused prefix billed at a fraction of the input price) and batch APIs (asynchronous processing at roughly half price). See [LLM caching]({{ '/llm/llm-caching/' | relative_url }}) and [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).
- **Reasoning models bill hidden reasoning as output tokens**, which can multiply cost and latency for the same visible answer.

This runnable calculator shows the history effect. Prices are illustrative, not any provider's list price:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    input_per_mtok: float   # USD per million input tokens
    output_per_mtok: float  # USD per million output tokens

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok
                + output_tokens * self.output_per_mtok) / 1_000_000


def conversation_tokens(system: int, turns: int, user: int,
                        reply: int) -> tuple[int, int]:
    """Total (input, output) tokens when every turn resends the history."""
    history = total_in = total_out = 0
    for _ in range(turns):
        total_in += system + history + user
        total_out += reply
        history += user + reply
    return total_in, total_out


def latency_s(ttft_s: float, output_tokens: int, tokens_per_s: float) -> float:
    return ttft_s + output_tokens / tokens_per_s


if __name__ == "__main__":
    price = Pricing(input_per_mtok=3.0, output_per_mtok=15.0)  # illustrative
    tin, tout = conversation_tokens(system=1_500, turns=20, user=150, reply=400)
    chat = price.cost(tin, tout)
    independent = 20 * price.cost(1_500 + 150, 400)
    print(f"20-turn chat: {tin:,} in / {tout:,} out = ${chat:.3f}")
    print(f"20 independent single-turn calls: ${independent:.3f}")
    assert (tin, tout) == (137_500, 8_000)
    assert round(chat / independent, 1) == 2.4

    tin40, _ = conversation_tokens(system=1_500, turns=40, user=150, reply=400)
    assert tin40 / tin > 3.5  # twice the turns, ~3.6x the input tokens

    t = latency_s(ttft_s=0.5, output_tokens=400, tokens_per_s=60)
    print(f"latency for a 400-token reply: {t:.1f}s")
    assert 7.0 < t < 7.5
```

The 20-turn conversation costs about 2.4x what 20 independent calls would, and doubling the turns more than triples the input tokens. That is why long-running chats and agents need history trimming, summarization or caching, and why a 400-token reply takes about 7 seconds no matter how small the prompt is.

## Where everything else fits

A production LLM feature is mostly ordinary backend engineering around one expensive, probabilistic call:

```text
 request
    │
    ▼
 input guardrails
    │
    ▼
 context builder ◄── retrieval (Phase 3), memory, tool results (Phase 4)
    │
    ▼
 model call: routing, caching, retries, serving (Phases 7–8)
    │
    ├── tool call? ─► execute tool ─► back to context builder (agent loop)
    ▼
 output checks: schema validation, guardrails (Phases 2, 8)
    │
    ▼
 response        every step traced, measured, evaluated (Phases 5, 8)
```

| Phase | Question it answers | Start with |
|---|---|---|
| 1 Foundations | What is the model doing? | [Tokenization]({{ '/llm/tokenization/' | relative_url }}), [Decoding and sampling]({{ '/llm/decoding-and-sampling/' | relative_url }}), [Choosing a model]({{ '/llm/model-landscape/' | relative_url }}) |
| 2 Prompting and context | How do I get reliable output? | [Prompting]({{ '/llm/prompting-and-structured-outputs/' | relative_url }}), [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}) |
| 3 Retrieval | How do I give it my data? | [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}), [Embeddings]({{ '/llm/embeddings/' | relative_url }}) |
| 4 Agents | How do I let it act? | [Tool calling]({{ '/llm/tool-calling/' | relative_url }}), [Agents]({{ '/llm/agents/' | relative_url }}) |
| 5 Evaluation | How do I know it works? | [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) |
| 6 Fine-tuning | When do I change the weights? | [When to fine-tune]({{ '/llm/when-to-fine-tune/' | relative_url }}) |
| 7 Inference and serving | How do I run models fast and cheap? | [Inference basics]({{ '/llm/inference-basics/' | relative_url }}) |
| 8 Production | How do I run it safely at scale? | [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}), [Observability]({{ '/llm/observability-and-tracing/' | relative_url }}) |

## In practice

A provider-neutral call wrapper should pin everything that affects behavior and record everything you need to debug it later. This sketch is illustrative; `client.chat` stands for your provider adapter:

```python
from dataclasses import dataclass, field
import time


class TruncatedOutput(Exception):
    pass


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
    finish_reason: str              # "stop", "length", "tool_calls", ...
    input_tokens: int
    output_tokens: int
    latency_ms: int


def complete(client, req: LLMRequest) -> LLMResponse:
    t0 = time.monotonic()
    raw = client.chat(req)
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

Count tokens before sending, not after a 400 error. Use the provider's tokenizer or token-counting endpoint when one exists, and a conservative estimate (characters / 3) when it does not. Budget explicitly:

```python
CONTEXT = 128_000
RESERVED_OUTPUT = 4_000
SAFETY = 0.9


def fits(prompt_tokens: int) -> bool:
    return prompt_tokens <= (CONTEXT - RESERVED_OUTPUT) * SAFETY
```

Timeouts need the same arithmetic. A non-streaming call that may produce 2,000 tokens at 40 tokens/s needs at least 50 seconds plus TTFT plus queueing; a 30-second default will cut it off and you will likely be billed for the tokens anyway. With streaming, use a short timeout for the first chunk and a separate idle timeout between chunks.

> **In production:** Log `model`, `input_tokens`, `output_tokens`, `finish_reason`, latency and a prompt hash on every call. It is the only way to answer "why did cost double on Tuesday" or "why did this answer change." See [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).

## Pitfalls

- **Ignoring `finish_reason`.** A `length` finish produces truncated JSON that fails to parse, or a confident half-answer. Symptom: sporadic parse errors on long inputs. Fix: check it, raise `max_tokens`, or ask for shorter output.
- **Floating model aliases.** "latest" aliases change underneath you. Symptom: eval scores or output format shift with no deploy. Fix: pin dated versions and upgrade deliberately behind an eval.
- **Assuming the model remembers.** Each call is stateless. Symptom: follow-up questions lose context. Fix: your code owns conversation state and decides what to resend or summarize.
- **Unbounded conversation growth.** History grows until requests fail with context errors or cost spikes. Symptom: input tokens per request climbing over a session. Fix: window, summarize or retrieve older turns.
- **Treating `temperature=0` as reproducible.** Caching on identical inputs is fine; asserting identical outputs in tests is flaky.
- **Mixing tokenizer assumptions across providers.** A budget computed with one tokenizer can overflow on another. Leave headroom.
- **Retrying non-idempotent work blindly.** A timeout does not mean the model did nothing: you may still be billed, and a tool call may already have run. See [Reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }}).

## Checklist

- Pin model versions and store them with every logged call.
- Set `max_tokens` deliberately and handle `finish_reason == "length"`.
- Pre-count tokens and reserve room for output.
- Use low temperature for anything you parse or evaluate.
- Validate outputs structurally instead of trusting determinism.
- Keep conversation state server-side and trim it intentionally.
- Size timeouts from expected output tokens, not from a default.
- Track cost per feature and per tenant from day one.

## Interview questions

- **What happens when you call a chat completion API?** Messages are rendered through a chat template into one token sequence, the server batches the request, prefill processes the prompt in parallel and fills the KV cache, then a decode loop generates one token per forward pass until a stop condition, streaming detokenized text and returning usage counts.
- **Why is `temperature=0` not deterministic?** Batched GPU inference changes floating-point reduction order, which can flip near-tied tokens; MoE routing and silent model updates add drift.
- **Why do output tokens cost more and take longer than input tokens?** Input tokens are processed in one parallel prefill pass that uses the GPU's compute well. Output tokens are generated one at a time, each pass reading all weights from memory, so they are bandwidth-bound and slower per token.
- **What limits the context window in practice?** Attention quality over long inputs, KV cache memory on the server, and cost and latency that scale with input tokens.
- **Where does chat "memory" live?** In your application. The model is stateless; each request carries the full context you choose to send.
- **How do you estimate the cost of a feature before building it?** Estimate input and output tokens per request (system prompt, retrieved context, tool schemas, history), multiply by prices and expected volume, and add headroom for retries and agent steps.
- **Why does a 20-turn chat cost more than 20 single calls?** Each turn resends all previous turns, so input tokens grow roughly quadratically with the number of turns.

## Further reading

- Vaswani et al., 2017. "Attention Is All You Need."
- Brown et al., 2020. "Language Models are Few-Shot Learners."
- Ouyang et al., 2022. "Training language models to follow instructions with human feedback."
- Liu et al., 2023. "Lost in the Middle: How Language Models Use Long Contexts."
- Provider API references for chat or messages endpoints, token counting, prompt caching and batch processing (OpenAI, Anthropic, Google).
