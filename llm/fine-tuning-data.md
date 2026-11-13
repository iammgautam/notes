---
title: Fine-tuning data
section: llm
order: 33
phase: "Phase 6 · Fine-tuning"
summary: Chat formats and templates, where training data comes from, how much you need, and how to clean, dedup, decontaminate, mask and version it.
---

In fine-tuning, the dataset is the spec. The training code is a few dozen lines that barely change between projects; what the model learns is whatever your examples consistently show, including their mistakes, their length, their tone and their formatting quirks. Most fine-tunes that disappoint do so because of data: inconsistent labels, a train set that does not look like production traffic, a chat template that differs from the one used at serving time, or eval items that leaked into training. This note covers the formats, the sources, the sizes, and the pipeline that turns raw examples into a versioned training artifact.

## Formats

### Prompt-completion

The simplest shape: one input, one output. Good for classification, extraction and rewriting.

```json
{"prompt": "Classify the ticket: My invoice shows two charges for May.", "completion": "billing.duplicate_charge"}
```

Older instruction datasets use an `instruction` / `input` / `output` triple (Alpaca style). It is the same idea and converts directly to either format here.

### Conversational (messages)

The format most hosted fine-tuning APIs and TRL accept: one conversation per JSONL line, as a list of role-tagged messages. Train on the same message structure you will send at inference, including the system prompt.

```json
{"messages": [{"role": "system", "content": "You are the support assistant for Acme. Answer in two sentences or fewer."}, {"role": "user", "content": "Can I change my billing date?"}, {"role": "assistant", "content": "Yes. Go to Settings, then Billing, and pick a new date; the change applies from your next cycle."}]}
```

For tool use, the assistant turn carries `tool_calls`, followed by `tool` results and the final answer. Shown pretty-printed here; in the file it is one line:

```json
{
  "messages": [
    {"role": "user", "content": "Where is order 8812?"},
    {"role": "assistant", "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "get_order", "arguments": "{\"order_id\": \"8812\"}"}
      }
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": "{\"status\": \"shipped\"}"},
    {"role": "assistant", "content": "Order 8812 has shipped."}
  ]
}
```

Include the tool definitions in each example if your training API supports it, so the model learns to choose from the list it is given rather than memorizing one fixed toolset.

### Other shapes

- **Preference pairs** (`prompt`, `chosen`, `rejected`) for DPO-style training: see [preference tuning]({{ '/llm/preference-tuning/' | relative_url }}).
- **Raw text** for continued pretraining: documents, no roles, loss on every token.

### Chat templates

The model never sees JSON. A chat template (a Jinja template stored with the tokenizer) renders the messages into one string with special tokens marking turns. The same conversation in two common templates:

```text
ChatML (used by Qwen and others):
<|im_start|>system
You are Acme support.<|im_end|>
<|im_start|>user
Where is order 8812?<|im_end|>
<|im_start|>assistant
Order 8812 has shipped.<|im_end|>

Llama 3:
<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are Acme support.<|eot_id|><|start_header_id|>user<|end_header_id|>

Where is order 8812?<|eot_id|><|start_header_id|>assistant<|end_header_id|>

Order 8812 has shipped.<|eot_id|>
```

In transformers, `tokenizer.apply_chat_template(messages, tokenize=False)` renders a training example; with `add_generation_prompt=True` it renders a prompt ending in the assistant header, ready for generation. Rules that prevent most template bugs:

- **Fine-tuning an instruct model:** use its existing template, unchanged.
- **Fine-tuning a base model:** you choose a template. If it adds new special tokens, resize the embeddings and make sure the new embedding rows are trained, or the model cannot read or emit them.
- **Train and serve with the identical template.** Serving engines such as vLLM apply the template stored in the tokenizer config; if your training code rendered text by hand, they will disagree. Symptoms: the model prints role headers, does not stop, or quality is mysteriously lower than offline.
- **Watch for double BOS.** Some templates add the beginning-of-sequence token and the tokenizer adds it again. Decode one tokenized training example and read it before every first run.

See [tokenization]({{ '/llm/tokenization/' | relative_url }}) for how special tokens are handled.

## Data sources

