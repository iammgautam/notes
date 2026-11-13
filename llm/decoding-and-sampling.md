---
title: Decoding and sampling
section: llm
order: 6
phase: "Phase 1 · Foundations"
summary: From logits to text with greedy, temperature, top-k, top-p, min-p, beam search, penalties, logprobs and stop rules, and why outputs are not deterministic.
---

The model computes a probability distribution over the next token; the decoder decides which token you actually get. Every knob in the request that is not the prompt (temperature, top-p, penalties, stop sequences, `max_tokens`, seed, logprobs) acts here. They decide whether extraction is stable, whether creative output is varied or incoherent, whether JSON parses, and whether identical requests match. This note covers each strategy, what logprobs are good for, and where nondeterminism really comes from.

## How it works

### From logits to probabilities

At each step the model emits one logit per vocabulary entry (100k+ numbers). Softmax turns them into probabilities:

```text
p_i = exp(z_i / T) / sum_j exp(z_j / T)        T = temperature
```

The decoder then runs a pipeline of transforms over those numbers before choosing one token:

```text
logits (one per vocabulary entry)
  │
  ▼
penalties             repetition, frequency, presence
  │
  ▼
bias and masks        logit_bias, grammar or schema mask (-inf)
  │
  ▼
temperature           z / T
  │
  ▼
softmax ─► truncate   top-k, top-p, min-p, then renormalize
  │
  ▼
choose                sample, or argmax when greedy
  │
  ▼
append token, check EOS / stop sequences / max_tokens ─► next step
```

The exact order varies by engine, and it matters: applying top-p before or after temperature selects different token sets. Hugging Face `transformers` models this as a chain of logits processors; vLLM and SGLang expose the same knobs through their sampling parameters.

## Techniques

### Greedy decoding

Always take the highest-probability token. It is deterministic given fixed logits, cheap and good for short, constrained outputs such as a label, a number or a short extraction. For long open-ended text it tends to fall into loops ("I think that I think that...") and bland phrasing, because the single most likely next token is rarely what a person would write over hundreds of steps. Setting `temperature=0` on most APIs means greedy.

### Beam search

Keep the `B` highest-scoring partial sequences (by summed log-probability), extend each by every candidate token, and keep the best `B` again. It finds sequences with higher total probability than greedy, because greedy can commit to a likely first token that leads nowhere good. A length penalty usually normalizes scores, since longer sequences accumulate more negative log-probability.

Beam search suits tasks with one correct output: translation, speech recognition, some summarization with encoder-decoder models. For chat and open-ended generation it produces generic, repetitive text, and it multiplies compute by `B`. Hosted chat APIs generally do not offer it; open-source engines do.

```python
import math

# A toy "model": probability of the next token given the previous token.
NEXT = {
    "<s>": {"A": 0.55, "B": 0.45},
    "A": {"x": 0.35, "y": 0.35, "z": 0.30},
    "B": {"x": 0.90, "y": 0.10},
}


def greedy_decode(steps):
    seq, logp = ["<s>"], 0.0
    for _ in range(steps):
        tok, p = max(NEXT[seq[-1]].items(), key=lambda kv: kv[1])
        seq.append(tok)
        logp += math.log(p)
    return seq[1:], logp


def beam_search(steps, width):
    beams = [(["<s>"], 0.0)]
    for _ in range(steps):
        candidates = [(seq + [tok], logp + math.log(p))
                      for seq, logp in beams
                      for tok, p in NEXT.get(seq[-1], {}).items()]
        beams = sorted(candidates, key=lambda c: c[1], reverse=True)[:width]
    seq, logp = beams[0]
    return seq[1:], logp


if __name__ == "__main__":
    g_seq, g_lp = greedy_decode(2)
    b_seq, b_lp = beam_search(2, width=2)
    print("greedy", g_seq, round(math.exp(g_lp), 3))   # A x  0.192
    print("beam  ", b_seq, round(math.exp(b_lp), 3))   # B x  0.405
    assert g_seq == ["A", "x"] and b_seq == ["B", "x"]
    assert b_lp > g_lp
```

Greedy commits to `A` (0.55) and ends with a sequence of probability 0.19; beam search keeps `B` alive and finds a sequence twice as likely.

### Temperature

