---
title: Distillation and small models
section: llm
order: 37
phase: "Phase 6 · Fine-tuning"
summary: Train a small model to match a large one with soft targets or teacher outputs, build synthetic data safely, and prove the student is good enough.
---

The most common fine-tuning story in production goes like this: you prototype a feature with a frontier model and a careful prompt, it works, and then the bill and the latency arrive. Distillation is how you keep the behavior and lose the cost. A large **teacher** produces the targets, a small **student** learns to reproduce them on your task, and you end up serving a 1–8B model that answers in tens of milliseconds for a fraction of the price. The teacher's knowledge is broad; your task is narrow. That gap is what makes distillation work.

## Why small models win

For a narrow task, a fine-tuned small model often matches a general large model, and it is cheaper and faster on every axis that matters in serving:

| | ~8B model | ~70B model |
|---|---|---|
| BF16 weights | ~16 GB | ~140 GB |
| GPUs to serve (80 GB class) | 1, with room for KV cache | 2–4 with tensor parallelism |
| Weight bytes read per decode step | ~16 GB | ~140 GB |
| Single-stream decode on an H100 (bandwidth bound, ~3.35 TB/s) | roughly 150–200 tok/s | far lower per GPU spent |
| API price (as of 2025–2026, typical) | cents per million tokens | 10x or more higher |

Decode is memory-bandwidth bound (see [how inference works]({{ '/llm/inference-basics/' | relative_url }})), so tokens per second scale roughly inversely with the bytes of weights read per token. A model 9x smaller is close to 9x cheaper to run per token, before you even quantize it. It also fits on cheaper GPUs, on-prem, or on device, and it is yours: no provider deprecations, rate limits or data leaving your network.

What you give up: breadth. The student is good at the distribution you trained it on and worse than the teacher everywhere else. That is fine for extraction, classification, routing, rewriting in a house style, and narrow Q&A. It is not fine for open-ended agents that hit novel situations.

## How it works

### Soft targets and temperature

Classic knowledge distillation (Hinton et al., 2015) trains the student on the teacher's full output distribution, not just the correct label. A teacher that says "refund 0.80, billing 0.18, cancel 0.02" tells the student that refund and billing are related; a one-hot label throws that information away. Hinton called it dark knowledge.

The teacher's distribution is usually very peaked, so you soften both distributions with a temperature `T > 1` before comparing them:

```text
p_T = softmax(z_teacher / T)          q_T = softmax(z_student / T)

loss = alpha * CE(y_true, softmax(z_student))
     + (1 - alpha) * T^2 * KL(p_T || q_T)
```

The `T^2` factor keeps the soft-target gradient magnitude comparable to the hard-label one as you change `T`. Typical values: `T` between 1 and 4, `alpha` between 0.1 and 0.5.

```python
import math


def softmax(logits: list[float], T: float = 1.0) -> list[float]:
    m = max(logits)
    exps = [math.exp((z - m) / T) for z in logits]
    s = sum(exps)
    return [e / s for e in exps]


def kl(p: list[float], q: list[float]) -> float:
    """KL(p || q) in nats; p is the target (teacher) distribution."""
    return sum(pi * math.log(pi / qi) for pi, qi in zip(p, q) if pi > 0)


def entropy(p: list[float]) -> float:
    return -sum(pi * math.log(pi) for pi in p if pi > 0)


def kd_loss(teacher, student, label, T=2.0, alpha=0.5) -> float:
    """alpha * hard CE + (1 - alpha) * T^2 * KL(teacher_T || student_T)."""
    hard = -math.log(softmax(student)[label])
    soft = kl(softmax(teacher, T), softmax(student, T))
    return alpha * hard + (1 - alpha) * T * T * soft


if __name__ == "__main__":
    # Intent classes: refund, billing, cancel, shipping. True label: refund.
    teacher = [6.0, 4.5, 1.0, -2.0]   # refund, with billing a close second
    student = [3.0, 0.0, 2.5, 0.0]    # right argmax, wrong runner-up

    p1, p4 = softmax(teacher, T=1.0), softmax(teacher, T=4.0)
    assert entropy(p4) > entropy(p1)   # higher T = softer distribution
    assert p4[1] > p1[1]               # runner-up gets visible mass
    print("teacher T=1:", [round(x, 3) for x in p1])
    print("teacher T=4:", [round(x, 3) for x in p4])

    # KL is zero only for identical distributions, and it is asymmetric.
    pt, qt = softmax(teacher, 2.0), softmax(student, 2.0)
    assert kl(pt, pt) == 0.0 and kl(pt, qt) > 0
    assert abs(kl(pt, qt) - kl(qt, pt)) > 1e-3

    # A student that matches the teacher's ranking scores better than one
    # that only gets the top class right.
    aligned = [5.0, 4.0, 1.5, -1.0]
    before, after = kd_loss(teacher, student, 0), kd_loss(teacher, aligned, 0)
    assert after < before
    print(f"KD loss {before:.3f} -> {after:.3f}")
```