| Source | Quality | Cost | Main risks |
|---|---|---|---|
| Production logs | Matches real inputs | Low | PII, consent, training on the model's own mistakes |
| Human-written (experts, annotators) | Highest when guidelines are good | High | Slow, inconsistent between annotators |
| Synthetic from a stronger model | Good, scalable | Medium | Licensing, generator style and errors, low diversity |
| Generated and verified (rejection sampling) | High for verifiable tasks | Medium | Needs a reliable verifier |
| Public datasets | Varies | Free | License, contamination, mismatch with your task |

**Production logs** are the most valuable source because the inputs are real. Select outputs by quality signals: thumbs up, accepted without edits, resolved without escalation. The best examples are where a human corrected the model: the human's final version is the label. Do not train on raw model outputs without filtering, or you teach the model its own errors. Check that your terms of service and privacy commitments allow training on customer data, scrub PII, and sample across tenants and time so one large customer does not define the model's behavior.

**Human-written data** needs written annotation guidelines with worked examples and edge cases. Measure agreement by having two annotators label the same 50 to 100 items; where they disagree, the spec is ambiguous and the model will be too.

**Synthetic data** from a stronger model is how most teams bootstrap ([distillation]({{ '/llm/distillation/' | relative_url }})). Generate inputs that cover your real distribution (seed with production inputs), generate outputs with the strong model and your best prompt, then filter with graders. Two caveats. Several closed-model providers' terms restrict using outputs to build competing models, and some open-weight licenses attach conditions to derivatives; read the current terms and record the generator model in your dataset manifest. And synthetic data inherits the generator's style tics and blind spots, so keep a real-traffic eval set to catch it.

**Generate and verify** works when correctness is checkable (code against tests, JSON against a schema): sample several candidates per input and keep those that pass.

## Quality over quantity

LIMA (Zhou et al., 2023) fine-tuned a 65B model on 1,000 carefully curated examples and got strong instruction following, supporting the idea that a model's knowledge comes from pretraining and fine-tuning mostly teaches format and style. The practical consequence: a few hundred consistent, correct, representative examples beat tens of thousands of noisy ones for behavioral goals. A bad pattern repeated 200 times will be learned faithfully.

A good example is correct, consistent with every other example of the same kind, representative of production inputs, and shows exactly the output you want at inference, including length. If production answers should be two sentences, training answers must be two sentences.

### How much data

Orders of magnitude that hold up in practice, not rules:

| Goal | Typical size |
|---|---|
| Format, tone, style | 50 to 500 examples |
| Narrow classification or extraction | 500 to 5,000 (coverage per class matters more) |
| Tool use and agent trajectories | 1,000 to 10,000, covering every tool and no-tool cases |
| Distilling a broad task to a small model | 10,000 to 100,000+ |
| Continued pretraining | Hundreds of millions to billions of tokens |

Hosted APIs accept as few as ten or so examples, but noticeable gains usually need at least 50 to 100. To find out whether more data will help, train on 25%, 50% and 100% of your set and plot the eval metric. Still climbing: collect more. Flat: the bottleneck is quality, consistency or the spec.

## Cleaning, dedup and decontamination

```text
 sources        clean and filter        split and check      artifact
 prod logs --+                          group-aware split    train.jsonl
 human     --+-> validate, scrub PII -> decontaminate    --> val.jsonl
 synthetic --+   dedup, balance         against eval set     manifest.json
```

**Cleaning** removes: structurally invalid rows; empty or truncated outputs; wrong language; refusals and disclaimers you do not want learned; PII; outputs whose JSON fails your schema; inconsistent formats (dates, casing, units); and examples longer than your max sequence length, because truncation cuts off the answer and teaches the model to stop mid-sentence. Find label errors by training once, then reviewing examples where the model confidently disagrees with the label.

**Deduplication** matters because duplicates overweight one pattern, increase memorization, and leak between train and validation splits. Lee et al. (2021) showed deduplicating pretraining data reduces memorized output and improves models. Do exact dedup on a hash of normalized text, then near-dup detection with word shingles and Jaccard similarity (MinHash with locality-sensitive hashing at scale). Near-duplicate prompts with different answers are label conflicts: resolve them rather than keep both.