Dividing logits by `T` before softmax reshapes the distribution without changing the ranking. `T < 1` sharpens it toward the top token, `T = 1` leaves it unchanged, `T > 1` flattens it and gives the tail more mass. As `T → 0` sampling becomes greedy. Temperature alone never removes tokens: at `T = 1.5` a nonsense token with tiny probability is still possible, which is why truncation methods exist.

### Top-k

Keep the `k` most likely tokens, renormalize, sample. Simple, but `k` is fixed regardless of context: when the model is confident, `k = 50` lets in 49 bad options; when many continuations are reasonable, it may cut good ones.

### Top-p (nucleus)

Keep the smallest set of tokens whose cumulative probability reaches `p`, renormalize, sample (Holtzman et al., 2020). The set adapts to the model's confidence: one token when it is sure, dozens when it is not. `top_p` around 0.9–0.95 is a common default. Adjust temperature or top-p, not both, unless you have measured the combination.

### Min-p

Keep tokens whose probability is at least `min_p × p_max` (Nguyen et al., 2024). With `min_p = 0.1` and a top token at 0.9, anything below 0.09 is dropped; with a flat distribution whose top token is 0.2, anything above 0.02 survives. It scales with confidence more directly than top-p and holds up better at high temperatures. It is widely available in open-source engines (vLLM, llama.cpp, recent `transformers`) and less common on hosted APIs.

| Method | Keeps | Adapts to confidence | Typical setting | Use when |
|---|---|---|---|---|
| Greedy | Top 1 | n/a | `temperature=0` | Short, deterministic outputs |
| Beam search | Top `B` sequences | n/a | `B = 4–5` | One correct output, seq2seq |
| Temperature | All, reshaped | No | 0–1.0 | Always, as the main dial |
| Top-k | `k` tokens | No | 20–100 | Crude tail cut, often combined |
| Top-p | Cumulative mass `p` | Yes | 0.9–0.95 | Default truncation on most APIs |
| Min-p | `p ≥ min_p × p_max` | Yes, strongly | 0.05–0.1 | Creative output at higher temperature |

This runnable demo implements the sampling pipeline in pure Python:

```python
import math
import random


def softmax(logits, temperature=1.0):
    if temperature <= 0:
        raise ValueError("use greedy() for temperature 0")
    z = [x / temperature for x in logits]
    m = max(z)
    e = [math.exp(x - m) for x in z]
    s = sum(e)
    return [x / s for x in e]


def renormalize(probs):
    s = sum(probs)
    return [p / s for p in probs]


def top_k(probs, k):
    keep = set(sorted(range(len(probs)), key=probs.__getitem__, reverse=True)[:k])
    return renormalize([p if i in keep else 0.0 for i, p in enumerate(probs)])


def top_p(probs, p):
    """Keep the smallest set of tokens whose cumulative probability >= p."""
    keep, total = set(), 0.0
    for i in sorted(range(len(probs)), key=probs.__getitem__, reverse=True):
        keep.add(i)
        total += probs[i]
        if total >= p:
            break
    return renormalize([q if i in keep else 0.0 for i, q in enumerate(probs)])


def min_p(probs, ratio):
    """Keep tokens with probability >= ratio * max probability."""
    floor = ratio * max(probs)
    return renormalize([q if q >= floor else 0.0 for q in probs])


def penalize(logits, generated, repetition=1.0, frequency=0.0, presence=0.0):
    out = list(logits)
    counts = {}
    for t in generated:
        counts[t] = counts.get(t, 0) + 1
    for t, c in counts.items():
        if repetition != 1.0:  # CTRL-style: shrink positive, grow negative
            out[t] = out[t] / repetition if out[t] > 0 else out[t] * repetition
        out[t] -= c * frequency + presence  # OpenAI-style additive penalties
    return out


def greedy(logits):
    return max(range(len(logits)), key=logits.__getitem__)


def sample(probs, rng):
    r, acc = rng.random(), 0.0
    for i, p in enumerate(probs):
        acc += p
        if r < acc:
            return i
    return max(i for i, p in enumerate(probs) if p > 0)  # float round-off


def entropy(probs):
    return -sum(p * math.log2(p) for p in probs if p > 0)


if __name__ == "__main__":
    vocab = ["Paris", "Lyon", "London", "Nice", "banana"]
    logits = [5.0, 3.0, 2.5, 2.0, -1.0]

    for t in (0.2, 0.7, 1.0, 1.5):
        probs = softmax(logits, t)
        print(f"T={t}: " + " ".join(f"{w}={p:.3f}" for w, p in zip(vocab, probs))
              + f"  entropy={entropy(probs):.2f} bits")
    assert entropy(softmax(logits, 0.2)) < entropy(softmax(logits, 1.0)) \
        < entropy(softmax(logits, 1.5))
    assert greedy(logits) == 0
    assert softmax(logits, 0.05)[0] > 0.999      # low T approaches greedy

    probs = softmax(logits)
    assert sum(q > 0 for q in top_k(probs, 2)) == 2
    nucleus = top_p(probs, 0.9)
    print("top_p=0.9 keeps", [w for w, q in zip(vocab, nucleus) if q > 0])
    assert nucleus[-1] == 0.0                    # the long tail is cut
    assert abs(sum(nucleus) - 1) < 1e-12

    # min-p adapts: a confident distribution keeps few tokens, a flat one many
    confident = softmax([8.0, 2.0, 1.0, 0.5, 0.0])
    flat = softmax([1.0, 0.9, 0.8, 0.7, 0.6])
    assert sum(q > 0 for q in min_p(confident, 0.1)) == 1
    assert sum(q > 0 for q in min_p(flat, 0.1)) == 5

    # frequency/presence penalties lower already-used tokens
    pen = penalize(logits, generated=[0, 0, 0], frequency=0.5, presence=0.5)
    assert pen[0] == 5.0 - 3 * 0.5 - 0.5 and pen[1:] == logits[1:]
    assert penalize([2.0, -2.0], [0, 1], repetition=2.0) == [1.0, -4.0]

    # same seed, same draws; frequencies match the distribution
    a = [sample(probs, random.Random(42)) for _ in range(3)]
    b = [sample(probs, random.Random(42)) for _ in range(3)]
    assert a == b
    rng = random.Random(7)
    draws = [sample(probs, rng) for _ in range(20_000)]
    assert abs(draws.count(0) / len(draws) - probs[0]) < 0.02
```

