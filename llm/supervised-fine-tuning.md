---
title: Supervised fine-tuning (SFT)
section: llm
order: 34
phase: "Phase 6 · Fine-tuning"
summary: What changes during SFT, the hyperparameters that matter, how to read loss curves, the memory math for full fine-tuning, and how training scales across GPUs.
---

Supervised fine-tuning continues training a pretrained model with the same next-token objective it was pretrained on, but on curated input/output demonstrations and with the loss restricted to the output tokens. It is the first stage of almost every post-training pipeline: instruct models are made with SFT before preference tuning, and most application fine-tunes are SFT alone. The training loop itself is standard deep learning. What makes it work or fail is the data (see [fine-tuning data]({{ '/llm/fine-tuning-data/' | relative_url }})), a handful of hyperparameters, and fitting the job into GPU memory without wasting a week. This note covers full-parameter SFT; [LoRA and PEFT]({{ '/llm/lora-and-peft/' | relative_url }}) covers training a small set of extra parameters instead.

## How it works

### The objective

For a training example with prompt `x` and response `y`, SFT minimizes the average negative log-likelihood of the response tokens:

```text
loss = -(1 / |A|) * sum over t in A of  log p_theta(token_t | tokens_<t)

A = positions inside assistant turns (labels != -100)
```

Training uses teacher forcing: the full sequence goes through the model in one forward pass, the causal mask stops each position from seeing the future, and the model predicts every next token in parallel. Prompt positions are in the input (the model conditions on them) but carry no loss. One optimizer step looks like this:

```text
 input_ids (B x S)                    labels (B x S, -100 on prompt)
        |                                         |
        v                                         |
 embed -> N transformer blocks -> logits (B x S x V)
        |                                         |
        +-------> cross-entropy on positions with a label
                              |
                       loss.backward()
                              |
       clip grad norm -> AdamW update -> LR scheduler -> next batch
```

With gradient accumulation, several micro-batches run forward and backward before one optimizer step, so the effective batch is `per_device_batch * grad_accum_steps * num_gpus`.

### What changes in the model

Every trainable weight moves a little in the direction that makes your responses more likely. With the small learning rates used in SFT the weight changes are tiny in norm, but they reshape behavior: the model's output distribution concentrates on the formats, phrasing and decisions in your data. Two consequences follow. Outputs become less diverse (the distribution sharpens), and behavior not represented in your data drifts, which is catastrophic forgetting. LIMA's result (strong instruction following from 1,000 examples) is evidence that SFT mostly changes how the model responds, not what it knows; see [when to fine-tune]({{ '/llm/when-to-fine-tune/' | relative_url }}).

### Full fine-tuning vs partial

| Approach | Trainable params | Memory | Quality ceiling | Forgetting |
|---|---|---|---|---|
| Full fine-tuning | 100% | Highest (about 16 bytes/param plus activations) | Highest | Highest risk |
| Freeze lower layers, train top k | 10 to 50% | Lower; no grads or optimizer for frozen layers | Good for style tasks | Moderate |
| Train selected modules (for example attention only) | 30 to 60% | Moderate | Task-dependent | Moderate |
| LoRA, QLoRA | Under 1% (extra params) | Lowest | Close to full for most behavioral tasks | Lowest |

Full fine-tuning is worth it when you have lots of data (tens of thousands of examples or more), the task departs strongly from the base model's behavior (a new language, continued pretraining on a large domain corpus, heavy code or math training), and you have the GPUs. Otherwise start with LoRA. Freezing is done by setting `requires_grad = False` on parameters; frozen parameters need no gradients or optimizer state, which is where the memory saving comes from.

## Hyperparameters

| Hyperparameter | Typical for full SFT | Notes |
|---|---|---|
| Learning rate | 5e-6 to 5e-5 (1e-5 to 2e-5 common) | Lower for bigger models; LoRA uses about 10x higher |
| Epochs | 1 to 3 | More only for tiny datasets; watch validation |
| Effective batch | 16 to 128 sequences | Via gradient accumulation if memory-bound |
| Warmup | 3 to 10% of steps | Avoids early large updates from fresh Adam state |
| Scheduler | Cosine or linear decay | Constant with warmup is fine for short runs |
| Weight decay | 0 to 0.1 | Small effect in short SFT runs |
| Max grad norm | 1.0 | Clipping prevents spikes from outlier batches |
| Max sequence length | Covers 95 to 99% of examples | Memory and time scale with it |

