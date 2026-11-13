---
title: Speculative decoding
section: llm
order: 44
phase: "Phase 7 · Inference and serving"
summary: Draft-then-verify decoding, why rejection sampling keeps outputs exact, acceptance-rate speedup math, draft models, Medusa, EAGLE and prompt lookup.
---

At small batch sizes, a decode step reads every weight from memory to produce a single token per sequence and leaves most of the GPU's compute idle (see [How inference works]({{ '/llm/inference-basics/' | relative_url }})). Running the model over five tokens instead of one costs almost the same wall time, because the step is dominated by reading weights, not by math. Speculative decoding exploits that: something cheap guesses the next few tokens, the large model checks all the guesses in one forward pass, and every correct guess is a token produced without its own memory-bound step. Done with the right acceptance rule, the output follows exactly the same distribution as normal decoding. It is the main lever for cutting TPOT on latency-sensitive, low-concurrency workloads such as interactive coding assistants, agents waiting on each tool call, and voice.

## How it works

### Draft, verify, accept

Each round has three steps:

1. **Draft.** A cheap proposer generates `γ` candidate tokens autoregressively (a small model, extra heads, or a lookup in the prompt).
2. **Verify.** The target model runs one forward pass over the prefix plus all `γ` draft tokens, producing its own next-token distribution `p` at each of the `γ + 1` positions. Thanks to the KV cache this pass costs about one decode step when decode is memory-bound.
3. **Accept.** Walk the drafts left to right, accepting each with a probabilistic test. At the first rejection, sample a corrected token and discard the remaining drafts. If all `γ` pass, sample one bonus token from the last position's distribution.

```text
prefix: "The capital of France is"
draft (γ = 4):    Paris   .     It    is
target pass:      one forward pass scores all four positions (+1)
accept test:      yes     yes   no    (not checked)
emitted:          Paris   .     The   <- 2 accepted + 1 corrected token

1 target pass + 4 cheap draft steps -> 3 tokens instead of 1
rejected positions' KV entries are discarded; the draft re-syncs
```

Each round emits between 1 and `γ + 1` tokens, never fewer than normal decoding.

### Why the output distribution is exact

Let `q(x)` be the draft's probability for token `x` and `p(x)` the target's. The rule from Leviathan et al. and Chen et al. (both 2023) is a form of rejection sampling:

```text
accept draft x with probability   min(1, p(x) / q(x))
on reject, sample from            r(x) = (p(x) − q(x))+ / Σ_y (p(y) − q(y))+

P(emit x) = q(x) · min(1, p(x)/q(x))  +  P(reject) · r(x)
          = min(p(x), q(x))           +  (1 − Σ_y min(p, q)) · r(x)

Σ_y (p − q)+ = 1 − Σ_y min(p, q), so the second term is (p(x) − q(x))+

P(emit x) = min(p(x), q(x)) + (p(x) − q(x))+ = p(x)
```

Tokens the draft over-predicts get thinned out by the acceptance test; tokens it under-predicts get topped up from the residual. The emitted token is distributed exactly as if the target had sampled it, regardless of how bad the draft is. A bad draft only costs speed. With greedy decoding the rule reduces to "accept while the draft token equals the target's argmax", so outputs match plain greedy decoding, up to the low-order floating-point differences any change in batch shape can cause.

The per-token acceptance probability is `α = Σ_x min(p(x), q(x)) = 1 − TV(p, q)`, one minus the total variation distance between the two distributions. Speculation works when the draft agrees with the target on the easy tokens: punctuation, boilerplate, code syntax, copied spans.

### Acceptance rate and speedup

Assuming each draft token is accepted independently with probability `α`:

```text
expected tokens per round  E = (1 − α^(γ+1)) / (1 − α)
cost per round             γ × c + v
                           c = draft step cost / target step cost
                           v = verify pass cost / normal decode step
                               (≈ 1 when memory-bound, larger at high batch)
speedup                    E / (γ × c + v)
```

Three consequences:

- **Acceptance rate dominates.** At `α = 0.8`, four drafts yield about 3.4 tokens per round; at `α = 0.5`, about 1.9.
- **There is an optimal `γ`.** Longer drafts add cost linearly while their chance of being reached decays geometrically. Typical deployments use 3–8 draft tokens.
- **Verification is only free when memory-bound.** At high batch the target is closer to compute-bound, `v` grows, and rejected tokens waste real FLOPs. Speculation helps latency at low batch and can reduce throughput at high batch.

## Techniques

