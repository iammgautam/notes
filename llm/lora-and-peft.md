---
title: LoRA, QLoRA and PEFT
section: llm
order: 35
phase: "Phase 6 · Fine-tuning"
summary: The low-rank update math behind LoRA, which modules to target, how QLoRA fits big models on one GPU, other PEFT methods, and merging vs multi-LoRA serving.
---

Parameter-efficient fine-tuning (PEFT) freezes the pretrained weights and trains a small number of new parameters instead. LoRA, the default method, learns a low-rank correction to selected weight matrices, typically under 1% of the parameter count. The operational consequences: training needs a fraction of the memory, a fine-tune is a file of tens of megabytes instead of a model copy, one base model can serve hundreds of per-task or per-tenant adapters, and rollback means not loading the adapter. For most application fine-tunes LoRA gets close to full fine-tuning quality, and it forgets less.

## How LoRA works

### The low-rank update

A linear layer computes `h = W0 x` with a weight matrix `W0` of shape `d_out x d_in`. Full fine-tuning learns an update `delta_W` of the same shape. LoRA constrains that update to be low-rank by writing it as the product of two thin matrices:

```text
h = W0 x + (alpha / r) * B (A x)

W0 : d_out x d_in   frozen
A  : r x d_in       trainable, random init (Kaiming-style)
B  : d_out x r      trainable, zero init
r  << min(d_in, d_out)        rank, typically 8 to 64
alpha / r                     scaling factor

trainable per matrix:  r * (d_in + d_out)   instead of   d_in * d_out
4096 x 4096, r = 16:   131,072              instead of   16,777,216  (0.8%)
```

```text
               x  (d_in)
               |
      +--------+-----------+
      |                    |
  W0 (frozen)         A  (r x d_in)      random init
  d_out x d_in             |
      |               B  (d_out x r)     zero init
      |                    |
      |               x (alpha / r)
      +------ (+) ---------+
               |
               h  (d_out)
```

Because `B` starts at zero, `BA = 0` at step 0, so the model starts exactly at the pretrained behavior and training moves away from it gradually. After training, the update can be folded into the weight, `W = W0 + (alpha / r) * B A`, which leaves a normal model with zero inference overhead.

Why a low-rank update is enough: Aghajanyan et al. (2020) showed that fine-tuning pretrained language models has a low intrinsic dimension; good solutions exist in a small subspace of weight space. The LoRA paper (Hu et al., 2021) reported performance comparable to full fine-tuning on GPT-3 175B with about 10,000 times fewer trainable parameters and about a third of the GPU memory.

### What the scaling factor does

The update is multiplied by `alpha / r`, which keeps its magnitude roughly stable across ranks so you need not retune the learning rate every time you change `r`. In practice people set `alpha = r` or `alpha = 2r` and tune the learning rate. Rank-stabilized LoRA (rsLoRA, Kalajdzievski, 2023) argues the factor should be `alpha / sqrt(r)`, because with `alpha / r` high ranks learn too slowly to benefit from their extra capacity; PEFT exposes it as `use_rslora=True`. Whichever you use, changing `r` or `alpha` changes the effective step size, so treat them together.

### Where the memory goes

LoRA removes gradients and optimizer states for the frozen weights, which is 14 of the 16 bytes per parameter in mixed-precision AdamW (see [supervised fine-tuning]({{ '/llm/supervised-fine-tuning/' | relative_url }})). It does not remove:

- **The base weights themselves** (2 bytes per parameter in bf16, about 0.5 in 4-bit with QLoRA).
- **Activations.** Gradients still flow backward through every frozen layer to reach adapters in earlier layers, so activation memory is about the same as full fine-tuning. Gradient checkpointing still matters.

### Which modules to target

A decoder block has attention projections (`q_proj`, `k_proj`, `v_proj`, `o_proj`) and MLP projections (`gate_proj`, `up_proj`, `down_proj` in Llama-style models). The original LoRA paper adapted only the query and value projections. The QLoRA paper found that applying LoRA to all linear layers was what closed the gap to full fine-tuning, and that rank mattered less than coverage. The current default is all linear layers (`target_modules="all-linear"` in PEFT). The MLP matrices hold most of the parameters, and they matter most when the task needs new input-to-output mappings rather than a style change.

Embeddings and the output head are not adapted by default. If you add new special tokens, the new embedding rows must be trained: list `embed_tokens` and `lm_head` in `modules_to_save`, which trains full copies of those layers. With a 128k vocabulary that is about a billion extra trainable parameters for an 8B model, so do it only when you must.

