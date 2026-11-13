---
title: "Preference tuning: RLHF, DPO and beyond"
section: llm
order: 36
phase: "Phase 6 · Fine-tuning"
summary: Why SFT alone is not enough, how RLHF and DPO turn preferences into a training signal, and how GRPO trains reasoning with verifiable rewards.
---

Supervised fine-tuning teaches a model to imitate demonstrations. Preference tuning teaches it to tell a better answer from a worse one and shift probability toward the better kind. It is the stage that makes assistants helpful, safe and well-toned, and its RL variants (GRPO, RL with verifiable rewards) are how reasoning models learn to think longer. You will rarely run full RLHF, but DPO on a few thousand pairs is well within reach, and the pipeline explains a lot of model behavior: sycophancy, verbosity, over-refusal and reward hacking all come from here.

## Why SFT is not enough

SFT minimizes cross-entropy on reference answers. That has three structural limits:

- **It only sees good examples.** The model never gets a signal that a specific kind of output is bad. It learns "what an answer looks like", not "which of these two answers is better".
- **Demonstrations cap quality.** Writing a perfect answer is much harder than judging which of two answers is better. Humans (and models) are better judges than authors, so comparisons scale further than demonstrations.
- **Exposure bias.** SFT trains on gold prefixes, but at inference the model conditions on its own outputs. Preference methods (especially online ones) train on the model's own samples, so they correct the mistakes the model actually makes.

The typical post-training stack is therefore: pretraining, then [SFT]({{ '/llm/supervised-fine-tuning/' | relative_url }}) for format and instruction following, then one or more preference or RL stages for quality, safety and reasoning.

## How it works

### Preference data

The unit of data is a triple: a prompt `x`, a preferred response `y_w` ("chosen", winner) and a dispreferred response `y_l` ("rejected", loser). Comparisons are more reliable than absolute 1–10 scores because raters disagree on scales far more than on rankings. Even so, agreement between trained human labelers in published RLHF work is only around 70–75% on open-ended chat, so the data is noisy by nature.

### The reward model (Bradley-Terry)

Classic RLHF first trains a reward model `r(x, y)`, usually the SFT model with its LM head replaced by a scalar head. It uses the Bradley-Terry model of pairwise preferences:

```text
P(y_w preferred over y_l | x) = sigmoid(r(x, y_w) - r(x, y_l))
loss_RM = -log sigmoid(r(x, y_w) - r(x, y_l))
```

Only the difference matters, so rewards have no absolute scale and are normalized before RL. Every flaw in the reward model becomes a behavior in the final policy.

### RLHF with PPO

The policy (initialized from the SFT model) generates a response, the reward model scores it, and PPO updates the policy to increase reward. A KL penalty keeps the policy close to a frozen reference (the SFT model):

```text
maximize  E[ r(x, y) ]  -  beta * KL( pi(y|x) || pi_ref(y|x) )

per-token KL estimate: log pi(y_t | x, y_<t) - log pi_ref(y_t | x, y_<t)
```

Without the KL term the policy drifts into regions where the reward model is wrong and exploits them. PPO adds a value model (critic) for advantages and a clipped probability ratio to keep updates small. That is four models in memory (policy, reference, reward, value) plus generation inside the training loop, which is why most teams outside large labs moved to DPO.

```text
                 prompts x
                     |
            SFT model samples 2+ answers
                     |
         humans / AI judge pick a winner
                     |
               (x, y_w, y_l) pairs
            /                       \
   RLHF path                         DPO path
   1. train reward model r(x,y)      train policy directly on
      with Bradley-Terry loss        pairs vs frozen reference
   2. PPO: max r - beta*KL           (no reward model, no
      (policy, ref, RM, critic)       sampling in the loop)
            \                       /
               preference-tuned policy
```

### DPO: the reward model is implicit

Direct Preference Optimization starts from a closed-form fact: the optimal policy for the KL-constrained objective above is

```text
pi*(y|x) = pi_ref(y|x) * exp(r(x,y) / beta) / Z(x)
=> r(x,y) = beta * log( pi*(y|x) / pi_ref(y|x) ) + beta * log Z(x)
```

