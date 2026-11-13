---
title: When to fine-tune
section: llm
order: 32
phase: "Phase 6 · Fine-tuning"
summary: A decision framework for prompting vs RAG vs fine-tuning vs pretraining, what fine-tuning fixes and breaks, and the cost math that justifies it.
---

Fine-tuning changes the model's weights so that a behavior you want becomes the model's default instead of something you ask for in every prompt. It is the most expensive lever an application team normally pulls: you need a dataset, an eval suite, training runs, a serving path for a custom model, and a plan for retraining when the base model changes. It is also the lever most often pulled for the wrong reason, usually "the model doesn't know our product", which is a retrieval problem. This note is the decision framework: what each lever changes, what fine-tuning is genuinely good at, what it cannot do, and how to prove the cost is worth it.

## Core ideas

### Five levers, in order of cost

| Lever | What it changes | Cost to try | Iteration loop | Best for |
|---|---|---|---|---|
| Prompting (instructions, few-shot, structured outputs) | What the model is told per request | Minutes | Seconds | Spec, format, most behavior |
| RAG and tools | What the model can see at request time | Days | Minutes (reindex) | Facts, private or fresh data, citations, permissions |
| Fine-tuning (SFT, LoRA, preference tuning) | Weights, on input/output examples | Days to weeks | Hours per run | Consistent behavior, style, narrow tasks, smaller models |
| Continued pretraining | Weights, on raw domain text | Weeks | Days per run | New language, heavy domain vocabulary |
| Pretraining from scratch | Everything | Months, millions of dollars | Weeks | Almost never an application team's job |

Work down the table and stop at the first lever that meets your eval bar. Each step down costs roughly an order of magnitude more in time and money, and each one makes the system harder to change later. A prompt edit ships in a deploy; a fine-tuned model ships after a data pipeline, a training run and a regression eval.

### Knowledge vs behavior

The single most useful question: is the failure about what the model **knows** or about how it **behaves**?

- **Knowledge failures:** wrong or missing facts, outdated information, nothing about your internal documents, no awareness of a customer's account state. Fix with [RAG]({{ '/llm/rag-architecture/' | relative_url }}) or [tools]({{ '/llm/tool-calling/' | relative_url }}).
- **Behavior failures:** wrong format, wrong tone, inconsistent field semantics, calls the wrong tool, too verbose, misses a domain convention that is easier to show than describe. Fix with [prompting]({{ '/llm/prompting-and-structured-outputs/' | relative_url }}) first, then fine-tuning.
- **Capability failures:** the task is beyond the model (multi-step math, long-horizon planning). Fix with a stronger or [reasoning model]({{ '/llm/reasoning-models/' | relative_url }}), task decomposition, or tools. Small-data fine-tuning rarely adds capability that the base model lacks.

Why fine-tuning is a poor way to add facts:

1. **Weak, unattributed recall.** A fact that appears in a handful of training examples is stored diffusely. The model cannot tell which facts it learned well, so it answers confidently either way. Gekhman et al. (2024) found that fine-tuning examples that introduce knowledge the model did not already have are learned more slowly than examples consistent with its knowledge, and that as the model eventually fits them its tendency to hallucinate increases.
2. **Staleness.** Facts change. Updating a vector index takes minutes; updating weights takes a data refresh, a training run and an eval cycle.
3. **No provenance.** RAG can cite the passage it used. Weights cannot.
4. **No access control.** Every user of a model sees the same weights. You cannot filter by tenant or role inside a weight matrix, and models can regurgitate training data verbatim. Per-tenant data belongs in retrieval with permission filters, not in shared weights.
5. **No deletion.** A deletion request for data in an index is a `DELETE`. For data in weights, it is a retrain.

Comparisons of knowledge injection (for example Ovadia et al., 2023) consistently find retrieval beats unsupervised fine-tuning on raw documents for answering questions about those documents.

### What fine-tuning is good at

- **Format adherence.** The model follows your output conventions on every request, including the semantics of fields, not just the syntax. Constrained decoding and [structured outputs]({{ '/llm/structured-outputs/' | relative_url }}) already guarantee valid JSON; fine-tuning fixes the cases where the JSON is valid but `priority` is filled inconsistently.
- **Tone, style and voice.** Brand voice, terse vs explanatory, how to phrase a refusal. Easy to show in 300 examples, hard to describe in a system prompt.
- **Narrow, high-volume tasks.** Classification, extraction, routing, rewriting with fixed rules. A small fine-tuned model often matches a frontier model on one narrow task.
- **Latency and cost through smaller models.** Train an 8B model on outputs of a much larger one ([distillation]({{ '/llm/distillation/' | relative_url }})) and serve it at a fraction of the cost and time per token.
- **Tool-use reliability.** When to call a tool, which one, argument conventions, and when not to call anything. Agents that are 90% reliable per step are unreliable over ten steps; fine-tuning on good trajectories can raise per-step reliability.
- **Prompt compression.** A 3,000-token system prompt with 20 few-shot examples can be baked into weights, cutting input tokens and time to first token on every request.
- **Domain conventions.** Clinical note structure, legal citation style, your internal ticket taxonomy.