**Decontamination** removes training examples that overlap your eval sets. If eval items leak into training, your eval measures memorization and you ship a model that is worse than its scores. Check long n-gram overlap (GPT-3's contamination analysis used 13-grams on long documents; use shorter n-grams for short prompts), embedding similarity for paraphrases, and make sure your synthetic generator was never seeded with eval items.

A runnable validator and cleaner for chat-format JSONL:

```python
import hashlib
import json
import re

ROLES = {"system", "user", "assistant", "tool"}


def validate(line: str, max_tokens: int = 4096) -> list[str]:
    """Return problems for one JSONL line; an empty list means valid."""
    try:
        row = json.loads(line)
    except json.JSONDecodeError as e:
        return [f"invalid JSON: {e.msg}"]
    msgs = row.get("messages") if isinstance(row, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return ["missing or empty 'messages'"]
    if not all(isinstance(m, dict) for m in msgs):
        return ["every message must be an object"]
    errors, roles = [], [m.get("role") for m in msgs]
    for i, (m, role) in enumerate(zip(msgs, roles)):
        if role not in ROLES:
            errors.append(f"msg {i}: bad role {role!r}")
        if role == "system" and i != 0:
            errors.append(f"msg {i}: system message not first")
        content = m.get("content")
        tool_call = role == "assistant" and bool(m.get("tool_calls"))
        if not tool_call and not (isinstance(content, str) and content.strip()):
            errors.append(f"msg {i}: empty content")
        if i and role == roles[i - 1] and role != "tool":
            errors.append(f"msg {i}: two {role} turns in a row")
    if roles[-1] != "assistant":
        errors.append("last message must be from the assistant")
    chars = sum(len(m.get("content") or "") for m in msgs)
    if chars / 4 > max_tokens:  # rough: ~4 characters per English token
        errors.append(f"~{chars // 4} tokens > max {max_tokens}")
    return errors


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return " ".join(re.findall(r"\w+", text.lower()))


def prompt_text(row: dict) -> str:
    # User turns only: a shared system prompt makes every pair look similar.
    return " ".join(m.get("content") or "" for m in row["messages"]
                    if m["role"] == "user")


def fingerprint(row: dict) -> str:
    text = "\n".join(f"{m['role']}:{normalize(m.get('content') or '')}"
                     for m in row["messages"])
    return hashlib.sha256(text.encode()).hexdigest()


def ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = normalize(text).split()
    return {tuple(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def clean(lines, eval_prompts, near_dup=0.7, shingle_n=3, contam_n=8):
    report = dict(invalid=0, exact_dup=0, contaminated=0, near_dup=0)
    eval_grams = set().union(*(ngrams(p, contam_n) for p in eval_prompts))
    kept, seen, kept_shingles = [], set(), []
    for line in lines:
        if validate(line):
            report["invalid"] += 1
            continue
        row = json.loads(line)
        fp, prompt = fingerprint(row), prompt_text(row)
        if fp in seen:
            report["exact_dup"] += 1
            continue
        if ngrams(prompt, contam_n) & eval_grams:
            report["contaminated"] += 1
            continue
        sh = ngrams(prompt, shingle_n)
        # Pairwise scan is O(n^2); switch to MinHash + LSH past ~100k rows.
        if any(jaccard(sh, s) >= near_dup for s in kept_shingles):
            report["near_dup"] += 1
            continue
        seen.add(fp)
        kept_shingles.append(sh)
        kept.append(row)
    return kept, report


def conv(user: str, answer: str, last_role: str = "assistant") -> str:
    msgs = [{"role": "system", "content": "You are Acme support."},
            {"role": "user", "content": user},
            {"role": last_role, "content": answer}]
    return json.dumps({"messages": msgs})


if __name__ == "__main__":
    tool_row = json.dumps({"messages": [
        {"role": "user", "content": "Where is order 8812?"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "shipped"},
        {"role": "assistant", "content": "Order 8812 has shipped."}]})
    lines = [
        conv("How do I reset my password?", "Use the Forgot password link."),
        conv("How do I  RESET my password?", "Use the forgot password link."),
        conv("How do I reset my password, please?", "Click Forgot password."),
        conv("Do you ship to Canada?", "Yes, in 5 to 7 business days."),
        conv("What is your refund window for annual plans bought "
             "during a promotion?", "30 days."),
        conv("Cancel my plan", "Done.", last_role="user"),
        conv("Hello", "   "),
        '{"messages": [',
        tool_row,
    ]
    eval_prompts = ["What is your refund window for annual plans bought "
                    "during a promotion last year?"]
    kept, report = clean(lines, eval_prompts)
    print(report, "kept:", len(kept))
    assert validate(lines[0]) == []
    assert validate(tool_row) == []
    assert "last message must be from the assistant" in validate(lines[5])
    assert any("two user turns" in e for e in validate(lines[5]))
    assert validate(lines[6]) == ["msg 2: empty content"]
    assert validate(lines[7])[0].startswith("invalid JSON")
    assert report == dict(invalid=3, exact_dup=1, contaminated=1, near_dup=1)
    assert len(kept) == 3
```

## Balancing

- **Coverage over uniformity.** Every class, tool, language and input type you expect in production needs enough examples to be learned; rare-but-important cases need deliberate oversampling.
- **Negative examples.** Include cases where the right behavior is no tool call, a clarifying question, an out-of-scope refusal, or "the documents do not say". A model trained only on positive tool calls will call tools for everything.
- **Do not erase the production prior blindly.** If 70% of real tickets are one class and you train on a 50/50 split, the model over-predicts the rare class. Balance for coverage, then check calibration on a set with the production distribution.
- **Cap dominant sources.** Limit examples per tenant, per template and per generator prompt so one source does not define the style.

## Train, validation and test splits

- **Split by group,** not by row: by conversation, customer, source document or generation template. Otherwise near-variants of the same item land in both train and validation, and validation loss looks great while generalization is poor.
- **Validation set** (5 to 10%, a few hundred examples is enough): used during training for loss curves and checkpoint selection.
- **Test set**: your frozen eval suite, never trained on, scored with task metrics rather than loss. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).
- **For production logs, split by time.** Train on older traffic, test on newer, which is what deployment actually looks like.

