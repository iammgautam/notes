---
title: Quantization
section: llm
order: 43
phase: "Phase 7 · Inference and serving"
summary: Number formats from FP32 to FP4, weight-only vs W8A8, GPTQ, AWQ, SmoothQuant, NF4 and GGUF, memory math, and how to tell when quantization hurts.
---

Quantization stores a model's numbers in fewer bits: 8 or 4 instead of 16. Because decode is memory-bandwidth-bound (see [How inference works]({{ '/llm/inference-basics/' | relative_url }})), fewer bytes per weight means faster tokens, and smaller weights leave more GPU memory for the KV cache, which means more concurrent users. It is often the difference between needing two GPUs or one. The cost is quality loss that ranges from unmeasurable to severe depending on format, method, model and, above all, task. The job is picking a format your hardware runs fast and proving on your eval that the loss is acceptable.

## Core ideas

### Number formats

| Format | Layout (sign/exponent/mantissa) | Range and resolution | Typical use |
|---|---|---|---|
| FP32 | 1/8/23 | ~1e±38, ~7 significant digits | Optimizer state, master weights, accumulators |
| BF16 | 1/8/7 | FP32's range, ~2–3 digits | Default dtype for training and serving |
| FP16 | 1/5/10 | Max 65,504; finer but narrower | Older GPUs; activations can overflow |
| FP8 E4M3 | 1/4/3 | Max ±448 | Inference weights and activations |
| FP8 E5M2 | 1/5/2 | Max ±57,344, coarser | Gradients; sometimes KV cache |
| INT8 | Integer plus scale | 256 levels | W8A8, LLM.int8(), weight-only |
| INT4 | Integer plus group scale | 16 levels | GPTQ, AWQ weight-only |
| NF4 | Levels at normal-distribution quantiles | 16 levels | bitsandbytes, QLoRA |
| FP4 (E2M1) + block scale | 1/2/1, shared scale per 16–32 values | 16 levels | MXFP4, NVFP4 on Blackwell-class GPUs |

Hardware decides what is fast. INT8 tensor cores have existed since Turing and Ampere; FP8 tensor cores arrived with Ada (L4, L40S) and Hopper (H100, H200); FP4 with Blackwell (as of 2025–2026). A format without native support can still save memory, but it runs through kernels that upcast to 16-bit before the multiply, so it may be no faster, or slower, than BF16.

### The mapping and why outliers hurt

Integer quantization maps a range of real numbers onto evenly spaced levels with a scale (and optionally a zero point):

```text
q = clamp(round(x / scale), -2^(b-1), 2^(b-1) - 1)   store q in b bits
x ≈ q × scale                                        dequantize when used

symmetric: scale = max(|x|) / (2^(b-1) - 1)      e.g. max|x| / 127 for int8
```

Rounding error is at most half a step, and the step is set by the largest value in the group. One outlier forces a large scale and crushes the resolution of every small value sharing it. LLMs have outliers: a few activation channels run 10–100x larger than the rest, and some weights are similarly extreme. **Granularity** is the first defense:

| Granularity | One scale per | Notes |
|---|---|---|
| Per-tensor | Whole matrix | Fine for FP8 weights; poor for int4 |
| Per-channel | Output row or column | Standard for int8 weights |
| Per-token | Activation row | Standard for dynamic activation scales |
| Per-group / block | 32–128 consecutive weights | Required for 4-bit; costs ~0.1–0.5 extra bits per weight |

### What gets quantized

| Scheme | Weights | Activations | Speeds up | Best for |
|---|---|---|---|---|
| Weight-only (W8A16, W4A16) | 8 or 4 bit | 16-bit | Memory-bound decode | Low batch, latency-sensitive, fitting big models |
| W8A8 (INT8 or FP8) | 8 bit | 8 bit | Decode and compute-bound prefill | High-throughput serving on GPUs with 8-bit tensor cores |
| W4A8, FP4 | 4 bit | 8 or 4 bit | Both, on supporting hardware | Newest kernels and GPUs |
| KV cache (FP8, INT8) | – | KV only | Long-context decode, concurrency | Long prompts, many users |