### What fine-tuning is bad at

- Injecting fresh or frequently changing facts (see above).
- Fixing a vague spec. If two humans would label the same example differently, the model will learn the noise. Fix the guidelines first.
- Making a small model generally smarter with a few thousand examples. You get the narrow behavior, not new reasoning.
- Anything that needs per-user data, citations, or deletion guarantees.

### Fine-tuning and RAG together

The two are complements. A common production pattern is RAG for facts plus a fine-tuned model that is good at using retrieved context: citing passages in your format, saying "not in the documents" instead of guessing, ignoring distractor chunks. RAFT (Zhang et al., 2024) trains on questions paired with a mix of relevant and distractor documents for exactly this reason.

## Decision framework

```text
 Build an eval set first (100-500 real examples + a grader)
                         |
     Best prompt on the strongest model: does it pass?
            |                                 |
           yes                                no
            |                                 |
  Is cost or latency the            What kind of failure?
  problem at your volume?        /           |            \
       |          |         knowledge     behavior     capability
      no         yes           |             |              |
       |          |         RAG/tools   few-shot,      stronger or
     ship      distill a                 schema,       reasoning model,
               smaller model             decompose     decompose task
               (fine-tune)                   |
                                    Still fails consistently,
                                    with >= ~100 good examples?
                                             |
                                       fine-tune (LoRA first)
```

The steps, in order:

1. **Write the eval first.** Without a fixed eval set and a grader you cannot tell whether a fine-tune helped. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).
2. **Establish a ceiling.** Run the best prompt on the strongest model you could afford. If even that fails, fine-tuning a smaller model on the same task is unlikely to beat it unless you have high-quality labels the prompt cannot express.
3. **Classify failures.** Read 50 failures and tag each as knowledge, behavior or capability. The distribution tells you which lever to pull.
4. **Exhaust cheap fixes.** Better instructions, more and better few-shot examples, a schema, splitting one prompt into two calls, retrieval for facts.
5. **Fine-tune when** the failure is behavioral and consistent, you can collect at least about 100 good examples (more for harder tasks), you have an eval that shows the gap, and someone owns the model after launch. Or when the prompt already works on a big model and the business case is cost or latency at volume.
6. **Start with parameter-efficient methods.** [LoRA]({{ '/llm/lora-and-peft/' | relative_url }}) is cheaper, forgets less, and makes rollback a matter of not loading an adapter. Move to [full fine-tuning]({{ '/llm/supervised-fine-tuning/' | relative_url }}) only if LoRA plateaus below the bar.

| Symptom | Likely lever |
|---|---|
| "Doesn't know our product / policies / prices" | RAG |
| "Knows it but answers in the wrong format 5% of the time" | Schema, then fine-tune |
| "Sounds generic, not like us" | Few-shot, then fine-tune |
| "Great on GPT-class model, too slow and expensive" | Distill to a smaller fine-tuned model |
| "Calls the search tool when it should ask a clarifying question" | Better tool descriptions, then fine-tune on trajectories |
| "Fails at multi-step reasoning" | Reasoning model, decomposition |
| "Needs a language the base model barely speaks" | Continued pretraining, then SFT |

## Options: hosted vs self-hosted

| | Hosted tuning of closed models | Hosted tuning of open-weight models | Self-hosted (TRL, PEFT on your GPUs) |
|---|---|---|---|
| Setup | Upload JSONL, start a job | Upload JSONL, pick a base | Training code, GPUs, infra |
| Control | Epochs, LR multiplier, batch size | Most hyperparameters, LoRA rank | Everything |
| You get | A model ID on that provider | Usually the adapter or weights | Weights you own |
| Serving | Provider's API, often at a higher per-token price than the base model | Provider or anywhere | You run it (vLLM, SGLang, TGI) |
| Data boundary | Training data goes to the provider | Same | Stays with you |
| Lifecycle risk | Tied to the base snapshot; retired with it | Portable | Portable |
| Best when | You already use the provider and want quick wins | You want portability without running training | Data cannot leave, or you need full control |

As of 2025–2026, several major providers offer hosted supervised tuning for selected models (not usually their largest), and some also offer preference-based or reinforcement-based tuning for some models. Availability changes often, so check current docs before planning around a specific model. Self-hosting gives you the weights and the freedom to serve many adapters on one base (see [LoRA and PEFT]({{ '/llm/lora-and-peft/' | relative_url }})), at the cost of owning GPUs, training code and a serving stack.

