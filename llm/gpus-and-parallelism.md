---
title: GPUs and parallelism
section: llm
order: 46
phase: "Phase 7 · Inference and serving"
summary: GPU anatomy for inference, memory sizing, tensor, pipeline, expert and sequence parallelism, interconnects, choosing GPUs, and cost per million tokens.
---

Every self-hosting decision eventually becomes a hardware question: which GPU, how many, connected how, and what each token will cost. The answers come from a handful of numbers (memory capacity, memory bandwidth, tensor-core FLOPs, interconnect bandwidth) and a few ways of splitting a model across devices. Get them right and a 70B model serves interactive traffic on two GPUs at a cost per token below a hosted API; get them wrong and you pay for eight GPUs that spend their time waiting on PCIe. This note gives you the anatomy, the sizing math, the parallelism strategies and when each applies, and the cost model to compare self-hosting with an API.

## Core ideas

### GPU anatomy

```text
┌──────────────────────────── GPU ────────────────────────────┐
│  SM  SM  SM  SM  ...  (100+ streaming multiprocessors)       │
│  each: tensor cores, registers, ~200 KB SRAM (shared/L1)     │
│                         │                                    │
│               L2 cache (tens of MB)                          │
│                         │                                    │
│      HBM: 80–192 GB at 2–8 TB/s  (weights, KV cache)         │
└─────────┬───────────────────────────────┬────────────────────┘
          │ NVLink: ~900 GB/s per GPU      │ PCIe Gen5 x16:
          │ (H100), GPU to GPU             │ ~64 GB/s each way
     other GPUs in the node           CPU, host RAM, NICs
```

- **Tensor cores** do the matrix multiply-accumulates where nearly all LLM FLOPs happen. Each halving of precision roughly doubles their throughput on hardware that supports it: FP8 is about 2x BF16 on Hopper, and FP4 doubles again on Blackwell.
- **HBM** holds weights and KV cache. Its capacity caps model size and concurrency; its bandwidth caps decode speed (see [How inference works]({{ '/llm/inference-basics/' | relative_url }})).
- **SRAM** is tiny but an order of magnitude faster than HBM; kernels like FlashAttention exist to keep work in it.
- **Datasheet FLOPs** are often quoted "with sparsity", double the dense figure, which LLM inference rarely uses. Plan with dense numbers, and expect 40–60% of peak in good prefill kernels.

Approximate specs as of 2025–2026 (SXM variants; check vendor datasheets before buying):

| GPU | Memory | Bandwidth | Dense BF16 | FP8 | GPU-to-GPU |
|---|---|---|---|---|---|
| A100 80GB | 80 GB HBM2e | ~2.0 TB/s | ~312 TFLOP/s | No | NVLink ~600 GB/s |
| H100 | 80 GB HBM3 | ~3.35 TB/s | ~990 TFLOP/s | Yes | NVLink ~900 GB/s |
| H200 | 141 GB HBM3e | ~4.8 TB/s | ~990 TFLOP/s | Yes | NVLink ~900 GB/s |
| B200 | ~180–192 GB HBM3e | ~8 TB/s | ~2x H100 | Yes, plus FP4 | NVLink ~1.8 TB/s |
| AMD MI300X | 192 GB HBM3 | ~5.3 TB/s | ~1,300 TFLOP/s | Yes | Infinity Fabric |
| L40S | 48 GB GDDR6 | ~0.86 TB/s | ~360 TFLOP/s | Yes | PCIe only |
| L4 | 24 GB GDDR6 | ~0.3 TB/s | ~120 TFLOP/s | Yes | PCIe only |

The H200 has the same compute as the H100 but about 75% more memory and 40% more bandwidth, which is exactly what inference needs: it often serves on one GPU what needs two H100s.

### Memory sizing for inference

```text
per-GPU memory = weights / TP
               + KV cache for (concurrency × context) / TP
               + activations and workspace (largest prefill chunk; ~GBs)
               + runtime overhead (CUDA context, NCCL buffers, CUDA graphs)
               + headroom (fragmentation, spikes)
```

Weights are `params × bytes per param` ([Quantization]({{ '/llm/quantization/' | relative_url }})); KV is `2 × layers × kv_heads × head_dim × bytes × tokens` ([KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }})). Under tensor parallelism each GPU holds `kv_heads / TP` heads. When TP exceeds the number of KV heads, heads are replicated rather than split, so per-GPU KV stops shrinking: a GQA model with 8 KV heads gains no KV savings beyond TP=8.

### Parallelism strategies