Substitute that reward into the Bradley-Terry loss. `Z(x)` is the same for both responses, so it cancels:

```text
loss_DPO = -log sigmoid( beta * [ (log pi(y_w) - log pi_ref(y_w))
                                - (log pi(y_l) - log pi_ref(y_l)) ] )
```

Intuition: `beta * (log pi(y) - log pi_ref(y))` is the policy's **implicit reward** for `y`. DPO raises the implicit reward of chosen answers relative to rejected ones. The gradient is weighted by how wrong the model currently is: pairs it already ranks correctly contribute little. `beta` plays the KL role: smaller `beta` lets the policy move further from the reference.

DPO is just a classification loss over sequence log-probabilities. You need the policy and a frozen reference model, no sampling, no reward model, no critic. The toy below computes the reward-model loss, the DPO loss and GRPO's group advantages from raw numbers.

```python
import math
import statistics


def log_sigmoid(x: float) -> float:
    """Numerically stable log(sigmoid(x))."""
    if x >= 0:
        return -math.log1p(math.exp(-x))
    return x - math.log1p(math.exp(x))


def bradley_terry_loss(r_chosen: float, r_rejected: float) -> float:
    """Reward-model loss: -log P(chosen beats rejected)."""
    return -log_sigmoid(r_chosen - r_rejected)


def seq_logprob(token_logprobs: list[float]) -> float:
    """log pi(y|x) is the sum of the response tokens' logprobs."""
    return sum(token_logprobs)


def dpo_loss(pi_c, pi_r, ref_c, ref_r, beta=0.1):
    """Return (loss, implicit reward of chosen, implicit reward of rejected)."""
    reward_c = beta * (pi_c - ref_c)
    reward_r = beta * (pi_r - ref_r)
    return -log_sigmoid(reward_c - reward_r), reward_c, reward_r


def grpo_advantages(rewards: list[float]) -> list[float]:
    """Group-relative advantage: (r - mean) / std within one prompt's samples."""
    mean = statistics.fmean(rewards)
    std = statistics.pstdev(rewards)
    return [(r - mean) / (std + 1e-6) for r in rewards]


if __name__ == "__main__":
    # Reward model: loss is log 2 at a tie and falls as the margin grows.
    assert abs(bradley_terry_loss(1.0, 1.0) - math.log(2)) < 1e-9
    assert bradley_terry_loss(2.0, 0.0) < bradley_terry_loss(0.5, 0.0)

    # Reference-model logprobs for one prompt's chosen and rejected answers.
    ref_c = seq_logprob([-1.2, -0.8, -2.0, -0.5])
    ref_r = seq_logprob([-0.9, -1.1, -1.5])

    # Step 0: policy == reference, so both implicit rewards are 0.
    loss0, rc, rr = dpo_loss(ref_c, ref_r, ref_c, ref_r)
    assert abs(loss0 - math.log(2)) < 1e-9 and rc == rr == 0.0

    # Later: chosen up 3 nats, rejected down 4 nats.
    loss1, rc, rr = dpo_loss(ref_c + 3.0, ref_r - 4.0, ref_c, ref_r)
    assert loss1 < loss0 and rc > 0 > rr
    print(f"DPO loss {loss0:.3f} -> {loss1:.3f}, margin {rc - rr:.2f}")

    # Likelihood displacement: chosen logprob FALLS, loss still improves,
    # because only the margin matters.
    loss2, rc, rr = dpo_loss(ref_c - 1.0, ref_r - 6.0, ref_c, ref_r)
    assert loss2 < loss0 and rc < 0
    print(f"chosen reward {rc:.2f} < 0 yet loss {loss2:.3f} < {loss0:.3f}")

    # GRPO: 4 samples for one prompt, graded by a verifier (1 = correct).
    adv = grpo_advantages([1.0, 0.0, 0.0, 1.0])
    assert adv[0] > 0 > adv[1] and abs(sum(adv)) < 1e-9
    # All-correct (or all-wrong) groups give zero advantage: no signal.
    assert all(abs(a) < 1e-9 for a in grpo_advantages([1.0] * 4))
    print("GRPO advantages", [round(a, 2) for a in adv])
```

