---
title: Fine-tuning in practice
section: llm
order: 39
phase: "Phase 6 · Fine-tuning"
summary: "An end-to-end fine-tuning runbook: eval set first, prompting baseline, data, method and tools, GPU cost, regression checks, deployment and retraining."
---

Fine-tuning projects rarely fail at the training step: the script is a few dozen lines and a LoRA run often costs single-digit dollars. They fail earlier (no success metric or eval set, so nobody can say whether the model is better) or later (nobody planned serving, monitoring or the next retrain). This note is the runbook that ties the previous notes together: [when to fine-tune]({{ '/llm/when-to-fine-tune/' | relative_url }}), [data]({{ '/llm/fine-tuning-data/' | relative_url }}), [SFT]({{ '/llm/supervised-fine-tuning/' | relative_url }}), [LoRA]({{ '/llm/lora-and-peft/' | relative_url }}), [preference tuning]({{ '/llm/preference-tuning/' | relative_url }}) and [distillation]({{ '/llm/distillation/' | relative_url }}). It ends with a worked plan for a realistic task.

## The runbook at a glance

```text
 1 define success metric + build eval set    <- before anything else
 2 baseline: best prompt (+ RAG) on strong and small models
        |  gate: is the gap worth closing with training?
 3 prepare data (real inputs, labels, splits, decontamination)
 4 choose base model + method (LoRA / QLoRA / full / DPO / API)
 5 pick tooling, size GPUs, estimate cost
 6 train + track experiments (config, data hash, seed, metrics)
 7 evaluate: task metric, format, general skills, safety, latency
        |  gate: beats baseline on the metric that matters?
 8 deploy: merge or serve adapter, quantize, shadow, canary
 9 monitor + retrain on triggers  ----------------> back to 3
```

Two gates matter. Stop after step 2 if prompting already meets the bar. Stop after step 7 if the fine-tuned model does not beat the baseline by enough to justify owning a model.

## Step 1: define success and build the eval set first

Write down the ship/no-ship metric and threshold before you look at training data: field-level exact match for extraction, macro-F1 and critical-class recall for classification, a human-calibrated judge score for generation. Add operational constraints: p95 latency, cost per 1k requests, whether output must always parse.

Then build the eval set:

- 200–1,000 real examples is enough to detect meaningful differences for most narrow tasks. Labeled by humans, or labeled by a strong model and corrected by humans.
- Stratified to cover every category, plus the hard and rare cases you care about.
- Frozen and versioned. Never train on it, never generate synthetic data from it.
- Held out by entity or time (different customers or a later month than training data), so you measure generalization, not memorization.

See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}) for scoring methods.

## Step 2: baseline with prompting

Run the eval set against your best prompt (few-shot, retrieval if relevant) on a frontier model and on the small model you would fine-tune. That gives the ceiling, the starting point and the gap training has to close, and the frontier prompt doubles as your labeling prompt if you distill. If the small model plus a good prompt plus [structured outputs]({{ '/llm/structured-outputs/' | relative_url }}) already meets the threshold, you are done.

## Step 3: prepare data

- **Inputs from production**, scrubbed of PII, sampled to match the real distribution, with rare classes upsampled.
- **Labels** from humans, from logs (resolved tickets, accepted edits), or from a teacher model with verification. Measure label error on a sample.
- **Format** as chat messages with the exact system prompt and template you will use at inference. Train on assistant tokens only.
- **Size**: a few hundred to a few thousand high-quality examples for format and extraction tasks; more for broad behavior changes. Quality beats volume.
- **Splits**: train and a small validation split for early stopping, separate from the frozen eval set. Deduplicate and decontaminate against the eval set.
- **Version** the dataset (content hash in the run config).

## Step 4: choose the base model and method

