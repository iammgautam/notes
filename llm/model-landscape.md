---
title: The model landscape and choosing a model
section: llm
order: 7
phase: "Phase 1 · Foundations"
summary: Open-weight vs API models, sizes, MoE, reasoning and multimodal models, licenses and hosting, plus a repeatable way to choose a model and run a bake-off.
---

Model names change every quarter; the axes you choose along do not. A model choice is a bundle of decisions: who hosts it, what it costs per task, how fast it answers, where your data goes, what the license allows, and whether it clears your quality bar on your task. This note describes those axes, the main categories of models as of 2025–2026, and a process for choosing that survives the next release cycle: hard constraints first, then a quality gate on your own eval set, then cost and latency. Any specific model named here is an example of a category, not a recommendation, and will date quickly.

## The axes

### Closed API vs open-weight

| | Closed API model | Open-weight model |
|---|---|---|
| Access | HTTP API, provider controls the weights | Download weights, run anywhere the license allows |
| Quality frontier | Usually the strongest available | Typically a few months to a year behind on the hardest tasks, competitive on many |
| Data control | Data leaves your boundary; governed by contract and retention terms | Can stay entirely inside your infrastructure |
| Customization | Prompting, provider fine-tuning where offered | Full fine-tuning, LoRA, quantization, custom decoding |
| Cost shape | Per token, no idle cost | Per GPU-hour (or per token via a host), idle capacity costs money |
| Operations | None beyond rate limits and outages | Serving, scaling, upgrades, on-call |
| Longevity | Versions are deprecated and retired on the provider's schedule | The checkpoint is yours to keep |

Examples as of 2025–2026: closed families include OpenAI's GPT and o-series models, Anthropic's Claude and Google's Gemini; open-weight families include Meta's Llama, Mistral, Alibaba's Qwen, DeepSeek, Google's Gemma, Microsoft's Phi and OpenAI's gpt-oss.

"Open-weight" is not "open source." Most open models publish weights and inference code under custom or permissive licenses but not the training data or full training pipeline. The Open Source Initiative's Open Source AI Definition (2024) sets a stricter bar that few large models meet.

### Size

Parameter count sets memory, cost and, loosely, capability. Capability per parameter improves with each generation, so tiers shift, but the shape holds:

| Size | Weights in bf16 / 4-bit | Typical hosting | Good fit |
|---|---|---|---|
| 1–4B | 2–8 GB / 1–2 GB | Laptop, phone, CPU, small GPU | Classification, extraction, routing, autocomplete, on-device |
| 7–14B | 14–28 GB / 4–8 GB | One GPU | Narrow tasks, RAG over a domain, the usual fine-tuning target |
| 30–70B | 60–140 GB / 16–40 GB | 1–4 GPUs | Strong general use, tool use, harder reasoning |
| 100B+ and frontier | Hundreds of GB and up | Multi-GPU nodes, or API only | Hardest reasoning, long agentic tasks, broad knowledge |

Weights are only part of the memory: the KV cache grows with context and concurrency. See [Transformer architecture]({{ '/llm/transformer-architecture/' | relative_url }}) for the cache formula and [Quantization]({{ '/llm/quantization/' | relative_url }}) for what 4-bit costs in quality.

### Dense vs mixture of experts

A dense model uses all parameters for every token. A mixture-of-experts (MoE) model routes each token through a few expert sub-networks, so it has many more total parameters than active ones. Examples as of 2025–2026 include Mixtral, DeepSeek-V3, Llama 4, gpt-oss and Qwen3's MoE variants.

- **Speed and compute** track active parameters: an MoE with 3B active parameters generates tokens at roughly small-model cost.
- **Memory** tracks total parameters: all experts must be loaded.
- **Serving** favors high batch sizes. MoE is attractive for a provider or a busy self-hosted cluster, less so on a single small GPU.

When you compare models, compare active parameters for latency and cost, and total parameters for hardware.

### Reasoning models

Reasoning models are trained with reinforcement learning to produce a long internal chain of thought before the answer. As of 2025–2026, examples include OpenAI's o-series, DeepSeek-R1, Claude with extended thinking, Gemini's thinking models and Qwen3's thinking mode. Many expose an effort or thinking-budget control.

- **Better at:** math, code, multi-step planning, problems where checking intermediate steps helps.
- **Costs:** reasoning tokens are billed as output and add latency, often several times the visible answer.
- **Constraints:** some restrict sampling parameters, hide the raw reasoning, or do not return logprobs.
- **Not worth it for:** extraction, classification, short RAG answers and anything latency-sensitive, where a standard model is cheaper and as accurate.

See [Reasoning models]({{ '/llm/reasoning-models/' | relative_url }}).

### Multimodal