### Token-level distillation for LLMs

For a language model, the "class" is the next token and the distribution is over the whole vocabulary at every position. Token-level (logit) distillation sums the KL above over every position of a sequence. It is the most information-rich signal, and several open-weight families trained their small models this way from larger siblings (Gemma 2's smaller sizes and Llama 3.2 1B/3B, as reported by their developers). Constraints:

- **Same tokenizer.** The KL compares distributions over the same vocabulary. Cross-tokenizer distillation exists but is research territory.
- **Access to logits.** You need the teacher's full or top-k distribution per position. Closed APIs expose no logprobs or only a handful of top logprobs, so logit distillation effectively requires an open-weight teacher.
- **Storage.** Full distributions are huge (vocab of 100k+ times sequence length). Store only top-k (say 20–100) per position, or compute teacher logits on the fly.

**Forward vs reverse KL.** `KL(teacher || student)` (forward) is mode-covering: the student spreads mass over everything the teacher considers plausible, which can produce bland or incoherent mixtures when the student is too small. `KL(student || teacher)` (reverse) is mode-seeking: the student commits to the teacher's main modes. **On-policy distillation** (GKD, 2024) goes further: the student generates sequences and the teacher scores those, so the student learns to recover from its own mistakes, the same exposure-bias fix that online RL provides. TRL ships a `GKDTrainer` for this.

### Sequence-level distillation

Sequence-level distillation (Kim and Rush, 2016) skips logits entirely: the teacher generates complete outputs, and you [SFT]({{ '/llm/supervised-fine-tuning/' | relative_url }}) the student on them. This is what most teams do, because it works with any teacher, including closed APIs, and with any student tokenizer. It is also how reasoning was transferred into small open models in 2025: DeepSeek fine-tuned Qwen and Llama models on around 800k samples generated by DeepSeek-R1, and the distilled models beat much larger non-reasoning models on math benchmarks.

| | Token-level (logit) KD | Sequence-level KD |
|---|---|---|
| Signal | Full distribution per token | Teacher's sampled text |
| Teacher access | Open weights (logits) | Any, including APIs |
| Tokenizer | Must match | Anything |
| Cost | Teacher forward pass per training token | One generation per example |
| Typical use | Model builders pretraining small models | Application teams distilling a task |

```text
  production inputs (real, scrubbed)
              |
     teacher + best prompt  ----> N candidates per input
              |
     filter: validators, tests, judge, dedupe, decontaminate
              |
     curated (input, output) pairs ----> SFT / LoRA on student
              |                                   |
     held-out REAL eval set  <---- compare ---- student
```

## Choosing the student

| Factor | Guidance |
|---|---|
| Size | Set by your latency and hardware budget first. Try the smallest that might work (1–3B for classification and extraction, 7–8B for generation with reasoning), then step up if the eval says so. |
| Family | For logit KD, the same family as the teacher (shared tokenizer). For sequence KD, any. |
| Starting point | The instruct variant at that size, unless you need a clean base for a very different format. |
| License | Must allow commercial use and fine-tuning for your case. |
| Context length | Must cover your p99 input plus output. |
| Languages and domain | Check the base model's tokenizer efficiency and quality on your languages before training. |

See the [model landscape]({{ '/llm/model-landscape/' | relative_url }}) for current open-weight families.

## Synthetic data generation

The teacher is only as good as the pipeline around it. Treat generation as a data engineering job with stages you can rerun:

1. **Seed inputs.** Use real production inputs wherever possible (scrubbed of PII). Synthetic inputs drift toward what the generator finds typical and miss the long tail. If you must synthesize inputs, condition on varied seeds (personas, topics, difficulty levels) to avoid collapse into near-duplicates.
2. **Teacher prompt.** Use your best prompt, few-shot examples and retrieval context. Asking the teacher to reason first and then answer often improves labels; decide whether the student should learn the reasoning or just the answer.
3. **Sample and select.** Generate several candidates per input and keep the best: **rejection sampling** with a verifier (schema validation, unit tests, exact match against known answers) or a judge model. Self-consistency (majority vote across samples) is a cheap filter for label noise.
4. **Filter.** Drop outputs that fail validation, refusals, truncated outputs, and near-duplicates (MinHash or normalized-text hashing).
5. **Decontaminate.** Remove any example that overlaps your eval set (n-gram overlap or embedding similarity). Leakage makes the student look better than it is.
6. **Balance.** Check label and length distributions against production. Teachers overproduce the common case; upsample rare classes deliberately.
7. **Human spot-check.** Review a random sample (a few hundred) and measure the teacher's error rate. The student will inherit those errors and rarely exceed the teacher's accuracy on the same distribution.

Data volume: for narrow tasks, a few thousand clean examples typically beats tens of thousands of noisy ones. See [fine-tuning data]({{ '/llm/fine-tuning-data/' | relative_url }}) for formatting and quality checks.

## Licensing and terms of service

Check this before you generate a single example; it is the step teams skip and regret.

- **Closed API terms.** Several major providers' terms (OpenAI and Anthropic among them, as of 2025–2026) restrict using outputs to develop models that compete with them. How that applies to a narrow internal model (a ticket classifier, say) is a legal question, not an engineering one. Get a written answer from your legal team and keep it with the dataset.
- **Open-weight teacher licenses vary.** Permissive licenses (Apache 2.0, MIT) generally allow it; DeepSeek released R1 under MIT and explicitly permitted distillation. Custom community licenses can attach conditions to models trained on outputs. The Llama 3.1 license, for example, requires derived models that are distributed to include "Llama" in their name. Read the specific license version.
- **Dataset inheritance.** Datasets built from model outputs carry the teacher's restrictions. Stanford's Alpaca was released for research use only, in part because its data came from an OpenAI model.
- **Your own data.** Sending production inputs to a teacher API is a data transfer. It needs the same review as any other processor: DPA, retention settings, PII scrubbing. See [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).
- **Provenance.** Record teacher model and version, prompt version, date and filters for every dataset. You will be asked.

## Evaluating the student against the teacher

The question is not "is the student good?" but "is the student good enough to replace the teacher for this traffic?"

- **Evaluate on real, held-out data.** Never on synthetic data from the same generator; the student learned its quirks. Hold out by entity or time, not random rows.
- **Three-way comparison.** Score the teacher (with its production prompt), the student, and the student's base model before fine-tuning on the same eval set. The base-model score tells you how much the training did.
- **Task metrics first.** Exact match or field-level F1 for extraction, accuracy and per-class recall for classification, a calibrated judge for generation. Pairwise judge comparisons (student vs teacher, both orders) give a win/tie/loss rate.
- **Agreement rate.** The share of inputs where student and teacher agree. Inspect disagreements by hand: some are student errors, some are teacher errors the student fixed.
- **Slices.** Break down by category, input length, language and difficulty. Students usually match on the head and lose on the tail. Decide whether to route tail inputs to the teacher (a cascade) instead of pushing the student further.
- **Confidence and routing.** If the student exposes logprobs, low-confidence outputs make a good escalation signal. See [multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}).
- **Operational metrics.** p50/p95 latency, throughput per GPU and cost per 1k requests, measured under realistic concurrency.

