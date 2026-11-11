---
title: Continuous batching and PagedAttention
section: llm
order: 13
summary: Why static batching wastes GPUs, how continuous batching and PagedAttention in vLLM raise throughput, and how prefix caching and chunked prefill work.
---

Decode is memory-bandwidth-bound, so the way to get value from an expensive GPU is to run many sequences at once: one read of the weights serves the whole batch (see [how inference works]({{ '/llm/inference-basics/' | relative_url }})). The hard part is that requests arrive at random times and generate wildly different numbers of tokens. Continuous batching solves the scheduling problem; PagedAttention solves the memory problem. Together they are why engines like vLLM serve several times to an order of magnitude more throughput than naive serving.

## How it works

### Static batching

A naive server collects N requests, runs them together until **all** finish, then takes the next batch.

```text
time ─────────────────────────────────────────►
req A  ████████████████████████████████  (500 tokens)
req B  █████░░░░░░░░░░░░░░░░░░░░░░░░░░░  (done at 80, slot idle)
req C  ██████████░░░░░░░░░░░░░░░░░░░░░░  (done at 150, slot idle)
req D  (arrived just after start: waits for the whole batch)
```

Problems: finished sequences leave idle slots until the longest one completes; new requests wait for the entire batch (terrible TTFT); and memory is reserved for the maximum possible length of each request.

### Continuous batching

Continuous (iteration-level) batching, introduced in the Orca paper, schedules at every decode step instead of per batch. After each step the scheduler removes finished sequences and admits waiting ones.

```text
step:   1  2  3  4  5  6  7  8  9 ...
slot 1  A  A  A  A  A  A  A  A  A
slot 2  B  B  B  E  E  E  E  E  E     (B finished, E admitted at step 4)
slot 3  C  C  C  C  C  F  F  F  F
slot 4  D  D  G  G  G  G  G  G  G
```

The GPU stays full, and a new request waits at most until a slot or memory frees up, not until an entire batch drains. New requests still need a prefill; the scheduler mixes their prefill work with ongoing decode steps.

### The memory problem

Continuous batching needs KV cache memory for every running sequence, and you do not know how long a sequence will grow. Early systems allocated a contiguous buffer per request sized for its maximum length. Most of that memory sat unused (reserved for tokens never generated) or fragmented between allocations; the PagedAttention paper measured that existing systems used only around 20–40% of KV memory for actual tokens.

### PagedAttention

vLLM's PagedAttention borrows virtual memory from operating systems:

- The KV cache is split into fixed-size **blocks** (pages), for example 16 tokens each.
- Each sequence has a **block table** mapping logical token positions to physical blocks, which need not be contiguous.
- Blocks are allocated on demand as a sequence grows. Waste is limited to the last partially filled block per sequence, typically a few percent.
- The attention kernel reads K and V through the block table.

```text
seq A logical blocks:  [0] [1] [2]
block table A:          7   2   9        physical KV blocks on GPU
seq B logical blocks:  [0] [1]          ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐
block table B:          7   4           │  │  │A1│  │B1│  │  │A0│  │A2│
                                        └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘
                       (block 7 shared:  0  1  2  3  4  5  6  7  8  9
                        same prompt prefix, ref-counted)
```

Benefits:

- **Near-zero waste** means more concurrent sequences fit, so batches are larger and throughput is higher.
- **Sharing.** Blocks are reference-counted. Parallel sampling (`n > 1`) and beam search share prompt blocks; copy-on-write duplicates a block only when sequences diverge.
- **Preemption.** When memory runs out, the scheduler can evict a sequence's blocks and later recompute them (or swap them to CPU memory), instead of failing.

### Prefix caching

If many requests share a prefix (the same system prompt, tool definitions, few-shot examples or document), their KV blocks are identical. With automatic prefix caching, vLLM hashes each full block by its tokens and the tokens before it; a new request reuses matching cached blocks and skips prefill for them. Unreferenced cached blocks are kept and evicted LRU when memory is needed.

Effect: TTFT and GPU work drop sharply for long shared prefixes. This is the same mechanism hosted providers expose as prompt caching. It only matches **exact token prefixes** at block granularity: put stable content first and variable content (user question, timestamps, retrieved passages) last.

### Chunked prefill

