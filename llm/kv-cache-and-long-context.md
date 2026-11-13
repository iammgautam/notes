---
title: KV cache and long context
section: llm
order: 42
phase: "Phase 7 · Inference and serving"
summary: What the KV cache stores and costs, GQA and prefix caching, FlashAttention, sliding-window attention, RoPE scaling, and what long context really costs.
---

The KV cache is the per-request memory that makes generation affordable, and the resource that runs out first on almost every serving deployment. Its size decides how many users fit on a GPU, what a 128k-token prompt really costs, and whether prefix caching can save you a second of TTFT. This note covers what is stored, how to size it, the techniques that shrink or reuse it, how kernels and position encodings make long context possible, and why a model that advertises a huge window may still miss facts at a fraction of it.

## How it works

### What is stored and why

In each attention layer, every token produces a query, a key and a value. The new token's query is compared with the keys of all earlier tokens, and the result weights their values. Earlier tokens' keys and values never change, so the engine stores them once and reuses them at every later step. Queries are not cached: only the current token's query is ever needed.

```text
per layer, per sequence:
  K cache  [kv_heads × tokens × head_dim]
  V cache  [kv_heads × tokens × head_dim]

decode step t:
  compute q_t, k_t, v_t for the new token only
  append k_t, v_t to the cache
  attend: softmax(q_t · K^T / sqrt(head_dim)) · V   over all t tokens
```

Without the cache, every step would recompute keys and values for all earlier tokens in every layer, making generation quadratic in length. With it, each step does constant weight work plus a linearly growing cache read.

### Size math

```text
KV bytes per token = 2 (K and V) × layers × kv_heads × head_dim × bytes
KV bytes for a request = per-token bytes × (prompt + generated tokens)
```

The number of KV heads is the architectural lever:

| Attention variant | KV heads | KV size vs MHA | Notes |
|---|---|---|---|
| Multi-head (MHA) | = query heads | 1x | Original transformer |
| Grouped-query (GQA) | query heads / group (often 8 total) | 1/group | Default in most open models since 2023–2024 |
| Multi-query (MQA) | 1 | 1/query heads | Smallest cache; some quality cost |
| Multi-head latent (MLA) | One compressed latent per token per layer | Far smaller than MHA | DeepSeek-V2 and later; K and V are reconstructed from the latent |

GQA reduces memory and memory traffic, not attention FLOPs: every query head still attends over the whole context.

### Prefix caching and RadixAttention

Requests often share a prefix: the same system prompt, tool schemas, few-shot examples, a document being asked several questions, or the growing history of a multi-turn chat or agent loop. The KV for an identical token prefix is identical, so engines can reuse it and skip that part of prefill.

- **Block hashing (vLLM automatic prefix caching).** Each full KV block is identified by a hash of its tokens chained with the hash of every block before it (plus extra keys such as the LoRA adapter). A new request reuses every leading block already in memory. Unreferenced blocks stay cached until evicted least-recently-used.
- **RadixAttention (SGLang).** Cached prefixes live in a radix tree keyed by token sequences: longest-prefix lookup at token granularity, LRU eviction of leaves, and scheduling that orders waiting requests to maximize hits. It shines on agentic and multi-turn workloads with branching histories.
- **Provider prompt caching.** Hosted APIs expose the same mechanism, automatically or through explicit cache markers, and bill cached input at a discount (see [LLM caching]({{ '/llm/llm-caching/' | relative_url }})).

Only an exact token prefix matches. Put stable content first and variable content last:

```text
[system prompt][tool schemas][few-shot][retrieved docs][history][question]
 ◄──────── stable: cache hits ────────►◄───── varies per request ─────►
```

A timestamp or request id at the top of the system prompt shifts every block and defeats the cache entirely. So does a load balancer that spreads one conversation's turns across replicas; session- or prefix-aware routing keeps hits high.

### KV cache quantization and offloading