Image input is now common among frontier and many open models; audio and video input are available in some. Images are converted into tokens, so cost scales with resolution and image count, and a single high-resolution image can cost as much as a page or more of text. Image generation and speech synthesis are usually separate models or endpoints. Test document-heavy tasks (charts, scanned forms, tables) specifically: quality varies far more across models than on plain text. See [Multimodal models]({{ '/llm/multimodal-models/' | relative_url }}).

### Context length

As of 2025–2026, 128k tokens is a common context size, and several hosted models offer a million or more. Treat the headline as an upper bound:

- **Effective context is shorter.** Benchmarks such as RULER show many models degrading well before their advertised limit, especially on tasks that need several facts from different parts of the input.
- **Long prompts are slow and expensive.** Prefill time and cost scale with input length on every call.
- **Output limits are separate** and much smaller.

Retrieval plus a moderate context usually beats stuffing a huge one. See [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }}) and [Context engineering]({{ '/llm/context-engineering/' | relative_url }}).

### Licenses and terms

| License type | Examples (as of 2025–2026) | What to check |
|---|---|---|
| Permissive (Apache 2.0, MIT) | Mistral 7B and Mixtral, many Qwen and gpt-oss checkpoints (Apache 2.0), DeepSeek-R1 (MIT) | Attribution, notices |
| Custom community licenses | Llama community licenses | Acceptable use policy, a user-count threshold for very large services, attribution and naming rules |
| Model-specific terms | Gemma terms of use | Use restrictions that pass to derivatives |
| Non-commercial or research-only | Some research checkpoints | Whether production use is allowed at all |

Licenses differ even within one family and across versions; read the license file for the exact checkpoint. For closed APIs, the equivalent questions are in the terms and data processing agreement: whether inputs are used for training (major providers state that business API data is not, by default, as of 2025–2026), retention periods, zero-retention options, regional processing, and clauses restricting use of outputs to build competing models. See [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).

### Hosting options

| Option | Control | Ops burden | Cost shape | Data path |
|---|---|---|---|---|
| First-party API | Low | None | Per token | Provider |
| Cloud platform (Bedrock, Vertex AI, Azure) | Low to medium | Low | Per token or provisioned throughput | Your cloud account and region |
| Inference provider serving open weights | Medium | Low | Per token, often cheap | Third party |
| Self-hosted on your GPUs (vLLM, SGLang, TGI, TensorRT-LLM) | Full | High | Per GPU-hour | Your infrastructure |
| Local or edge (llama.cpp, Ollama) | Full | Per device | Hardware you own | Device |

The same open model can be reached several ways; you can start on an inference provider and move to self-hosting when volume justifies it. See [Serving engines]({{ '/llm/serving-engines/' | relative_url }}).

## A decision framework

```text
hard constraints ─► shortlist ─► quality gate ─► optimize ─► decide
privacy, region,    3–6 models   your eval set,  cost and    + fallback
license, on-prem,                pass/fail       latency     + re-eval
latency SLO                      thresholds      on passers    schedule
```

Evaluate the five axes in this order, because the early ones eliminate options and the later ones rank what is left:

| Axis | Measure | Typical deciders |
|---|---|---|
| Privacy and compliance | Data residency, retention, training use, certifications | Regulated data, customer contracts |
| Control | Ability to pin versions, fine-tune, run offline, inspect logprobs | Long-lived products, custom behavior, air-gapped deployments |
| Quality | Task score on your eval set, format validity, refusal rate | Every decision; set a threshold, not "best" |
| Latency | TTFT and end-to-end p50 and p95 at realistic prompt sizes | Interactive UX, agent loops with many steps |
| Cost | Cost per task with real token counts, at expected volume | High-volume pipelines, thin margins |

Patterns that hold up:

- **Establish the ceiling with a frontier model**, then try smaller and cheaper models to see which still clear the bar. The ceiling tells you whether the task is solvable and gives graders a reference.
- **Pick the cheapest model that passes, not the best scorer.** Quality above the threshold is usually worth less than the cost and latency it adds.
- **Route by difficulty.** Send most traffic to a cheap model and escalate hard or low-confidence cases. See [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}).
- **Fine-tune when the task is narrow, stable and high-volume**, and a small model gets close but not over the bar. See [When to fine-tune]({{ '/llm/when-to-fine-tune/' | relative_url }}).
- **Keep a fallback** from a second provider or a self-hosted model, tested on the same eval.

## Running a bake-off