## Loss masking

During supervised fine-tuning you usually compute loss only on the tokens the model should learn to produce: the assistant turns. Prompt tokens are given at inference, so learning to predict them wastes capacity, and with long prompts and short answers they dominate the loss and drown the signal. In PyTorch the convention is to set those label positions to `-100`, the default `ignore_index` of `CrossEntropyLoss`; Hugging Face causal LM models shift labels by one internally, so `labels` is aligned with `input_ids`.

Two details matter. The end-of-turn token must be inside the trained span, or the model never learns to stop. And in multi-turn data, mask all user and tool turns; train on every assistant turn only if they are all good, otherwise mask earlier assistant turns that came from a weaker model.

```python
IGNORE = -100  # PyTorch CrossEntropyLoss default ignore_index


def render(messages):
    """Toy ChatML-style template: (tokens, is_trainable) per token."""
    tokens, train = [], []
    for m in messages:
        header = ["<|im_start|>", m["role"]]
        body = m["content"].split() + ["<|im_end|>"]
        tokens += header + body
        train += [False] * len(header)
        train += [m["role"] == "assistant"] * len(body)
    return tokens, train


def build(messages, vocab, pad_id=None):
    tokens, train = render(messages)
    ids = [vocab.setdefault(t, len(vocab)) for t in tokens]
    labels = [i if t else IGNORE for i, t in zip(ids, train)]
    if pad_id is not None:  # the classic bug: masking pad when pad == eos
        labels = [IGNORE if i == pad_id else lab for i, lab in zip(ids, labels)]
    return ids, labels


if __name__ == "__main__":
    vocab: dict[str, int] = {}
    chat = [{"role": "user", "content": "where is order 8812"},
            {"role": "assistant", "content": "it shipped today"}]
    ids, labels = build(chat, vocab)
    trained = [t for t, lab in zip(render(chat)[0], labels) if lab != IGNORE]
    print("trained tokens:", trained)
    assert trained == ["it", "shipped", "today", "<|im_end|>"]
    assert labels[:8] == [IGNORE] * 8  # user turn + assistant header
    eos = vocab["<|im_end|>"]
    _, bad = build(chat, vocab, pad_id=eos)
    assert eos in labels and eos not in bad  # model never learns to stop
```

In TRL, completion-only loss is the default for prompt-completion datasets, and conversational datasets can be trained on assistant tokens only when the chat template marks the assistant spans; older versions used a separate collator for this. The option names have changed across releases, so check the version you run and verify by decoding the unmasked tokens of one batch, as the toy above does.