| Method | Use when | Memory for an ~8B model | Notes |
|---|---|---|---|
| Prompting + structured output | Baseline meets the bar | None | Always try first |
| LoRA (BF16 base) | Default for task adaptation | ~16 GB weights + activations; one 24–48 GB GPU | Small adapters, fast iteration |
| QLoRA (4-bit base) | GPU-poor, larger models | ~5–6 GB weights + activations; one 16–24 GB GPU | Slower per step; slight quality cost |
| Full fine-tune | Large datasets, big behavior shifts | ~16 bytes/param with AdamW: ~130 GB + activations; multi-GPU with FSDP or ZeRO | Highest ceiling, highest risk of forgetting |
| DPO / KTO after SFT | Choosing between acceptable outputs | As LoRA | Needs preference data |
| Provider fine-tuning API | Want a hosted model, no GPUs | None | Limited models and knobs; data leaves your network |

Base model: the smallest instruct model in a family whose license, context length and language coverage fit. Try two sizes (for example ~3B and ~8B) on a short run; the eval decides.

## Step 5: pick tooling

| Tool | What it is | Good for |
|---|---|---|
| Hugging Face TRL | Trainers for SFT, DPO, KTO, ORPO, GRPO and more on top of transformers | The standard library; full control in Python |
| PEFT | LoRA, QLoRA and other adapter methods; merging | Used by TRL and most other tools |
| Axolotl | YAML-configured training over the HF stack, multi-GPU support | Reproducible configs, teams that prefer config over code |
| Unsloth | Optimized kernels for fast, low-memory LoRA/QLoRA | Single-GPU runs, notebooks, cheap iteration |
| Provider fine-tuning APIs | Upload JSONL, get a hosted model ID | No infrastructure; OpenAI, Google Vertex AI and Amazon Bedrock offer it for some models (check current docs) |

An Axolotl config captures a whole run in one reviewable file (illustrative; keys vary by version):

```yaml
base_model: Qwen/Qwen2.5-3B-Instruct
adapter: lora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_linear: true
datasets:
  - path: data/tickets_train.jsonl
    type: chat_template
val_set_size: 0.05
sequence_len: 2048
sample_packing: true
micro_batch_size: 8
gradient_accumulation_steps: 4
num_epochs: 3
learning_rate: 0.0002
lr_scheduler: cosine
warmup_ratio: 0.03
bf16: auto
output_dir: ./out/ticket-extractor-lora
```

## Step 6: size the GPU and estimate cost

Training time is roughly total training tokens divided by throughput. Two ways to get throughput: measure it on a 50-step run (best), or estimate from FLOPs. A forward pass costs about `2 x params` FLOPs per token and the backward about twice that, so full fine-tuning is about `6 x N x D` for `N` parameters and `D` tokens. LoRA skips weight gradients for the frozen base, so it is closer to `4 x N x D`. Real GPUs reach 30–50% of peak (model FLOPs utilization, MFU) on training workloads.

```python
from dataclasses import dataclass

# Dense BF16 tensor-core peak, vendor spec sheets (TFLOPS).
PEAK_BF16_TFLOPS = {"A100": 312, "H100": 989}


@dataclass
class Run:
    examples: int
    tokens_per_example: int    # prompt + completion, after templating
    epochs: float
    params_b: float            # base model size, billions
    method: str = "lora"       # "lora" or "full"

    @property
    def train_tokens(self) -> float:
        return self.examples * self.tokens_per_example * self.epochs


def hours_from_throughput(run: Run, tok_per_s_per_gpu: float, gpus: int = 1) -> float:
    """Use a throughput you measured on a short run of the real config."""
    return run.train_tokens / (tok_per_s_per_gpu * gpus) / 3600


def hours_from_flops(run: Run, gpu: str, gpus: int = 1, mfu: float = 0.35) -> float:
    """Optimistic estimate: ~6ND FLOPs for full FT, ~4ND for LoRA."""
    flops_per_token = (6 if run.method == "full" else 4) * run.params_b * 1e9
    peak = PEAK_BF16_TFLOPS[gpu] * 1e12 * mfu * gpus
    return flops_per_token * run.train_tokens / peak / 3600


def cost(hours: float, gpus: int, usd_per_gpu_hour: float) -> float:
    return hours * gpus * usd_per_gpu_hour


if __name__ == "__main__":
    # 6,000 tickets x 700 tokens x 3 epochs, LoRA on a 3B model.
    run = Run(examples=6_000, tokens_per_example=700, epochs=3, params_b=3)
    assert run.train_tokens == 12_600_000

    theory = hours_from_flops(run, "A100")
    measured = hours_from_throughput(run, tok_per_s_per_gpu=5_000)  # assumed
    assert theory < measured            # real runs rarely hit the FLOPs bound
    price = 2.0                          # assumed $/GPU-hour; use your quote
    print(f"LoRA 3B: {theory:.2f} h (FLOPs) / {measured:.2f} h (measured), "
          f"~${cost(measured, 1, price):.2f} per run, "
          f"~${cost(measured, 1, price) * 10:.0f} for a 10-run sweep")

    # Full fine-tune of a 70B model on 1B tokens, 8 x H100.
    big = Run(examples=1_000_000, tokens_per_example=1_000, epochs=1,
              params_b=70, method="full")
    h = hours_from_flops(big, "H100", gpus=8, mfu=0.4)
    assert h > 10 * theory
    assert abs(hours_from_flops(big, "H100", gpus=16, mfu=0.4) - h / 2) < 1e-9
    assert hours_from_flops(Run(6_000, 700, 3, 3, "full"), "A100") > theory
    print(f"Full FT 70B on 1B tokens: {h:.0f} h on 8 GPUs = "
          f"{h * 8:.0f} GPU-hours, ~${cost(h, 8, 3.0):,.0f} at an assumed $3/h")
```