1. **Define the task and success criteria.** What a correct output is, which fields must be right, what latency and cost per task are acceptable.
2. **Build the eval set from real inputs.** 100–300 examples is enough to separate models that differ meaningfully; stratify by input type and include hard and adversarial cases. Keep it private.
3. **Write graders.** Exact checks where possible (schema validation, field match, unit tests), LLM-as-judge with a rubric where not, and spot-check the judge. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).
4. **Give each model a fair prompt.** Start from one prompt and allow light per-model adjustments (formatting conventions, system prompt placement). A prompt tuned for one model unfairly penalizes the others.
5. **Run with production settings.** Same temperature, max tokens, tools and context sizes you will ship. Run more than once if outputs vary.
6. **Record everything per item.** Score, schema validity, input and output tokens from the API's usage field (tokenizers differ, so token counts differ), TTFT, total latency, refusals and errors.
7. **Apply gates, then compare.** Drop anything failing a hard requirement, then rank the rest by cost per task and latency. Check whether differences are real with a paired bootstrap over items.
8. **Read failures.** Twenty failed examples from each finalist tell you more than the aggregate score about whether a model's errors are acceptable.

This runnable sketch applies gates, picks the cheapest passing model, computes self-hosting cost per million tokens, and uses a paired bootstrap to check whether one model really beats another. All prices and scores are illustrative:

```python
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    name: str
    quality: float        # mean grader score on your eval set, 0..1
    schema_valid: float   # fraction of outputs that parse and validate
    p95_latency_s: float
    in_tokens: float      # mean per task, counted with this model's tokenizer
    out_tokens: float
    in_price: float       # USD per million tokens (illustrative)
    out_price: float

    def cost_per_1k_tasks(self) -> float:
        return 1000 * (self.in_tokens * self.in_price
                       + self.out_tokens * self.out_price) / 1e6


GATES = {"quality": 0.85, "schema_valid": 0.99, "p95_latency_s": 4.0}


def passes(c: Candidate) -> bool:
    return (c.quality >= GATES["quality"]
            and c.schema_valid >= GATES["schema_valid"]
            and c.p95_latency_s <= GATES["p95_latency_s"])


def choose(candidates):
    """Hard gates first, then the cheapest model that clears them."""
    ok = [c for c in candidates if passes(c)]
    return min(ok, key=Candidate.cost_per_1k_tasks) if ok else None


def selfhost_usd_per_mtok(gpu_hourly: float, gpus: int, tokens_per_s: float,
                          utilization: float) -> float:
    return gpu_hourly * gpus / (tokens_per_s * 3600 * utilization) * 1e6


def paired_bootstrap_ci(a, b, n=2000, seed=0):
    """95% CI of mean(b - a), resampling the same eval items."""
    rng = random.Random(seed)
    diffs = [y - x for x, y in zip(a, b)]
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs)
                   for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


if __name__ == "__main__":
    candidates = [
        Candidate("frontier-api", 0.93, 0.998, 6.5, 3000, 400, 3.00, 15.00),
        Candidate("mid-tier-api", 0.89, 0.995, 2.8, 3200, 420, 0.40, 1.60),
        Candidate("small-api", 0.81, 0.990, 1.2, 3200, 420, 0.10, 0.40),
        Candidate("open-8b-hosted", 0.86, 0.992, 1.9, 3400, 450, 0.20, 0.20),
    ]
    for c in candidates:
        print(f"{c.name:15s} pass={passes(c)!s:5s} "
              f"${c.cost_per_1k_tasks():6.2f} per 1k tasks")
    best = choose(candidates)
    print("choose:", best.name)
    assert best.name == "open-8b-hosted"

    busy = selfhost_usd_per_mtok(2.50, 1, tokens_per_s=2000, utilization=0.4)
    idle = selfhost_usd_per_mtok(2.50, 1, tokens_per_s=2000, utilization=0.1)
    print(f"self-host: ${busy:.2f}/M tokens at 40% util, ${idle:.2f} at 10%")
    assert 0.8 < busy < 0.9 and abs(idle - 4 * busy) < 1e-9

    items = range(200)
    a = [0 if i % 5 == 0 else 1 for i in items]                 # 80% correct
    b = [1 if i % 25 == 0 else x for i, x in zip(items, a)]     # 84% correct
    lo, hi = paired_bootstrap_ci(a, b)
    print(f"B - A: 95% CI [{lo:+.3f}, {hi:+.3f}]")
    assert lo > 0                                  # a real improvement
    c = list(a)
    c[1], c[2], c[5] = 0, 0, 1                     # lost 2 items, gained 1
    lo2, hi2 = paired_bootstrap_ci(a, c)
    print(f"C - A: 95% CI [{lo2:+.3f}, {hi2:+.3f}]")
    assert lo2 < 0 < hi2                           # indistinguishable
```

The frontier model scores highest but fails the latency gate; the small API model is cheapest but fails quality; the open 8B model on a hosted provider passes everything at less than half the cost of the mid-tier API. The bootstrap shows a 4-point gain on 200 items is distinguishable from noise, while a 0.5-point loss is not.

## In practice

### Self-hosting vs API math