## Data versioning

Treat a dataset like a build artifact: immutable files plus a manifest, with every model checkpoint pointing at the exact dataset that produced it.

```json
{
  "name": "support-assistant-sft",
  "version": "2026-03-v4",
  "train_sha256": "9f2c...",
  "val_sha256": "41ab...",
  "rows": {"train": 4812, "val": 402},
  "sources": {"prod_logs_edited": 2950, "human": 610, "synthetic": 1654},
  "filters": {"invalid": 37, "exact_dup": 212, "near_dup": 96, "contaminated": 14},
  "generator": "record the model name and terms version used",
  "eval_set_sha256": "c07e...",
  "created_from_commit": "a1b2c3d"
}
```

Store files in object storage or a dataset hub with revisions (DVC works too), never overwrite a version, and log the dataset version alongside the training config and code commit. When a fine-tune regresses three months later, this is how you find out which rows changed.

## Pitfalls

| What you see | Cause | Fix |
|---|---|---|
| Model never stops generating | End-of-turn token masked or missing; pad token equals EOS and was masked | Keep EOS in labels; set a distinct pad token |
| Model prints role headers or weird tokens | Template mismatch between training and serving | Render with the tokenizer's template in both places |
| Eval scores great, production mediocre | Contamination or synthetic eval from the same generator | Decontaminate; eval on real traffic |
| Answers cut off mid-sentence | Truncation at max sequence length during training | Drop or split long examples; raise max length |
| Model calls tools for everything | No negative (no-tool) examples | Add clarifying-question and no-tool cases |
| Inconsistent field values | Annotators disagree; guidelines vague | Measure agreement, fix the spec, relabel |

## Checklist

- Format matches how you will call the model, including system prompt and tool definitions.
- Rendered with the model's own chat template; one decoded example read by a human.
- Every example validated, PII scrubbed, deduplicated, decontaminated against eval sets.
- Negative and edge cases present; dominant sources capped.
- Group-aware or time-based split; test set frozen.
- Loss only on assistant tokens, end-of-turn token included.
- Manifest with hashes, counts, sources, generator and filters; checkpoints reference it.

## Interview questions

**How much data do you need to fine-tune?** Depends on the goal: tens to hundreds of examples for format or tone, low thousands for narrow classification or tool use, tens of thousands for distilling a broad task. Run a data-scaling experiment on 25/50/100% subsets to see whether more data or better data is the bottleneck.

**Why mask the prompt tokens in the loss?** The model is given them at inference, so learning to predict them wastes capacity and, with long prompts, swamps the gradient from the answer. Labels for prompt positions are set to `-100` so the loss ignores them; the end-of-turn token stays trained so the model learns to stop.

**What is a chat template and why does it matter?** It renders role-tagged messages into the exact token sequence the model was trained on, with special tokens for turn boundaries. Training and serving must use the identical template; a mismatch causes leaked headers, runaway generation and silent quality loss.

**What are the risks of training on production logs, or on synthetic data?** Logs: privacy and consent, PII memorization, reinforcing the current model's mistakes, over-representing large tenants. Synthetic: license terms on training with outputs, the generator's errors and style tics, low diversity. Filter with graders, prefer human-corrected outputs, and keep a real-traffic eval.

**What is decontamination and how do you do it?** Removing training examples that overlap your evaluation sets, via n-gram overlap, embedding similarity for paraphrases, and keeping eval items away from synthetic generators. Without it, eval scores measure memorization.

**After fine-tuning, the model rambles past the answer. Why?** Usually the end-of-turn or EOS token was masked or missing from the labels, often because pad was set to EOS and pad positions were masked, or a template mismatch at serving. Decode a training batch and check which tokens carry labels.

## Further reading

- Zhou et al., "LIMA: Less Is More for Alignment" (2023).
- Wang et al., "Self-Instruct: Aligning Language Models with Self-Generated Instructions" (2022).
- Lee et al., "Deduplicating Training Data Makes Language Models Better" (2021).
- Brown et al., "Language Models are Few-Shot Learners" (2020), the contamination analysis section.
- Hugging Face transformers documentation on chat templates.
- TRL documentation for SFTTrainer dataset formats.