## Pruning, briefly

Distillation shrinks a model by training a smaller one. **Pruning** shrinks an existing model by removing parts of it:

- **Unstructured** pruning zeroes individual weights (SparseGPT and Wanda are well-known one-shot methods). It reaches high sparsity with little quality loss, but GPUs only speed it up for specific patterns such as 2:4 semi-structured sparsity on recent NVIDIA hardware.
- **Structured** pruning removes whole layers, attention heads or hidden dimensions, producing a genuinely smaller dense model that runs fast everywhere, but it hurts quality more.

The two combine well: NVIDIA's Minitron work (2024) pruned width and depth from a larger model, then used distillation from the original to recover quality, at a fraction of the cost of training from scratch. For application teams, starting from an existing small model is usually simpler than pruning a large one, and [quantization]({{ '/llm/quantization/' | relative_url }}) is the cheaper first lever for serving cost.

## Pitfalls

- **Inherited teacher errors.** The student copies the teacher's mistakes with confidence. Symptom: student and teacher agree on wrong answers in the human spot-check. Fix: verify labels (tests, validators, humans on a sample) before training.
- **Synthetic-only evaluation.** The student scores 98% on synthetic eval and 85% on real traffic. Always hold out real data.
- **Mode collapse in generated data.** Thousands of near-identical inputs teach one pattern. Check n-gram diversity and cluster the embeddings; seed generation with varied real inputs.
- **Capacity mismatch.** A student far too small for the task learns surface patterns and fails on harder slices. Plot quality against student size on two or three sizes before committing.
- **Missing the long tail.** Head classes look great, rare ones collapse. Report per-class metrics and upsample.
- **Teacher drift.** API teachers change over time; regenerating data later gives a different dataset. Pin the teacher version and store outputs.
- **Silent license exposure.** A dataset made under one provider's terms gets reused for a public model. Keep provenance metadata with every dataset.