At `T=0.2` the top token takes essentially all the mass; at `T=1.5` "banana" rises from 0.2% to about 1%. Over a 500-token answer, a 1% chance per step of a nonsense token is a near certainty somewhere, which is why high temperature needs truncation.

### Repetition, frequency and presence penalties

| Penalty | Formula on the logit of a token already seen | Typical range |
|---|---|---|
| Repetition (CTRL-style) | Divide by `r` if positive, multiply by `r` if negative | 1.0–1.3 |
| Frequency (OpenAI-style) | Subtract `count × α` | 0–1 (API allows −2 to 2) |
| Presence (OpenAI-style) | Subtract `β` once if the count is above 0 | 0–1 (API allows −2 to 2) |

The repetition penalty comes from the CTRL paper (Keskar et al., 2019) and is common in open-source engines; implementations often count tokens in the prompt as well as the output. Frequency and presence penalties are the OpenAI API's additive versions. Modern instruct models rarely need any of them. They hurt whenever repetition is correct: JSON keys, code identifiers, names, table columns, legal text that must quote exactly. If structured output breaks after someone "fixed repetition," check the penalties first.

## Logprobs and their uses

Many APIs can return the log-probability of each generated token and the top alternatives at each position. Illustrative, with the OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI()
resp = client.chat.completions.create(
    model="gpt-4o-mini",               # example; use a model that supports logprobs
    messages=[{"role": "user", "content":
               "Is this review positive or negative? Answer with one word.\n"
               "Review: The battery died after two days."}],
    max_tokens=1,
    temperature=0,
    logprobs=True,
    top_logprobs=5,
)
for alt in resp.choices[0].logprobs.content[0].top_logprobs:
    print(alt.token, round(alt.logprob, 3))