The self-hosting function above makes the main point: GPU cost per token depends on utilization. One GPU at an illustrative $2.50/hour producing 2,000 tokens/s across concurrent requests costs about $0.87 per million tokens at 40% average utilization and about $3.47 at 10%. Compare that with per-token prices for a model of similar quality, then add what the formula leaves out:

- **Engineering and on-call time** to run serving, autoscaling, upgrades and incidents.
- **Idle and peak capacity.** Traffic is bursty; you pay for GPUs sized to the peak.
- **Throughput depends on workload.** Long prompts, long outputs and strict latency targets all lower tokens per second per GPU. Measure with your traffic shape; see [Batching and paged attention]({{ '/llm/batching-and-paged-attention/' | relative_url }}).

Self-hosting usually wins on cost only at sustained high volume, and it often wins on privacy, control and latency predictability regardless of cost.

### Sizing hardware quickly

```text
weights  = parameters × bytes each   (bf16 2, int8 1, 4-bit ~0.5)
KV cache = tokens in flight × KV bytes per token
total    ≈ weights + KV cache + 10–20% runtime overhead
```

A 70B dense model is about 140 GB in bf16, so it needs at least two 80 GB GPUs before any KV cache, and more for real concurrency. At 4-bit it is about 35–40 GB and fits one 80 GB GPU with room for cache. An 8B model in bf16 fits comfortably on a single 24 GB GPU for low concurrency. See [GPUs and parallelism]({{ '/llm/gpus-and-parallelism/' | relative_url }}).

### Keeping the decision current

- Pin model versions and record them with every request.
- Track provider deprecation schedules; plan migrations as eval-gated changes.
- Re-run the bake-off when a new model in your price band appears or your task changes, which is easy if the eval set and graders are already in CI.

## Pitfalls

- **Choosing from leaderboards.** Public benchmarks are contaminated, saturated or unrelated to your task. Use them to shortlist, never to decide.
- **One prompt for all models.** A prompt tuned on one model makes it look better than it is. Allow light per-model tuning.
- **Comparing prices per token instead of per task.** Different tokenizers and verbosity change token counts; reasoning models add hidden tokens. Measure cost per completed task.
- **Ignoring p95 latency.** Averages hide the tail that users and agent loops feel.
- **Floating aliases.** "Latest" aliases change behavior without a deploy. Pin versions.
- **License surprises.** A model chosen for quality turns out to forbid your use case or impose obligations on derivatives. Check before the bake-off.
- **Assuming open-weight means cheap.** Low-utilization self-hosting can cost more per token than an API.
- **Confusing total and active parameters.** An MoE's total size sets your hardware; its active size sets speed.

## Checklist

- Write down hard constraints (privacy, region, license, deployment, latency) before looking at models.
- Shortlist 3–6 candidates across tiers and providers.
- Build a private eval set of 100–300 real examples with graders.
- Run every candidate with production settings; record quality, validity, tokens, latency and cost per task.
- Apply gates, then choose the cheapest model that passes, with a tested fallback.
- Pin versions, log them, and schedule re-evaluation.

## Interview questions

- **How do you choose a model for a new feature?** List hard constraints, shortlist candidates, build a task-specific eval set, run a bake-off with production settings, gate on quality, format and latency, then pick the cheapest passing model and keep a fallback.
- **When would you self-host an open-weight model?** When data cannot leave your infrastructure, when you need fine-tuning or custom decoding the APIs do not offer, when volume is high and steady enough to keep GPUs busy, or when you need full control over versions and latency.
- **What is the difference between total and active parameters in an MoE?** Total is every parameter that must be in memory; active is what one token uses, which sets compute per token and speed.
- **When is a reasoning model worth its cost?** For multi-step math, code, planning and hard analysis where accuracy gains outweigh extra output tokens and latency; not for extraction, classification or short answers.
- **Why compare cost per task rather than price per token?** Tokenizers, verbosity and hidden reasoning tokens make token counts differ across models for the same task.
- **How do you know a model's improvement on your eval is real?** Use enough examples, compare per item with a paired test such as a bootstrap confidence interval on the difference, and read failure cases.
- **What does "open-weight" not give you?** Usually not the training data or full pipeline, and not necessarily a permissive license; read the license for each checkpoint.

## Further reading

- Chiang et al., 2024. "Chatbot Arena: An Open Platform for Evaluating LLMs by Human Preference."
- Liang et al., 2022. "Holistic Evaluation of Language Models" (HELM).
- Hsieh et al., 2024. "RULER: What's the Real Context Size of Your Long-Context Language Models?"
- DeepSeek-AI, 2025. "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning."
- Kwon et al., 2023. "Efficient Memory Management for Large Language Model Serving with PagedAttention."
- Open Source Initiative, 2024. "The Open Source AI Definition."