**Quantization.** FP8 K and V halve KV memory and decode KV reads, roughly doubling concurrency. Quality impact is usually small; verify on long-context tasks, where errors accumulate. Research methods (KIVI, KVQuant) reach 4 and 2 bits, quantizing keys per-channel (they have outlier channels) and values per-token.

**Offloading.** KV can live in a hierarchy: GPU HBM, CPU DRAM, local SSD, a remote store. Engines swap preempted sequences to CPU, and tiered prefix caches (LMCache integrates with vLLM, for example) keep long shared prefixes and idle conversations warm outside GPU memory. Reloading usually beats recomputing: 1 GiB of KV (8k tokens of an 8B GQA model) crosses PCIe Gen5 x16 (~64 GB/s) in about 17 ms, while re-running that prefill takes on the order of 300 ms.

**Eviction.** Lossy methods drop tokens that receive little attention: StreamingLLM keeps a few initial "attention sink" tokens plus a recent window; H2O and SnapKV keep tokens with high accumulated attention. They trade exact recall for memory, so they suit streaming chat more than document question answering.

### FlashAttention

Naive attention writes the full `n × n` score matrix to HBM, applies softmax, reads it back and multiplies by `V`; at long lengths that matrix dominates memory (`O(n²)`) and traffic. FlashAttention never materializes it. It holds a tile of queries in on-chip SRAM, streams tiles of keys and values past it, and keeps a running max, softmax denominator and output; when a tile raises the max, the accumulators are rescaled by `exp(old_max − new_max)`. This online softmax is exact, not an approximation (the demo below verifies it). Memory becomes linear in length and HBM traffic drops sharply. FlashAttention-2 improved parallelism and work partitioning, and FlashAttention-3 targets Hopper-generation features including FP8. For decode at long context and small batch, Flash-Decoding splits the KV sequence across thread blocks so that one query can still use the whole GPU. Serving engines use these kernels (or their own paged equivalents) by default.

### Sliding-window and sparse attention

| Approach | What each token attends to | KV memory | Trade-off |
|---|---|---|---|
| Full causal attention | Every earlier token | Grows with context | Best recall; quadratic prefill |
| Sliding window | The last `W` tokens | Capped at `W` per layer | Older information only propagates through stacked layers |
| Interleaved local/global | Windowed in most layers, full in some | Mostly capped | Global layers preserve recall; Gemma 2 and 3, for example |
| Fixed sparse patterns | Local window plus strided or global tokens | Reduced | Longformer, BigBird; mostly research and encoders |
| Learned sparse attention | Blocks selected per query | Full cache, less read | DeepSeek's Native Sparse Attention (2025) |
| State space / linear attention | A fixed-size recurrent state | Constant | Mamba-style and hybrid models; weaker exact recall |

These are training-time architecture choices, not serving switches: windowing a model trained with full attention degrades it.

### Extending context: RoPE scaling and YaRN

Most open models encode position with rotary embeddings (RoPE): each pair of query and key dimensions is rotated by an angle proportional to the token's position, so `q · k` depends only on the relative distance.

```text
angle for dimension pair i at position p:   p × θ_i,   θ_i = base^(−2i/d)

high-frequency pairs (small i): rotate quickly, encode local order
low-frequency pairs (large i):  rotate slowly; within the training
                                length they cover only part of a turn
```

Beyond the trained length, low-frequency pairs reach angles the model never saw and quality collapses. Extension methods remap positions or frequencies so that long inputs fall back into familiar ranges:

| Method | Idea | Fine-tuning |
|---|---|---|
| Position interpolation | Scale every position by `trained_len / target_len` | Short fine-tune; blurs local order |
| NTK-aware scaling | Raise the RoPE base so low frequencies stretch more than high ones | Partial gains without, better with |
| YaRN | Leave high-frequency pairs alone, interpolate low-frequency pairs, blend between, and adjust attention temperature | Small fine-tune; widely used in open models' long-context configs |
| LongRoPE | Searched per-dimension rescale factors with progressive extension | Yes; reaches very long windows |
| Long-context continued pretraining | Train on long documents, usually with a larger base | Expensive; how strong models get real long context |