## In practice: the cost math

A fine-tune pays for itself in one of two ways: it raises quality on a task that matters enough to fund it, or it lowers the per-request cost at a volume where the savings beat the one-time and ongoing costs. The second is easy to compute. The calculator below compares a large model with a long few-shot prompt against a small fine-tuned model with a short prompt. All prices are illustrative placeholders; plug in your real ones.

```python
from dataclasses import dataclass


@dataclass
class Option:
    name: str
    in_price: float           # $ per 1M input tokens
    out_price: float          # $ per 1M output tokens
    in_tokens: int            # input tokens per request
    out_tokens: int           # output tokens per request
    fixed_monthly: float = 0  # hosting, eval runs, owner's time

    def monthly_cost(self, requests: int) -> float:
        per_request = (self.in_tokens * self.in_price
                       + self.out_tokens * self.out_price) / 1e6
        return requests * per_request + self.fixed_monthly


def training_cost(examples: int, avg_tokens: int, epochs: int,
                  price_per_m: float) -> float:
    """Hosted training is usually billed per trained token."""
    return examples * avg_tokens * epochs * price_per_m / 1e6


def breakeven_months(one_time: float, baseline: Option, tuned: Option,
                     requests: int) -> float:
    saving = baseline.monthly_cost(requests) - tuned.monthly_cost(requests)
    return float("inf") if saving <= 0 else one_time / saving


# Illustrative prices only: a large model with a 3k-token few-shot prompt
# vs a small fine-tuned model that needs only a 300-token prompt.
big = Option("large + few-shot", 3.00, 15.00, in_tokens=3000, out_tokens=200)
small = Option("small fine-tuned", 0.30, 1.20, in_tokens=300, out_tokens=200,
               fixed_monthly=2000)

one_time = training_cost(5000, 1000, epochs=3, price_per_m=25.0) + 15000
# 15M trained tokens -> $375 compute; $15k for labeling and engineering

if __name__ == "__main__":
    for volume in (20_000, 200_000, 2_000_000):
        b, s = big.monthly_cost(volume), small.monthly_cost(volume)
        months = breakeven_months(one_time, big, small, volume)
        print(f"{volume:>9,} req/mo  big ${b:>9,.0f}  tuned ${s:>7,.0f}"
              f"  break-even {months:.1f} months")
    assert round(training_cost(5000, 1000, 3, 25.0)) == 375
    assert round(big.monthly_cost(2_000_000)) == 24000
    assert round(small.monthly_cost(2_000_000)) == 2660
    assert breakeven_months(one_time, big, small, 20_000) == float("inf")
    assert breakeven_months(one_time, big, small, 2_000_000) < 1
```

Output:

```text
   20,000 req/mo  big $      240  tuned $  2,007  break-even inf months
  200,000 req/mo  big $    2,400  tuned $  2,066  break-even 46.0 months
2,000,000 req/mo  big $   24,000  tuned $  2,660  break-even 0.7 months
```

What the numbers say:

- **Volume decides.** At 20k requests a month, the fixed cost of owning a model dwarfs the token savings; fine-tuning for cost never pays. At 2M a month it pays back in weeks.
- **The fixed monthly cost is the number people forget.** Someone has to own the eval suite, refresh data, retrain when the base model is deprecated, and watch production. Put a real number on it.
- **Check prompt caching before you decide.** If the 3k-token prompt is static, [prompt caching]({{ '/llm/llm-caching/' | relative_url }}) can cut the baseline's input cost severalfold. Rerun the comparison with the cached input price.
- **Quality-driven fine-tunes are justified differently.** If the fine-tune moves extraction accuracy from 92% to 98% and each error costs a human review at $2, multiply errors avoided by cost per error.

For self-hosting, replace the per-token prices with GPU cost: a GPU-hour price divided by tokens per hour at your realistic utilization, plus idle time. See [latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).

## Risks

**Catastrophic forgetting.** Training on a narrow distribution shifts the model away from everything else. Symptoms: general instruction following degrades, the model forces your output format onto unrelated questions, multilingual or coding ability drops. Mitigations: LoRA instead of full fine-tuning (Biderman et al., 2024, found LoRA forgets less), lower learning rate and fewer epochs, mixing in a slice of general instruction data, and a regression eval on general tasks alongside your task eval.

**Safety regression.** Qi et al. (2023) showed that fine-tuning an aligned model can erode its safety behavior: a few dozen harmful examples are enough to undo much of it, and even benign datasets measurably weaken refusals. Hosted providers screen training data, but self-hosted runs have no such check. Mitigations: keep refusal and policy examples in the training set, run a safety and jailbreak eval before and after, and keep [guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}) outside the model so they do not depend on weights you just changed.