| Strategy | What is split | Communication | Use in inference when | Cost |
|---|---|---|---|---|
| Data parallel (replicas) | Requests | None between replicas | The model fits on one GPU or one group; you need more throughput | Each replica holds full weights |
| Tensor parallel (TP) | Each layer's weight matrices | Two all-reduces per layer per step | Weights or KV do not fit one GPU; you want lower TPOT | Needs NVLink; efficiency falls past 4–8 GPUs |
| Pipeline parallel (PP) | Layers into stages | Activations passed stage to stage | The model does not fit one node; slow links between nodes | Adds latency per request; bubbles at low load |
| Expert parallel (EP) | MoE experts across GPUs | All-to-all token routing per MoE layer | Large mixture-of-experts models | Load imbalance; needs fast all-to-all |
| Sequence / context parallel | The sequence dimension | Exchanging KV blocks (ring attention) or activations | Very long prompts where one GPU's prefill is too slow or too big | Complex; only pays at 100k+ tokens |

```text
Tensor parallel, TP=2, one MLP block:
  x ─┬─► GPU0: x · W1[:, :h/2] ─► act ─► · W2[:h/2, :] ─┐
     └─► GPU1: x · W1[:, h/2:] ─► act ─► · W2[h/2:, :] ─┴─ all-reduce ─► y
  column-split then row-split: one all-reduce per block, every layer

Pipeline parallel, PP=2:
  GPU0: layers 0-39  ── activations ──►  GPU1: layers 40-79
  micro-batches keep both stages busy; each request still visits both
```

**Tensor parallelism** (from Megatron-LM) splits matmuls so every GPU works on every token. It reduces per-GPU weight bytes, so it lowers TPOT as well as fitting bigger models, but each layer ends with an all-reduce. For a 70B-class model (80 layers, hidden size 8192) decoding a batch of 64, each step runs 160 all-reduces of about 1 MB each. Over NVLink that adds on the order of a millisecond or two per step; over PCIe it can approach the step time itself, because of both lower bandwidth and higher per-collective latency. Keep TP inside an NVLink domain.

**Pipeline parallelism** (GPipe and successors) gives each GPU a contiguous block of layers and sends only activations between stages, which tolerates slower links, so it is the usual way to span nodes. It does not reduce single-request latency: a token still passes through every stage in order. Under load, micro-batching keeps stages busy.

**Expert parallelism** (GShard, Switch Transformer) places different experts on different GPUs and routes each token to its experts with all-to-all communication. Large MoE deployments typically combine it with data-parallel attention. Hot experts create imbalance, so engines replicate popular experts or balance load.

**Sequence and context parallelism** split a long sequence across GPUs. Ring attention passes KV blocks around a ring of devices so each computes attention for its chunk of queries against all keys. For inference this mainly cuts TTFT on very long prompts.

Typical combinations: TP within a node and PP across nodes for a dense model too big for one node; DP attention plus EP for large MoE; DP replicas of a TP group for throughput. Training adds FSDP and ZeRO, which shard parameters, gradients and optimizer states across data-parallel ranks (see [Fine-tuning in practice]({{ '/llm/fine-tuning-in-practice/' | relative_url }})); they matter less for inference, where there are no gradients or optimizer states.

### Interconnects

| Link | Bandwidth (order of magnitude) | Use |
|---|---|---|
| NVLink + NVSwitch | ~900 GB/s per H100, ~1.8 TB/s per B200 | TP and EP within a node; rack-scale NVLink domains (GB200 NVL72) extend this to 72 GPUs |
| PCIe Gen4 / Gen5 x16 | ~32 / ~64 GB/s each way | Host transfers, KV offload; poor for TP |
| InfiniBand / RoCE | 200–400 Gb/s per NIC (25–50 GB/s) | PP, EP and KV transfer across nodes, with GPUDirect RDMA |

Rule: TP inside the fastest domain, PP and DP across slower links, EP only where all-to-all is fast. GPUs like the L4 and L40S have no NVLink, so serve them as independent replicas, not TP groups.

## In practice

### Sizing a deployment