| Method | Draft source | Training | Exact | Best for |
|---|---|---|---|---|
| Draft model | Small model sharing the target's tokenizer | None, or distill on target outputs | Yes | Model families with a small sibling |
| Medusa | Extra heads on the target's last hidden state, candidate tree | Train heads | Yes with rejection sampling; its "typical acceptance" option trades exactness for speed | Low-batch serving |
| EAGLE family | One-layer draft head fed the target's hidden features | Train head | Yes | Strong speedups; supported in major engines |
| MTP heads | The model's own multi-token prediction modules | Built in at pretraining | Yes | Models trained with MTP, e.g. DeepSeek-V3 |
| Layer skipping | The target with some layers skipped | None or light | Yes | No separate draft available |
| Lookahead decoding | n-grams from parallel Jacobi iterations | None | Yes | Generic, training-free |
| Prompt lookup (n-gram) | Spans copied from the prompt or earlier output | None | Yes | Extraction, editing, RAG answers, code edits |

**Separate draft model.** The classic setup: a model roughly 10–50x smaller from the same family. It must share the target's tokenizer and vocabulary, because verification compares probabilities token by token. Costs: draft weights and its own KV cache in GPU memory, and a scheduler that keeps the two in sync. Distilling the draft on the target's outputs (DistillSpec, for example) raises acceptance noticeably.

**Medusa.** Adds several lightweight decoding heads to the target; head `k` predicts the token `k + 1` positions ahead from the current hidden state. Their top candidates form a tree, and tree attention verifies many candidate continuations in one pass. Medusa-1 trains only the heads on a frozen backbone; Medusa-2 fine-tunes jointly for higher acceptance.