```

Uses:

- **Classification with a confidence score.** Constrain the answer to label words, read the probability of each label token, and threshold. Route low-confidence items to a stronger model or a human. Watch for the variants: `"Positive"`, `" positive"` and `"positive"` are different tokens, and you should sum over them.
- **Calibration checks.** Bucket predictions by probability and compare to accuracy on labeled data. Post-trained models are often overconfident, so calibrate on your data before trusting raw numbers.
- **Scoring and ranking.** The sum of token log-probabilities is the model's log-likelihood of a text. Divide by length to compare candidates of different lengths. Useful for reranking generated candidates and detecting unusual inputs.
- **Flagging uncertain spans.** Low-probability tokens in a generated answer (names, numbers, dates) are candidates for verification. Low probability is a hint, not proof of a hallucination, and high probability is no guarantee of truth.

Availability varies: as of 2025–2026, some providers expose logprobs and some do not, reasoning models often do not, and self-hosted engines (vLLM, SGLang, TGI) do. Design features so that losing logprobs degrades gracefully.

## Seeds and nondeterminism

A seed fixes the random number generator used for sampling. It does not fix the logits, and the logits are where most nondeterminism comes from:

- **Floating-point addition is not associative.** GPU kernels sum in different orders depending on batch size, sequence position and the kernel chosen. Results differ in the last bits.
- **Batching changes the math.** Your request is batched with others, and batch size changes which kernel and reduction strategy runs. Recent analysis (He and Thinking Machines Lab, 2025) attributes most serving nondeterminism to this lack of batch invariance rather than to concurrency races.
- **Near-ties flip.** A difference in the last bits changes the argmax when two tokens are nearly tied, and from that point the continuation diverges completely.
- **Other sources:** MoE routing that depends on other requests in the batch, prefix-cache hits versus misses, different GPU types or engine versions across replicas, and silent model updates behind an alias.

```python
import math


def sum_in_order(xs):
    total = 0.0
    for x in xs:
        total += x
    return total


if __name__ == "__main__":
    a = sum_in_order([1e16, 1.0, -1e16, 1.0])   # 1.0 is lost next to 1e16
    b = sum_in_order([1e16, -1e16, 1.0, 1.0])   # same numbers, new order
    print(a, b, math.fsum([1e16, 1.0, -1e16, 1.0]))
    assert a == 1.0 and b == 2.0                # order changed the result
```

The same effect, at the scale of millions of additions in bf16, is enough to change a token. Practical consequences: OpenAI's `seed` is documented as best effort; local engines can be made deterministic with fixed batch size, deterministic kernels and a seed, at a throughput cost. Design tests to assert properties (valid schema, correct label, contains the cited fact), not exact strings, and cache responses when you need repeatability.

## Stopping

Generation ends when one of these fires, and the API tells you which:

- **End-of-turn or EOS token.** The normal finish (`finish_reason: "stop"` or `stop_reason: "end_turn"`).
- **Stop sequences.** Strings that end generation when they appear in the decoded text, usually excluded from the output. Useful with base models and delimited formats. Servers match on text, so a stop string split across tokens still works.
- **`max_tokens`.** A hard cap on output tokens (`finish_reason: "length"`). The output is truncated wherever it was, so check the reason before parsing. For reasoning models, hidden reasoning tokens count against the output budget. OpenAI uses `max_completion_tokens` for those models, and Anthropic's extended thinking budget must be smaller than `max_tokens`, as of 2025–2026.
- **Tool calls.** The model stops to request a tool (`tool_calls` or `tool_use`).

## Constrained decoding (preview)

You can make invalid output impossible rather than unlikely. At every step, mask the logits of tokens that would violate a grammar, regex or JSON schema, then sample from what remains. Libraries such as outlines and XGrammar compile a schema into a state machine over tokens, and provider "structured output" modes do the same server-side. `logit_bias` is the crude version: add or subtract a fixed amount for specific token ids. Constraining guarantees syntax, not correct content, and overly tight constraints can lower quality. See [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}). Speculative decoding, by contrast, changes speed without changing the output distribution; see [Speculative decoding]({{ '/llm/speculative-decoding/' | relative_url }}).

## In practice

| Task | Temperature | Truncation | Other |
|---|---|---|---|
| Classification, extraction, routing | 0 | none | Schema or label constraint, logprobs for confidence |
| Structured output (JSON) | 0–0.2 | none | Constrained decoding, no penalties |
| RAG answers, summaries | 0.1–0.4 | top-p 0.9–1.0 | Stop on end of answer |
| Code generation | 0–0.4 | top-p 0.95 | Sample several and test when you can |
| Chat assistant | 0.5–0.8 | top-p 0.9–0.95 | Provider defaults are reasonable |
| Brainstorming, creative | 0.8–1.2 | min-p 0.05–0.1 or top-p 0.95 | Diversity across several samples |

Parameter support differs by provider, as of 2025–2026: temperature ranges differ (0–2 on OpenAI, 0–1 on Anthropic), `top_k` exists on Anthropic and Gemini but not on OpenAI chat, and several reasoning models reject or ignore temperature and top-p. A multi-provider client should map and validate parameters per provider; see [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}).

For self-hosted models, illustrative vLLM usage:

```python
from vllm import LLM, SamplingParams