For calibration, the Llama 2 paper reports SFT with an initial learning rate of 2e-5, cosine schedule, weight decay 0.1, batch size 64, sequence length 4,096, two epochs, and loss zeroed on prompt tokens. Pretraining peak learning rates are an order of magnitude higher (around 3e-4 for 7B-scale models); SFT stays low because large updates destroy pretrained features.

**Learning rate** is the most important knob. Too high: loss spikes, general ability collapses, outputs become repetitive. Too low: loss barely moves and the model keeps its old behavior. Sweep three values (for example 5e-6, 1e-5, 2e-5) on one epoch and compare task evals, not just loss.

**Epochs.** With a few thousand examples, one to three epochs is typical. Each pass over the same examples increases memorization; past two or three epochs you are usually fitting noise.

**Max sequence length and truncation.** Set it from the token-length distribution of your data. Examples longer than the limit get truncated, usually cutting off the end of the answer, so the model learns to stop mid-thought. Drop or split those examples instead.

**Packing** concatenates several short examples into one sequence of max length so that no compute is wasted on padding. On datasets with many short examples it can speed training severalfold. The risk: with naive packing, tokens attend across example boundaries to unrelated text. Implementations that reset position IDs and use variable-length attention kernels (FlashAttention) avoid cross-example attention; recent TRL versions support this, but check your version's behavior. Packing also changes the number of optimizer steps per epoch, so revisit learning rate and warmup when you switch it on. The alternative without packing is to group examples of similar length into batches to reduce padding.

## Reading loss curves

Log training loss, validation loss, learning rate, gradient norm and tokens per second at every step or every few steps, and generate outputs from a fixed set of prompts at every checkpoint.

| Pattern | Meaning | Action |
|---|---|---|
| Train loss drops fast in the first 50 to 200 steps, then flattens | Format and style learned quickly; normal | None |
| Stair-step drop at each epoch boundary | Model recognizing examples it has seen; memorization starting | Fine if validation is flat; stop if it rises |
| Validation loss bottoms out, then rises | Overfitting | Use the earlier checkpoint; fewer epochs; more data |
| Train loss near zero | Memorized the training set | Fewer epochs, lower LR, more diverse data |
| Loss flat from step 0 | All labels masked, LR zero, or params frozen | Decode labels; check LR schedule and `requires_grad` |
| Loss NaN or inf | fp16 overflow, LR too high, an example with no labeled tokens | Use bf16; lower LR; filter empty examples |
| Spikes with large gradient norms | LR too high or outlier batches | Lower LR, keep clipping, inspect those batches |
| Starting loss much higher than expected | Chat template or tokenization mismatch | Decode one batch and read it |

Validation loss and task quality can disagree. Validation loss measures likelihood of the exact reference text; a model can become better at the task while getting less likely to reproduce references word for word. Choose checkpoints by your task eval and use validation loss to spot overfitting trends. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).

> **Rule of thumb:** before the real run, overfit 10 to 20 examples. Loss should go to near zero within a few dozen steps. If it does not, the bug is in your masking, template or training loop, not your hyperparameters.

## Memory math for full fine-tuning

Mixed-precision training with AdamW keeps, per trainable parameter:

```text
bf16 weights            2 bytes
bf16 gradients          2 bytes
fp32 master weights     4 bytes   (updates are too small for bf16)
fp32 Adam momentum m    4 bytes
fp32 Adam variance v    4 bytes
----------------------------------
total                  16 bytes/param   (the ZeRO paper's accounting)
```

So model states alone are about 112 GB for a 7B model and over 1 TB for 70B, before activations. That is why a 7B full fine-tune does not fit on one 80 GB GPU, and why sharding or LoRA is the norm.

**Activations** are the intermediate tensors saved for backward. A rough estimate from Korthikanti et al. (2022) is about `34 * s * b * h` bytes per layer in 16-bit (plus an attention-score term that FlashAttention removes), where `s` is sequence length, `b` micro-batch size and `h` hidden size. With full activation checkpointing you keep roughly `2 * s * b * h` bytes per layer and recompute the rest. **Logits** are easy to forget: with a 128k vocabulary, the fp32 logits for one 4,096-token sequence are about 2 GB, plus the same again for their gradient. Fused cross-entropy kernels (for example Liger Kernel) avoid materializing them in full.