The calculator below counts LoRA parameters for an 8B Llama-3-style model and estimates model-state memory for full fine-tuning, LoRA on a bf16 base and QLoRA on a 4-bit base. It also checks the two properties above: a zero-initialized `B` leaves outputs unchanged, and the merged weight gives the same output as the separate adapter path.

```python
import random

# Llama-3-8B-like shapes: name -> (d_in, d_out) of each linear layer
HIDDEN, INTER, LAYERS, VOCAB, KV = 4096, 14336, 32, 128_256, 1024
SHAPES = {
    "q_proj": (HIDDEN, HIDDEN), "k_proj": (HIDDEN, KV),
    "v_proj": (HIDDEN, KV), "o_proj": (HIDDEN, HIDDEN),
    "gate_proj": (HIDDEN, INTER), "up_proj": (HIDDEN, INTER),
    "down_proj": (INTER, HIDDEN),
}
LINEAR = LAYERS * sum(i * o for i, o in SHAPES.values())
OTHER = 2 * VOCAB * HIDDEN  # input embeddings + untied output head


def lora_params(targets, r: int) -> int:
    """A is r x d_in and B is d_out x r: r * (d_in + d_out) per matrix."""
    return LAYERS * sum(r * sum(SHAPES[t]) for t in targets)


def state_gb(method: str, trainable: int) -> float:
    """Model-state memory: weights + grads + AdamW (16 B per trainable)."""
    if method == "full":
        return (LINEAR + OTHER) * 16 / 1e9
    base_bytes = 2 if method == "lora" else (4 + 0.127) / 8  # NF4 + consts
    # QLoRA quantizes linear layers only; embeddings and head stay bf16.
    base = LINEAR * base_bytes + OTHER * 2
    return (base + trainable * 16) / 1e9


def matvec(m, v):
    return [sum(a * b for a, b in zip(row, v)) for row in m]


def lora_forward(w0, a, b, scale, x):
    delta = matvec(b, matvec(a, x))
    return [h + scale * d for h, d in zip(matvec(w0, x), delta)]


def merge(w0, a, b, scale):
    return [[w0[i][j] + scale * sum(b[i][k] * a[k][j] for k in range(len(a)))
             for j in range(len(w0[0]))] for i in range(len(w0))]


if __name__ == "__main__":
    all_linear = list(SHAPES)
    for targets, r in [(["q_proj", "v_proj"], 8), (all_linear, 8),
                       (all_linear, 16), (all_linear, 64)]:
        n = lora_params(targets, r)
        print(f"{len(targets)} modules r={r:<3} {n / 1e6:7.1f}M params"
              f"  {100 * n / (LINEAR + OTHER):.2f}% of base"
              f"  adapter {n * 2 / 1e6:.0f} MB in bf16")
    n16 = lora_params(all_linear, 16)
    for method in ("full", "lora", "qlora"):
        print(f"{method:<6} model states ~{state_gb(method, n16):6.1f} GB")

    assert round((LINEAR + OTHER) / 1e9, 2) == 8.03
    assert n16 == 41_943_040
    assert lora_params(["q_proj", "v_proj"], 8) == 3_407_872
    assert state_gb("qlora", n16) < 8 < state_gb("lora", n16) < 20

    rng = random.Random(0)
    d_out, d_in, r, alpha = 6, 5, 2, 4
    w0 = [[rng.gauss(0, 1) for _ in range(d_in)] for _ in range(d_out)]
    a = [[rng.gauss(0, 1) for _ in range(d_in)] for _ in range(r)]
    b = [[0.0] * r for _ in range(d_out)]
    x = [rng.gauss(0, 1) for _ in range(d_in)]
    assert lora_forward(w0, a, b, alpha / r, x) == matvec(w0, x)  # B = 0
    b = [[rng.gauss(0, 1) for _ in range(r)] for _ in range(d_out)]
    merged = matvec(merge(w0, a, b, alpha / r), x)
    unmerged = lora_forward(w0, a, b, alpha / r, x)
    assert all(abs(p - q) < 1e-9 for p, q in zip(merged, unmerged))
    print("merge check passed")
```

Output:

```text
2 modules r=8       3.4M params  0.04% of base  adapter 7 MB in bf16
7 modules r=8      21.0M params  0.26% of base  adapter 42 MB in bf16
7 modules r=16     41.9M params  0.52% of base  adapter 84 MB in bf16
7 modules r=64    167.8M params  2.09% of base  adapter 336 MB in bf16
full   model states ~ 128.5 GB
lora   model states ~  16.7 GB
qlora  model states ~   6.4 GB
merge check passed
```