**Hallucination drift.** Training on answers that contain facts the model did not know teaches it to produce confident specifics. Prefer training data whose facts are present in the input (extraction, rewriting, RAG-grounded answers).

**Memorization and privacy.** Models can reproduce training examples verbatim, including PII. Scrub training data, never train a shared model on one tenant's private data, and treat the weights as containing everything in the dataset. See [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).

**Maintenance and lock-in.** A fine-tune is a model you now own. Hosted fine-tunes are tied to a base snapshot and are retired with it. A new base model may beat your fine-tune out of the box six months later. Budget for retraining, and keep the dataset, config and eval versioned so a retrain is a pipeline run, not an archaeology project. See [fine-tuning in practice]({{ '/llm/fine-tuning-in-practice/' | relative_url }}).

## Pitfalls

| What you see | Cause | Fix |
|---|---|---|
| Fine-tuned on product docs, still invents product details | Knowledge problem treated as behavior | RAG; fine-tune only on how to use retrieved context |
| Strong offline results, weak in production | Eval drawn from the same synthetic generator as training | Eval on sampled, labeled production traffic |
| Model ignores new instructions in the system prompt | Trained with one fixed prompt; prompt became a no-op | Vary prompts in training; keep the inference prompt identical to training |
| Cost savings smaller than projected | Forgot hosting, idle GPUs, retraining, owner's time | Include fixed monthly cost; check volume |
| Fine-tune obsolete after a base model release | No plan to re-evaluate | Re-run the decision when new bases ship; keep data portable |
| Agent got worse at tools it was not trained on | Tool-call distribution narrowed | Include all tools and no-tool cases in training data |

## Checklist

- Eval set and grader exist, and the best prompt on the strongest model has been measured.
- Failures are classified; the fine-tune targets behavior, not facts.
- At least about 100 high-quality examples for a narrow behavior; more for harder tasks.
- Cost model includes fixed monthly ownership cost and prompt caching on the baseline.
- LoRA first; full fine-tuning only with evidence that LoRA plateaus.
- Regression evals for general capability and safety, not just the target task.
- A named owner, versioned data and config, and a retrain plan for base model deprecation.

## Interview questions

**When would you fine-tune instead of using RAG?** When the gap is behavior rather than knowledge: format consistency, tone, a narrow task, tool-use decisions, or when you need a smaller, cheaper model to do what a large prompted model already does. Facts, fresh data and anything per-user go in retrieval.

**A customer wants to fine-tune on 10k internal documents so the chatbot "knows the product". What do you say?** That is a knowledge problem. Fine-tuning on raw documents gives weak, unattributable recall, goes stale, cannot enforce access control and cannot honor deletion. Build RAG with citations and permission filters. If answers are poorly formatted or ignore the context, then fine-tune on RAG-grounded examples.

**Can you teach a model new facts with fine-tuning?** Partly, but poorly. New facts are learned slowly, recall is unreliable, and research suggests that pushing a model to fit unfamiliar facts increases hallucination. Continued pretraining on large volumes of domain text can shift knowledge, but it is expensive and still lacks provenance.

**How do you justify the cost of a fine-tuning project?** Either quality (errors avoided times cost per error) or unit economics (per-request savings times volume minus fixed ownership cost, against one-time data and training cost). Compare against a cached, prompted baseline, and show the break-even volume.

**What can go wrong when you fine-tune an aligned model?** Catastrophic forgetting of general skills, regression in safety behavior even on benign data, increased hallucination when training on unfamiliar facts, memorized PII, and lifecycle lock-in to a base snapshot. Mitigate with LoRA, conservative hyperparameters, mixed data, safety evals and external guardrails.

**Hosted fine-tuning or self-hosted?** Hosted when you are already on that provider, the data may leave your boundary, and you want speed. Self-hosted open-weight when data cannot leave, you need portability or many per-tenant adapters on one base, or you need full control over training. The hidden cost of self-hosting is serving.

**How do you know the fine-tune worked?** Compare against the prompted baseline on a held-out eval that reflects production traffic, plus regression evals for general capability and safety, then shadow or A/B test in production. Validation loss alone does not tell you.

## Further reading

- Ovadia et al., "Fine-Tuning or Retrieval? Comparing Knowledge Injection in LLMs" (2023).
- Gekhman et al., "Does Fine-Tuning LLMs on New Knowledge Encourage Hallucinations?" (2024).
- Qi et al., "Fine-tuning Aligned Language Models Compromises Safety, Even When Users Do Not Intend To!" (2023).
- Zhang et al., "RAFT: Adapting Language Model to Domain Specific RAG" (2024).
- Biderman et al., "LoRA Learns Less and Forgets Less" (2024).
- OpenAI and Google Vertex AI fine-tuning documentation (official guides for hosted tuning).
