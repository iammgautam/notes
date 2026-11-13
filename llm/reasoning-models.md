---
title: Reasoning models
section: llm
order: 11
phase: "Phase 2 · Prompting and context"
summary: How reasoning models spend test-time compute, how RL on verifiable rewards trains them, when they pay off, how to prompt them and the cost math.
---

Reasoning models generate a long internal chain of thought before the visible answer. They trade latency and tokens for accuracy on problems that need multi-step work. As of 2025 to 2026 the category includes OpenAI's o-series and GPT-5 reasoning modes, DeepSeek-R1, Claude with extended or adaptive thinking, Gemini thinking models and open-weight hybrids like Qwen3 that switch thinking on and off. The engineering question is not "is the reasoning model better" but "which requests are worth 5x to 50x more tokens and seconds to minutes of latency". This note covers the mechanism, the training recipe at a conceptual level, how to prompt and budget them, and how to evaluate them.

## Core ideas

### Test-time compute

There are two ways to buy accuracy: a bigger or better-trained model (train-time compute), or more computation per query (test-time compute). Test-time compute comes in three forms:

| Form | How | Example |
|---|---|---|
| Sequential | Longer chains of thought: plan, try, check, backtrack | Reasoning models |
| Parallel | Sample N answers, then vote or pick with a verifier | Self-consistency, best-of-N |
| Search | Explore partial solutions, guided by a reward model | Beam search with a process reward model, tree search |

Why longer outputs help: a transformer does a fixed amount of computation per token, with a fixed number of layers (see [Transformer architecture]({{ '/llm/transformer-architecture/' | relative_url }})). A problem that needs more serial steps than one forward pass provides has to be unrolled into tokens. Thinking tokens are a scratchpad, extra serial compute and a chance to notice and fix mistakes ("wait, that contradicts step 2").

OpenAI's o1 report showed accuracy rising smoothly with both RL training compute and test-time thinking. Snell et al. (2024) found that, in FLOPs-matched comparisons, well-allocated test-time compute let a smaller model beat one roughly 14x larger on easy and medium problems, but not on the hardest ones. Test-time compute substitutes for model size only up to a point.

### Thinking tokens and budgets

```text
request --> [ thinking: hidden, summarized or raw ] --> [ answer ]
            |<--------- billed as output tokens ------------------>|
            |<---- counts toward max output tokens and window ---->|
            ^ visible time-to-first-token comes after the thinking
```

