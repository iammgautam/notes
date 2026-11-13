---
title: How inference works
section: llm
order: 40
phase: "Phase 7 · Inference and serving"
summary: Prefill vs decode, why decode is memory-bound, arithmetic intensity, and how TTFT, TPOT and throughput follow from bandwidth, FLOPs and model size.
---

Every LLM call, hosted or self-hosted, runs the same two-phase execution: one parallel pass over the prompt, then a strictly sequential loop that produces one token per pass. The first phase is limited by arithmetic, the second by how fast the GPU can read its own memory. Almost every serving decision follows from that split: why long prompts raise time-to-first-token, why output tokens cost more than input tokens, why batching is the main throughput lever, why quantization speeds up generation, and why a GPU reporting 100% utilization can be doing 1% of the math it is capable of. This note builds that mental model and a small calculator you can use to sanity-check any latency or throughput claim.

## Core ideas

### A forward pass is mostly matrix multiplies

A dense decoder-only transformer with `P` parameters spends about `2P` FLOPs per token on its weight matrices: one multiply and one add per weight (see [Transformer architecture]({{ '/llm/transformer-architecture/' | relative_url }})). Attention adds a term that grows with context, because each token's query is scored against every earlier key and then used to weight every earlier value:

```text
FLOPs per token ≈ 2P  +  4 × layers × context × d_attn
                  ^^     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                  weights   attention (scores + weighted sum)

d_attn = query_heads × head_dim
```

For an 8B-class model (32 layers, `d_attn` 4096) the attention term equals the weight term at roughly 30k tokens of context. Below that, weights dominate the math; above it, attention does. Grouped-query attention shrinks the KV cache but not these FLOPs, because every query head still attends over the full context.

### Two phases: prefill and decode

```text
request: 2,000-token prompt, 300-token answer

PREFILL  one forward pass over all 2,000 prompt tokens in parallel
         writes K and V for every prompt token into the KV cache
         samples the first output token
         bound by: compute (large matrix-matrix multiplies)

DECODE   pass 1: 1 new token attends to 2,000 cached tokens -> token 2
         pass 2: 1 new token attends to 2,001 cached tokens -> token 3
         ... 299 strictly sequential passes
         bound by: memory bandwidth (all weights + KV read per pass)
```

Prefill turns the prompt into a matrix with `n` rows, so each weight read from memory is reused `n` times. Decode has one row per sequence, so each weight read is reused once per sequence in the batch. The [KV cache]({{ '/llm/kv-cache-and-long-context/' | relative_url }}) is what lets decode process only the new token instead of re-running the whole prefix.

### Arithmetic intensity and the roofline

Arithmetic intensity is FLOPs performed per byte moved from GPU memory (HBM). A GPU has two ceilings, peak FLOP/s and peak bytes/s. Their ratio, the **ridge point**, is the intensity a workload needs before compute becomes the limit. Below it you are memory-bound and extra FLOPs are free; above it you are compute-bound and extra bytes are free.

| GPU (dense BF16, approximate) | Peak compute | HBM bandwidth | Ridge point |
|---|---|---|---|
| A100 80GB SXM | ~312 TFLOP/s | ~2.0 TB/s | ~150 FLOP/byte |
| H100 SXM | ~990 TFLOP/s | ~3.35 TB/s | ~295 FLOP/byte |

```text
attainable
FLOP/s
  peak  - - - - - - - - - - +--------------------------- compute roof
                          / |
                        /   |
        bandwidth     /     |
        roof        /       |
        (slope =  /         |
        TB/s)   /           |
              /             |
  ----------+---------------+---------------+-----> FLOPs per byte
            ~1             ~295           ~2,000
        decode, batch 1   ridge (H100)   prefill, 2k tokens
```

The intensity of each kind of work, assuming 16-bit weights:

| Work | FLOPs | Bytes read | Intensity |
|---|---|---|---|
| Prefill of `n` tokens | `2P × n` | `2P` | about `n` |
| Decode step, batch `B` | `2P × B` | `2P` + every sequence's KV | at most `B` |
| Decode attention, one sequence | `4 × layers × c × d_attn` | that sequence's KV | about query heads / KV heads |