The 3B LoRA run takes under an hour and a few dollars. For task-specific LoRA, compute is a rounding error next to labeling, teacher API calls, evaluation and engineering time. Budget for 10–30 runs, not one. Large full fine-tunes are where GPU cost becomes real money, and they also need multi-GPU parallelism (see [GPUs and parallelism]({{ '/llm/gpus-and-parallelism/' | relative_url }})).

## Step 7: train and track experiments

The core training script with TRL and PEFT is short (illustrative; argument names shift between TRL versions):

```python
from datasets import load_dataset
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer

data = load_dataset("json", data_files={"train": "tickets_train.jsonl",
                                        "validation": "tickets_val.jsonl"})
trainer = SFTTrainer(
    model="Qwen/Qwen2.5-3B-Instruct",
    train_dataset=data["train"],
    eval_dataset=data["validation"],
    peft_config=LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05,
                           target_modules="all-linear", task_type="CAUSAL_LM"),
    args=SFTConfig(
        output_dir="out/ticket-extractor-lora",
        num_train_epochs=3,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        per_device_train_batch_size=8,
        gradient_accumulation_steps=4,
        bf16=True,
        eval_strategy="steps",
        eval_steps=50,
        logging_steps=10,
        report_to="wandb",          # or "mlflow"
        seed=42,
    ),
)
trainer.train()
trainer.save_model()
```

Starting hyperparameters that usually work: LoRA rank 8–32 with alpha equal to or twice the rank on all linear layers, learning rate 1e-4 to 2e-4 for LoRA (1e-5 to 2e-5 for full fine-tuning), 1–3 epochs, cosine schedule with a short warmup, effective batch size 16–64. Change one thing at a time.

Track every run in Weights & Biases or MLflow with: base model and revision, dataset hash, full config, seed, git commit, training and validation loss, and the eval-set metrics from step 1. Validation loss going up while training loss falls is overfitting; pick the checkpoint by eval metric, not by the last step.

## Step 8: evaluate, including regressions

Run the frozen eval set on the fine-tuned model with the exact inference prompt, decoding settings and constrained decoding you will use in production. Then check what fine-tuning might have broken:

| Check | How | Why |
|---|---|---|
| Task metric vs baselines | Same eval set, same scorer | The ship decision |
| Format validity | Parse rate, schema validation | Should be ~100% with constrained decoding |
| Slices | Per category, length, language | Averages hide tail regressions |
| General capability | Small fixed set of general prompts or a benchmark subset (for example IFEval for instruction following) | Catastrophic forgetting |
| Safety | Fixed set of harmful and borderline prompts; refusal and over-refusal rates | Fine-tuning, even on benign data, can erode safety training |
| Latency and cost | Load test on the serving stack | The reason you fine-tuned |

Ship only if it beats the prompting baseline on the step 1 metric without unacceptable regressions.

## Step 9: deploy

