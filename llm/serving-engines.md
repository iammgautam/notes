---
title: Serving engines
section: llm
order: 45
phase: "Phase 7 · Inference and serving"
summary: vLLM, SGLang, TGI, TensorRT-LLM, llama.cpp, Ollama and Triton compared, plus the knobs, multi-LoRA, structured output, autoscaling and benchmarking that matter.
---

Self-hosting an open model means choosing an inference engine, and the engine decides most of what you get: throughput per GPU, which models and quantization formats load, whether tool calls and JSON schemas work, and how the server behaves at 3 a.m. under a traffic spike. The good news is that the serious engines converged on the same core (continuous batching, paged KV cache, prefix caching, an OpenAI-compatible API), so a provider-agnostic client can treat a self-hosted model like any hosted one (see [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})). The differences are in performance on your hardware, features at the edges, and operational fit. This note compares the engines, then covers the knobs, multi-LoRA, structured output, autoscaling, cold starts and how to benchmark without fooling yourself.

## What an engine does

```text
client ── HTTP, OpenAI-compatible ──► API server
                                        │ auth, chat template, tokenize,
                                        │ validate, parse tool calls
                                        ▼
                                    scheduler ◄──── KV block manager
                                        │ continuous batch: prefill
                                        │ chunks + decode tokens
                                        ▼
                                  model executor (TP/PP/EP workers)
                                        │ attention + GEMM kernels,
                                        │ CUDA graphs, LoRA, quant
                                        ▼
                                  sampler (+ grammar mask, spec decode)
                                        │
                     detokenize, stream SSE chunks ──► client
```

The features that separate a production engine from a demo: continuous batching and paged KV ([Continuous batching and PagedAttention]({{ '/llm/batching-and-paged-attention/' | relative_url }})), prefix caching and chunked prefill ([KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }})), optimized kernels for your [quantization]({{ '/llm/quantization/' | relative_url }}) format, tensor, pipeline and expert parallelism ([GPUs and parallelism]({{ '/llm/gpus-and-parallelism/' | relative_url }})), [speculative decoding]({{ '/llm/speculative-decoding/' | relative_url }}), structured output, multi-LoRA, Prometheus metrics and health endpoints.

## The engines

| Engine | Strengths | Hardware | Pick it when |
|---|---|---|---|
| vLLM | Broadest model and hardware support, PagedAttention, large community | NVIDIA, AMD, TPU, others via plugins | Default for self-hosted GPU serving |
| SGLang | RadixAttention, fast structured output, strong large-MoE support | NVIDIA, AMD | Agentic, multi-turn, prefix-heavy or schema-heavy traffic |
| TGI | Hugging Face integration, Rust router, early production features | NVIDIA, AMD, others | Already invested in the HF stack |
| TensorRT-LLM | Peak NVIDIA performance, FP8 and FP4 kernels, in-flight batching | NVIDIA only | Large fleets where a few percent pays for the build complexity |
| llama.cpp | GGUF, CPU, Apple silicon and consumer GPUs, tiny footprint | CPU, Metal, CUDA, Vulkan | Local, edge, CPU-only or air-gapped |
| Ollama | One-command model pulls and a simple API over a llama.cpp-class runtime | Same as llama.cpp | Developer laptops and prototypes |
| Triton Inference Server | Multi-framework model server, dynamic batching, ensembles | Mostly NVIDIA | Mixed fleets: LLMs beside embedders, rerankers, classic models |

**vLLM** started as the PagedAttention reference implementation and became the default open engine. A 2025 re-architecture (the V1 engine) simplified the scheduler and made prefix caching and chunked prefill default. It supports most open model families, GPTQ, AWQ, FP8 and other formats, multi-LoRA, speculative decoding and structured outputs. Its weakness is velocity: flags, defaults and metric names change between minor versions.

**SGLang** pairs a fast runtime with RadixAttention, a radix-tree prefix cache that excels when requests share and branch histories, as agent loops and multi-turn chat do. It has strong structured-output performance and is a common choice for very large MoE models with data-parallel attention and expert parallelism. Its OpenAI-compatible server is a drop-in alternative to vLLM's.

**TGI** (Text Generation Inference) is Hugging Face's server: a Rust router for batching and streaming in front of model shards, with Prometheus and OpenTelemetry support early on. Its momentum has slowed relative to vLLM and SGLang as of 2025–2026; check the project's maintenance status before adopting it for new work.