- **Billing.** Thinking tokens are billed as output tokens even when you never see them. Usage objects report them (for example, OpenAI's `usage.completion_tokens_details.reasoning_tokens`).
- **Visibility.** Hosted models increasingly hide the raw chain of thought and return a summary or nothing. Open-weight models like DeepSeek-R1 and Qwen3 emit it inline between `<think>` and `</think>`, and you strip it before showing or storing the answer.
- **Controls.** Effort levels (`reasoning_effort` low, medium or high on OpenAI; an `effort` setting on Anthropic) or token budgets (Gemini's `thinking_budget`, Anthropic's older `budget_tokens`). As of 2026 the trend is toward effort levels and adaptive thinking, where the model decides how much to think per request.
- **Multi-turn and tools.** Some APIs require you to pass thinking blocks back unchanged during tool-use loops (Anthropic). Others carry reasoning between turns through response ids or encrypted reasoning items (OpenAI's Responses API). Dropping them can hurt quality or cause errors.
- **Sampling parameters.** Many reasoning APIs fix or reject `temperature` and `top_p`. Open-weight model cards often recommend a specific temperature (DeepSeek-R1's guidance was around 0.6) and warn that greedy decoding causes repetition.
- **Budget forcing.** The s1 paper (Muennighoff et al., 2025) controlled thinking length directly: stop early by forcing the end-of-thinking delimiter, or extend by appending "Wait". Longer forced thinking improved accuracy up to a point.

### How they're trained

```text
 pretrained base model
        |
        v
 cold-start SFT on a small set of long, readable reasoning traces
        |
        v
 RL on verifiable tasks:
   for each problem sample a group of solutions
   reward = checker(final answer)   math: matches key
                                    code: unit tests pass
                                    plus a format reward
   push the policy toward above-average samples (PPO, GRPO)
        |
        v
 rejection sampling -> SFT on good traces + general data,
 then RL for helpfulness and safety
        |
        v
 reasoning model  --(distill traces via SFT)-->  smaller models
```

The key ideas, conceptually:

- **Verifiable rewards (RLVR).** The reward comes from a program, not a learned reward model: the math answer matches, the unit tests pass, the output follows the format. Programs are much harder to game than learned reward models, though reward hacking still happens (special-casing tests, for instance). The term RLVR was popularized by the Tulu 3 report (Lambert et al., 2024). See [Preference tuning]({{ '/llm/preference-tuning/' | relative_url }}) for the learned-reward side.
- **GRPO.** Group Relative Policy Optimization (Shao et al., 2024, DeepSeekMath) samples a group of G outputs per prompt and scores each one against its own group: advantage `A_i = (r_i - mean(r)) / std(r)`. That removes PPO's separate value network, which saves memory and compute. Correct samples get pushed up and incorrect ones down, relative to how hard the prompt was for the current model.
- **Emergent behavior.** DeepSeek-R1-Zero applied RL with rule-based rewards directly to a base model. Response length grew over training, and self-verification and backtracking appeared without being taught. It also mixed languages and was hard to read, so DeepSeek-R1 added cold-start SFT and multi-stage training.
- **Process vs outcome rewards.** Process reward models score each step. Lightman et al. (2023) showed they beat outcome supervision for selecting solutions. DeepSeek's R1 report listed PRMs and tree search among its unsuccessful attempts at scale (reward hacking, annotation cost), and outcome rewards plus long sampling did the work.
- **Distillation.** Fine-tuning small models on a strong reasoner's traces worked better for them than running RL on the small models directly (R1's distilled Qwen and Llama models). See [Distillation]({{ '/llm/distillation/' | relative_url }}).
- **Transfer.** Training data is heavy on math and code. Gains transfer partly to planning, analysis and agentic work, much less to knowledge recall and writing quality.

GRPO's group-relative advantage is small enough to compute by hand:

```python
from statistics import mean, pstdev


def grpo_advantages(rewards: list[float], eps: float = 1e-6) -> list[float]:
    """Score each sample against its own group: (r - mean) / std."""
    mu, sd = mean(rewards), pstdev(rewards)
    return [(r - mu) / (sd + eps) for r in rewards]


if __name__ == "__main__":
    # 8 sampled solutions to one math problem, 3 passed the checker
    adv = grpo_advantages([1, 0, 0, 1, 0, 1, 0, 0])
    assert abs(sum(adv)) < 1e-9                       # zero-centered
    assert adv[0] > 0 and adv[1] < 0                  # pass up, fail down
    assert all(a == 0 for a in grpo_advantages([1, 1, 1, 1]))  # no signal
    hard = grpo_advantages([1, 0, 0, 0, 0, 0, 0, 0])[0]
    easy = grpo_advantages([1, 1, 1, 1, 1, 1, 1, 0])[0]
    assert hard > easy        # a rare success on a hard prompt counts more
    print([round(a, 2) for a in adv])
```

A prompt where every sample passes, or every sample fails, contributes no gradient. That's why training data is filtered to problems the current model solves sometimes, not always or never.

## When they help and when they hurt

| Task | Reasoning model? | Why |
|---|---|---|
| Math, quantitative analysis | Yes | Multi-step, checkable, large gains |
| Hard coding: debugging, algorithms, multi-file changes | Yes | Planning and self-checking |
| Agentic planning, multi-hop tool use | Usually | Better plans and fewer wasted tool calls, slower steps |
| Extraction with cross-field rules, contract or financial analysis | Often | Consistency checks across the document |
| Classification, routing, simple extraction | No | Same accuracy at 5x-50x the tokens |
| Chat, rewriting, summarization | Rarely | Little gain, slower first token |
| Factual recall | No | Thinking doesn't add knowledge and can rationalize a wrong fact |
| Real-time UX (autocomplete, voice) | No | Latency budget |

How they hurt: latency of seconds to minutes; cost; overthinking easy questions (sometimes talking themselves out of a correct answer); longer, harder-to-predict token usage with heavy cost tails; and HTTP timeouts in clients configured for 30-second calls.

## Prompting reasoning models

- **State the goal, constraints and success criteria. Skip the steps.** Hand-written "first do X, then Y" scaffolding from the GPT-4 era can block a better strategy the model would have found. Provider guidance (OpenAI, Anthropic) says high-level instructions work better than prescriptive chains of thought.
- **Don't ask it to "think step by step" or to show its reasoning in the answer.** It already reasons, and reasoning in the visible answer is billed twice.
- **Zero-shot first.** The DeepSeek-R1 report found few-shot prompting consistently degraded its performance. If you use examples, keep them few and check that they help.
- **Give all the context up front.** Reasoning models are good at using a complete brief and bad at guessing what you left out.
- **Ask for verification.** "Check the result against the constraints before answering" or "run the tests" points the extra compute at the right thing.
- **Be strict about the final format.** Delimiters for inputs, an explicit output format, or [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}). The answer should be short even when the thinking is long.
- **Tune effort before rewriting prompts.** Start low or medium and raise it only where evals show gains.
- **Open-weight specifics.** Follow the model card (temperature, whether to use a system prompt). Don't keep earlier `<think>` content in multi-turn history; Qwen3's guidance is to keep only final answers.

## Cost and latency math

`cost = input_tokens * p_in + (thinking_tokens + answer_tokens) * p_out`

`latency ~ TTFT + (thinking_tokens + answer_tokens) / decode_tokens_per_second`

Worked example with illustrative prices ($2 per million input, $8 per million output) and 80 tokens per second of decode:

| | Input | Thinking | Answer | Cost per request | Decode time |
|---|---|---|---|---|---|
| Non-reasoning | 3,000 | 0 | 400 | $0.0092 | ~5 s |
| Reasoning, medium effort | 3,000 | 4,000 | 400 | $0.0412 | ~55 s |

At a million requests a month, that's $9.2k against $41.2k, and a 5-second response against nearly a minute. A cascade usually wins: answer with the cheap model, escalate to the reasoning model when a verifier fails or confidence is low. With 15% escalation the expected cost is `0.0092 + 0.15 * 0.0412 = $0.0154` per request. Whether accuracy holds depends on how well the escalation trigger catches hard cases. See [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}) and [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).

```python
def request_cost(inp, think, out, p_in=2.0, p_out=8.0):
    """USD per request; prices are per million tokens."""
    return (inp * p_in + (think + out) * p_out) / 1e6


def decode_seconds(think, out, tps=80, ttft=0.5):
    return ttft + (think + out) / tps


def cascade_cost(cheap, strong, escalation_rate):
    return cheap + escalation_rate * strong


def max_tokens_needed(think_p95, answer_max, margin=1.2):
    """Size max_tokens from the p95 of logged thinking, not the mean."""
    return int((think_p95 + answer_max) * margin)


if __name__ == "__main__":
    fast = request_cost(3000, 0, 400)
    slow = request_cost(3000, 4000, 400)
    assert round(fast, 4) == 0.0092 and round(slow, 4) == 0.0412
    assert round(decode_seconds(0, 400), 1) == 5.5
    assert round(decode_seconds(4000, 400), 1) == 55.5
    mixed = cascade_cost(fast, slow, 0.15)
    assert fast < mixed < slow and round(mixed, 4) == 0.0154
    assert max_tokens_needed(think_p95=9000, answer_max=800) == 11760
    print(f"fast=${fast:.4f} slow=${slow:.4f} cascade=${mixed:.4f}")
```

The last function matters in production. If `max_tokens` is sized for the answer alone, the model can spend the whole budget thinking and return an empty or truncated answer. Size it from the p95 of logged thinking tokens.

## Evaluation

- **Plot accuracy against tokens and latency for each effort level**, and pick the knee of the curve per route. Compare at equal cost: a reasoning model at low effort against a non-reasoning model with self-consistency at the same token spend.
- **Average over samples.** Single runs are noisy. Report pass@1 averaged over several samples per problem, as the R1 report did.
- **pass@k for code and math.** The probability that at least one of k samples is correct, estimated without bias from n samples with c correct: `pass@k = 1 - C(n-c, k) / C(n, k)` (Chen et al., 2021). Majority-vote accuracy (cons@k) is the self-consistency counterpart.
- **Track token distributions, not means.** Log thinking tokens per request and watch p50, p95 and p99. Cost and latency tails come from the long tail of thinking.
- **Grade the final answer, not the reasoning.** Reasoning summaries are not faithful logs of the computation.
- **Beware contamination.** Public math and code benchmarks leak into training data. Use private, recent problems from your own domain. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).