Add activations on top (several GB at 4k tokens with checkpointing). The practical result: full fine-tuning an 8B model needs a multi-GPU node, LoRA fits on one 40 to 80 GB GPU, and QLoRA fits on a 24 GB card.

## QLoRA

QLoRA (Dettmers et al., 2023) trains LoRA adapters on top of a base model stored in 4 bits. The paper fine-tuned a 65B model on a single 48 GB GPU and largely matched 16-bit fine-tuning quality on its benchmarks. Its pieces:

- **4-bit NormalFloat (NF4).** Pretrained weights are roughly normally distributed, so NF4 places its 16 levels at quantiles of a normal distribution instead of evenly spaced, which uses the levels efficiently. Weights are quantized in blocks of 64, each with its own absmax scale.
- **Double quantization.** The per-block scales are themselves quantized to 8 bits (in blocks of 256), cutting their overhead from about 0.5 to about 0.127 bits per parameter.
- **Paged optimizers.** Optimizer state lives in NVIDIA unified memory and pages to CPU RAM when a long sequence causes a memory spike, instead of crashing with out-of-memory.
- **bf16 compute.** Each 4-bit block is dequantized to bf16 just in time for the matmul. Gradients flow through the dequantized weights into the bf16 LoRA adapters; the 4-bit base is never updated.

Trade-offs: QLoRA training is slower than bf16 LoRA because of dequantization on every forward and backward pass, and the adapter was trained against quantized weights, so applying it to the original bf16 base gives a slightly different model from the one you evaluated during training. Evaluate the exact artifact you deploy. See [quantization]({{ '/llm/quantization/' | relative_url }}) for NF4 vs GPTQ and AWQ.

## DoRA and LoRA variants

**DoRA** (Liu et al., 2024) decomposes each weight into a magnitude vector and a direction matrix, `W = m * V / ||V||` (norm taken per column), trains the magnitude directly and applies LoRA to the direction. The motivation is that full fine-tuning changes magnitude and direction in a different pattern than plain LoRA can express; DoRA reported consistent gains over LoRA, especially at low ranks. It adds a little training overhead and still merges into a plain weight. In PEFT it is `use_dora=True`.

Other variants worth recognizing by name:

| Variant | Idea |
|---|---|
| rsLoRA | Scale by `alpha / sqrt(r)` so high ranks actually help |
| LoRA+ | Higher learning rate for `B` than for `A` |
| PiSSA | Initialize `A` and `B` from the top singular vectors of `W0` |
| AdaLoRA | Allocate rank adaptively across layers during training |

## Other PEFT methods

| Method | What is trained | Typical share | Inference overhead | Mergeable |
|---|---|---|---|---|
| LoRA, DoRA | Low-rank updates to weight matrices | 0.1 to 2% | None after merge | Yes |
| Adapters (Houlsby et al., 2019) | Small bottleneck MLPs inserted after sublayers | 0.5 to 5% | Extra sequential layers | No |
| Prefix tuning (Li and Liang, 2021) | Trainable key/value vectors prepended at every layer | About 0.1% | Uses context positions | No |
| Prompt tuning (Lester et al., 2021) | Soft prompt embeddings at the input only | Under 0.01% | Uses context positions | No |
| IA3 (Liu et al., 2022) | Vectors that rescale keys, values and MLP activations | About 0.01% | None after merge | Yes |
| BitFit | Bias terms only | Under 0.1% | None | Yes |

Adapters add latency on every token because their layers cannot be folded away. Prefix and prompt tuning learn continuous "virtual tokens" that consume context, and prompt tuning matches full fine-tuning only at large model sizes. IA3 trains the fewest parameters but has less capacity than LoRA. BitFit does not apply to Llama-style models, which have no bias terms.

LoRA won because it is simple, has no inference overhead once merged, works across model types, and has good library and serving support. Reach for something else only with a specific reason.

## Merging vs serving many adapters

### Merging

`merge_and_unload()` in PEFT folds the adapter into the weights and returns a plain model. Serving is identical to the base model, with no overhead and no special engine support required. The costs: every task becomes a full-size model copy (16 GB for 8B in bf16), and swapping behavior means swapping models. Merge into a bf16 copy of the base, never into a 4-bit model; if you want a quantized deployment, merge first and then quantize the merged model with your serving quantization method, and evaluate after quantizing.

### Multi-LoRA serving

Keep one base model resident on the GPU and apply a different adapter per request. Batching requests that use different adapters needs special kernels: Punica (Chen et al., 2023) introduced a segmented gather matrix-vector kernel for this, and S-LoRA (Sheng et al., 2023) added unified paging of adapter weights and KV cache to serve thousands of adapters. vLLM, SGLang, TGI and LoRAX support multi-LoRA serving.