## Checklist

- Eval set of real, held-out inputs exists before any generation.
- Teacher, prompt and filters are versioned; outputs are stored, not regenerated.
- Legal sign-off on teacher terms and data transfer is on file.
- Labels are verified by validators or tests, and a human sample measured teacher error.
- Eval data is decontaminated from training data.
- Teacher, student and untuned base are scored on the same set, by slice.
- Latency and cost measured under load, and a fallback route to the teacher exists for low-confidence inputs.

## Interview questions

**What is knowledge distillation and why does temperature matter?** Training a student to match a teacher's output distribution, not just hard labels. Temperature softens both distributions so the small probabilities on wrong-but-related classes carry gradient; the KL term is scaled by `T^2` to keep its gradient magnitude stable.

**Token-level vs sequence-level distillation?** Token-level matches the teacher's per-token distribution and needs open weights and a shared tokenizer. Sequence-level fine-tunes the student on teacher-generated text and works with any teacher, which is why it dominates in application work.

**Forward vs reverse KL in distillation?** Forward KL (teacher to student) is mode-covering and can make a small student spread probability over too many options. Reverse KL is mode-seeking and keeps the student on the teacher's main modes. On-policy distillation trains on the student's own samples to fix exposure bias.

**Why can a 3B fine-tuned model beat a frontier model on a task?** A narrow task needs a small slice of capability. Fine-tuning concentrates the student's capacity on that slice and bakes in the format and domain conventions that the large model only gets from a prompt.

**How do you evaluate a distilled model?** On real held-out data, comparing teacher, student and untuned base with task metrics, pairwise judges and agreement rate, broken down by slice, plus latency and cost under load.

**What legal issues come with distillation?** Provider terms may prohibit using outputs to build competing models, open-weight licenses can attach conditions to derivatives, generated datasets inherit restrictions, and sending user data to a teacher is a data transfer needing privacy review.

**Distillation vs pruning vs quantization?** Distillation trains a different, smaller model. Pruning removes weights or structures from the existing model. Quantization keeps all weights at lower precision. Quantization is cheapest to try; they stack.

## Further reading

- "Distilling the Knowledge in a Neural Network", Hinton, Vinyals and Dean, 2015.
- "Sequence-Level Knowledge Distillation", Kim and Rush, 2016.
- "DistilBERT, a distilled version of BERT: smaller, faster, cheaper and lighter", Sanh et al., 2019.
- "On-Policy Distillation of Language Models: Learning from Self-Generated Mistakes", Agarwal et al., 2024 (GKD).
- "Compact Language Models via Pruning and Knowledge Distillation", Muralidharan et al., 2024 (Minitron).
- "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning", DeepSeek-AI, 2025.
