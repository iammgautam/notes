---
title: How inference works
section: llm
order: 12
summary: What happens on the GPU when a model generates text, why decode is memory-bound, how to size the KV cache, and what TTFT and TPOT measure.
---

Calling an LLM API hides a specific execution model: a compute-heavy pass over your prompt, then a long sequence of memory-bound steps that each produce one token. Once you see that split, most serving behavior makes sense: why long prompts hurt time-to-first-token, why output tokens cost more than input tokens, why batching is the main throughput lever, and why GPU memory, not compute, usually caps how many users a server can handle.

## How it works

### Two phases: prefill and decode

```text
prompt: "Summarize this ticket: ..." (2,000 tokens)

PREFILL   all 2,000 tokens processed in parallel in one forward pass
          → writes K,V for every prompt token into the KV cache
          → produces the first output token
          cost: compute-bound, grows with prompt length

DECODE    step 1: 1 new token attends to 2,000 cached K,V → token 2
          step 2: 1 new token attends to 2,001 cached K,V → token 3
          ...
          one forward pass per output token, strictly sequential
          cost: memory-bandwidth-bound, grows with output length
```

**Prefill** is a big matrix-matrix multiply over all prompt tokens. GPUs are excellent at this; it is compute-bound and fast per token (thousands to tens of thousands of tokens per second per GPU, model-dependent).

**Decode** generates one token per forward pass, because each token depends on the previous one. Each step must read every model weight from GPU memory (HBM) to do a small amount of math for one token per sequence.

### Why decode is memory-bound

Rough arithmetic for one decode step of a dense model with `P` parameters in 16-bit:

- Bytes read: `2P` (every weight once).
- FLOPs: about `2P` per token in the batch (one multiply-add per weight).
- Arithmetic intensity at batch size 1: about 1 FLOP per byte.

A modern datacenter GPU delivers hundreds of TFLOPs of 16-bit compute and a few TB/s of memory bandwidth, so it needs on the order of 100+ FLOPs per byte to be compute-bound. At batch size 1, decode uses roughly 1% of the GPU's compute. The speed limit is bandwidth:

```text
8B-param model, 16-bit weights = ~16 GB read per step
HBM bandwidth ~3 TB/s       → ~5 ms per step → ~200 tokens/s ceiling
70B-param model             = ~140 GB (needs multiple GPUs)
```

**Batching** is the fix: one read of the weights serves every sequence in the batch, so throughput grows almost linearly with batch size until compute or memory runs out. This is why serving systems care so much about batching (see [continuous batching]({{ '/llm/batching-and-paged-attention/' | relative_url }})) and why quantization (fewer bytes per weight) speeds up decode directly.

### The KV cache

Attention for a new token needs the keys and values of every previous token. Recomputing them each step would make generation quadratic, so the server caches them per sequence: the KV cache.

```text
KV bytes/token = 2 (K,V) × layers × kv_heads × head_dim × bytes_per_value
```

Worked examples at 16-bit (2 bytes):

| Model shape | Per token | 8K-token sequence |
|---|---|---|
| ~8B: 32 layers, 8 KV heads, head_dim 128 | 2·32·8·128·2 = 128 KiB | ~1 GiB |
| ~70B: 80 layers, 8 KV heads, head_dim 128 | 2·80·8·128·2 = 320 KiB | ~2.5 GiB |
| Same 70B without GQA (64 KV heads) | 2.5 MiB | ~20 GiB |

Grouped-query attention (GQA) shares each KV head across several query heads, cutting KV memory several-fold; multi-query attention and latent-attention variants go further. This is why modern architectures use them.

Capacity math for a single GPU with 80 GB serving an 8B model in 16-bit:

```text
80 GB − 16 GB weights − ~4 GB activations/overhead ≈ 60 GB for KV cache
60 GB / 128 KiB per token ≈ 450k tokens of KV cache
= ~55 concurrent sequences at 8K tokens, or ~220 at 2K tokens
```

KV cache capacity, not compute, typically sets maximum concurrency. Long contexts eat it fast. KV cache quantization (8-bit) roughly doubles capacity.

### Latency metrics