```python
from dataclasses import dataclass

GB = 1e9


@dataclass(frozen=True)
class Shape:
    params_b: float
    layers: int
    kv_heads: int
    head_dim: int


def per_gpu_gb(m: Shape, tp: int, weight_bytes: float, kv_bytes: float,
               context: int, concurrency: int, overhead_gb: float = 3.0) -> float:
    weights = m.params_b * GB * weight_bytes / tp
    heads = max(1, m.kv_heads // tp)          # replicated once TP exceeds KV heads
    kv = 2 * m.layers * heads * m.head_dim * kv_bytes * context * concurrency
    return (weights + kv) / GB + overhead_gb


def min_tp(m: Shape, gpu_gb: float, usable: float = 0.9, **load) -> int | None:
    for tp in (1, 2, 4, 8):
        if per_gpu_gb(m, tp, **load) <= gpu_gb * usable:
            return tp
    return None


def max_concurrency(m: Shape, gpu_gb: float, tp: int, weight_bytes: float, kv_bytes: float,
                    context: int, usable: float = 0.9, overhead_gb: float = 3.0) -> int:
    free = gpu_gb * usable - overhead_gb - m.params_b * weight_bytes / tp
    per_seq = 2 * m.layers * max(1, m.kv_heads // tp) * m.head_dim * kv_bytes * context / GB
    return max(0, int(free // per_seq))


if __name__ == "__main__":
    m70 = Shape(params_b=70, layers=80, kv_heads=8, head_dim=128)
    m8 = Shape(params_b=8, layers=32, kv_heads=8, head_dim=128)
    load = {"context": 8192, "concurrency": 32}

    plans = {
        "70B BF16, BF16 KV on 80 GB": min_tp(m70, 80, weight_bytes=2, kv_bytes=2, **load),
        "70B FP8,  BF16 KV on 80 GB": min_tp(m70, 80, weight_bytes=1, kv_bytes=2, **load),
        "70B FP8,  FP8 KV on 80 GB": min_tp(m70, 80, weight_bytes=1, kv_bytes=1, **load),
        "70B FP8,  FP8 KV on 141 GB": min_tp(m70, 141, weight_bytes=1, kv_bytes=1, **load),
    }
    for name, tp in plans.items():
        print(f"{name}: TP={tp} for 32 users x 8k tokens")
    assert list(plans.values()) == [4, 4, 2, 1]

    l4_bf16 = max_concurrency(m8, 24, 1, weight_bytes=2, kv_bytes=2, context=8192)
    l4_fp8 = max_concurrency(m8, 24, 1, weight_bytes=1, kv_bytes=1, context=8192)
    print(f"8B on a 24 GB L4 at 8k context: {l4_bf16} seqs in BF16, {l4_fp8} in FP8")
    assert l4_bf16 == 2 and l4_fp8 >= 15
```

Four lessons in four lines of output: a BF16 70B model needs TP=4 on 80 GB GPUs for 32 users at 8k; FP8 weights alone do not drop that to TP=2 because the KV cache no longer fits; FP8 weights plus FP8 KV do; and a single 141 GB H200 handles the same load. On a 24 GB L4, an 8B model in BF16 barely serves two 8k-token users, while FP8 serves almost twenty.

### Choosing GPUs by workload

| Workload | Good fit | Why |
|---|---|---|
| Model up to ~8B, moderate traffic | L4, L40S or similar, as replicas | Fits one card; cheap per hour; no TP needed |
| Small model, high traffic | H100/H200 replicas | Bandwidth and KV capacity for large batches |
| ~70B interactive | 2–4x H100 in FP8, or 1–2x H200 | NVLink TP; memory left for KV |
| Large MoE (hundreds of billions of params) | 8-GPU H200, B200 or MI300X nodes, multi-node with EP | All expert weights must fit; fast all-to-all |
| Long context (128k+) | Highest-memory GPUs, FP8 KV, context parallelism | KV dominates memory and decode time |
| Offline batch: evals, synthetic data, backfills | Cheapest cost per token: older or spot GPUs, maximum batch | Latency does not matter |
| Latency-critical: voice, autocomplete | High bandwidth per GPU, small models, speculative decoding | TPOT is bandwidth per sequence |
| Local development | Apple silicon or consumer GPUs with llama.cpp | Unified memory, no cloud bill |

Beyond specs, weigh availability and quotas in your regions, software maturity (CUDA support is broadest; ROCm support in vLLM and SGLang has improved but check your model), and commitment discounts, which change cost more than any GPU choice.

### Cost per million tokens

```text
cost per 1M tokens = hourly cost × 1,000,000
                     / (tokens per second × utilization × 3600)
```

`tokens per second` is what one replica sustains at your latency SLO (goodput, not peak), and `utilization` is the fraction of paid hours that capacity is actually used. Utilization is where self-hosting usually loses.

```python
def cost_per_million(hourly_usd: float, tokens_per_s: float, utilization: float = 1.0) -> float:
    return hourly_usd / (tokens_per_s * utilization * 3600) * 1e6


def breakeven_tokens_per_month(hourly_usd: float, api_usd_per_million: float,
                               hours: float = 730) -> float:
    return hourly_usd * hours / api_usd_per_million * 1e6


if __name__ == "__main__":
    assert abs(cost_per_million(2.0, 1000) - 0.5556) < 1e-3

    # Placeholders: replace with your quotes and your own measured goodput.
    gpu_hour = 2.50                 # USD per GPU-hour
    replica = 2 * gpu_hour          # a 70B FP8 model on two GPUs
    goodput_tps = 3000              # output tokens/s one replica sustains within SLO
    api_price = 0.90                # USD per 1M tokens for a comparable hosted model

    full = cost_per_million(replica, goodput_tps)
    for u in (1.0, 0.5, 0.2, 0.05):
        print(f"utilization {100 * u:3.0f}%: ${cost_per_million(replica, goodput_tps, u):6.2f} per 1M tokens")
    assert abs(cost_per_million(replica, goodput_tps, 0.2) - 5 * full) < 1e-9

    be = breakeven_tokens_per_month(replica, api_price)
    be_tps = be / (730 * 3600)
    print(f"break-even vs API: {be / 1e9:.1f}B tokens/month = {be_tps:,.0f} tokens/s around the clock "
          f"({100 * be_tps / goodput_tps:.0f}% of one replica)")
    assert be_tps < goodput_tps
```