In engines that read Hugging Face configs, scaling is set in the model's `config.json` (illustrative; exact keys depend on the model family and library version):

```json
{
  "rope_scaling": {
    "rope_type": "yarn",
    "factor": 4.0,
    "original_max_position_embeddings": 32768
  }
}
```

Static scaling can slightly hurt quality on short inputs, so enable it only on deployments that need the longer window.

### What long context really costs

- **Prefill compute grows quadratically.** The attention term overtakes the weight matmuls in the tens of thousands of tokens for an 8B-class model; at 128k it is most of the work.
- **Decode slows linearly.** Every step reads the whole KV cache; at 128k, an 8B model reads more KV than weights per step, so TPOT roughly doubles even at batch 1.
- **Concurrency collapses.** One 128k-token request on a 70B GQA model holds 40 GiB of KV in BF16.
- **Money.** Agent loops that resend a growing history pay quadratically over the conversation unless prefix caching applies.
- **Quality degrades before the limit.** Effective context is shorter than advertised:
  - Needle-in-a-haystack tests (find one inserted sentence) are easy because question and needle share words; passing says little about reasoning over a document.
  - RULER adds multi-needle, multi-hop and aggregation tasks; many models fall off well before their claimed length.
  - "Lost in the middle": accuracy is often best for facts at the start or end of the context, worst in the middle.
  - NoLiMa (Modarressi et al., 2025) removes the literal overlap between question and needle and finds steep drops as length grows.
  - Near-duplicate distractor passages hurt far more than random filler.

Long context wins for whole-document reasoning over small corpora; [retrieval]({{ '/llm/rag-architecture/' | relative_url }}) wins for large or changing corpora and tight cost or latency budgets. Often the best design retrieves generously and lets a long window absorb the top results.

## In practice

### Sizing the cache and the cost of length

```python
GiB = 1024**3


def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, bytes_per_value: int = 2) -> int:
    return 2 * layers * kv_heads * head_dim * bytes_per_value      # 2 = K and V


def max_sequences(gpu_gib: float, weights_gib: float, per_token: int, context: int,
                  usable: float = 0.9, overhead_gib: float = 2.0) -> int:
    free = gpu_gib * usable - weights_gib - overhead_gib
    return int(free * GiB // (per_token * context))


def prefill_flops(params: float, layers: int, d_attn: int, n: int) -> tuple[float, float]:
    linear = 2 * params * n
    attention = 2 * layers * n * n * d_attn    # 4·layers·d_attn per (query, key) pair, causal half
    return linear, attention


if __name__ == "__main__":
    shapes = {"8B MHA, 32 KV heads": (32, 32, 128), "8B GQA, 8 KV heads": (32, 8, 128),
              "8B MQA, 1 KV head": (32, 1, 128), "70B GQA, 8 KV heads": (80, 8, 128)}
    for name, (layers, kv_heads, head_dim) in shapes.items():
        per = kv_bytes_per_token(layers, kv_heads, head_dim)
        print(f"{name:20} {per // 1024:4d} KiB/token  {per * 8192 / GiB:5.2f} GiB @8k  "
              f"{per * 131072 / GiB:5.1f} GiB @128k")

    gqa = kv_bytes_per_token(32, 8, 128)
    assert gqa == 128 * 1024 and gqa * 8192 == GiB          # 128 KiB/token, 1 GiB per 8k
    assert kv_bytes_per_token(32, 32, 128) == 4 * gqa         # GQA saves 4x vs MHA here
    assert kv_bytes_per_token(80, 8, 128) * 131072 == 40 * GiB

    # 8B model in BF16 (~15 GiB of weights) on an 80 GB GPU
    fp8 = kv_bytes_per_token(32, 8, 128, bytes_per_value=1)
    for ctx in (2048, 8192, 32768, 131072):
        print(f"context {ctx:>6}: {max_sequences(80, 15, gqa, ctx):4d} seqs BF16 KV, "
              f"{max_sequences(80, 15, fp8, ctx):4d} FP8 KV")
    assert max_sequences(80, 15, gqa, 8192) == 55
    assert max_sequences(80, 15, gqa, 131072) == 3
    assert max_sequences(80, 15, fp8, 131072) == 6

    for n in (8192, 32768, 131072):
        lin, att = prefill_flops(8e9, 32, 4096, n)
        print(f"prefill {n:>6} tokens: attention = {100 * att / (lin + att):.0f}% of FLOPs")
    lin, att = prefill_flops(8e9, 32, 4096, 131072)
    assert att > lin

    kv_128k, weights = gqa * 131072, 8e9 * 2
    print(f"decode at 128k reads {kv_128k / weights:.1f}x the weight bytes in KV")
    assert kv_128k > weights
```