The third case is worth remembering: DPO can lower the probability of the chosen answer as long as the rejected one falls faster. In practice both log-probabilities often drop during training, which is one reason DPO models can degrade in subtle ways.

## Techniques

### DPO variants

| Method | Data | Reference model | Key idea | Use when |
|---|---|---|---|---|
| PPO (RLHF) | Pairs for RM, prompts for RL | Yes, plus RM and critic | Online RL against a learned reward | Large labs, nuanced rewards, budget for instability |
| DPO | Pairs | Yes | Implicit reward, classification loss | Default offline choice |
| IPO | Pairs | Yes | Squared loss toward a fixed margin instead of pushing it to infinity | Near-deterministic preferences, DPO overfits |
| KTO | Single examples labeled good or bad | Yes | Prospect-theory value relative to a reference point | You only have thumbs up/down, not pairs |
| ORPO | Pairs | No | Adds an odds-ratio penalty to the SFT loss, one stage | Skip a separate SFT stage, save memory |
| SimPO | Pairs | No | Length-normalized average logprob as reward, plus a target margin | Save reference-model memory, curb length bias |
| Online / iterative DPO | Fresh pairs from the current policy each round | Yes | Re-sample, re-judge, re-train | Offline DPO plateaus |

Rules for choosing: start with DPO on pairs sampled from your own SFT model. Switch to KTO if your signal is unpaired feedback. Consider ORPO or SimPO when memory for a reference model is the constraint. Go iterative when a single offline round stops improving.

### RLAIF and constitutional AI

Human labels are slow and expensive. **RLAIF** replaces the human rater with a model: an LLM judge compares two responses (ideally in both orders, to cancel position bias) and its preferences train the reward model or feed DPO directly. **Constitutional AI** (Anthropic, 2022) made this systematic for harmlessness: the model critiques and revises its own outputs against a written list of principles (the constitution) to produce SFT data, then an AI judge applies the same principles to generate preference labels for RL. The result trades human labeling cost for the judge's biases (verbosity, self-preference, position), so you still need a human-labeled sample to measure judge agreement. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) for judge calibration.

### GRPO and RL with verifiable rewards

For tasks with a checkable answer (math with a known result, code with tests, output that must validate against a schema), you do not need a learned reward model at all. A program grades the output. This is **RL with verifiable rewards (RLVR)**, and it is how [reasoning models]({{ '/llm/reasoning-models/' | relative_url }}) learn long chains of thought: nobody writes the reasoning, the model discovers whatever reasoning produces correct answers.

**GRPO** (Group Relative Policy Optimization, introduced in DeepSeekMath in 2024 and used for DeepSeek-R1 in 2025) drops PPO's critic. For each prompt it samples a group of `G` responses (often 8–64), scores each, and uses the group-normalized reward as the advantage: `A_i = (r_i - mean(r)) / std(r)`. Then it applies a PPO-style clipped update, usually with a KL term to the reference. Consequences:

- No value model, so memory drops to policy plus reference (and the sampler).
- Prompts where every sample is right or every sample is wrong give zero gradient. Curate prompts at the edge of the model's ability; filter out ones that are trivially easy or hopeless.
- Rollout generation dominates wall-clock time, so RL frameworks pair the trainer with a fast inference engine such as vLLM.
- Follow-up work (for example DAPO and Dr. GRPO, both 2025) adjusts the normalization, clipping and length handling, because plain GRPO can bias response length.

## In practice

### Collecting preference data

| Source | Signal | Notes |
|---|---|---|
| Two samples from your SFT model, judged | Pairs | On-policy, the best default |
| User edits to a generated answer | Edited version beats original | Cheap, strongly on-distribution |
| Regenerate clicks | Kept answer beats discarded | Noisy; users regenerate for many reasons |
| Thumbs up/down | Unpaired good/bad | Fits KTO; heavily skewed toward silence |
| Strong model vs weak model answers | Pairs | Easy to make, but off-policy and teaches "sound like the strong model" |
| Public datasets (HH-RLHF, UltraFeedback) | Pairs | General helpfulness; rarely your domain |