Reading the table:

- A 2,000-token prefill has intensity around 2,000, far above the ridge: compute-bound. One GPU prefills thousands to tens of thousands of tokens per second.
- Decode at batch 1 has intensity around 1. The GPU uses well under 1% of its compute and waits on memory the rest of the time.
- Batching raises the intensity of the weight matmuls linearly, but not of attention. Every sequence reads its own KV cache, so decode attention stays memory-bound at any batch size, with intensity equal to the GQA group size (8 for a 32-query-head, 8-KV-head model). At long contexts and large batches, KV reads rather than weight reads dominate step time.

### Why decode is slow

Four things compound:

1. **Sequential dependency.** Token `t+1` needs token `t`, so there is no parallelism across output positions of one sequence. [Speculative decoding]({{ '/llm/speculative-decoding/' | relative_url }}) is the workaround.
2. **Memory-bound steps.** Every step streams all weights (roughly the model size in bytes) plus the KV cache through the memory system to produce one token per sequence.
3. **Growing KV.** Each generated token makes the next step read a little more.
4. **Fixed overheads.** For small models a step takes a few milliseconds, so kernel launches, Python scheduling, sampling and detokenization become a visible fraction. Engines use CUDA graphs and overlap CPU work with GPU work to hide them.

The batch-1 floor is `time per token ≥ weight bytes / memory bandwidth`. An 8B model in BF16 is 16 GB; at 3.35 TB/s that is about 4.8 ms, so roughly 210 tokens/s is a hard ceiling on one H100, and real engines reach perhaps 60–80% of it. For a mixture-of-experts model, use the bytes of the *active* parameters at batch 1. At large batches different tokens route to different experts, so nearly every expert's weights are read each step, and memory capacity must hold all of them.

The levers follow directly from the bottleneck:

| Lever | What it changes | Effect |
|---|---|---|
| [Batching]({{ '/llm/batching-and-paged-attention/' | relative_url }}) | One weight read serves `B` sequences | Throughput up roughly linearly; TPOT rises |
| [Quantization]({{ '/llm/quantization/' | relative_url }}) | Fewer bytes per weight | Faster decode, more memory for KV |
| FP8 KV cache | Fewer bytes per cached token | More concurrency, faster long-context decode |
| [Speculative decoding]({{ '/llm/speculative-decoding/' | relative_url }}) | Several tokens verified per weight read | Lower TPOT at low batch |
| Tensor parallelism | Bandwidth of several GPUs per step | Lower TPOT, adds interconnect traffic ([GPUs and parallelism]({{ '/llm/gpus-and-parallelism/' | relative_url }})) |
| Prefix caching | Skips prefill of a repeated prefix | Lower TTFT and prefill cost |

### Latency and throughput metrics

| Metric | Definition | Mostly driven by |
|---|---|---|
| TTFT | Request sent to first token received | Queueing + prefill (prompt length) + network |
| TPOT | `(E2E − TTFT) / (output_tokens − 1)` for one request | Decode step time: model bytes, batch, context |
| ITL | Each individual gap between streamed tokens | Same as TPOT, plus stalls when big prefills join the batch |
| E2E latency | Total request time | `TTFT + TPOT × (output_tokens − 1)` |
| Throughput | Output tokens/s (or requests/s) across all users | Batch size, hardware, model size |
| Goodput | Requests/s that meet the latency SLO | All of the above at the chosen operating point |

Example: TTFT 300 ms, TPOT 20 ms and 500 output tokens give `0.3 + 0.02 × 499 ≈ 10.3 s`. Output length dominates. People read roughly 5–10 tokens/s, so streaming at 30+ tokens/s feels fast to a human even when E2E is long; an agent that waits for the full response before its next tool call feels every one of those seconds.

Throughput and per-user latency pull in opposite directions. A bigger batch raises total tokens/s but makes each step slower for everyone in it. Serving is the job of picking the batch size and scheduler settings that maximize throughput subject to an SLO such as "p95 TTFT under 1 s and p95 TPOT under 50 ms". The rate of SLO-meeting requests is goodput, and it is what you size capacity on.

