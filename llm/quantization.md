---
title: Quantization
section: llm
order: 14
summary: How storing weights, activations and KV cache in fewer bits shrinks memory and speeds decode, what GPTQ and AWQ do, and how to check quality.
---

Quantization stores model numbers in fewer bits: 8 or 4 instead of 16. Because decode is memory-bandwidth-bound, fewer bytes per weight means faster tokens, and smaller weights leave more GPU memory for the KV cache, which means more concurrent users. It is often the difference between a model needing two GPUs or one, or fitting on a single consumer card. The cost is some quality loss, which ranges from unmeasurable to severe depending on the method, bit width and task.

## How it works

### The basic mapping

Quantization maps a range of real values onto a small set of integers with a scale (and sometimes a zero point):

```text
q = round(x / scale) + zero_point          (store q as int8 or int4)
x ≈ (q − zero_point) × scale               (dequantize at compute time)

symmetric int8: scale = max(|x|) / 127
```

The error comes from rounding and from outliers: one large value forces a large scale, which crushes the resolution available for all the small values. **Granularity** controls this:

| Granularity | Scale shared by | Accuracy | Overhead |
|---|---|---|---|
| Per-tensor | Whole weight matrix | Lowest | Negligible |
| Per-channel | Each output row | Good for int8 | Small |
| Per-group | Every 32–128 weights | Needed for int4 | ~0.1–0.5 bits per weight |

### What gets quantized

| Scheme | Weights | Activations | Speeds up | Typical use |
|---|---|---|---|---|
| Weight-only (W8A16, W4A16) | 8 or 4 bit | 16 bit | Memory-bound decode | Low-batch, latency-sensitive serving; fitting large models |
| Weight + activation (W8A8 int8 or FP8) | 8 bit | 8 bit | Compute-bound prefill and large batches too | High-throughput serving on GPUs with 8-bit tensor cores |
| KV cache (FP8 or int8) | – | KV in 8 bit | Halves KV memory | Long context, high concurrency |
| 4-bit floating formats (FP4 variants) | 4 bit | 4 or 8 bit | Both, on hardware that supports them | Newest GPU generations |

Weight-only quantization dequantizes weights to 16-bit inside the matmul kernel. It cuts bytes read per decode step, which is exactly the bottleneck at small batch sizes. At large batch sizes, decode becomes more compute-bound and weight-only gains shrink; there, W8A8 or FP8 with native 8-bit matmuls wins.

Activations are harder to quantize than weights because they contain large outlier channels. Methods like SmoothQuant migrate the difficulty from activations to weights by rescaling channels. FP8 (E4M3) handles outliers better than int8 thanks to its exponent bits, and on GPUs with FP8 tensor cores it is now the default "almost free" quantization for serving.

### Post-training methods: GPTQ and AWQ

Naively rounding to 4 bits ("round-to-nearest") degrades quality noticeably. Two widely used post-training methods do better with a small calibration dataset (a few hundred samples) and no retraining:

- **GPTQ** quantizes weights column by column and uses approximate second-order information (from a Hessian of the layer's inputs) to adjust the not-yet-quantized weights, compensating for the error just introduced. Layer-wise error minimization.
- **AWQ** (activation-aware weight quantization) observes that a small fraction of weight channels matter most, identified by large activation magnitudes. It scales those channels up before quantization so they keep more precision, folding the inverse scale into the preceding operation. Simpler and robust across domains.

Both usually produce 4-bit, group-size-128 weight-only models that run with fast kernels in serving engines. For CPU and consumer hardware, llama.cpp's GGUF format offers a family of mixed-precision "k-quant" types (for example 4- to 6-bit variants) that quantize more important tensors at higher precision.

**Quantization-aware training** (simulating quantization during training or fine-tuning) gives the best low-bit quality but needs training infrastructure. Some model publishers ship official quantized checkpoints produced this way; prefer those when available.

### Memory math

```text
weights bytes ≈ params × bits / 8   (+ group scales, + unquantized layers)

70B params
  16-bit:        140 GB    → multiple 80 GB GPUs
  8-bit:          70 GB    → one 80 GB GPU, little room for KV cache
  4-bit (g128):  ~38 GB    → one 80 GB GPU with ~35 GB for KV cache

8B params
  16-bit:         16 GB
  8-bit:           8 GB
  4-bit:          ~5 GB    → fits a consumer GPU or a laptop
```

Embeddings and the output head are often kept at higher precision, so real files are a bit larger than the formula.

### Quality trade-offs

Rough expectations for modern models, which you must verify for your task:

| Format | Typical quality impact |
|---|---|
| FP8 weights + activations | Usually indistinguishable on benchmarks |
| int8 weight-only | Usually negligible |
| 4-bit GPTQ/AWQ (g128) | Small drop on general benchmarks; larger on math, code, long context and non-English |
| 3-bit and below | Noticeable degradation; mostly for fitting very large models on small hardware |

Larger models tolerate quantization better than small ones. A 4-bit large model often beats a 16-bit model half its size at the same memory footprint. Aggregate benchmarks hide task-specific regressions: structured output, tool calling and multi-step reasoning are sensitive and must be in your eval.

## In practice

### Serving a quantized model in vLLM

```bash
# Pre-quantized AWQ or GPTQ checkpoint: format is read from the model config
vllm serve <org>/<model>-AWQ --max-model-len 16384

# Dynamic FP8 weight quantization at load time (GPU with FP8 support)
vllm serve <model-id> --quantization fp8

# FP8 KV cache to roughly double KV capacity
vllm serve <model-id> --kv-cache-dtype fp8
```

Check the startup log for the selected kernel and the KV cache capacity. A format without an optimized kernel on your GPU can run slower than 16-bit.

### Evaluating before you ship

```text
1. Baseline: 16-bit model on your task eval (golden set, structured
   output validity rate, tool-call accuracy, judge scores)
2. Candidate: quantized model, same prompts, same sampling settings
3. Compare per category, not just the average
4. Load-test both: tokens/s, TTFT, TPOT at target concurrency,
   and max concurrency before preemption
5. Ship if quality delta is within tolerance and capacity gain is real
```

Perplexity on a generic corpus is a quick sanity check, not a decision metric.

> **Rule of thumb:** Start with FP8 (or int8) on hardware that supports it; it is almost always worth it. Go to 4-bit when memory is the binding constraint, and only after a task-level eval.

## Pitfalls

- **Assuming 4-bit is faster.** Weight-only 4-bit speeds up memory-bound decode; at high batch sizes, or with poor kernels, it can be slower than FP8. Fix: benchmark at your real concurrency.
- **Calibration data mismatch.** GPTQ/AWQ calibrated on generic web text may underperform on code or another language. Fix: calibrate with samples resembling your traffic.
- **Hidden regressions in structured output.** JSON validity or tool-call accuracy drops while chat quality looks fine. Fix: include these in the eval.
- **Forgetting the KV cache.** Weight quantization alone may not raise concurrency enough for long contexts. Fix: also consider FP8 KV cache.
- **Stacking quantized fine-tunes.** LoRA adapters trained on a 16-bit base behave slightly differently on a quantized base. Fix: evaluate the exact combination you serve.

## Checklist

- Compute memory: weights at target bit width plus KV cache for target concurrency.
- Prefer FP8/int8 first; 4-bit when memory-bound; avoid below 4-bit for production quality.
- Use official or well-known GPTQ/AWQ checkpoints, or calibrate on in-domain data.
- Evaluate on your tasks, including structured output and tool calling.
- Benchmark throughput and latency at real concurrency, per kernel and GPU.

## Interview angle

- **Why does quantization speed up decode?** Decode is memory-bandwidth-bound; fewer bytes per weight means less data read per token.
- **Weight-only vs weight-and-activation quantization?** Weight-only cuts memory traffic and helps small batches; quantizing activations too enables 8-bit matmuls, which helps compute-bound prefill and large batches.
- **GPTQ vs AWQ?** GPTQ minimizes layer-wise error with second-order updates; AWQ protects salient channels identified by activation magnitudes via scaling. Both are post-training, need small calibration sets, and target 4-bit weights.
