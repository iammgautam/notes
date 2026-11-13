---
title: Continuous batching and PagedAttention
section: llm
order: 41
phase: "Phase 7 · Inference and serving"
summary: Static, dynamic and continuous batching, how PagedAttention removes KV fragmentation, chunked prefill, disaggregated serving and the throughput-latency curve.
---

Decode is memory-bandwidth-bound, so the only way to get value from an expensive GPU is to run many sequences at once: one read of the weights serves the whole batch (see [How inference works]({{ '/llm/inference-basics/' | relative_url }})). The hard part is that LLM requests arrive at random times, carry prompts from 50 to 100,000 tokens, and generate an unknown number of output tokens. Continuous batching solves the scheduling problem; PagedAttention solves the memory problem. Together they are why engines like vLLM and SGLang deliver several times, sometimes an order of magnitude, more throughput than a naive server on the same GPU. Their knobs are also the knobs you tune when a self-hosted model is slow or expensive.

## Core ideas

### Static batching

The naive approach collects N requests, runs them together until **all** of them finish, then takes the next N.

```text
time ───────────────────────────────────────────────►
req A  ████████████████████████████████  (500 tokens)
req B  █████░░░░░░░░░░░░░░░░░░░░░░░░░░░  (done at 80, slot idle)
req C  ██████████░░░░░░░░░░░░░░░░░░░░░░  (done at 150, slot idle)
req D  waiting ......................... starts only when A ends
```

Three problems: finished sequences leave their slots idle until the longest one ends; a new request waits for the whole batch to drain, so TTFT is terrible; and memory is reserved for the maximum possible length of every request.

### Dynamic batching

A dynamic batcher waits up to a small delay (a few milliseconds) or until N requests arrive, then runs them as one batch. Classic model servers such as Triton Inference Server do this, and it works well for single-pass models: embeddings, rerankers, classifiers. For generation it is still request-level: once a batch starts, it runs until its longest member finishes, with the same idle-slot and waiting problems as static batching.

### Continuous batching

Continuous (iteration-level) batching, introduced by the Orca paper, makes scheduling decisions at every forward pass instead of once per batch. After each step the scheduler removes finished sequences and admits waiting ones.

```text
step:   1  2  3  4  5  6  7  8  9 ...
slot 1  A  A  A  A  A  A  A  A  A
slot 2  B  B  B  E  E  E  E  E  E     B finished, E admitted at step 4
slot 3  C  C  C  C  C  F  F  F  F
slot 4  D  D  G  G  G  G  G  G  G
```

The GPU stays full, and a new request waits only until a slot and enough memory free up. Orca's second idea, selective batching, makes this possible: the weight matmuls run over all tokens in the step concatenated together (regardless of which sequence they belong to), while attention runs per sequence against its own KV cache. A single step can therefore mix prefill tokens from new requests with decode tokens from running ones.

### The scheduler loop

Every engine implements some version of this loop:

```text
every iteration:
  1. each running sequence needs one token slot, plus a new KV block
     if its last block is full
  2. if free KV blocks run out: preempt a running sequence (lowest
     priority or most recent), free its blocks, requeue it
  3. admit waiting requests in policy order while
       running seqs   < max_num_seqs
       step tokens    < max_num_batched_tokens
       free blocks cover the prompt (or the next prefill chunk)
  4. one forward pass over [decode tokens + prefill tokens]
  5. sample, append tokens, free finished sequences, stream output
```

Policy choices that matter in production:

- **Order.** First-come-first-served is the default. Priority scheduling lets interactive traffic jump ahead of batch jobs; without it, one tenant's backfill job can starve everyone.
- **Preemption mode.** *Recompute* drops the victim's KV blocks and re-runs its prefill later; *swap* copies blocks to CPU memory and back. Recompute is usually cheaper for short sequences because prefill is fast; swap can win for long ones. Either way, preemptions are a sign you are over-admitting.
- **Admission control.** An unbounded waiting queue turns overload into unbounded TTFT. Cap the queue and return 429 (or route elsewhere) instead of letting requests age out behind it.