| | Merged weights | Base + adapter |
|---|---|---|
| How | `merge_and_unload()` folds LoRA into the base | Serve base once, load adapters per request |
| Latency | Same as the base model | Small overhead per adapter matmul |
| Multi-task or multi-tenant | One deployment per model | Many adapters share one base on one GPU |
| Quantization | Quantize the merged model | Base quantized, adapter in higher precision |
| Rollback | Swap model | Swap adapter name |

Merge when you have one model and want maximum speed (illustrative):

```python
import torch
from peft import AutoPeftModelForCausalLM

model = AutoPeftModelForCausalLM.from_pretrained(
    "out/ticket-extractor-lora", torch_dtype=torch.bfloat16)
model.merge_and_unload().save_pretrained("out/ticket-extractor-merged")
# also save the tokenizer next to it
```

**Quantize after fine-tuning**, not before: merge the adapter into the BF16 base (also for QLoRA-trained adapters), then quantize the merged model with AWQ, GPTQ or FP8, and re-run the eval, because quantization can cost more accuracy on a narrow fine-tuned task than on general benchmarks. See [quantization]({{ '/llm/quantization/' | relative_url }}).

Serve with vLLM, either variant (see [serving engines]({{ '/llm/serving-engines/' | relative_url }})):

```bash
# merged model
vllm serve ./out/ticket-extractor-merged --max-model-len 4096

# base model + LoRA adapters, selected by the "model" field per request
vllm serve Qwen/Qwen2.5-3B-Instruct --enable-lora \
  --lora-modules ticket-extractor=./out/ticket-extractor-lora \
  --max-lora-rank 16
```

Keep JSON-schema constrained decoding on even if the model learned the format; it turns a rare malformed output into a valid one. Roll out via shadow traffic, then a canary, with the prompting baseline as the fallback.

## Step 10: monitor and retrain

- **Log** inputs, outputs, model version, latency and validation failures, with trace IDs ([observability]({{ '/llm/observability-and-tracing/' | relative_url }})).
- **Sample for review**: a weekly human-labeled batch gives a live accuracy number; disagreements become new training and eval examples.
- **Watch for drift**: new categories, products, input lengths or languages. A sudden shift in the predicted-label distribution means the world changed or the model broke.
- **Retrain on triggers**, not a calendar: live accuracy below threshold, a new category or schema field, enough new corrected examples to matter, or a better base model release. Because steps 3–8 are scripted and versioned, a retrain is a pipeline run plus an eval review. See [online evaluation]({{ '/llm/online-evaluation/' | relative_url }}) and the [LLMOps lifecycle]({{ '/llm/llmops-lifecycle/' | relative_url }}).

## Worked example: extracting fields from support tickets

**Task.** Every incoming ticket must become JSON with `category` (6 values), `priority` (p1–p3), `product_area`, `order_id` (or null), `sentiment` and `needs_human`. Volume is about 50k tickets a day. The current pipeline calls a frontier model with a long prompt.

**Success criteria (illustrative targets).** Macro-F1 on `category` at least 0.92 and p1 recall at least 0.95, 100% schema-valid output, p95 latency under 400 ms, cost per ticket at least 5x lower than today.

**Plan.**

| Step | Concrete decision |
|---|---|
| Eval set | 600 tickets from the last month, stratified by category, held out by customer, labeled by two support leads with disagreements adjudicated |
| Baseline | Frontier model with current prompt; ~3B instruct model with the same prompt and few-shot examples; both with JSON-schema constrained output |
| Data | 6,000 tickets from earlier months, labeled by the frontier model, filtered by schema validation and self-consistency, 10% reviewed by humans; p1 and rare categories upsampled |
| Model and method | ~3B instruct model, LoRA r=16 on all linear layers; ~8B as a comparison if the 3B misses the bar |
| Tooling | TRL + PEFT on one rented 48–80 GB GPU; runs tracked in W&B |
| Cost | A few dollars per run; the main cost is the teacher labeling pass and two people-days of review |
| Evaluation | Category F1 and p1 recall by slice, field-level exact match, schema validity, safety and general-instruction spot checks, load test |
| Deploy | Merged weights, FP8 or AWQ, served by vLLM with JSON-schema constrained decoding; shadow for a week, then 10% canary |
| Fallback | Low-confidence or unparseable tickets routed to the frontier-model path |
| Monitor | 200 tickets a week human-reviewed; retrain when F1 drops 2 points, a category is added, or 1,000 new corrections accumulate |