### Why output tokens cost more

Input tokens are processed in a compute-efficient prefill pass. Each output token needs its own memory-bound pass and keeps its KV cache resident for the rest of the generation. Output tokens therefore consume many more GPU-seconds each, which is why providers price them several times higher than input tokens, and price cached input lower still (see [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }})).

## In practice

### A runnable latency model

The calculator below is a first-order roofline model: each step takes the longer of "time to move the bytes" and "time to do the FLOPs", scaled by realistic efficiency factors (MBU, model bandwidth utilization, and MFU, model FLOPs utilization). It ignores scheduler overhead, interconnect and queueing, so treat its output as a ceiling to compare measurements against.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class GPU:
    hbm_gb: float          # memory capacity, GB
    bandwidth_gbs: float   # HBM bandwidth, GB/s
    tflops: float          # dense BF16 tensor-core TFLOP/s
    mbu: float = 0.7       # achievable fraction of bandwidth
    mfu: float = 0.5       # achievable fraction of peak FLOPs


@dataclass(frozen=True)
class Model:
    params_b: float        # parameters read per token, billions
    layers: int
    q_heads: int
    kv_heads: int
    head_dim: int
    weight_bytes: float = 2.0   # per parameter (BF16)
    kv_bytes: float = 2.0       # per cached value (BF16)

    def weights(self) -> float:
        return self.params_b * 1e9 * self.weight_bytes

    def kv_per_token(self) -> float:
        return 2 * self.layers * self.kv_heads * self.head_dim * self.kv_bytes

    def flops_per_token(self, context: int) -> float:
        d_attn = self.q_heads * self.head_dim
        return 2 * self.params_b * 1e9 + 4 * self.layers * context * d_attn


def decode_step(m: Model, g: GPU, batch: int, context: int) -> tuple[float, str]:
    mem = (m.weights() + batch * context * m.kv_per_token()) / (g.bandwidth_gbs * 1e9 * g.mbu)
    comp = batch * m.flops_per_token(context) / (g.tflops * 1e12 * g.mfu)
    return max(mem, comp), ("memory" if mem >= comp else "compute")