**TensorRT-LLM** is NVIDIA's library of heavily optimized kernels, in-flight batching, paged KV, and FP8 and FP4 paths, typically the fastest option on NVIDIA hardware. The cost is complexity: engine builds or version-specific workflows, NVIDIA-only, and a narrower model list. Recent versions include an OpenAI-compatible `trtllm-serve`; it is also commonly hosted behind Triton.

**llama.cpp** is a C/C++ runtime for GGUF models that runs on CPUs, Apple silicon and consumer GPUs, with partial GPU offload by layer. `llama-server` exposes an OpenAI-compatible API, grammar-constrained output and parallel slots. Excellent for local and edge use; not designed for high-concurrency GPU serving.

**Ollama** wraps a llama.cpp-class runtime with a model registry (`ollama pull`), a Modelfile format and a REST API on port 11434 with OpenAI-compatible `/v1` routes. Ideal for development; watch its default context length (historically a few thousand tokens), which silently truncates long prompts unless you raise `num_ctx`.

**Triton Inference Server** is NVIDIA's general model server: many framework backends (TensorRT-LLM, vLLM, Python, ONNX and others), dynamic batching, model ensembles and metrics. Use it when one platform serves LLMs alongside embedding models, rerankers and classical models. For multi-node, disaggregated LLM serving, NVIDIA's newer Dynamo framework (2025) targets that layer.

## In practice

### OpenAI-compatible endpoints

vLLM, SGLang, TGI, llama.cpp, Ollama and TensorRT-LLM's server all expose `/v1/chat/completions` (and usually `/v1/completions` and `/v1/models`; embedding models add `/v1/embeddings`). Illustrative client:

```python
from openai import OpenAI

client = OpenAI(base_url="http://vllm.internal:8000/v1", api_key="<token>")
resp = client.chat.completions.create(
    model="support-assistant",            # --served-model-name, or a LoRA adapter name
    messages=[{"role": "user", "content": "Summarize ticket 4812"}],
    max_tokens=300,
    temperature=0.2,
    extra_body={"top_k": 40},             # engine-specific sampling params
)
```

Compatibility is good, not perfect:

- **Chat templates** are applied server-side from the tokenizer config. A wrong or missing template silently degrades quality; override it explicitly if the model repo's template is wrong.
- **Tool calling** needs the engine to parse the model's tool-call format, for example vLLM's `--enable-auto-tool-choice --tool-call-parser <parser>`. Without it, tool calls come back as plain text. Reasoning models similarly need a reasoning parser to split thinking from the answer.
- **Defaults differ**: `max_tokens`, stop tokens, usage reporting in streams, and handling of unknown parameters (ignored or rejected).
- **Errors differ**: context-length overflow, queue-full and validation errors map to different status codes and bodies. Normalize them in your provider adapter.

### Key flags and knobs

Illustrative equivalents; names drift between versions, so check `--help` for yours.

| Concept | vLLM | SGLang | TGI | llama.cpp server |
|---|---|---|---|---|
| Max context per request | `--max-model-len` | `--context-length` | `--max-total-tokens` | `-c` (split across slots) |
| GPU memory share | `--gpu-memory-utilization` | `--mem-fraction-static` | – | `-ngl` (layers on GPU) |
| Tensor parallel | `--tensor-parallel-size` | `--tp` | `--num-shard` | – |
| Concurrency cap | `--max-num-seqs` | `--max-running-requests` | `--max-concurrent-requests` | `-np` (parallel slots) |
| Quantization | `--quantization` | `--quantization` | `--quantize` | GGUF type of the file |

```bash
vllm serve <model-id> --served-model-name support-assistant \
  --tensor-parallel-size 2 --max-model-len 32768 \
  --gpu-memory-utilization 0.90 --max-num-seqs 128 \
  --enable-auto-tool-choice --tool-call-parser <parser>

python -m sglang.launch_server --model-path <model-id> \
  --tp 2 --context-length 32768 --mem-fraction-static 0.85 --port 30000

llama-server -m model-Q4_K_M.gguf -c 16384 -ngl 99 -np 4 --port 8080
```

How to choose values:

- **Max context:** the p99 of real prompt plus output length, not the model's maximum. Every extra token of allowed length is worst-case KV you plan for.
- **Memory share:** 0.85–0.95 on a dedicated GPU; lower if anything else (a second process, a monitoring agent's CUDA context) shares it.
- **Tensor parallel:** the smallest degree that fits the weights with enough KV headroom for your target concurrency, within one NVLink domain.
- **Concurrency cap:** from a load sweep, the highest value whose p95 TPOT still meets the SLO.

### Multi-LoRA serving

Fine-tuned LoRA adapters are small: rank 16 on every linear layer of an 8B model is about 42M parameters, roughly 84 MB in BF16, under 1% of the base (see [LoRA and PEFT]({{ '/llm/lora-and-peft/' | relative_url }})). Multi-LoRA serving keeps one copy of the base model and many adapters, and batches requests for different adapters together using kernels that apply each request's adapter within the same batch (the Punica and S-LoRA papers introduced the techniques). One GPU can then serve per-tenant or per-task fine-tunes that would otherwise each need a dedicated replica.

```bash
vllm serve <base-model-id> --enable-lora \
  --lora-modules support=/adapters/support-v3 sql=/adapters/sql-v1 \
  --max-loras 4 --max-lora-rank 16
# clients select an adapter by passing its name as "model"
```

Trade-offs: some throughput overhead versus the bare base model; `--max-loras` bounds how many adapters are active in one batch, and others are swapped in from CPU; adapters must be trained on the same base (and evaluated on the quantized base if you serve one); a rank above `--max-lora-rank` will not load.

### Structured output

Engines enforce JSON schemas, regexes or grammars by masking disallowed tokens at each sampling step. Backends such as XGrammar, llguidance and Outlines compile the schema into a token-level automaton. Through the OpenAI-compatible API you usually pass `response_format` with a JSON schema; engines also accept engine-specific fields for regex and grammar constraints. llama.cpp uses GBNF grammars or a JSON schema, and Ollama a `format` field.

Costs to know: schema compilation adds latency to the first request with a new schema (engines cache compiled grammars); per-token masking adds small overhead; some combinations with speculative decoding or specific samplers are unsupported. Constrained decoding guarantees syntax, not correct content, so keep Pydantic validation and retries on the client (see [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }})).

### Autoscaling and cold starts

Scale on signals that lead user pain: waiting requests, KV cache usage and TTFT percentiles from the engine's Prometheus metrics, exported to Kubernetes HPA or KEDA. GPU utilization is a poor signal, because memory-bound decode reads near 100% at any load.

```python
import math


def desired_replicas(current: int, running: int, waiting: int, kv_usage: float,
                     ttft_p95_s: float, *, per_replica: int = 48, ttft_slo_s: float = 1.0,
                     min_r: int = 1, max_r: int = 16) -> int:
    """running/waiting: totals across replicas. per_replica: concurrency one replica
    sustains within the SLO, measured in a load test."""
    want = math.ceil((running + waiting) / per_replica)
    if ttft_p95_s > ttft_slo_s or kv_usage > 0.9:
        want = max(want, current + 1)              # pressure: add capacity now
    if want < current:                             # scale in one step at a time,
        healthy = ttft_p95_s < 0.5 * ttft_slo_s and kv_usage < 0.6
        want = current - 1 if healthy else current  # and only when clearly healthy
    return max(min_r, min(max_r, want))


if __name__ == "__main__":
    assert desired_replicas(2, running=90, waiting=0, kv_usage=0.5, ttft_p95_s=0.4) == 2
    assert desired_replicas(2, running=96, waiting=60, kv_usage=0.7, ttft_p95_s=0.8) == 4
    assert desired_replicas(2, running=60, waiting=0, kv_usage=0.95, ttft_p95_s=0.5) == 3
    assert desired_replicas(4, running=30, waiting=0, kv_usage=0.3, ttft_p95_s=0.2) == 3
    assert desired_replicas(4, running=30, waiting=0, kv_usage=0.3, ttft_p95_s=0.7) == 4
    assert desired_replicas(16, running=2000, waiting=500, kv_usage=0.95, ttft_p95_s=3) == 16
    print("autoscaling policy checks pass")
```

Scale-in is deliberately slow because a GPU replica's cold start is measured in minutes:

| Phase | Typical cost | Mitigation |
|---|---|---|
| GPU node provisioning | Seconds to many minutes; can fail when capacity is scarce | Warm node pool, reservations |
| Container image pull | Engine images are often 10 GB+ | Pre-pulled or cached images on nodes |
| Weight download | 140 GB for a 70B BF16 model; minutes even at 1 GB/s | Local NVMe cache, fast shared storage, quantized weights |
| Load into GPU memory | Tens of seconds | safetensors, parallel or streaming loaders |
| Warm-up | CUDA graph capture, compilation, autotuning: seconds to minutes | Persisted compile caches, warm-up requests before readiness |

Keep minimum replicas at one or more for interactive traffic, gate Kubernetes readiness on the engine's health endpoint plus a warm-up request, give startup probes minutes rather than seconds, and fall back to a hosted provider while new replicas come up (see [Reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }})). Scale to zero only for development and batch workloads.

### Benchmarking methodology

Most published engine comparisons are not your workload. Run your own:

1. **Workload from production.** Sample prompt and output length distributions, prefix-sharing ratio, sampling parameters and streaming from real logs, not fixed 512-in/128-out requests.
2. **Open loop, not closed loop.** Send requests on a Poisson schedule at a target rate, independent of responses. A closed loop of N workers slows its own arrival rate when the server slows and hides queueing collapse (coordinated omission).
3. **Sweep the rate** from light load past saturation; record TTFT, TPOT and ITL percentiles, E2E, output tokens/s, error rate, KV usage and preemptions at each step.
4. **Report goodput:** the highest rate at which, say, 99% of requests meet both the TTFT and TPOT SLOs.
5. **Control variables:** same model, precision, tokenizer, max context and GPU; warm up first; count tokens with the same tokenizer. Forcing fixed output lengths (ignoring end-of-sequence) is fine for comparing kernels, misleading for capacity planning.

Bundled tools (vLLM's and SGLang's serving benchmarks, NVIDIA GenAI-Perf, guidellm) implement most of this. The harness below shows the mechanics against a fake streaming engine; swap `FakeEngine.stream` for a real streaming call.

```python
import asyncio
import random
import statistics
import time


class FakeEngine:
    """Stand-in for a streaming endpoint: four batch slots, and a decode
    step that slows as the batch fills."""

    def __init__(self, slots: int = 4, step_s: float = 0.001):
        self.slots = asyncio.Semaphore(slots)
        self.active = 0
        self.step_s = step_s

    async def stream(self, prompt_tokens: int, output_tokens: int):
        async with self.slots:
            self.active += 1
            try:
                await asyncio.sleep(self.step_s * prompt_tokens / 200)    # prefill
                for _ in range(output_tokens):
                    await asyncio.sleep(self.step_s * (1 + 0.1 * self.active))
                    yield "tok"
            finally:
                self.active -= 1


async def one_request(engine: FakeEngine, prompt: int, output: int, results: list) -> None:
    t0 = time.perf_counter()
    first, n = None, 0
    async for _ in engine.stream(prompt, output):
        n += 1
        if first is None:
            first = time.perf_counter()
    end = time.perf_counter()
    results.append({"ttft": first - t0, "tpot": (end - first) / max(n - 1, 1)})


async def open_loop(qps: float, n: int, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    engine, results, tasks = FakeEngine(), [], []
    for _ in range(n):
        prompt, output = rng.randint(100, 1000), rng.randint(10, 30)
        tasks.append(asyncio.create_task(one_request(engine, prompt, output, results)))
        await asyncio.sleep(rng.expovariate(qps))     # arrivals ignore response times
    await asyncio.gather(*tasks)
    return results


def summarize(results: list[dict], slo_ttft: float, slo_tpot: float) -> dict:
    def pct(values: list[float], q: int) -> float:
        return statistics.quantiles(values, n=100, method="inclusive")[q - 1]
    ttft = [r["ttft"] for r in results]
    good = sum(r["ttft"] <= slo_ttft and r["tpot"] <= slo_tpot for r in results)
    return {"p50_ttft_ms": round(pct(ttft, 50) * 1e3, 1),
            "p99_ttft_ms": round(pct(ttft, 99) * 1e3, 1),
            "p50_tpot_ms": round(pct([r["tpot"] for r in results], 50) * 1e3, 2),
            "goodput": round(good / len(results), 2)}


if __name__ == "__main__":
    slo = {"slo_ttft": 0.05, "slo_tpot": 0.005}
    light = summarize(asyncio.run(open_loop(qps=25, n=60)), **slo)
    heavy = summarize(asyncio.run(open_loop(qps=400, n=60)), **slo)
    print("25 req/s: ", light)
    print("400 req/s:", heavy)
    assert light["goodput"] > 0.9
    assert heavy["p99_ttft_ms"] > 3 * light["p99_ttft_ms"]
    assert heavy["goodput"] < light["goodput"]
```