**Training time** follows from the `6 * N * D` rule: about 6 FLOPs per parameter per token for forward plus backward, or about 8 with activation recomputation.

```python
from dataclasses import dataclass

GB = 1e9


@dataclass
class Model:
    params: float
    layers: int
    hidden: int
    vocab: int


def model_states(params: float, gpus: int = 1, zero: int = 0) -> float:
    """Bytes per GPU for bf16 weights + grads + fp32 AdamW states (16 B/param).

    zero=1 shards optimizer states, 2 also grads, 3 also weights (= FSDP)."""
    weights, grads, optim = 2 * params, 2 * params, 12 * params
    if zero >= 1:
        optim /= gpus
    if zero >= 2:
        grads /= gpus
    if zero >= 3:
        weights /= gpus
    return weights + grads + optim


def activations(m: Model, seq: int, micro_batch: int, ckpt: bool) -> float:
    """Rough bytes: bf16, FlashAttention, plus fp32 logits and their grads."""
    per_layer = 34 * seq * micro_batch * m.hidden
    if ckpt:
        saved = 2 * seq * micro_batch * m.hidden * m.layers + per_layer
    else:
        saved = per_layer * m.layers
    logits = 2 * 4 * seq * micro_batch * m.vocab
    return saved + logits


def train_hours(params: float, tokens: float, gpus: int, peak_tflops: float,
                mfu: float = 0.4, ckpt: bool = True) -> float:
    flops = (8 if ckpt else 6) * params * tokens
    return flops / (gpus * peak_tflops * 1e12 * mfu) / 3600


if __name__ == "__main__":
    m = Model(params=8.0e9, layers=32, hidden=4096, vocab=128_256)
    print("setup (8 GPUs, seq 4096, micro-batch 1)  states  acts  total")
    for name, zero, ckpt in [("DDP, no sharding", 0, True),
                             ("ZeRO-1", 1, True),
                             ("ZeRO-2", 2, True),
                             ("ZeRO-3 / FSDP", 3, True),
                             ("ZeRO-3 / FSDP, no ckpt", 3, False)]:
        s = model_states(m.params, gpus=8, zero=zero) / GB
        a = activations(m, 4096, 1, ckpt) / GB
        fits = "fits 80 GB" if s + a < 75 else "does not fit"
        print(f"{name:<24} {s:6.0f} {a:5.1f} {s + a:6.1f}  {fits}")

    tokens = 5000 * 1000 * 3  # examples * avg tokens * epochs
    hours = train_hours(m.params, tokens, gpus=8, peak_tflops=1000)
    print(f"{tokens / 1e6:.0f}M tokens on 8 GPUs: ~{hours * 60:.0f} minutes")

    assert model_states(8e9) == 128e9
    assert model_states(8e9, gpus=8, zero=1) == 44e9
    assert model_states(8e9, gpus=8, zero=2) == 30e9
    assert model_states(8e9, gpus=8, zero=3) == 16e9
    assert activations(m, 4096, 1, True) < activations(m, 4096, 1, False)
    assert model_states(70e9, gpus=8, zero=3) / GB == 140  # 70B needs more
```

Output:

```text
setup (8 GPUs, seq 4096, micro-batch 1)  states  acts  total
DDP, no sharding            128   5.8  133.8  does not fit
ZeRO-1                       44   5.8   49.8  fits 80 GB
ZeRO-2                       30   5.8   35.8  fits 80 GB
ZeRO-3 / FSDP                16   5.8   21.8  fits 80 GB
ZeRO-3 / FSDP, no ckpt       16  22.5   38.5  fits 80 GB
15M tokens on 8 GPUs: ~5 minutes
```

Treat these as planning numbers: real usage adds framework overhead, memory fragmentation, communication buffers and temporary tensors, so keep 10 to 20% headroom. The last assertion is the useful one for 70B: even fully sharded across 8 GPUs, model states alone are 140 GB per GPU, so you need more GPUs, CPU offload, or LoRA. The `peak_tflops` value is a placeholder for an H100-class GPU's dense bf16 peak; check your hardware's spec sheet and measure your real utilization (MFU, model FLOPs utilization) in a short run.

## Fitting it in memory

**Gradient checkpointing** (activation recomputation) saves only some activations, such as each layer's input, and recomputes the rest during backward. It costs roughly one extra forward pass, about 20 to 35% more compute, and cuts activation memory by an order of magnitude. Enable it with `gradient_checkpointing=True` in the trainer config or `model.gradient_checkpointing_enable()`, and turn off the KV cache during training (`use_cache=False`).