One training record, in the chat format the trainer expects:

```json
{"messages": [{"role": "system", "content": "Extract ticket fields as JSON matching the schema."}, {"role": "user", "content": "Subject: Charged twice\n\nI was billed twice for order A-1932 this morning. Please fix this today."}, {"role": "assistant", "content": "{\"category\": \"billing\", \"priority\": \"p2\", \"product_area\": \"payments\", \"order_id\": \"A-1932\", \"sentiment\": \"negative\", \"needs_human\": false}"}]}
```

## Pitfalls

- **No eval set until after training.** You end up judging by vibes and cherry-picked examples. Build it first and freeze it.
- **Train/inference prompt mismatch.** A different system prompt or chat template at serving time quietly drops accuracy. Ship the template with the model.
- **Training on the eval set**, directly or through synthetic data generated from it. Decontaminate and split by entity or time.
- **Overfitting.** Validation loss rises after epoch one or two; outputs become rigid. Fewer epochs, lower learning rate, more diverse data.
- **Catastrophic forgetting and safety erosion.** The model aces the task and gets worse at following other instructions or refusing harmful requests. Keep regression sets in the eval suite.
- **Quantizing without re-evaluating.** The merged FP16 model passed; the AWQ build lost 3 points on rare classes. Evaluate the artifact you actually serve.
- **No owner for retraining.** The model degrades as the product changes. Assign triggers and an owner before launch.

## Checklist

- Success metric, threshold and operational constraints written down.
- Frozen, versioned eval set of real data, held out by entity or time.
- Prompting baseline on strong and small models recorded.
- Data versioned, decontaminated, formatted with the inference template.
- Cost estimated from a measured short run; sweep budget approved.
- Every run tracked with config, data hash, base revision and eval metrics.
- Regression checks: format, slices, general skills, safety, latency.
- Served artifact (merged and quantized) evaluated, with constrained decoding on.
- Shadow, canary and fallback route in place; monitoring and retrain triggers owned.

## Interview questions

**Walk me through a fine-tuning project end to end.** Define the metric and build a frozen eval set, baseline with prompting, prepare and version data, choose the base model and method (usually LoRA), estimate cost, train with experiment tracking, evaluate the task plus regressions, deploy merged or as an adapter with quantization and constrained decoding, then monitor and retrain on triggers.

**Why build the eval set before the training set?** Without it you cannot tell whether fine-tuning helped, and building it later risks contamination with training data and choosing metrics that flatter the result.

**How do you estimate training cost?** Total tokens (examples x tokens x epochs) divided by throughput, which you measure on a short run or estimate from about `6ND` FLOPs (full) or `4ND` (LoRA) at 30–50% MFU. Multiply by GPU-hours price and by the number of runs in your sweep.

**Merged weights or adapters in production?** Merge for a single model and lowest latency. Serve adapters on a shared base when you have many tasks or tenants, want cheap rollbacks, or iterate often; vLLM serves multiple LoRA adapters on one base model.

**What can fine-tuning break, and how do you check?** General instruction following, safety behavior and calibration on out-of-domain inputs. Keep fixed general and safety sets in the eval suite and compare against the base model.

**Why quantize after fine-tuning rather than before?** Training on the quantized base and then quantizing again compounds error; the standard path is to merge the adapter into a high-precision base, quantize once, and re-evaluate the exact artifact you serve.

## Further reading

- "LoRA: Low-Rank Adaptation of Large Language Models", Hu et al., 2021.
- "QLoRA: Efficient Finetuning of Quantized LLMs", Dettmers et al., 2023.
- "LIMA: Less Is More for Alignment", Zhou et al., 2023.
- "Fine-tuning Aligned Language Models Compromises Safety, Even When Users Do Not Intend To!", Qi et al., 2023.
- Hugging Face TRL and PEFT documentation: SFTTrainer, LoRA configuration and adapter merging.
- vLLM documentation: LoRA adapters and structured outputs.