A 30k-token prefill admitted into a running batch stalls every decoding sequence for that whole pass, causing TPOT spikes for other users. Chunked prefill splits long prefills into chunks and mixes them with decode steps under a per-step token budget (`max_num_batched_tokens`). This smooths inter-token latency at a small cost to the long request's TTFT.

### Beyond one GPU

- **Tensor parallelism** splits each layer across GPUs in a node (needs fast interconnect); used when weights do not fit on one GPU or to cut latency.
- **Pipeline parallelism** splits layers across GPUs or nodes.
- **Disaggregated prefill and decode** runs the two phases on separate GPU pools and transfers KV caches between them, so compute-heavy prefill does not interfere with latency-sensitive decode.
- **Speculative decoding** uses a small draft model (or extra prediction heads) to propose several tokens that the large model verifies in one pass, cutting TPOT when the guess rate is high.

## In practice

### Running vLLM

```bash
vllm serve <model-id> \
  --tensor-parallel-size 1 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 256 \
  --max-num-batched-tokens 8192 \
  --enable-prefix-caching
```

It exposes an OpenAI-compatible HTTP API, so a provider adapter can target it like any hosted endpoint. Recent vLLM versions enable prefix caching and chunked prefill by default; check your version's defaults.

| Flag | Effect |
|---|---|
| `--gpu-memory-utilization` | Fraction of GPU memory vLLM claims; the rest after weights becomes KV cache |
| `--max-model-len` | Max context per request; lower it to fit more sequences and reduce worst-case usage |
| `--max-num-seqs` | Cap on concurrent sequences in a batch |
| `--max-num-batched-tokens` | Token budget per scheduler step; trades TTFT against TPOT smoothness |
| `--kv-cache-dtype fp8` | Quantized KV cache: roughly double capacity, small quality risk |
| `--tensor-parallel-size` | Number of GPUs to shard the model across |

On startup, vLLM logs how many KV cache blocks or tokens it allocated and the maximum concurrency that implies at full context length. Read that line; it is your capacity number.

### Metrics to watch

vLLM exports Prometheus metrics (names vary slightly across versions):

- Running and waiting requests (`vllm:num_requests_running`, `vllm:num_requests_waiting`).
- KV cache usage percentage. Sustained high usage plus a growing waiting queue means you are memory-bound.
- Preemptions. A rising count means sequences are being evicted and recomputed; throughput and latency suffer.
- Prefix cache hit rate.
- TTFT, inter-token latency and E2E latency histograms.

> **In production:** Autoscale on queue depth (waiting requests) and TTFT percentiles, not on GPU utilization. A memory-bound decode server can show high "utilization" while doing poorly.

## Pitfalls

- **Max context set far above need.** Fewer sequences fit, worst-case memory planning is pessimistic. Fix: set `--max-model-len` to what your workload uses.
- **Prefix cache misses from a varying prefix.** A timestamp or request id at the top of the system prompt defeats caching. Fix: stable content first.
- **TPOT spikes from long prompts.** Fix: chunked prefill with a reasonable `max_num_batched_tokens`, or separate pools for long-context traffic.
- **Preemption storms.** Fix: lower `max-num-seqs`, shorter max length, FP8 KV cache, or more GPUs.
- **Load tests with uniform requests.** Real traffic has heavy-tailed prompt and output lengths. Fix: replay a production length distribution.

## Checklist

- Size KV cache for target concurrency × typical context.
- Tune `max-model-len`, `max-num-seqs` and `max-num-batched-tokens` against p95 TTFT and TPOT.
- Order prompts for prefix-cache hits; monitor hit rate.
- Monitor waiting queue, KV usage and preemptions; autoscale on queue and latency.
- Benchmark with realistic length distributions and concurrency.

## Interview angle

- **Why is continuous batching better than static batching?** It schedules per decode step, filling freed slots immediately, so GPUs stay busy and new requests do not wait for a whole batch.
- **What problem does PagedAttention solve?** KV cache fragmentation and over-reservation. Fixed-size blocks with per-sequence block tables give near-zero waste, enable sharing via copy-on-write, and allow preemption.
- **How does prefix caching work?** KV blocks are hashed by their token content plus prefix; requests with identical prefixes reuse cached blocks and skip that prefill.