| Metric | Meaning | Driven by |
|---|---|---|
| TTFT (time to first token) | Request sent → first token received | Queueing + prefill (prompt length) + network |
| TPOT / ITL (time per output token, inter-token latency) | Average gap between tokens after the first | Decode step time, which grows with batch size and context length |
| E2E latency | Total request time | `TTFT + TPOT × (output_tokens − 1)` |
| Throughput | Output tokens/s across all requests | Batch size, hardware, model size |

Example: TTFT 400 ms, TPOT 25 ms, 500 output tokens → `0.4 + 0.025 × 499 ≈ 12.9 s`. Output length dominates. Humans read at roughly 5–10 tokens per second, so streaming at 30+ tokens/s feels fast even when E2E is long.

The core serving trade-off: larger batches raise throughput (cheaper per token) but raise TPOT for everyone, and admitting new prefills into a running batch causes TPOT spikes. Serving systems tune for a target like "p95 TTFT under 1 s and TPOT under 50 ms" at maximum throughput.

### Sampling and the output

Each decode step produces logits over the vocabulary; temperature, top-p and top-k pick a token (see [LLM basics]({{ '/llm/llm-basics/' | relative_url }})). Structured output constrains this step with a token mask. Stop conditions (end-of-sequence token, stop strings, `max_tokens`) end the sequence and free its KV cache.

## In practice

### What this means for an API consumer

- **Output tokens are the expensive part of latency.** Cutting output from 800 to 200 tokens saves far more time than cutting the prompt from 4,000 to 3,000 tokens.
- **Long prompts hurt TTFT,** roughly linearly at moderate lengths and worse at very long ones (attention cost grows with length).
- **Prefix caching** lets a server skip prefill for a repeated prompt prefix, cutting TTFT and cost; that is why providers discount cached input tokens.
- **Output tokens are priced higher** than input tokens because decode uses the GPU far less efficiently.

### Measuring a self-hosted endpoint

```python
import time

async def measure(client, prompt: str, max_tokens: int = 256) -> dict:
    t0 = time.perf_counter()
    first = None
    n = 0
    async for event in client.stream(prompt=prompt, max_tokens=max_tokens):
        if event.text:
            n += 1                     # approximate: one chunk ≈ one token
            if first is None:
                first = time.perf_counter()
    end = time.perf_counter()
    return {
        "ttft_ms": (first - t0) * 1000,
        "tpot_ms": (end - first) / max(n - 1, 1) * 1000,
        "e2e_ms": (end - t0) * 1000,
        "output_tokens": n,
    }
```

Measure under realistic concurrency, not one request at a time; TPOT at batch 1 tells you little about TPOT at batch 64. Use the server's token counts rather than chunk counts when available.

## Pitfalls

- **Benchmarking at concurrency 1.** Single-request numbers look great and collapse under load. Fix: load-test with your real prompt and output length distribution.
- **Ignoring output length.** Symptom: E2E latency tracks response length. Fix: ask for concise outputs, cap `max_tokens`.
- **Sizing GPUs by weights only.** Symptom: out-of-memory or very low concurrency. Fix: budget KV cache from context length and target concurrency.
- **Averaging TTFT and TPOT.** Fix: track p50, p95 and p99 separately; prefill spikes show up in tails.
- **Assuming long context is free.** It costs KV memory per sequence and prefill time per request.

## Checklist

- Know your model's layers, KV heads and head dim; compute KV bytes per token.
- Budget GPU memory: weights + KV cache for target concurrency × context + overhead.
- Track TTFT, TPOT, E2E and throughput as separate metrics, with percentiles.
- Load-test with realistic input and output lengths.
- Treat output tokens as the main latency and cost driver.

## Interview angle

- **Why is decode memory-bound?** Each step reads all weights to compute one token per sequence; arithmetic intensity is about 1 FLOP/byte at batch 1, far below what the GPU needs to saturate compute.
- **Compute the KV cache for a request.** `2 × layers × kv_heads × head_dim × bytes × tokens`, for example 128 KiB/token for an 8B GQA model at 16-bit, so ~1 GiB for 8K tokens.
- **TTFT vs TPOT?** TTFT is queueing plus prefill, driven by prompt length and load. TPOT is decode step time, driven by model size, batch size and context length.