With these placeholder numbers, one replica costs under $0.50 per million tokens when fully busy, but over $2 at 20% utilization, and it only beats the assumed API price above roughly half utilization around the clock. Real comparisons also need: input tokens (prefill is cheap per token but not free; measure with your real input/output mix), redundancy (at least two replicas for availability), engineering and on-call time, and the hosted model's quality relative to the open one. Self-hosting wins with steady high volume, strict data residency, fine-tuned models or heavy prefix reuse; APIs win for spiky or low volume (see [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }})).

## Pitfalls

- **Planning with sparse or FP8 FLOPs for a BF16 workload.** Use dense numbers at the precision you will actually run.
- **Tensor parallelism over PCIe.** Symptom: TPOT barely improves or worsens as you add GPUs. Use replicas or pipeline parallelism instead.
- **TP beyond the KV head count.** KV is replicated, not split; memory per GPU stops falling.
- **No headroom.** Sizing to 100% of memory leads to out-of-memory errors at peak from fragmentation, CUDA graphs or a large prefill chunk. Keep 5–10% free and use the engine's capacity log as ground truth.
- **Mixed GPU generations in one TP or PP group.** The slowest device sets the pace.
- **Cost math at full utilization.** Real traffic is diurnal and spiky; use measured average utilization, and remember the minimum replicas you keep warm.
- **Ignoring capacity availability.** The ideal GPU may not be obtainable in your region or quota when you need to scale.

## Rules of thumb

- Decode speed tracks memory bandwidth; capacity tracks HBM size; prefill tracks FLOPs.
- Pick the fewest GPUs that hold weights plus KV for target concurrency with headroom; then add replicas for throughput.
- TP within NVLink, PP across nodes, EP for MoE, context parallelism only for very long prompts.
- FP8 weights and FP8 KV on FP8-capable GPUs is usually the best first optimization.
- Compute cost per million tokens from goodput and real utilization, and compare at your actual volume.

## Interview questions

**How many H100s do you need to serve a 70B model?**
Weights are 140 GB in BF16 or 70 GB in FP8, and each 8k-token sequence needs 2.5 GiB of BF16 KV. For dozens of concurrent users that is TP=4 in BF16 or TP=2 in FP8 with FP8 KV; a single H200 can handle the FP8 case.

**Tensor vs pipeline parallelism: when do you use each?**
Tensor parallelism splits every layer and needs two all-reduces per layer, so it needs NVLink and is used within a node; it lowers per-GPU memory and TPOT. Pipeline parallelism splits layers into stages and only passes activations, so it tolerates slower links across nodes, but it does not reduce single-request latency.

**Why is tensor parallelism over PCIe a bad idea?**
Decode runs two all-reduces per layer per step, hundreds per token. PCIe's lower bandwidth and higher per-collective latency make that communication comparable to the compute it parallelizes.

**How does expert parallelism differ from tensor parallelism?**
EP places whole experts on different GPUs and routes each token to its experts with all-to-all communication; TP splits every matrix across GPUs. EP scales to many experts but suffers from routing imbalance and needs fast all-to-all.

**Why does the H200 help inference more than its FLOPs suggest?**
Inference is mostly limited by memory capacity and bandwidth. The H200 keeps H100 compute but has 141 GB at about 4.8 TB/s, so it fits bigger models and more KV per GPU and decodes faster.

**Compute the cost per million tokens for a deployment.**
Hourly cost divided by tokens per hour actually served: `hourly / (goodput tokens/s × utilization × 3600) × 1e6`. For example, $5/hour at 3,000 tokens/s and full utilization is about $0.46 per million; at 20% utilization, five times that.

## Further reading

- Shoeybi et al., 2019, "Megatron-LM: Training Multi-Billion Parameter Language Models Using Model Parallelism".
- Huang et al., 2018, "GPipe: Efficient Training of Giant Neural Networks using Pipeline Parallelism".
- Lepikhin et al., 2020, "GShard: Scaling Giant Models with Conditional Computation and Automatic Sharding".
- Liu et al., 2023, "Ring Attention with Blockwise Transformers for Near-Infinite Context".
- Pope et al., 2022, "Efficiently Scaling Transformer Inference".
- NVIDIA and AMD datasheets and architecture whitepapers for current GPU specifications.