```python
from math import comb
from statistics import quantiles


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k from n samples with c correct (Chen et al., 2021)."""
    if n - c < k:
        return 1.0
    return 1 - comb(n - c, k) / comb(n, k)


def token_percentiles(samples: list[int]) -> dict[str, float]:
    q = quantiles(samples, n=100, method="inclusive")
    return {"p50": q[49], "p95": q[94], "p99": q[98]}


if __name__ == "__main__":
    assert pass_at_k(10, 0, 1) == 0.0
    assert abs(pass_at_k(10, 3, 1) - 0.3) < 1e-12
    assert abs(pass_at_k(10, 3, 5) - (1 - 21 / 252)) < 1e-12
    assert pass_at_k(10, 8, 5) == 1.0          # any 5 of 10 include a pass

    thinking = [800] * 80 + [3000] * 15 + [20000] * 5   # long tail
    pct = token_percentiles(thinking)
    assert pct["p50"] == 800 and pct["p99"] == 20000
    assert sum(thinking) / len(thinking) < pct["p95"]  # mean hides the tail
    print({k: round(v) for k, v in pct.items()})
```

## Pitfalls

- **Reasoning everywhere.** Symptom: cost and p95 latency jump after a model swap and accuracy barely moves. Fix: route by task type, and use reasoning only where evals show a gain.
- **`max_tokens` too small.** Symptom: empty or cut-off answers with `finish_reason` of `length`. Fix: size from the p95 of thinking tokens plus the answer.
- **Client timeouts.** Default HTTP timeouts kill long requests. Fix: streaming or background mode, and timeouts sized per route.
- **Silent UX.** A minute of nothing reads as a hang. Fix: stream thinking summaries or progress events, or run the work as an async job.
- **Dropping thinking blocks in tool loops.** Symptom: API errors or worse multi-step performance. Fix: pass them back exactly as the provider requires.
- **Old prompts on new models.** Prescriptive CoT scaffolding and emphatic rules make reasoning models worse. Fix: rewrite as goal plus constraints and re-run the eval.
- **Assuming fewer hallucinations.** Reasoning doesn't add knowledge, and on factual questions it can produce more elaborate fabrications. Fix: grounding and retrieval as usual.
- **Logging raw reasoning carelessly.** Thinking can quote sensitive context. Apply the same retention and PII rules as to outputs.