**Mixed precision (bf16).** Compute in bf16, keep fp32 master weights for the optimizer update. bf16 has the same 8-bit exponent as fp32, so it has the same range and does not overflow; fp16 has a 5-bit exponent and needs dynamic loss scaling to avoid overflow and underflow. bf16 needs Ampere-generation GPUs or newer (A100, H100, L4 and similar); on older cards such as V100 or T4 you are stuck with fp16. Master weights stay in fp32 because bf16 has only 8 bits of significand precision (2 to 3 decimal digits): an update smaller than roughly 1/256 of a weight's value is rounded away entirely.

**Smaller optimizer states.** 8-bit Adam (bitsandbytes) stores the two moments in 8 bits, cutting about 6 bytes per parameter. Paged optimizers move optimizer state to CPU memory during spikes. Adafactor factorizes the second moment. Each trades some stability or speed for memory.

**CPU and NVMe offload** (ZeRO-Offload, FSDP CPU offload) keeps optimizer states or parameters in host memory. It makes a big model fit on few GPUs at a large speed cost.

## Multi-GPU training

```text
 DDP          GPU0: full model + full optimizer   batch shard 0
              GPU1: full model + full optimizer   batch shard 1
              -> all-reduce gradients each step

 ZeRO-3/FSDP  GPU0: 1/N of weights, grads, optimizer   batch shard 0
              GPU1: 1/N of weights, grads, optimizer   batch shard 1
              -> all-gather a layer's weights before use, free after
              -> reduce-scatter gradients to their owner shard
```

| Strategy | Shards | Per-GPU model states | Communication | Use when |
|---|---|---|---|---|
| DDP | Nothing | 16 B/param | All-reduce grads | Model plus optimizer fits on one GPU (roughly up to 2 to 3B full FT on 80 GB) |
| ZeRO-1 | Optimizer states | `4 + 12/N` B/param | Like DDP | Moderate savings, near-DDP speed |
| ZeRO-2 | + gradients | `2 + 14/N` B/param | Like DDP | Common default for mid-size models |
| ZeRO-3 / FSDP full shard | + weights | `16/N` B/param | About 1.5x DDP | Large models; scales to many GPUs |
| Hybrid sharding | Shard within node, replicate across nodes | Between | Less cross-node traffic | Multi-node clusters |
| Tensor / pipeline parallel | Splits layers or matrices | Varies | High-bandwidth links needed | Very large models, usually with ZeRO or FSDP |

FSDP is PyTorch's native implementation of the ZeRO-3 idea; DeepSpeed implements all ZeRO stages plus offload. In the Hugging Face stack, Accelerate configures either, and the same training script runs under `accelerate launch --config_file fsdp.yaml train.py` or `torchrun --nproc_per_node 8 train.py`. Sharding needs fast interconnect (NVLink within a node, InfiniBand or similar across nodes) to stay efficient. See [GPUs and parallelism]({{ '/llm/gpus-and-parallelism/' | relative_url }}).

## In practice: TRL SFTTrainer

An illustrative full fine-tune of a 7B to 8B instruct model on chat-format JSONL. Argument names drift between TRL versions (for example `max_length` was `max_seq_length` in older releases), so check against the version you install.

```python
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer

data = load_dataset("json", data_files={"train": "train.jsonl",
                                        "validation": "val.jsonl"})

config = SFTConfig(
    output_dir="checkpoints/support-sft-v4",
    num_train_epochs=2,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=8,      # effective batch 2 * 8 * 8 GPUs = 128
    learning_rate=1e-5,
    lr_scheduler_type="cosine",
    warmup_ratio=0.05,
    weight_decay=0.0,
    max_grad_norm=1.0,
    bf16=True,
    gradient_checkpointing=True,
    max_length=4096,
    packing=False,
    assistant_only_loss=True,           # needs a template that marks assistant spans
    eval_strategy="steps",
    eval_steps=100,
    save_steps=100,
    logging_steps=10,
    report_to="none",
)

trainer = SFTTrainer(
    model="Qwen/Qwen2.5-7B-Instruct",   # any causal LM with a chat template
    args=config,
    train_dataset=data["train"],
    eval_dataset=data["validation"],
)
trainer.train()
trainer.save_model()
```

Launch it with FSDP or DeepSpeed through Accelerate for a model this size. A run plan that avoids most wasted GPU days:

1. Decode one collated batch and read which tokens carry labels.
2. Overfit 10 to 20 examples to prove the loop works.
3. Sweep learning rate (three values) for one epoch on the full data; compare task evals.
4. Do the full run, evaluating and saving every few hundred steps.
5. Pick the checkpoint by task eval, then run general-capability and safety regression evals.
6. Record dataset hash, config, code commit and the chosen checkpoint together. See [fine-tuning in practice]({{ '/llm/fine-tuning-in-practice/' | relative_url }}).

## Pitfalls

- **Catastrophic forgetting.** General skills regress after training. Notice it with a general regression eval; fix with lower LR, fewer epochs, mixing 5 to 20% general instruction data into the training set, or switching to LoRA.
- **Out-of-memory at the first eval or save, not the first step.** Eval batch size, generation, or gathering a sharded state dict needs extra memory. Use a smaller eval batch and sharded checkpoint saving.
- **Effective batch changed silently.** Adding GPUs multiplies effective batch size; the same LR now behaves differently. Recompute and keep effective batch fixed across hardware changes.
- **Overfitting to a fixed system prompt.** If every example has the same system prompt, the model ignores changes to it. Vary it, or accept that it is now baked in.
- **Saving a checkpoint you cannot serve.** FSDP checkpoints may be sharded; consolidate to a standard format and test loading in your serving engine (vLLM, SGLang, TGI) before declaring victory.
- **fp16 on modern hardware.** NaNs from overflow. Use bf16 wherever supported.

## Rules of thumb

- Start with LoRA; go full only with lots of data or a large distribution shift.
- LR around 1e-5 for full SFT of 7B-scale models; 1 to 3 epochs; warmup a few percent; cosine decay; clip at 1.0.
- Budget 16 bytes per parameter for model states, then add activations and logits.
- bf16 and gradient checkpointing on by default.
- DDP if it fits on one GPU, FSDP or ZeRO-3 if not.

## Interview questions

**What is the SFT loss, and which tokens contribute?** Cross-entropy of the next token, averaged over response (assistant) positions only. Prompt tokens are in the input but labeled `-100` so they are ignored. The objective is the same as pretraining; the data and the masking differ.

**How much GPU memory does full fine-tuning of a 7B model with AdamW need?** About 16 bytes per parameter for bf16 weights and gradients plus fp32 master weights and two Adam moments: roughly 112 GB, plus activations and logits. It does not fit on one 80 GB GPU; shard with FSDP or ZeRO-3 across several GPUs, or use LoRA or QLoRA.

**Why bf16 instead of fp16?** Same exponent range as fp32, so no overflow and no loss scaling; fp16's narrow range causes NaNs without dynamic loss scaling. Master weights stay fp32 because bf16's precision is too coarse for small updates.

**What does gradient checkpointing trade?** Memory for compute: it stores only checkpoints (such as each layer's input) and recomputes the rest during backward, cutting activation memory by an order of magnitude for about 20 to 35% more compute.

**Explain DDP, FSDP and the ZeRO stages.** DDP replicates everything and all-reduces gradients. ZeRO-1 shards optimizer states, ZeRO-2 also gradients, ZeRO-3 also the weights, which are all-gathered layer by layer when needed. FSDP is PyTorch's ZeRO-3-style implementation. More sharding means less memory per GPU and more communication.

**How do you detect overfitting in SFT?** Validation loss rising after a minimum, training loss near zero, outputs reproducing training text verbatim, and task or general evals declining on held-out data. Fix with fewer epochs, lower LR, more or more diverse data, or an earlier checkpoint.

**What is packing and what can go wrong?** Concatenating short examples into full-length sequences to avoid padding waste. Without attention-boundary handling, tokens attend across unrelated examples, and step counts and loss averaging change, which affects LR schedules and comparisons.

## Further reading

- Touvron et al., "Llama 2: Open Foundation and Fine-Tuned Chat Models" (2023), the SFT section.
- Rajbhandari et al., "ZeRO: Memory Optimizations Toward Training Trillion Parameter Models" (2020).
- Zhao et al., "PyTorch FSDP: Experiences on Scaling Fully Sharded Data Parallel" (2023).
- Korthikanti et al., "Reducing Activation Recomputation in Large Transformer Models" (2022).
- Micikevicius et al., "Mixed Precision Training" (2017).
- Hugging Face TRL documentation for SFTTrainer.