### Why KV memory is the real constraint

Continuous batching needs KV cache for every running sequence, and you do not know how long a sequence will grow. Early systems allocated one contiguous buffer per request, sized for the maximum length. That wastes memory three ways:

| Waste | Cause |
|---|---|
| Reservation | Space held for tokens that may never be generated |
| Internal fragmentation | Reserved space the request ends up not using |
| External fragmentation | Gaps between variable-size buffers that no new request fits into |

The PagedAttention paper measured that prior systems used only about 20–40% of KV memory for actual token states. Less usable memory means smaller batches, which means lower throughput.

### PagedAttention

vLLM's PagedAttention borrows virtual memory paging from operating systems:

- The KV cache is carved into fixed-size **blocks** (16 tokens by default in vLLM), each holding K and V for those tokens across a layer's KV heads.
- Each sequence has a **block table** mapping its logical blocks to physical blocks, which need not be contiguous.
- Blocks are allocated on demand as the sequence grows. The only waste is the unfilled tail of each sequence's last block: under 16 tokens per sequence, typically a few percent overall.
- Because every block is the same size, there is no external fragmentation: any free block fits any sequence.
- The attention kernel reads K and V by following the block table.

```text
seq A logical blocks:  [0] [1] [2]
block table A:          7   2   9        physical KV blocks on the GPU
seq B logical blocks:  [0] [1]         ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐
block table B:          7   4          │  │  │A1│  │B1│  │  │AB│  │A2│
                                       └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘
block 7 is shared (same prompt prefix,   0  1  2  3  4  5  6  7  8  9
reference count 2)
```

Paging also enables **sharing**. Blocks are reference-counted, so parallel sampling (`n > 1`), beam search and requests with a common prefix point at the same physical blocks. When a sequence must write into a shared, partially filled block, the engine copies that one block first (copy-on-write). This sharing is the foundation of prefix caching, covered in [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }}).

Block size is a trade-off: smaller blocks waste less memory but mean bigger block tables and less efficient kernel memory access. The default is rarely worth changing.

### Chunked prefill

A 30k-token prompt admitted into a running batch makes that step take as long as a 30k-token prefill. Every user decoding in the same batch sees one huge inter-token gap, a "generation stall". Chunked prefill, analyzed in the Sarathi-Serve paper, splits long prefills into chunks and builds each step from all running decodes plus at most a token budget of prefill work.

It also improves utilization. A decode-only step is memory-bound and leaves compute idle; piggybacking a few hundred prefill tokens onto it costs little extra time. The trade-off sits in the chunk budget (`max_num_batched_tokens` in vLLM):

| Budget | Effect |
|---|---|
| Small (e.g. 512–2k tokens) | Smooth ITL for decoding users; long prompts take more steps, so their TTFT rises |
| Large (8k+ tokens) | Fast TTFT for long prompts; bigger ITL spikes for everyone else |

Recent vLLM versions enable chunked prefill by default; check your version.

### Prefill/decode disaggregation