Write rater guidelines that define "better" for your product (correct beats fluent, an honest "I don't know" beats a guess) and measure agreement on an overlap set. Drop ties. Keep chosen and rejected similar in length, or the model learns that length is the preference.

### DPO with TRL

Illustrative; argument names shift between TRL versions (older releases take `tokenizer=` instead of `processing_class=`).

```python
from datasets import Dataset
from peft import LoraConfig
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import DPOConfig, DPOTrainer

model_id = "Qwen/Qwen2.5-1.5B-Instruct"   # start from your SFT checkpoint
model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype="bfloat16")
tokenizer = AutoTokenizer.from_pretrained(model_id)

pairs = Dataset.from_list([
    {
        "prompt": [{"role": "user", "content": "Can I get a refund after 45 days?"}],
        "chosen": [{"role": "assistant", "content": "Refunds are available for 30 days. After that I can offer store credit; want me to start that?"}],
        "rejected": [{"role": "assistant", "content": "Sure, refunds are always possible!"}],
    },
    # ... a few thousand more
])

args = DPOConfig(
    output_dir="support-dpo",
    beta=0.1,                       # KL strength; lower = drift further
    learning_rate=5e-6,             # LoRA; full fine-tunes use ~1e-7 to 1e-6
    num_train_epochs=1,
    per_device_train_batch_size=4,
    gradient_accumulation_steps=8,
    bf16=True,
    logging_steps=10,
)
trainer = DPOTrainer(
    model=model,
    ref_model=None,                 # with PEFT, the frozen base is the reference
    args=args,
    train_dataset=pairs,
    processing_class=tokenizer,
    peft_config=LoraConfig(r=16, lora_alpha=32, target_modules="all-linear",
                           task_type="CAUSAL_LM"),
)
trainer.train()
```

With LoRA, the reference model is free: TRL disables the adapter to get reference log-probabilities, so you do not hold a second copy of the weights.

### What to watch during DPO

TRL logs `rewards/chosen`, `rewards/rejected`, `rewards/margins` and `rewards/accuracies` (the share of pairs where the implicit chosen reward beats the rejected one).

- Accuracy climbing to 1.0 within a fraction of an epoch with a huge margin: overfitting. Lower the learning rate, raise `beta`, or train fewer steps.
- `rewards/chosen` going strongly negative: likelihood displacement. Check outputs for degradation; consider adding an SFT term on chosen responses (some trainers expose this as an extra loss weight) or switching to a variant.
- Average response length rising steadily: the model is learning length, not quality.
- Always judge on held-out prompts against the SFT model, not just on training curves.

### GRPO with a verifiable reward

Illustrative TRL usage. Reward functions receive the completions plus any extra dataset columns as keyword arguments and return one float per completion. For conversational datasets, each completion is a list of messages rather than a string.

```python
import json
from trl import GRPOConfig, GRPOTrainer


def schema_reward(completions, expected, **kwargs):
    """1.0 if the output parses and matches the expected fields, 0.5 if it
    only parses, 0.0 otherwise. Deterministic, cheap, hard to game."""
    scores = []
    for text, gold in zip(completions, expected):
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            scores.append(0.0)
            continue
        scores.append(1.0 if out == json.loads(gold) else 0.5)
    return scores


trainer = GRPOTrainer(
    model="Qwen/Qwen2.5-1.5B-Instruct",
    reward_funcs=schema_reward,
    args=GRPOConfig(output_dir="grpo-extract", num_generations=8,
                    max_completion_length=256, learning_rate=1e-6),
    train_dataset=train,    # columns: "prompt", "expected"
)
trainer.train()
```

## Pitfalls