**EAGLE, EAGLE-2, EAGLE-3.** EAGLE observed that predicting the next *feature* (the target's second-to-top-layer hidden state) is easier than predicting the next token from scratch, and feeds the draft head both the features and the actually sampled token to resolve uncertainty. EAGLE-2 grows the draft tree dynamically based on draft confidence; EAGLE-3 fuses features from several layers and trains to scale with more data. As of 2025–2026, EAGLE-style heads are among the most effective drafters supported by vLLM, SGLang and TensorRT-LLM, and pre-trained heads exist for many popular open models.

**Multi-token prediction.** Models pretrained to predict several future tokens (Gloeckle et al., 2024; DeepSeek-V3's MTP module) can use those predictions as drafts at no extra training cost.

**Self-speculation.** Draft by running the target with layers skipped (Draft & Verify, LayerSkip), then verify with the full model. No second model to host.

**Prompt lookup decoding.** Match the last few generated tokens against the prompt and propose whatever followed the match there. Zero training and near-zero cost, and very effective when the output copies the input: summarizing with quotes, extracting fields, rewriting or refactoring code, answering from retrieved passages. Speculation on the copied spans lands almost every time.

### When it helps and when it doesn't

| Helps | Hurts or wastes |
|---|---|
| Low concurrency, memory-bound decode | High batch, compute-bound decode |
| Predictable output: code, JSON, extraction, edits | Creative, high-temperature sampling (lower acceptance) |
| Large gap between target and draft cost | Drafts nearly as expensive as the target |
| Long outputs where TPOT dominates latency | Very short outputs where TTFT dominates |
| Plenty of spare GPU memory | KV-bound deployments (draft KV competes for space) |

Engines may switch speculation off above a batch-size threshold or adapt the number of draft tokens to load; check what yours does. Also check whether it supports speculation together with structured outputs, multi-LoRA and your sampling settings.

## In practice

### Rejection sampling preserves the target distribution

A deliberately bad draft, verified with the acceptance rule, still yields tokens distributed exactly as the target's; the acceptance rate equals `Σ min(p, q)`.

```python
import random


def speculative_token(p: list[float], q: list[float], rng: random.Random) -> tuple[int, bool]:
    """Draft one token from q, verify against p. Returns (emitted token, accepted?)."""
    vocab = range(len(p))
    x = rng.choices(vocab, weights=q)[0]
    if rng.random() < min(1.0, p[x] / q[x]):
        return x, True
    residual = [max(0.0, pi - qi) for pi, qi in zip(p, q)]
    return rng.choices(vocab, weights=residual)[0], False


if __name__ == "__main__":
    rng = random.Random(0)
    p = [0.50, 0.25, 0.15, 0.10]      # target's next-token distribution
    q = [0.30, 0.40, 0.05, 0.25]      # draft's distribution: wrong on purpose
    n = 200_000
    counts, accepted = [0] * len(p), 0
    for _ in range(n):
        x, ok = speculative_token(p, q, rng)
        counts[x] += 1
        accepted += ok
    empirical = [c / n for c in counts]
    print("target   ", p)
    print("emitted  ", [round(e, 3) for e in empirical])
    print(f"acceptance {accepted / n:.3f}, expected {sum(map(min, p, q)):.3f}")
    assert all(abs(e - t) < 0.01 for e, t in zip(empirical, p))
    assert abs(accepted / n - sum(map(min, p, q))) < 0.01
```

### Speedup math and the optimal draft length

```python
import random


def expected_tokens(alpha: float, gamma: int) -> float:
    """Tokens emitted per round: accepted drafts plus one corrected or bonus token."""
    if alpha == 1.0:
        return gamma + 1.0
    return (1 - alpha ** (gamma + 1)) / (1 - alpha)


def speedup(alpha: float, gamma: int, c: float, verify_cost: float = 1.0) -> float:
    return expected_tokens(alpha, gamma) / (gamma * c + verify_cost)


def best_gamma(alpha: float, c: float, verify_cost: float = 1.0, max_gamma: int = 10) -> int:
    return max(range(1, max_gamma + 1), key=lambda g: speedup(alpha, g, c, verify_cost))


def simulate_round(alpha: float, gamma: int, rng: random.Random) -> int:
    accepted = 0
    while accepted < gamma and rng.random() < alpha:
        accepted += 1
    return accepted + 1


if __name__ == "__main__":
    rng = random.Random(1)
    sim = sum(simulate_round(0.8, 4, rng) for _ in range(100_000)) / 100_000
    print(f"alpha 0.8, gamma 4: simulated {sim:.3f} vs formula {expected_tokens(0.8, 4):.3f}")
    assert abs(sim - expected_tokens(0.8, 4)) < 0.03

    print("alpha  draft cost  best gamma  tokens/round  speedup")
    for alpha in (0.6, 0.7, 0.8):
        for c in (0.05, 0.1):
            g = best_gamma(alpha, c)
            print(f"{alpha:5.1f}  {c:10.2f}  {g:10d}  {expected_tokens(alpha, g):12.2f}  "
                  f"{speedup(alpha, g, c):6.2f}x")

    assert abs(speedup(0.8, 4, 0.05) - 2.80) < 0.01
    assert best_gamma(0.6, 0.1) == 3
    assert speedup(0.3, 4, 0.3) < 1.0                  # poor draft: slower than plain decoding
    assert speedup(0.8, 4, 0.05, verify_cost=2.5) < 1.3  # high batch: verification is not free
```

The model is optimistic: real acceptance falls with draft position, and verifying more tokens costs more as batches grow. Use it to reason about direction and to sanity-check measured speedups, then measure.

### Prompt lookup in a dozen lines

```python
def prompt_lookup(tokens: list[str], ngram: int = 3, k: int = 5) -> list[str]:
    """Propose up to k draft tokens: find the latest earlier occurrence of the
    last `ngram` tokens and return what followed it."""
    if len(tokens) <= ngram:
        return []
    tail = tokens[-ngram:]
    for start in range(len(tokens) - ngram - 1, -1, -1):
        if tokens[start:start + ngram] == tail:
            return tokens[start + ngram:start + ngram + k]
    return []


if __name__ == "__main__":
    context = "Clause 7 : payment of 4,200 USD is due on 30 June per the schedule .".split()
    answer_so_far = "The contract says payment of 4,200".split()
    draft = prompt_lookup(context + answer_so_far, ngram=2, k=5)
    print("draft:", draft)
    assert draft == ["USD", "is", "due", "on", "30"]
    assert prompt_lookup("a b c d".split(), ngram=2) == []
```

Real implementations work on token ids, try several n-gram sizes from largest to smallest, and search the generated output as well as the prompt.

### Turning it on

Illustrative vLLM configuration; recent versions take a JSON `--speculative-config`, older ones used separate flags, and key names vary by version:

```bash
# Separate draft model (must share the target's tokenizer)
vllm serve <target-model> \
  --speculative-config '{"model": "<draft-model>", "num_speculative_tokens": 5}'

# Prompt lookup (n-gram): no extra model
vllm serve <target-model> \
  --speculative-config '{"method": "ngram", "num_speculative_tokens": 5, "prompt_lookup_max": 4}'
```

In Hugging Face transformers, assisted generation and prompt lookup are `generate` arguments (illustrative):

```python
from transformers import AutoModelForCausalLM, AutoTokenizer

tok = AutoTokenizer.from_pretrained("<target-model>")
target = AutoModelForCausalLM.from_pretrained("<target-model>", device_map="auto")
draft = AutoModelForCausalLM.from_pretrained("<draft-model>", device_map="auto")

inputs = tok("Refactor this function to use pathlib: ...", return_tensors="pt").to(target.device)
out = target.generate(**inputs, assistant_model=draft, max_new_tokens=256)
out = target.generate(**inputs, prompt_lookup_num_tokens=10, max_new_tokens=256)
```

### Measuring

Run the same production-like prompts with speculation on and off at concurrency 1, 8, 32 and your peak. Record acceptance rate, mean accepted tokens per target step, TPOT, E2E, and aggregate throughput. Engines expose acceptance counters in their metrics. Expect the gain to shrink as concurrency rises and to vary by traffic type: a code-editing route may see large wins while a creative-writing route sees none, so enable it per deployment or per route.

## Pitfalls

- **Benchmarking at batch 1 only.** Speculation looks great alone and can cut throughput at production concurrency. Measure across the load range.
- **Tokenizer mismatch.** A draft with a different vocabulary cannot be verified token by token. Use the same family or a purpose-built head.
- **Low acceptance from domain mismatch.** A general draft on a specialized domain. Symptom: accepted length near 1. Fix: distill the draft or train a head on your traffic, or use prompt lookup where outputs copy inputs.
- **Memory pressure.** Draft weights and draft KV reduce the KV budget for the target, lowering concurrency. Check the engine's capacity log with speculation enabled.
- **Assuming every method is exact.** Rejection-sampling verification is; relaxed schemes such as Medusa's typical acceptance are not. Know which one you enabled before comparing eval scores.
- **Ignoring feature interactions.** Structured output, multi-LoRA, some samplers or penalties may be unsupported with speculation, or silently disable it.

## Rules of thumb

- Use it when TPOT matters and concurrency is low; skip it for throughput-bound batch jobs.
- Try prompt lookup first for extraction, editing and RAG-style answers: it is free.
- For general traffic, prefer an EAGLE-style head or a distilled draft over a generic small model.
- Keep 3–8 draft tokens; tune on measured acceptance, not intuition.
- Watch acceptance rate and accepted length per route; they explain most speedup surprises.

## Interview questions

**Why does verifying several tokens cost about the same as generating one?**
At low batch, decode is memory-bound: the pass is dominated by reading the weights. Scoring `γ + 1` positions in the same pass reuses the same weight reads, so it adds little time until compute becomes the limit.

**How can speculative sampling be exact when the draft is a different model?**
Each draft token is accepted with probability `min(1, p/q)` and, on rejection, a replacement is drawn from the normalized residual `max(0, p − q)`. The sum of the two paths equals `p(x)` for every token, so the output distribution is the target's.

**Derive the expected speedup.**
With per-token acceptance `α` and `γ` drafts, a round emits `(1 − α^(γ+1)) / (1 − α)` tokens and costs `γc + v` target-step equivalents, where `c` is the draft's relative cost and `v` the verify pass cost. Speedup is their ratio, for example about 2.8x at `α = 0.8`, `γ = 4`, `c = 0.05`, `v = 1`.

**Why does speculation help less at high batch sizes?**
The verify pass stops being free once the batch makes the target compute-bound, and every rejected token is wasted compute that other sequences could have used.

**Draft model vs Medusa vs EAGLE?**
A draft model is a separate smaller model with its own weights and KV. Medusa adds heads that predict several positions ahead from the last hidden state. EAGLE trains a small autoregressive head on the target's hidden features, which usually gives higher acceptance. Heads avoid hosting a second model but must be trained per target.

**When is prompt lookup decoding the right choice?**
When outputs copy long spans of the input: extraction, quoting summaries, code edits, answers grounded in retrieved text. It needs no training or extra memory.

## Further reading

- Leviathan, Kalman and Matias, 2023, "Fast Inference from Transformers via Speculative Decoding".
- Chen et al., 2023, "Accelerating Large Language Model Decoding with Speculative Sampling".
- Cai et al., 2024, "Medusa: Simple LLM Inference Acceleration Framework with Multiple Decoding Heads".
- Li et al., 2024, "EAGLE: Speculative Sampling Requires Rethinking Feature Uncertainty".
- Fu et al., 2024, "Break the Sequential Dependency of LLM Inference Using Lookahead Decoding".
- Miao et al., 2023, "SpecInfer: Accelerating Generative Large Language Model Serving with Tree-based Speculative Inference and Verification".