Even with chunking, prefill and decode compete for the same GPUs and have opposite needs: prefill wants compute and benefits from tensor parallelism for TTFT; decode wants memory capacity and bandwidth for large batches. Disaggregated serving (DistServe, Splitwise, and Mooncake's KV-centric design) runs them on separate pools and ships the KV cache from prefill to decode workers.

```text
            ┌───────────────┐   KV cache over   ┌───────────────┐
request ──► │ prefill pool  │ ── NVLink/RDMA ─► │ decode pool   │ ──► tokens
            │ compute-heavy │                   │ memory-heavy  │
            │ sized by TTFT │                   │ sized by TPOT │
            └───────────────┘                   └───────────────┘
```

Benefits: no prefill-decode interference, independent parallelism and hardware per phase, and independent scaling against the TTFT and TPOT SLOs. Costs: the KV transfer and operational complexity. The transfer is cheap for short prompts and significant for long ones: an 8B GQA model's 2k-token prompt is 256 MiB of KV, about 5 ms over a 50 GB/s RDMA link; a 70B model's 32k-token prompt is 10 GiB, about 200 ms. As of 2025–2026, vLLM, SGLang, TensorRT-LLM and orchestration layers such as NVIDIA Dynamo and llm-d support disaggregation at varying maturity. It pays off at scale with strict, separate TTFT and TPOT SLOs; a few replicas are better served by chunked prefill alone.

### The throughput-latency curve

Every serving deployment lives on one curve. As offered load increases, the engine batches more, throughput rises almost linearly and latency creeps up; past the knee, the batch is capped by memory or `max_num_seqs`, requests queue, and latency explodes while throughput flattens.

```text
p95 latency
    ^                                              *
    |                                            *
    |                                         *
SLO + - - - - - - - - - - - - - - - - - - - -*- - - - - - -
    |                                  *     |
    |                      *     *           |
    |  *     *      *                        |
    +----------------------------------------+-----------> load
      throughput rises ~linearly here       knee: run just
                                            left of it
```

Your operating point is the highest load where p95 TTFT and TPOT still meet the SLO. That rate is goodput, and it is the capacity number per replica you plan and autoscale against.

## In practice

### A runnable simulation of batching and paging

The first part compares static and continuous batching on the same heavy-tailed workload. The second implements a toy block allocator with block tables and copy-on-write, and measures waste against max-length reservation.

```python
import random
from collections import deque


def simulate(outputs: list[int], slots: int, continuous: bool) -> dict:
    """All requests arrive at t=0; each step every running sequence emits a token."""
    waiting = deque(enumerate(outputs))
    running: dict[int, int] = {}          # request id -> tokens still to generate
    first_token: dict[int, int] = {}
    step = busy = 0
    while waiting or running:
        if continuous or not running:     # static refills only when the batch is empty
            while waiting and len(running) < slots:
                rid, n = waiting.popleft()
                running[rid] = n
        step += 1
        busy += len(running)
        for rid in list(running):
            first_token.setdefault(rid, step)
            running[rid] -= 1
            if running[rid] == 0:
                del running[rid]
    return {"steps": step,
            "utilization": round(busy / (step * slots), 3),
            "mean_ttft_steps": round(sum(first_token.values()) / len(outputs), 1)}


class BlockAllocator:
    def __init__(self, num_blocks: int, block_size: int):
        self.block_size = block_size
        self.free = list(range(num_blocks))
        self.refs = [0] * num_blocks

    def alloc(self) -> int:
        if not self.free:
            raise MemoryError("out of KV blocks: preempt or queue")
        b = self.free.pop()
        self.refs[b] = 1
        return b

    def release(self, b: int) -> None:
        self.refs[b] -= 1
        if self.refs[b] == 0:
            self.free.append(b)


class Sequence:
    def __init__(self, alloc: BlockAllocator):
        self.alloc, self.table, self.length = alloc, [], 0

    def append_token(self) -> None:
        if self.length % self.alloc.block_size == 0:   # no block yet, or last is full
            self.table.append(self.alloc.alloc())
        elif self.alloc.refs[self.table[-1]] > 1:       # shared partial block
            self.alloc.release(self.table[-1])          # copy-on-write (a real engine
            self.table[-1] = self.alloc.alloc()         # also copies the KV data)
        self.length += 1

    def fork(self) -> "Sequence":
        child = Sequence(self.alloc)
        child.table, child.length = list(self.table), self.length
        for b in self.table:
            self.alloc.refs[b] += 1
        return child

    def free(self) -> None:
        for b in self.table:
            self.alloc.release(b)
        self.table = []


if __name__ == "__main__":
    rng = random.Random(7)
    outputs = [min(1000, int(rng.lognormvariate(4.5, 1.0)) + 1) for _ in range(64)]
    static = simulate(outputs, slots=8, continuous=False)
    cont = simulate(outputs, slots=8, continuous=True)
    print("static:    ", static)
    print("continuous:", cont)
    assert cont["steps"] < static["steps"]
    assert cont["utilization"] > static["utilization"]
    assert cont["mean_ttft_steps"] < static["mean_ttft_steps"]

    alloc = BlockAllocator(num_blocks=10_000, block_size=16)
    seqs = []
    for n in outputs:
        s = Sequence(alloc)
        for _ in range(200 + n):              # 200-token prompt + generated tokens
            s.append_token()
        seqs.append(s)
    tokens = sum(s.length for s in seqs)
    paged = sum(len(s.table) for s in seqs) * 16
    reserved = len(seqs) * 2048               # contiguous: reserve max_model_len each
    print(f"paged waste {100 * (1 - tokens / paged):.1f}%, "
          f"reserve-max waste {100 * (1 - tokens / reserved):.1f}%")
    assert paged - tokens < 16 * len(seqs)    # under one block per sequence
    assert tokens / reserved < 0.5
    for s in seqs:
        s.free()
    assert len(alloc.free) == 10_000

    parent = Sequence(alloc)                  # parallel sampling with n=4
    for _ in range(40):                       # 2 full blocks + 8 tokens in a third
        parent.append_token()
    children = [parent.fork() for _ in range(3)]
    assert 10_000 - len(alloc.free) == 3      # 4 sequences share 3 physical blocks
    for s in [parent, *children]:
        s.append_token()                      # 3 copy-on-writes, last owner writes in place
    used = 10_000 - len(alloc.free)
    print(f"n=4 sampling uses {used} blocks instead of {4 * 3}")
    assert used == 6
```

On this workload continuous batching needs about 40% fewer steps than static batching, raises slot utilization from about 37% to about 61%, and cuts mean time to first token by more than half. Its remaining idle time is the drain at the end, when only the longest requests are left; with a steady stream of arrivals the slots stay full. Paged allocation wastes about 2% of KV slots; reserving the maximum length wastes about 85%. That freed memory becomes extra batch slots.

### Engine knobs

Illustrative vLLM launch; flag names are stable across recent versions but check `vllm serve --help` for yours. Other engines expose the same concepts under different names (see [Serving engines]({{ '/llm/serving-engines/' | relative_url }})).

```bash
vllm serve <model-id> \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 128 \
  --max-num-batched-tokens 4096 \
  --enable-prefix-caching
```

| Knob | Raise it | Lower it |
|---|---|---|
| `--max-num-seqs` | More throughput, higher TPOT | Lower TPOT, less throughput |
| `--max-num-batched-tokens` | Faster long-prompt TTFT, bigger ITL spikes | Smoother ITL, slower long prefills |
| `--max-model-len` | Longer requests allowed | More concurrency, fewer preemptions |
| `--gpu-memory-utilization` | More KV blocks | Headroom for other processes and spikes |

On startup vLLM logs the KV cache size in tokens and the maximum concurrency that implies at full context length (wording varies by version). Read that line: it is your capacity number.

### Metrics to watch

vLLM exports Prometheus metrics (names vary slightly across versions): running and waiting requests (`vllm:num_requests_running`, `vllm:num_requests_waiting`), KV cache usage percentage, preemption count, prefix cache hit rate, and histograms for TTFT, inter-token latency and E2E latency. The diagnostic patterns:

| Pattern | Meaning |
|---|---|
| Waiting queue grows, KV usage near 100% | Memory-bound: need FP8 KV, shorter max length, or more GPUs |
| Waiting queue grows, KV usage moderate | `max_num_seqs` or token budget too low |
| Preemptions climbing | Over-admitting; latency and throughput both suffer |
| ITL p99 far above p50 | Long prefills stalling decodes: lower the chunk budget or isolate long-context traffic |

> **In production:** Autoscale on waiting-queue depth, KV usage and TTFT percentiles, not on GPU utilization. A memory-bound decode server reports high utilization while it is falling behind.

## Pitfalls

- **Head-of-line blocking from long prompts.** Symptom: ITL p99 spikes coincide with large prompts arriving. Fix: chunked prefill with a moderate budget, or a separate pool for long-context traffic.
- **Preemption storms.** Symptom: preemption counter rising, throughput dropping as load rises. Fix: lower `max-num-seqs`, shorter `max-model-len`, FP8 KV cache, or more replicas.
- **Unbounded queues.** Symptom: TTFT of tens of seconds under load while throughput is flat. Fix: cap queue length, shed or reroute load, alert on waiting requests.
- **`max-model-len` far above need.** Worst-case planning and fewer concurrent sequences. Set it from the p99 of real prompt plus output length.
- **Uniform load tests.** Real traffic has heavy-tailed prompt and output lengths, which is exactly what stresses the scheduler. Replay a production length distribution.
- **One tenant starving others.** A bulk job floods the FCFS queue. Use priority scheduling, per-tenant rate limits, or separate deployments.
- **Expecting bit-exact outputs.** Kernels reduce in different orders depending on batch shape, so the same prompt at temperature 0 can produce different text depending on what else is in the batch. Do not build tests that assume bit-exact reproducibility from a shared server.

## Rules of thumb

- Continuous batching plus paged KV is table stakes; any engine without them is for local use only.
- Size KV for target concurrency times typical context, then confirm with the engine's startup log.
- Tune `max-num-seqs` and the prefill token budget against p95 TTFT and TPOT, not peak throughput.
- Treat preemptions and a growing queue as capacity alarms.
- Reach for disaggregation only when chunked prefill cannot meet separate TTFT and TPOT SLOs at your scale.

## Interview questions

**Static vs dynamic vs continuous batching?**
Static runs a fixed batch until every member finishes. Dynamic forms batches from requests that arrive within a short window but still runs each batch to completion, which suits single-pass models. Continuous schedules at every forward pass, removing finished sequences and admitting new ones immediately, which suits generation.

**What problem does PagedAttention solve, and what waste remains?**
Contiguous per-request KV buffers waste memory through reservation and internal and external fragmentation. Fixed-size blocks with per-sequence block tables allocate on demand, eliminate external fragmentation, and leave only the partially filled last block per sequence.

**How does copy-on-write help parallel sampling and beam search?**
All candidates share the prompt's physical blocks via reference counts. Only when a candidate writes into a shared, partially filled block is that single block copied, so memory grows with divergence rather than with the number of candidates.

**What is chunked prefill and what does it trade?**
It splits long prompts into chunks processed alongside ongoing decodes under a per-step token budget. It removes generation stalls and uses idle compute in decode steps, at the cost of slightly higher TTFT for long prompts.

**When would you disaggregate prefill and decode?**
At scale, when TTFT and TPOT SLOs conflict on shared GPUs and you can afford fast interconnect for KV transfer. It lets each phase use its own parallelism, hardware and autoscaling.

**Recompute or swap on preemption?**
Recompute discards KV and re-runs prefill, which is cheap for short sequences because prefill is compute-efficient. Swap copies KV to CPU over PCIe and back, which can win for very long sequences. Frequent preemption of either kind means the engine is over-admitting.

**Why can the same prompt at temperature 0 give different outputs on a shared server?**
Batch composition changes kernel shapes and reduction order, and floating-point addition is not associative, so logits differ in low-order bits and can flip a near-tie.

## Further reading

- Yu et al., 2022, "Orca: A Distributed Serving System for Transformer-Based Generative Models" (OSDI).
- Kwon et al., 2023, "Efficient Memory Management for Large Language Model Serving with PagedAttention" (SOSP).
- Agrawal et al., 2024, "Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve".
- Zhong et al., 2024, "DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving".
- Patel et al., 2024, "Splitwise: Efficient Generative LLM Inference Using Phase Splitting".
- vLLM documentation: scheduler, engine arguments and metrics.