At the light rate nearly every request meets the SLO; at the overload rate TPOT barely changes but TTFT explodes from queueing, and goodput collapses. That is the pattern to look for in a real sweep: the knee where queueing starts is your per-replica capacity.

## Pitfalls

- **Wrong chat template or missing tool parser.** Symptom: quality far below the model card, or tool calls arriving as text. Fix: verify the template and parser flags with a smoke test in CI.
- **Unpinned engine versions.** Flags, defaults and metric names change between minor releases. Pin the image digest and rerun your benchmark and eval suite on upgrade.
- **Liveness probes that kill loading pods.** Model loading takes minutes; aggressive probes restart it forever. Use startup probes with generous limits.
- **Proxies that buffer or time out streams.** Load balancers with response buffering or 60-second idle timeouts break SSE streaming and long generations. Disable buffering and raise timeouts on the LLM route.
- **llama.cpp slots splitting context.** `-c 16384 -np 4` gives each slot 4,096 tokens. Size `-c` as per-slot context times slots.
- **Silent truncation.** Ollama's small default context or a low max length on any engine cuts prompts without an obvious error. Log prompt token counts against the limit.
- **Benchmarks that do not match traffic.** Closed-loop, fixed-length or single-tenant tests overstate capacity.

## Checklist

- Pick vLLM or SGLang for GPU serving unless you have a specific reason; llama.cpp or Ollama for local.
- Serve through the OpenAI-compatible API behind your own provider adapter; normalize errors.
- Set max context, memory share, TP degree and concurrency cap from measurements, and read the engine's capacity log.
- Smoke-test chat template, tool calling and structured output on every deploy.
- Autoscale on queue depth, KV usage and TTFT; keep warm capacity for interactive routes.
- Benchmark open-loop with production length distributions; report goodput at your SLO.

## Interview questions

**How would you choose between vLLM, SGLang, TensorRT-LLM and llama.cpp?**
vLLM for broad model and hardware support, SGLang for prefix-heavy agentic or structured-output workloads and large MoE models, TensorRT-LLM when peak NVIDIA performance justifies extra build and ops complexity, llama.cpp for CPU, Apple silicon and local use. Then confirm with a benchmark on your own traffic.

**What does an engine's OpenAI-compatible API not guarantee?**
Identical behavior: chat templates, tool-call parsing, defaults, unsupported parameters and error formats differ by engine and version, so your client should normalize them and CI should smoke-test them.

**How does multi-LoRA serving work and when is it worth it?**
One base model stays in memory; small adapters are loaded alongside and applied per request inside a shared batch with specialized kernels. It is worth it when you have many fine-tunes of the same base with modest traffic each, such as per-tenant or per-task adapters.

**What metrics would you autoscale an LLM deployment on, and why not GPU utilization?**
Waiting requests, KV cache usage and TTFT percentiles, because they rise before users feel pain. GPU utilization stays high for memory-bound decode at any batch size, so it does not track headroom.

**Why are cold starts a big deal for LLM serving, and how do you reduce them?**
Provisioning a GPU node, pulling a large image, downloading tens to hundreds of GB of weights and warming up kernels takes minutes. Keep warm minimum capacity, cache images and weights locally, persist compile caches, and fail over to a hosted API during scale-up.

**What is wrong with a closed-loop benchmark at fixed concurrency?**
When the server slows, the clients send less, so queueing delay never appears in the results. An open-loop Poisson load at fixed rates reveals the knee where TTFT explodes.

## Further reading

- Kwon et al., 2023, "Efficient Memory Management for Large Language Model Serving with PagedAttention" (vLLM).
- Zheng et al., 2024, "SGLang: Efficient Execution of Structured Language Model Programs".
- Sheng et al., 2023, "S-LoRA: Serving Thousands of Concurrent LoRA Adapters".
- Dong et al., 2024, "XGrammar: Flexible and Efficient Structured Generation Engine for Large Language Models".
- vLLM, SGLang and TensorRT-LLM documentation; the llama.cpp server README.