llm = LLM(model="Qwen/Qwen2.5-7B-Instruct")
params = SamplingParams(temperature=0.7, top_p=0.95, min_p=0.05,
                        max_tokens=512, stop=["\n\n###"], seed=1234,
                        logprobs=5)
out = llm.generate(["Write a haiku about database indexes."], params)
print(out[0].outputs[0].text)
```

## Pitfalls

- **High temperature without truncation.** Symptom: occasional garbage tokens or language switches mid-answer. Fix: lower temperature, add top-p or min-p.
- **Penalties corrupting structure.** Symptom: misspelled JSON keys, renamed variables, broken tables. Fix: remove repetition and frequency penalties for structured tasks.
- **Exact-match tests on LLM output.** Symptom: flaky CI even at `temperature=0`. Fix: property-based assertions, schema validation, cached fixtures.
- **Unhandled `length` finishes.** Symptom: truncated JSON or half answers on long inputs. Fix: check `finish_reason`, raise `max_tokens`, or request shorter output.
- **Trusting raw logprobs as confidence.** Symptom: 0.99 confidence on wrong answers. Fix: calibrate on labeled data and set thresholds empirically.
- **Label tokens that don't match.** Symptom: logprob classifier returns near-zero for every label. Fix: inspect the exact tokens the model emits, including leading spaces and casing.
- **Sending unsupported parameters.** Symptom: 400 errors after switching to a reasoning model or another provider. Fix: per-model parameter maps.

## Rules of thumb

- Use `temperature=0` for anything you parse, then rely on validation, not determinism.
- Tune one of temperature or top-p, not both.
- Prefer min-p or top-p over top-k for open-ended text.
- Leave penalties at their defaults (off) unless you see real loops.
- Always check the finish reason.
- Read logprobs as relative signals and calibrate before thresholding.

## Interview questions

- **What does temperature do mathematically?** Divides logits by `T` before softmax. Below 1 it sharpens the distribution, above 1 it flattens it; ranking is unchanged and `T → 0` approaches greedy.
- **Top-p vs top-k?** Top-k keeps a fixed number of tokens; top-p keeps the smallest set covering probability mass `p`, so it adapts to how confident the model is.
- **What is min-p and why might it beat top-p?** It keeps tokens whose probability is at least a fraction of the top token's, so truncation tracks confidence directly and stays coherent at higher temperatures.
- **Why not use beam search for chat?** It optimizes total sequence probability, which favors generic, repetitive text, and multiplies compute. It suits tasks with one correct answer, like translation.
- **Why can `temperature=0` with a seed still be nondeterministic?** The logits themselves vary: batch composition changes floating-point reduction order, near-ties flip, and MoE routing and model updates add drift. The seed only controls sampling randomness.
- **How would you build a classifier with confidence scores from an LLM?** Constrain output to label tokens, read their logprobs (summing over spelling variants), calibrate against labeled data, and route low-confidence cases to a fallback.
- **When do repetition penalties hurt?** Whenever the correct output repeats tokens: JSON keys, code identifiers, names, quoted text.

## Further reading

- Holtzman et al., 2020. "The Curious Case of Neural Text Degeneration."
- Fan, Lewis and Dauphin, 2018. "Hierarchical Neural Story Generation" (top-k sampling).
- Keskar et al., 2019. "CTRL: A Conditional Transformer Language Model for Controllable Generation."
- Nguyen et al., 2024. "Turning Up the Heat: Min-p Sampling for Creative and Coherent LLM Outputs."
- Willard and Louf, 2023. "Efficient Guided Generation for Large Language Models."
- He and Thinking Machines Lab, 2025. "Defeating Nondeterminism in LLM Inference."