Weight-only kernels (Marlin is a well-known family for 4-bit on NVIDIA) load packed low-bit weights, dequantize them in registers and multiply in 16-bit. That cuts bytes read, which is exactly the bottleneck at small batch. At large batch decode drifts toward compute-bound, the dequantization work stops being free, and W8A8 with native 8-bit matmuls wins. FP8 W8A8 has become the near-default on Hopper-class hardware because its exponent bits tolerate outliers better than INT8 and quality loss is usually negligible.

## Techniques

### Post-training methods

- **Round-to-nearest (RTN).** Quantize each group independently. Fine at 8 bits; noticeably lossy at 4 bits without the methods below.
- **LLM.int8()** (bitsandbytes). Finds activation outlier dimensions at runtime, computes them in FP16 and everything else in INT8. Near-lossless 8-bit with no calibration, but often slower than FP16; used mainly to save memory in transformers.
- **SmoothQuant.** Activations are hard to quantize; weights are easy. It divides each activation channel by a factor `s_j` and multiplies the matching weight row by `s_j`, which leaves the product unchanged and moves the outliers into the weights. With `s_j = max|X_j|^α / max|W_j|^(1−α)` and `α` around 0.5, both become INT8-friendly. Enables W8A8 INT8.
- **GPTQ.** Quantizes a layer's weights one column at a time and, using second-order information from a small calibration set (the Hessian `H = 2XX^T` of the layer's inputs), adjusts the not-yet-quantized columns to cancel the error just introduced. Minimizes each layer's output error; a large model quantizes in hours on one GPU. The "act-order" option quantizes columns in order of importance.
- **AWQ** (activation-aware weight quantization). About 1% of weight channels matter disproportionately, identified by large activation magnitudes, not large weights. AWQ searches a per-channel scale that enlarges those channels before quantization (folding the inverse into the previous operation) so they keep precision. No backpropagation or reconstruction, robust across domains, popular for instruction-tuned and multimodal models.
- **NF4** (bitsandbytes, from QLoRA). Sixteen levels placed at quantiles of a normal distribution, which fits how weights are distributed, with block size 64. Double quantization quantizes the block scales too, bringing overhead from about 0.5 to about 0.13 bits per weight. The standard for QLoRA fine-tuning and quick memory savings; serving engines' GPTQ, AWQ or FP8 kernels are faster for production inference.
- **FP8.** Per-tensor or per-channel weight scales and dynamic per-token activation scales (or static scales from calibration). Usually near-lossless; tools such as llm-compressor and NVIDIA's Model Optimizer produce FP8 checkpoints, and vLLM can quantize weights to FP8 at load time.
- **Rotation and codebook methods** (QuaRot, SpinQuant, QuIP#, AQLM). Rotate to spread outliers, or encode weight groups with learned codebooks, to reach 2–3 bits. Check engine support first.

**Quantization-aware training** (QAT) simulates quantization during training so the model learns to tolerate it. It gives the best low-bit quality; when a publisher ships official QAT checkpoints, prefer them over your own post-training quantization.

| Method | Bits | Calibration | Runs in |
|---|---|---|---|
| GPTQ | W4 (also W3, W8) | ~100s of samples | vLLM, SGLang, TGI, transformers |
| AWQ | W4 | Small set | vLLM, SGLang, TGI, transformers |
| SmoothQuant | W8A8 INT8 | Small set | TensorRT-LLM, vLLM (W8A8 checkpoints) |
| FP8 | W8A8 FP8 | Optional | vLLM, SGLang, TensorRT-LLM on FP8 GPUs |
| NF4 | W4 | None | transformers + bitsandbytes |
| GGUF k-/i-quants | 2–8 | Optional importance matrix | llama.cpp, Ollama |

### GGUF and llama.cpp quant types

GGUF is llama.cpp's single-file format: tensors plus metadata (tokenizer, chat template, architecture). It is what llama.cpp, Ollama and LM Studio load, and it targets CPUs, Apple silicon and consumer GPUs.

- **Legacy types** (`Q4_0`, `Q5_0`, `Q8_0`): blocks of 32 weights with one scale (and a minimum in the `_1` variants).
- **K-quants** (`Q2_K` to `Q6_K`): 256-weight super-blocks with quantized sub-block scales. The `_S`, `_M`, `_L` suffixes keep sensitive tensors (for example parts of attention and the FFN down projection) at higher precision.
- **I-quants** (`IQ2_XXS` through `IQ4_XS`): codebook-style formats that hold up better at 2–3 bits.
- **Importance matrix** (imatrix): per-weight importance computed from calibration text, used to weight rounding error. Strongly recommended below 4 bits.

| Type | Approx. bits/weight | Typical use |
|---|---|---|
| `Q8_0` | ~8.5 | Near-lossless reference |
| `Q6_K` | ~6.6 | Very close to 16-bit |
| `Q5_K_M` | ~5.7 | High quality, moderate size |
| `Q4_K_M` | ~4.8 | Common default for local use |
| `Q3_K_M` | ~3.9 | Memory-constrained; visible loss |
| `IQ2_*`, `IQ3_*` | ~2–3.5 | Extreme compression; needs imatrix |

## In practice

### Rounding error and group size

```python
import random


def quantize(xs: list[float], bits: int) -> list[float]:
    """Symmetric round-to-nearest with one scale for the whole list; returns dequantized values."""
    qmax = 2 ** (bits - 1) - 1
    scale = max(abs(x) for x in xs) / qmax or 1.0
    return [max(-qmax - 1, min(qmax, round(x / scale))) * scale for x in xs]


def quantize_grouped(xs: list[float], bits: int, group: int) -> list[float]:
    out = []
    for i in range(0, len(xs), group):
        out += quantize(xs[i:i + group], bits)
    return out


def rmse(a: list[float], b: list[float]) -> float:
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5


if __name__ == "__main__":
    rng = random.Random(0)
    w = [rng.gauss(0, 0.02) for _ in range(4096)]    # one weight row, typical scale
    w[100] = 1.0                                     # a single outlier

    err = {
        "int8 per-tensor": rmse(w, quantize(w, 8)),
        "int4 per-tensor": rmse(w, quantize(w, 4)),
        "int4 group 128": rmse(w, quantize_grouped(w, 4, 128)),
        "int4 group 32": rmse(w, quantize_grouped(w, 4, 32)),
    }
    for name, e in err.items():
        print(f"{name:16} RMSE {e:.4f}")
    zeroed = sum(1 for x in quantize(w, 4) if x == 0) / len(w)
    print(f"int4 per-tensor rounds {100 * zeroed:.1f}% of weights to zero")

    assert zeroed > 0.95                    # the outlier's scale wipes out the row
    assert err["int8 per-tensor"] < err["int4 per-tensor"]
    assert err["int4 group 128"] < err["int4 per-tensor"] / 3
    assert err["int4 group 32"] < err["int4 group 128"]
```

With one outlier, per-tensor int4 rounds almost every weight in the row to zero. Groups of 128 confine the damage to one group; smaller groups do better still, at the cost of more scale storage.

### Memory math

```text
weight bytes ≈ quantized params × bpw / 8 + unquantized params × 2
bpw (bits per weight) = bits + scale_bits / group_size
int4, group 128, FP16 scales: 4 + 16/128 = 4.125 bpw
```

GPTQ and AWQ checkpoints usually quantize only the linear layers inside transformer blocks; the embedding table and output head stay in 16-bit. For a small model with a large vocabulary that is a big share of parameters, so it shrinks less than you expect.

```python
def bits_per_weight(bits: float, group: int | None = None, scale_bits: int = 16) -> float:
    return bits + (scale_bits / group if group else 0.0)


def weights_gb(params_b: float, bpw: float, keep_16bit_b: float = 0.0) -> float:
    """Billions of params -> GB; keep_16bit_b = embeddings and output head left in BF16."""
    return (params_b - keep_16bit_b) * bpw / 8 + keep_16bit_b * 2


if __name__ == "__main__":
    formats = {"BF16": bits_per_weight(16), "FP8 per-channel": bits_per_weight(8),
               "INT4 g128": bits_per_weight(4, group=128), "INT3 g128": bits_per_weight(3, group=128)}
    # 70B-class (128k vocab, 8192 hidden): ~2.1B embedding + head params.
    # 8B-class (128k vocab, 4096 hidden, untied): ~1.05B.
    for name, bpw in formats.items():
        keep = (2.1, 1.05) if bpw < 16 else (0.0, 0.0)
        print(f"{name:16} {bpw:6.3f} bpw  70B: {weights_gb(70.6, bpw, keep[0]):6.1f} GB  "
              f"8B: {weights_gb(8.0, bpw, keep[1]):5.1f} GB")

    usable = 80 * 0.9 - 3                       # one 80 GB GPU minus runtime overhead
    kv_per_token = 2 * 80 * 8 * 128 * 2         # 70B GQA, BF16 KV cache
    for name in ("BF16", "FP8 per-channel", "INT4 g128"):
        w = weights_gb(70.6, formats[name], 2.1 if name != "BF16" else 0.0)
        tokens = max(0.0, (usable - w) * 1e9 // kv_per_token)
        print(f"70B {name:16} {w:6.1f} GB weights -> {tokens:>9,.0f} KV tokens free")

    assert round(weights_gb(70, 16)) == 140
    int4_70b = weights_gb(70.6, formats["INT4 g128"], 2.1)
    assert 38 < int4_70b < 41
    assert weights_gb(70.6, formats["FP8 per-channel"], 2.1) > usable   # FP8 70B needs 2 GPUs
    ratio_8b = weights_gb(8.0, 4.125, 1.05) / weights_gb(8.0, 16)
    ratio_70b = int4_70b / weights_gb(70.6, 16)
    print(f"INT4 size vs BF16: 8B {100 * ratio_8b:.0f}%, 70B {100 * ratio_70b:.0f}%")
    assert ratio_8b > ratio_70b                 # small models shrink less
```

A 70B model needs about 141 GB in BF16 (two or more 80 GB GPUs), about 73 GB in FP8 (still too tight for one 80 GB GPU once the KV cache is counted), and about 40 GB at INT4, leaving room for roughly 90k tokens of KV cache on one GPU. An 8B model at INT4 is about 36% of its BF16 size, not 25%, because of its 16-bit embeddings and head.

### Loading and serving

Illustrative; flags and class names are stable but check your versions.

```bash
# vLLM: pre-quantized AWQ/GPTQ/FP8 checkpoints are detected from the model config
vllm serve <org>/<model>-AWQ --max-model-len 16384
# Dynamic FP8 weight quantization at load time (best on FP8-capable GPUs)
vllm serve <model-id> --quantization fp8
# FP8 KV cache, independent of weight format
vllm serve <model-id> --kv-cache-dtype fp8
```

```python
import torch
from transformers import AutoModelForCausalLM, BitsAndBytesConfig

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)
model = AutoModelForCausalLM.from_pretrained("<model-id>", quantization_config=bnb, device_map="auto")
```

```bash
# llama.cpp: convert, compute an importance matrix, quantize (illustrative)
python convert_hf_to_gguf.py ./my-model --outtype f16 --outfile model-f16.gguf
./llama-imatrix -m model-f16.gguf -f calibration.txt -o imatrix.dat
./llama-quantize --imatrix imatrix.dat model-f16.gguf model-Q4_K_M.gguf Q4_K_M
```

### Evaluating quality

Aggregate benchmarks hide what matters. Evaluate the exact model, format, kernel and engine you will serve:

1. **Perplexity** on held-out, in-domain text. A quick sanity check; only comparable with the same tokenizer, and a small change can hide large task regressions.
2. **KL divergence** between the baseline's and the quantized model's next-token distributions on your prompts, plus top-1 token agreement. More sensitive than perplexity; llama.cpp's perplexity tool can compute it against saved baseline logits.
3. **Flips.** The share of eval items whose correctness changes between baseline and quantized model. Accuracy can stay flat while many individual answers flip in both directions.
4. **Task evals** from your [evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) suite: golden set, JSON validity, tool-call accuracy, long-context retrieval, each language you serve, code pass rates, math.
5. **Performance** at target concurrency: tokens/s, TTFT, TPOT, and maximum concurrency before preemption. A format with poor kernels on your GPU can be slower than BF16.

### When quantization hurts

- **Small models.** A 1–3B model loses far more at 4 bits than a 70B model. A larger model at 4 bits often beats a smaller one at 16 bits for the same memory.
- **3 bits and below.** Degradation becomes visible on most tasks, even with imatrix or codebook methods.
- **Reasoning, math and code.** Small per-token errors compound over long generations; reasoning models with long thinking traces are especially sensitive.
- **Long context.** Attention over many tokens accumulates KV quantization error; retrieval at depth degrades first.
- **Non-English and rare domains.** Calibration sets are usually English web text; underrepresented inputs lose more. Calibrate on samples resembling your traffic.
- **Structured output and tool calling.** A slightly perturbed distribution breaks strict formats that chat-quality spot checks never notice.
- **Heavily trained models.** Research such as "Scaling Laws for Precision" (Kumar et al., 2024) reports that models trained on more data can degrade more under post-training quantization.
- **Adapters on a different base.** A LoRA trained on a BF16 base behaves slightly differently on a quantized base (see [LoRA and PEFT]({{ '/llm/lora-and-peft/' | relative_url }})). Evaluate the exact combination.

## Pitfalls

- **Assuming 4-bit is faster.** Weight-only 4-bit speeds memory-bound decode; at high batch, or without good kernels for your GPU, FP8 or even BF16 can be faster. Benchmark at real concurrency.
- **Choosing a format the hardware cannot accelerate.** FP8 on an A100, for example, saves memory but runs through upcasting kernels.
- **Calibration mismatch.** Generic calibration text for a code or multilingual workload. Symptom: in-domain evals drop more than public benchmarks.
- **Quantizing only the weights when KV is the bottleneck.** Concurrency barely moves at long context. Add FP8 KV cache.
- **Untracked quantized artifacts.** Version quantized weights like any model artifact, so what you serve is what you evaluated.

## Checklist

- Compute memory: weights plus KV for target concurrency plus overhead.
- Start with FP8 (weights, activations, KV) on hardware that supports it; go to 4-bit weights when memory is the binding constraint.
- Prefer official or widely used GPTQ, AWQ or QAT checkpoints; calibrate on in-domain data when you quantize yourself.
- Evaluate with KL divergence, flips and task evals, including structured output and tool calls.
- Benchmark throughput and latency at real concurrency on the target GPU and engine.

## Interview questions

**Why does weight-only quantization speed up decode but not prefill?**
Decode is memory-bound: fewer bytes per weight means less data read per token. Prefill is compute-bound, and weight-only kernels still multiply in 16-bit after dequantizing, so it gains nothing and can lose a little. Speeding prefill needs 8-bit or 4-bit activations too.

**Why are activations harder to quantize than weights, and how does SmoothQuant help?**
Activations have a few channels with values 10–100x larger than the rest, which force a coarse scale. SmoothQuant divides those channels by a per-channel factor and multiplies the corresponding weights by it, an exact rewrite that moves the difficulty into the weights, which tolerate it.

**GPTQ vs AWQ?**
Both are post-training 4-bit weight methods with small calibration sets. GPTQ quantizes column by column and uses second-order information to compensate the remaining weights for each rounding error. AWQ identifies salient channels from activation magnitudes and scales them up before quantization so they keep precision.

**What does group size control?**
How many weights share a scale. Smaller groups isolate outliers and reduce error but add storage, for example 16 scale bits per 128 weights adds 0.125 bits per weight.

**Can you serve a 70B model on one 80 GB GPU?**
At 4-bit, yes: about 40 GB of weights leaves room for tens of thousands of KV tokens. At FP8, the 70 GB of weights leaves almost nothing for KV, so use two GPUs. At BF16, at least two, realistically four for useful concurrency.

**A 4-bit model matches the baseline on a public benchmark but users complain. Why?**
Benchmarks average over easy items and do not test your formats, languages or long contexts. Check flips, KL divergence on your prompts, and task-specific metrics such as JSON validity and tool-call accuracy.

**FP8 or INT8 for W8A8?**
On hardware with FP8 tensor cores, FP8 is usually preferred: its exponent bits handle outliers with less calibration. INT8 W8A8 needs outlier handling such as SmoothQuant but runs on older GPUs.

## Further reading

- Frantar et al., 2022, "GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers".
- Lin et al., 2023, "AWQ: Activation-aware Weight Quantization for LLM Compression and Acceleration".
- Xiao et al., 2022, "SmoothQuant: Accurate and Efficient Post-Training Quantization for Large Language Models".
- Dettmers et al., 2022, "LLM.int8(): 8-bit Matrix Multiplication for Transformers at Scale".
- Dettmers et al., 2023, "QLoRA: Efficient Finetuning of Quantized LLMs".
- Micikevicius et al., 2022, "FP8 Formats for Deep Learning".