```text
 requests:  tenant-a    tenant-b    tenant-a    (no adapter)
                \           |           |          /
                 v          v           v         v
        +------------------------------------------------+
        | shared base weights W0 (one copy on the GPU)   |
        | batched W0 x for every request                 |
        | + per-request (alpha/r) B_i A_i x, grouped by  |
        |   adapter in one kernel                        |
        +------------------------------------------------+
          adapter slots: [tenant-a] [tenant-b] [ free ]
          loaded from disk or object storage, LRU-evicted
```

Where this shines: per-tenant adapters in multi-tenant SaaS, several task adapters on one base, and A/B tests between adapter versions. The costs: unmerged adapters add a small per-token compute overhead that grows with rank and with the number of distinct adapters in a batch; there are limits on the number of resident adapters and the maximum rank; loading a cold adapter adds latency to the first request; and every adapter must have been trained on exactly the same base model revision.

| | Merge | Multi-LoRA |
|---|---|---|
| Latency overhead | None | Small, grows with rank and adapter mix |
| GPU memory per extra task | A full model copy | Tens to hundreds of MB |
| Engine support needed | None | LoRA support in the engine |
| Best for | One or two fine-tunes at high volume | Many tasks or tenants, uneven traffic |

Adapters can also be combined (weighted sums, or TIES and DARE merging via PEFT's `add_weighted_adapter`); evaluate the result like any other model.

## Hyperparameter guidance

| Knob | Starting point | Range | Notes |
|---|---|---|---|
| Rank `r` | 16 | 8 to 64; up to 128 or 256 for big shifts | More data or harder tasks benefit from higher rank |
| `lora_alpha` | `2 * r` (or `r`) | | Change with `r`; it sets the effective step size |
| Learning rate | 2e-4 | 5e-5 to 3e-4 | About 10x full fine-tuning; QLoRA used 2e-4 for smaller models, 1e-4 for the largest |
| Dropout | 0.05 | 0 to 0.1 | Matters more on small datasets |
| Target modules | all linear | | Coverage beats rank at equal parameter count |
| Epochs | 1 to 3 | | Same overfitting signals as full SFT |
| `use_rslora` | off | on for r of 64 or more | Use alpha / sqrt(r) scaling |

Biderman et al. (2024) compared LoRA with full fine-tuning on code and math: LoRA substantially underperformed full fine-tuning when training on large amounts of new data (continued pretraining scale), but it preserved the base model's other abilities better, and full fine-tuning learned updates of much higher rank than typical LoRA configurations. The practical reading: LoRA for behavior, format and narrow tasks; consider full fine-tuning when you are teaching a large new skill with lots of data.

## In practice

An illustrative QLoRA setup with transformers, PEFT and TRL. Exact argument names vary by version.

```python
import torch
from peft import LoraConfig
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

base_id = "meta-llama/Llama-3.1-8B-Instruct"

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)
model = AutoModelForCausalLM.from_pretrained(
    base_id, quantization_config=bnb, torch_dtype=torch.bfloat16)

lora = LoraConfig(
    r=16,
    lora_alpha=32,
    lora_dropout=0.05,
    target_modules="all-linear",
    bias="none",
    task_type="CAUSAL_LM",
)

config = SFTConfig(
    output_dir="adapters/support-v3",
    num_train_epochs=2,
    per_device_train_batch_size=4,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    lr_scheduler_type="cosine",
    warmup_ratio=0.03,
    bf16=True,
    gradient_checkpointing=True,
    optim="paged_adamw_8bit",
    logging_steps=10,
)

trainer = SFTTrainer(model=model, args=config, train_dataset=train_ds,
                     eval_dataset=val_ds, peft_config=lora)
trainer.model.print_trainable_parameters()  # sanity check: well under 1%
trainer.train()
trainer.save_model()  # writes only the adapter weights and config
```

When you build the PEFT model yourself instead of passing `peft_config`, call `prepare_model_for_kbit_training(model)` before `get_peft_model(model, lora)` for 4-bit bases; it enables input gradients needed by gradient checkpointing and casts a few layers for stability.

Merging for deployment (illustrative):

```python
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM

base = AutoModelForCausalLM.from_pretrained(base_id, torch_dtype=torch.bfloat16)
merged = PeftModel.from_pretrained(base, "adapters/support-v3").merge_and_unload()
merged.save_pretrained("models/support-v3-merged")
```

Serving several adapters on one base with vLLM; the client selects an adapter through the `model` field of an OpenAI-compatible request:

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct \
  --enable-lora \
  --lora-modules support-v3=/adapters/support-v3 billing-v1=/adapters/billing-v1 \
  --max-loras 4 \
  --max-lora-rank 16

curl -s localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "support-v3", "messages": [{"role": "user", "content": "Where is order 8812?"}]}'
```

Route to adapters from your gateway the same way you route between providers; see [multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}) and [serving engines]({{ '/llm/serving-engines/' | relative_url }}).

## Pitfalls

| What you see | Cause | Fix |
|---|---|---|
| Fine-tune barely changes behavior | Only q/v targeted, rank or LR too low, or adapter not loaded at inference | Target all linear layers; raise LR; confirm outputs differ from base |
| Quality collapses at serving | Adapter applied to a different base revision or to base instead of instruct | Pin the exact base model ID and revision with the adapter |
| Results changed after changing `r` | Effective scale `alpha / r` changed | Change `alpha` with `r`, or use rsLoRA |
| "Tensor does not require grad" with checkpointing | Frozen embeddings block gradient flow | `prepare_model_for_kbit_training` or `enable_input_require_grads()` |
| Merged model worse than adapter in eval | Merged into a 4-bit base or re-quantized carelessly | Merge into bf16, quantize after, re-evaluate |
| New special tokens produce garbage | Embedding rows for new tokens never trained | Add `embed_tokens` and `lm_head` to `modules_to_save` |
| vLLM refuses to load an adapter | Adapter rank above `--max-lora-rank` or unsupported target modules | Match server limits to the adapter config |

## Rules of thumb

- LoRA first, on all linear layers, `r = 16`, `alpha = 32`, LR 2e-4, 1 to 3 epochs.
- QLoRA when the bf16 base does not fit; bf16 LoRA when it does, for speed.
- Version the adapter with the exact base model ID and revision, dataset hash and config.
- Merge for one high-volume fine-tune; multi-LoRA for many tasks or tenants.
- Merge in bf16, quantize after, and evaluate the artifact you actually deploy.
- Move to full fine-tuning only when LoRA plateaus on a large, genuinely new skill.

## Interview questions

**Explain LoRA.** It freezes `W0` and learns `delta_W = (alpha / r) * B A` with `A` of shape `r x d_in` and `B` of shape `d_out x r`, so a layer computes `W0 x + (alpha / r) * B A x`. `B` starts at zero so training starts from the pretrained model. Trainable parameters drop from `d_in * d_out` to `r * (d_in + d_out)`, and the update can be merged into the weight for zero inference overhead.

**How many trainable parameters does LoRA add to a 4096 x 4096 matrix at rank 16?** `16 * (4096 + 4096) = 131,072`, versus about 16.8M for the full matrix, under 1%. For an 8B Llama-style model with all linear layers at rank 16, about 42M.

**What memory does LoRA save, and what does it not save?** It removes gradients and optimizer states for frozen weights, about 14 of 16 bytes per parameter. It still needs the base weights in memory and roughly the same activation memory, because gradients flow back through the frozen layers.

**What is QLoRA?** LoRA on top of a 4-bit NF4-quantized frozen base, with double quantization of the block scales and paged optimizers to survive memory spikes. Weights are dequantized to bf16 per matmul. It fits much larger models on one GPU at some training-speed cost.

**Merge the adapter or serve it separately?** Merge for one or two fine-tunes at high volume: no overhead and any engine can serve it. Serve unmerged with multi-LoRA when you have many tasks or tenants on one base: one GPU footprint, small per-request overhead, and per-request adapter selection.

**When does LoRA fall short of full fine-tuning?** When learning large amounts of genuinely new capability, such as continued pretraining on a big code or math corpus. Full fine-tuning learns higher-rank updates there. LoRA forgets less, so it remains the better default for behavioral fine-tunes.

**What does `lora_alpha` do?** It scales the update by `alpha / r`, setting the effective magnitude of the adapter's contribution. Keeping `alpha / r` constant (or using rsLoRA's `alpha / sqrt(r)`) avoids retuning the learning rate when you change rank.

## Further reading

- Hu et al., "LoRA: Low-Rank Adaptation of Large Language Models" (2021).
- Dettmers et al., "QLoRA: Efficient Finetuning of Quantized LLMs" (2023).
- Liu et al., "DoRA: Weight-Decomposed Low-Rank Adaptation" (2024).
- Biderman et al., "LoRA Learns Less and Forgets Less" (2024).
- Sheng et al., "S-LoRA: Serving Thousands of Concurrent LoRA Adapters" (2023).
- Hugging Face PEFT documentation (LoraConfig, merging, adapter methods).