## Rules of thumb

- Use reasoning models for math, hard code, planning and multi-constraint analysis. Not for classification, chat or real-time UX.
- Prompt with goals and constraints, zero-shot first. Let the model choose the steps.
- Effort is the first knob. Start low, raise it only where an eval shows a gain.
- Budget and alert on thinking-token p95 and p99, not the mean.
- Prefer a cascade (cheap model first, reasoning on escalation) for mixed traffic.
- Evaluate with multiple samples per item, at equal cost, on private data.

## Interview questions

**What is test-time compute, and what are the ways to spend it?** Extra computation at inference to improve answers: longer reasoning chains (sequential), sampling many answers and voting or verifying (parallel), and search guided by a reward model. It substitutes for model size on easy and medium problems, less so on the hardest.

**How are reasoning models trained?** Usually a small cold-start SFT on readable reasoning traces, then large-scale RL where rewards come from programs (answer checkers, unit tests, format checks), often with PPO or GRPO. That's followed by rejection-sampled SFT and general alignment RL. The traces can be distilled into smaller models with SFT.

**What is GRPO and why use it over PPO?** It samples a group of outputs per prompt and uses each output's reward relative to the group mean, divided by the group standard deviation, as its advantage. No value network is needed, which cuts memory and compute, and the group baseline adapts to prompt difficulty.

**When would you not use a reasoning model?** Simple classification, extraction, chat, summarization, real-time features, and pure factual recall. They cost 5x to 50x more tokens with little or no accuracy gain on those tasks.

**How does prompting differ for reasoning models?** Give goals, constraints and success criteria instead of step-by-step instructions. Skip "think step by step", start zero-shot, give complete context, ask for verification, and tune effort before rewriting the prompt.

**Estimate the cost difference for a 3k-in, 400-out request if the model thinks for 4k tokens.** Output grows from 400 to 4,400 tokens. At $2 and $8 per million, cost goes from about $0.009 to $0.041 per request, about 4.5x, and decode time from about 5 seconds to about 55 seconds at 80 tokens per second.

**How do you evaluate a reasoning model fairly?** Several samples per item with averaged pass@1 (pass@k for code), accuracy against tokens and latency per effort level, comparison against baselines at equal cost, thinking-token percentiles, and private, uncontaminated data.

## Further reading

- OpenAI, "Learning to Reason with LLMs" (2024)
- DeepSeek-AI, "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning" (2025)
- Shao et al., "DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models" (2024), which introduces GRPO
- Snell et al., "Scaling LLM Test-Time Compute Optimally can be More Effective than Scaling Model Parameters" (2024)
- Lightman et al., "Let's Verify Step by Step" (2023)
- Muennighoff et al., "s1: Simple test-time scaling" (2025)