### FlashAttention's trick, verified

Tiled attention with a running max and denominator gives the same output as materializing every score, while touching only one tile of keys and values at a time. This is the per-query core of FlashAttention, in pure Python.

```python
import math
import random


def naive_attention(q, keys, values):
    scores = [sum(a * b for a, b in zip(q, k)) / math.sqrt(len(q)) for k in keys]
    m = max(scores)
    w = [math.exp(s - m) for s in scores]
    z = sum(w)
    return [sum(wi * v[j] for wi, v in zip(w, values)) / z for j in range(len(values[0]))]


def tiled_attention(q, keys, values, tile=4):
    m, z, acc = -math.inf, 0.0, [0.0] * len(values[0])
    for start in range(0, len(keys), tile):
        ks, vs = keys[start:start + tile], values[start:start + tile]
        s = [sum(a * b for a, b in zip(q, k)) / math.sqrt(len(q)) for k in ks]
        m_new = max(m, max(s))
        scale = math.exp(m - m_new)          # rescale everything accumulated so far
        z, acc = z * scale, [a * scale for a in acc]
        for si, v in zip(s, vs):
            w = math.exp(si - m_new)
            z += w
            acc = [a + w * vj for a, vj in zip(acc, v)]
        m = m_new
    return [a / z for a in acc]


if __name__ == "__main__":
    rng = random.Random(0)
    d, n = 8, 37                              # n deliberately not a multiple of the tile
    q = [rng.gauss(0, 20) for _ in range(d)]  # large scores: tests numerical stability
    keys = [[rng.gauss(0, 1) for _ in range(d)] for _ in range(n)]
    values = [[rng.gauss(0, 1) for _ in range(d)] for _ in range(n)]
    exact = naive_attention(q, keys, values)
    for tile in (1, 4, 16, 64):
        tiled = tiled_attention(q, keys, values, tile)
        assert all(abs(a - b) < 1e-9 for a, b in zip(exact, tiled))
    print("tiled online-softmax attention matches naive attention")
```

### Prefix cache hits, and how one token ruins them

```python
import hashlib

BLOCK = 16


def block_hashes(tokens: list[int]) -> list[str]:
    """Hash each full block chained with the hash of everything before it."""
    hashes, parent = [], ""
    for i in range(0, len(tokens) - BLOCK + 1, BLOCK):
        parent = hashlib.sha256((parent + repr(tokens[i:i + BLOCK])).encode()).hexdigest()
        hashes.append(parent)
    return hashes


class PrefixCache:
    def __init__(self) -> None:
        self.blocks: set[str] = set()

    def lookup_and_insert(self, tokens: list[int]) -> int:
        """Return the number of prompt tokens that can skip prefill."""
        hashes = block_hashes(tokens)
        hit = 0
        while hit < len(hashes) and hashes[hit] in self.blocks:
            hit += 1
        self.blocks.update(hashes)
        return hit * BLOCK


if __name__ == "__main__":
    system = list(range(1000, 1600))             # 600-token system prompt + tools
    cache = PrefixCache()
    assert cache.lookup_and_insert(system + [1, 2, 3] * 20) == 0
    assert cache.lookup_and_insert(system + [7, 8, 9] * 20) == 592   # 37 full blocks
    stamped = [42] + system + [7, 8, 9] * 20     # e.g. a timestamp prepended
    assert cache.lookup_and_insert(stamped) == 0
    print("shared prefix reused; one leading token changed every block")
```