- **Reward hacking.** The policy finds outputs the reward scores highly that are not actually good: padding answers to look thorough, flattering the user, hedging every claim, or, with code rewards, special-casing the tests. Symptom: reward keeps rising while held-out human or judge scores flatten or fall. Fix: KL penalty, reward model ensembles, early stopping on a held-out metric, and graders that check the substance (run hidden tests, not visible ones).
- **Reward overoptimization.** True quality first rises then falls as you optimize harder against a fixed proxy reward. Pick checkpoints by held-out evaluation.
- **Length bias.** Both humans and reward models tend to prefer longer answers. Symptom: mean output tokens climbs across checkpoints. Fix: length-balanced pairs, length-normalized methods (SimPO), or a length penalty in the reward.
- **Sycophancy.** Raters prefer answers that agree with them, so preference tuning can teach the model to agree with wrong premises. Include pairs where the chosen response politely corrects the user.
- **Off-policy pairs.** Pairs from a different model teach its style, and DPO is weakest when both responses are unlikely under the policy. Sample from your own SFT model.
- **Skipping SFT.** DPO on a base model that cannot yet follow the format wastes the signal. SFT first (or use ORPO, which folds it in).
- **Safety and capability regressions.** Preference tuning on narrow data can erode refusals or general skills. Re-run safety and general benchmarks after every round.
- **Judge bias in RLAIF.** Position bias and self-preference leak straight into labels. Swap orders, use a different model family as judge, and spot-check against humans.

## Rules of thumb

- SFT gets the format right; preference tuning picks between acceptable answers. Do not use DPO to teach a format.
- Default to DPO with LoRA, `beta` around 0.1, one epoch, a learning rate one to two orders of magnitude below your SFT rate.
- If you can write a grader, prefer RL with a verifiable reward over learned preferences. A deterministic check is harder to hack than a reward model.
- Generate pairs from the model you are training; judge them with a different model; validate the judge on human labels.
- Choose checkpoints on held-out win rate and safety evals, never on training reward or DPO accuracy.

## Interview questions

**Why do we need preference tuning if SFT works?** SFT only imitates good examples and never learns which outputs are worse. Judging is easier than writing, so comparisons give a scalable signal, and online methods train on the model's own mistakes.

**Walk through the RLHF pipeline.** Collect pairwise preferences on SFT samples, train a reward model with the Bradley-Terry loss `-log sigmoid(r_w - r_l)`, then optimize the policy with PPO to maximize reward minus `beta` times the KL to the SFT reference. Four models are in memory: policy, reference, reward model and critic.

**What does the KL penalty do?** It keeps the policy near the reference distribution, where the reward model is trustworthy. Without it the policy drifts into out-of-distribution text that exploits reward model errors, and fluency degrades.

**How does DPO avoid a reward model?** The optimal KL-regularized policy has a closed form, so the reward can be written as `beta * log(pi/pi_ref)` plus a prompt-only term. Plugged into Bradley-Terry, that term cancels, leaving a classification loss on log-probability ratios of chosen versus rejected responses.

**DPO vs PPO trade-offs?** DPO is offline, stable, cheap and simple, but it learns only from a fixed dataset and can overfit or displace likelihood. PPO explores online and can exceed the data, but it is expensive, unstable and prone to reward hacking.

**What is GRPO and why is it popular for reasoning?** It samples a group of answers per prompt, normalizes their rewards within the group to get advantages, and applies a clipped policy update without a critic. With verifiable rewards (tests, exact answers) it needs no reward model either, which makes RL for math and code cheap and hard to game.

**When would you choose KTO?** When you have unpaired good/bad feedback such as thumbs up/down, which is far easier to collect in production than side-by-side comparisons.

**How do you detect reward hacking?** Training reward rises while held-out human or judge scores stall or drop, and outputs get longer, more flattering or special-case the tests. Track a held-out metric the optimizer never sees.

## Further reading

- "Training language models to follow instructions with human feedback", Ouyang et al., 2022 (InstructGPT).
- "Direct Preference Optimization: Your Language Model is Secretly a Reward Model", Rafailov et al., 2023.
- "Constitutional AI: Harmlessness from AI Feedback", Bai et al., 2022.
- "DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models", Shao et al., 2024 (introduces GRPO).
- "Scaling Laws for Reward Model Overoptimization", Gao et al., 2022.
- Hugging Face TRL documentation: DPOTrainer, KTOTrainer, ORPOTrainer and GRPOTrainer.