def prefill_s(m: Model, g: GPU, prompt: int) -> float:
    flops = prompt * m.flops_per_token(prompt // 2)   # causal: average context n/2
    return max(m.weights() / (g.bandwidth_gbs * 1e9 * g.mbu),
               flops / (g.tflops * 1e12 * g.mfu))


def max_batch(m: Model, g: GPU, context: int, usable: float = 0.9) -> int:
    free = g.hbm_gb * 1e9 * usable - m.weights()
    return int(free // (context * m.kv_per_token()))


if __name__ == "__main__":
    h100 = GPU(hbm_gb=80, bandwidth_gbs=3350, tflops=989)
    dense_8b = Model(params_b=8.0, layers=32, q_heads=32, kv_heads=8, head_dim=128)

    ridge = h100.tflops * 1e12 / (h100.bandwidth_gbs * 1e9)
    print(f"ridge point: {ridge:.0f} FLOP/byte")
    assert 250 < ridge < 350

    t1, bound = decode_step(dense_8b, h100, batch=1, context=1)
    print(f"batch 1: {t1 * 1e3:.1f} ms/token = {1 / t1:.0f} tok/s ({bound}-bound)")
    assert 120 < 1 / t1 < 200 and bound == "memory"

    ctx = 1024
    cap = max_batch(dense_8b, h100, ctx)
    print(f"KV memory fits {cap} sequences at {ctx} tokens")
    prev_total = 0.0
    for b in (1, 8, 32, 128, cap):
        t, bound = decode_step(dense_8b, h100, b, ctx)
        print(f"  batch {b:>3}: {t * 1e3:5.1f} ms/step  {1 / t:4.0f} tok/s/seq  "
              f"{b / t:6.0f} tok/s total  {bound}-bound")
        assert b / t > prev_total            # throughput keeps rising...
        prev_total = b / t
    assert bound == "memory"                 # ...but memory runs out before compute

    ttft = prefill_s(dense_8b, h100, 2000)
    tpot, _ = decode_step(dense_8b, h100, 1, 2000 + 250)
    e2e = ttft + tpot * 499
    print(f"2,000 in / 500 out: TTFT {ttft * 1e3:.0f} ms, TPOT {tpot * 1e3:.1f} ms, "
          f"E2E {e2e:.2f} s")
    assert ttft < 0.1 and tpot * 499 > 0.9 * e2e   # decode dominates E2E

    int4 = Model(8.0, 32, 32, 8, 128, weight_bytes=0.5)
    t4, _ = decode_step(int4, h100, 1, 1)
    print(f"4-bit weights, batch 1: {1 / t4:.0f} tok/s ceiling")
    assert 1 / t4 > 3 * (1 / t1)
```

What the output tells you:

- Batch-1 decode tops out around 150 tokens/s for an 8B BF16 model on one H100. Going meaningfully faster requires fewer bytes (quantization) or more tokens per pass (speculation), not a faster GPU core.
- Aggregate throughput rises from ~145 to over 13,000 tokens/s as the batch grows, while each sequence slows from ~145 to ~33 tokens/s. That is the throughput-latency trade-off in one table.
- At 1k context, KV memory runs out (around 400 sequences) while decode is still memory-bound. For most dense models, capacity, not compute, caps throughput.
- For a 2,000-in, 500-out request, decode is over 95% of E2E time.
- 4-bit weights raise the batch-1 ceiling about 4x on paper; real int4 kernels recover less of that because dequantization costs work.

### Measuring a real endpoint

Illustrative, using the OpenAI Python SDK against any OpenAI-compatible server (vLLM, SGLang, a hosted provider). It records TTFT, mean TPOT, and the worst inter-token gap, using the server's token count rather than counting chunks.

```python
import time
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")


def measure(model: str, prompt: str, max_tokens: int = 256) -> dict:
    t0 = time.perf_counter()
    first = last = None
    gaps, usage = [], None
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )
    for chunk in stream:
        now = time.perf_counter()
        if chunk.usage:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            if first is None:
                first = now
            else:
                gaps.append(now - last)
            last = now
    n = usage.completion_tokens
    return {
        "ttft_ms": (first - t0) * 1e3,
        "tpot_ms": (last - first) / max(n - 1, 1) * 1e3,
        "max_gap_ms": max(gaps, default=0.0) * 1e3,
        "e2e_ms": (last - t0) * 1e3,
        "output_tokens": n,
    }
```

Compare measured TPOT against the roofline ceiling. Reaching only 50% of the bandwidth ceiling at batch 1 means overhead is eating time (missing CUDA graphs, a slow sampler, extra network hops). TPOT that climbs steeply as you add concurrency means you are past the knee of the throughput-latency curve. A large `max_gap_ms` with a normal mean points at prefill stalls from other users' long prompts. For load-testing methodology, see [Serving engines]({{ '/llm/serving-engines/' | relative_url }}).

### What this means when you only call APIs

- **Cut output tokens first.** Going from 800 to 200 output tokens saves far more time than trimming the prompt from 4,000 to 3,000 tokens.
- **Prompt length drives TTFT and input cost,** roughly linearly at moderate lengths and worse at very long ones.
- **Keep prefixes stable.** Provider prompt caches skip prefill for an exact repeated prefix, cutting TTFT and price (see [LLM caching]({{ '/llm/llm-caching/' | relative_url }})).
- **Long context slows every decode step,** not just prefill, because each step reads the whole KV cache.
- **Stream** user-facing output, and track TTFT separately from E2E.

## Pitfalls

- **Benchmarking at concurrency 1.** Single-request numbers look great and say nothing about TPOT at batch 64. Load-test with your real prompt and output length distribution.
- **Reading nvidia-smi "GPU-Util" as efficiency.** It reports the fraction of time any kernel was running. Memory-bound decode shows near 100% while using a tiny fraction of peak FLOPs. Track tokens/s, MBU and queue depth instead.
- **Ignoring output length.** Symptom: E2E latency tracks response length almost perfectly. Fix: ask for concise outputs, cap `max_tokens`, use structured outputs instead of prose where possible.
- **Sizing GPUs by weights alone.** Symptom: out-of-memory at startup or tiny concurrency. Budget KV cache from context length and target concurrency.
- **Averaging latencies.** Means hide prefill stalls and queueing. Track p50, p95 and p99 of TTFT and TPOT separately.
- **Comparing tokens/s across models with different tokenizers.** The same text can be noticeably more tokens on another tokenizer. Compare characters or requests per second when tokenizers differ.
- **Forgetting the attention term at long context.** At 100k+ tokens, prefill is dominated by the quadratic attention term and decode by KV reads; short-context benchmarks do not transfer.
- **Measuring cold.** The first requests after start include CUDA graph capture, compilation and cache warm-up. Discard a warm-up period.

## Rules of thumb

- Batch-1 decode tokens/s ≈ `0.7 × bandwidth / bytes of (active) weights`.
- Prefill tokens/s ≈ `0.5 × peak FLOP/s / (2 × params)` at moderate prompt lengths.
- Decode is memory-bound; prefill is compute-bound; decode attention is memory-bound at any batch size.
- KV capacity usually caps concurrency before compute does.
- Output tokens dominate E2E latency; prompt tokens dominate TTFT.
- Size and autoscale on goodput at your SLO, not peak throughput.

## Interview questions

**Why is prefill compute-bound and decode memory-bound?**
Prefill multiplies each weight matrix by `n` prompt-token rows, so every byte read is reused about `n` times, well above the GPU's ridge point of roughly 150–300 FLOP/byte. Decode multiplies by one row per sequence, so intensity is at most the batch size, and step time is set by reading weights and KV cache from HBM.

**Estimate batch-1 decode speed for a 70B model in FP8 on two H100s with tensor parallelism.**
Weights are about 70 GB, 35 GB per GPU. At 3.35 TB/s that is about 10 ms per step ideal and about 15 ms at 70% efficiency, so roughly 65–95 tokens/s before all-reduce overhead.

**What is arithmetic intensity and why does it matter?**
FLOPs per byte moved. Compared with the hardware ridge point (peak FLOP/s divided by bandwidth), it tells you which resource limits a kernel and therefore which optimization helps: fewer bytes for memory-bound work, fewer or faster FLOPs for compute-bound work.

**What drives TTFT and TPOT, and how do you reduce each?**
TTFT is queueing plus prefill: reduce it with prefix caching, shorter prompts, more prefill capacity, chunked prefill or disaggregated prefill. TPOT is decode step time: reduce it with quantization, a smaller batch cap, tensor parallelism or speculative decoding.

**Why doesn't throughput grow forever with batch size?**
Each sequence needs its own KV cache, so memory caps the batch; attention reads grow with batch times context; eventually compute saturates; and usually the TPOT SLO sets the limit before any of those.

**Why can a GPU at 100% utilization deliver low throughput?**
GPU-Util only means a kernel was running. A small-batch decode workload keeps kernels running while they wait on memory. Check batch size, queue depth, MBU and whether the scheduler is admitting enough requests.

**How does mixture-of-experts change the math?**
FLOPs and batch-1 bytes scale with active parameters, so MoE decodes fast at low batch. At high batch nearly all experts are touched each step, so memory capacity must hold every expert and the weight bytes read per step approach the total parameter count, amortized over many tokens.

## Further reading

- Pope et al., 2022, "Efficiently Scaling Transformer Inference".
- Williams, Waterman and Patterson, 2009, "Roofline: An Insightful Visual Performance Model for Multicore Architectures".
- Kaplan et al., 2020, "Scaling Laws for Neural Language Models" (per-token FLOP accounting).
- Zhong et al., 2024, "DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving".
- vLLM documentation: metrics and benchmarking guides.