## Pitfalls

- **Sizing for average context.** One long request can hold as much KV as dozens of short ones. Size for the length distribution and cap `max-model-len`.
- **Cache-busting prompts.** Timestamps, ids or reordered tool lists at the top of the prompt. Symptom: prefix hit rate near zero despite a fixed system prompt.
- **Cross-tenant prefix sharing.** Cache-hit timing can reveal that another tenant sent a given prefix. Isolate caches per tenant (recent vLLM versions accept a per-request cache salt) when prompts are sensitive.
- **Trusting needle-in-a-haystack scores.** Evaluate long-context features with your own documents, questions without literal overlap, and distractors.
- **Ignoring decode cost of long prompts.** Long contexts also slow every output token and shrink the batch for everyone on the GPU. Split traffic by length: a pool with a modest `max-model-len` serves far more users per GPU than one sized for the rare 128k request.

## Rules of thumb

- `KV/token = 2 × layers × kv_heads × head_dim × bytes`; an 8B GQA model is 128 KiB/token, 1 GiB per 8k tokens in BF16.
- FP8 KV (`--kv-cache-dtype fp8` in vLLM) roughly doubles concurrency; verify long-context tasks before and after.
- Stable prefix first, variable suffix last; route by session to keep cache hits across replicas.
- Reload cached KV rather than recompute long prefixes when you have a CPU or SSD tier.
- Assume effective context is well below the advertised window until your eval says otherwise.

## Interview questions

**What exactly is in the KV cache, and why not cache queries?**
Keys and values for every earlier token, per layer and per KV head. A query is used only at its own step, while keys and values are reused by every later token.

**Compute the KV cache for a 70B GQA model at 32k tokens.**
`2 × 80 layers × 8 heads × 128 × 2 bytes = 320 KiB/token`; times 32,768 tokens is 10 GiB per sequence in BF16, 5 GiB in FP8.

**How does prefix caching work, and what breaks it?**
KV blocks are identified by a hash chain over their tokens and everything before them; an incoming request reuses all leading blocks that match. Any change early in the prompt changes every later hash, so variable content at the top breaks it.

**What does FlashAttention change, and is it approximate?**
It tiles queries, keys and values through on-chip SRAM and uses an online softmax with running max and denominator, so the `n × n` score matrix never reaches HBM. The result is exact; the gain is memory and IO, not fewer FLOPs.

**How do RoPE scaling methods like YaRN extend context?**
They remap positions or rotation frequencies so long inputs produce angles the model saw in training: YaRN leaves high-frequency dimensions alone, interpolates low-frequency ones, and adjusts attention temperature, usually with a short fine-tune.

**A model passes needle-in-a-haystack at 128k. Is it good at long context?**
Not necessarily. Literal retrieval of one distinctive sentence is easy. Test multi-hop, aggregation, non-literal questions and distractors at your real lengths; RULER-style results often show a much shorter effective length.

## Further reading

- Dao et al., 2022, "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness".
- Ainslie et al., 2023, "GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints".
- Zheng et al., 2024, "SGLang: Efficient Execution of Structured Language Model Programs" (RadixAttention).
- Peng et al., 2023, "YaRN: Efficient Context Window Extension of Large Language Models".
- Hsieh et al., 2024, "RULER: What's the Real Context Size of Your Long-Context Language Models?".
- Liu et al., 2023, "Lost in the Middle: How Language Models Use Long Contexts".
