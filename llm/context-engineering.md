---
title: Context engineering
section: llm
order: 10
phase: "Phase 2 · Prompting and context"
summary: Treat the context window as a budget. What goes in, ordering, lost-in-the-middle, history compression, cache-friendly layout and long context vs RAG.
---

Context engineering is deciding, on every call, which tokens the model sees and in what order. Prompt engineering is about the instructions. Context engineering covers everything else in the window: tool definitions, retrieved documents, conversation history, memory and tool results. For RAG systems and agents it is usually the biggest quality lever. Most failures come down to "the model didn't have the right information" or "it had too much of the wrong information", not "the model isn't smart enough". It is also the biggest cost lever, because every token in the window is paid for on every call.

## The context window as a budget

The window is a hard limit on input plus output tokens (APIs also cap output separately). Three softer limits bite earlier:

- **Quality.** Accuracy degrades as the context fills, well before the hard limit, especially when the task needs several facts spread across the window.
- **Latency.** Prefill time grows with input length (attention is `O(n^2)` in compute over the prompt), so time to first token rises with every document you add. See [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }}).
- **Cost.** Input tokens are billed on every call. In an agent loop the whole history is re-sent each turn, so total input grows quadratically with the number of turns.

What competes for the budget:

| Component | Typical size | Changes | Notes |
|---|---|---|---|
| System prompt | 0.5k-5k | Per release | Static; cache it |
| Tool definitions | 100-500 per tool | Per release or agent | 50 tools can be 10k-20k tokens; load tools selectively |
| Memory, user profile | 0.1k-2k | Per user or session | Retrieve what's relevant, don't dump everything |
| Retrieved documents | 2k-20k | Per request | Biggest lever on RAG answer quality |
| Conversation history | Grows | Per turn | Needs an explicit policy |
| Tool results | 0.1k-50k each | Per step | The usual agent offender; truncate at the source |
| Current message | Small | Per request | Goes last |
| Reasoning tokens | 1k-30k+ | Per request | Count against output on reasoning models |
| Output reservation | `max_tokens` | Per request | Reserve it or the answer is truncated |

## How models use context

- **Lost in the middle.** Liu et al. (2023) placed the one relevant document at different positions among distractors in multi-document QA. Accuracy was highest with the document at the start or end and dropped in the middle: a U-shaped curve. Newer models handle single-fact retrieval ("needle in a haystack") much better, but tasks that combine several facts still degrade with length. RULER (Hsieh et al., 2024) found that the effective context length of many models is well below the advertised one.
- **Distractors hurt more than noise.** Irrelevant documents that look relevant (same product, same entity names) lower accuracy more than unrelated text does. More retrieved chunks raise recall and lower precision, and answer quality usually peaks at a moderate k.
- **Recency.** The end of the prompt gets strong attention. Put the question and key constraints after long material. For very long inputs, restating the critical instruction at the end helps.
- **Context rot.** Performance tends to drift down as a long agent session fills with stale tool output and abandoned plans. Clearing and summarizing restores it.

## Ordering and layout

```text
+---------------------------------------------------+   stable
| tool definitions      (deterministic order)       |     ^
| system prompt         (no timestamps, no ids)     |     |  cached
| static few-shot examples                          |     |  prefix
+---------------------------------------------------+     v
| memory / user profile         (per user)          |
| conversation summary          (rewritten rarely)  |
| recent turns                  (append-only)       |
| retrieved docs for this turn  (best at the edges) |
| current question + restated key constraints       |   volatile
+---------------------------------------------------+
| reserved: reasoning + output tokens               |
+---------------------------------------------------+
```

- **Stable to volatile.** Prompt caches match byte-identical prefixes. A change anywhere invalidates everything after it, so the most stable content goes first.
- **Documents before the question.** Long material at the top of the dynamic section, the question at the bottom. The layout above satisfies both caching and recency.
- **Best documents at the edges.** If you have k reranked documents, put the strongest first and last and the weakest in the middle. The demo below includes this ordering.
- **Label everything.** Each document in its own tag with an id, source and date, so the model can cite and you can trace answers.
- **Remove silent cache breakers.** Timestamps or request ids in the system prompt, tools listed in nondeterministic order, JSON serialized without sorted keys, the user's name in the system prompt. Move per-request values to the end.

## Prompt caching

The provider stores the KV cache (see [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }})) for a prompt prefix. A later request with an identical prefix skips prefill for those tokens. You get cheaper input (cached reads are billed at a fraction of the normal input price, often in the 10% to 50% range as of 2025 to 2026, varying by provider and model) and lower time to first token. Mechanics differ:

- **OpenAI**: automatic for prompts over a minimum length (1,024 tokens). Cached tokens show up in `usage.prompt_tokens_details.cached_tokens`.
- **Anthropic**: explicit `cache_control` breakpoints (up to four per request) or automatic placement. Cache writes cost more than normal input (1.25x for the default 5-minute TTL, 2x for 1 hour) and reads cost much less. Check `usage.cache_read_input_tokens`.
- **Gemini**: implicit caching plus explicit cached-content objects with a TTL.
- **Self-hosted**: vLLM automatic prefix caching, SGLang RadixAttention. See [Serving engines]({{ '/llm/serving-engines/' | relative_url }}).

Check the usage fields in tests and monitoring. A cache that silently stops hitting shows up only as a higher bill. Application-level caches (exact and semantic) are a different layer, covered in [LLM caching]({{ '/llm/llm-caching/' | relative_url }}).

## Managing conversation history

| Strategy | How | Pros | Cons |
|---|---|---|---|
| Full history | Send everything | Lossless | Quadratic cost, eventually overflows |
| Sliding window | Last N turns or last T tokens | Simple, predictable | Forgets early facts (names, constraints) |
| Summary plus window | Old turns become a running summary, recent turns stay verbatim | Keeps the gist, bounded size | Summaries drop details; each rewrite breaks the cache |
| Pinned facts or memory | Extract durable facts to a store, inject the relevant ones | Survives across sessions | Extraction errors persist; needs retrieval |
| Tool-result clearing | Replace old tool outputs with stubs like `[result elided: 14k tokens]` | Large savings in agents | The model must re-call the tool to see it again |
| Compaction | Near the limit, have the model write a structured handoff, then restart from it | Long-running agents | Lossy; quality depends on the handoff prompt |

Practical rules:

- **Trigger early.** Compact at 60% to 80% of the budget, not at the hard limit, so you have room for the summary call and the next turn.
- **Summarize in blocks.** Fold 10 turns at a time so the summary changes rarely and the cached prefix survives between rewrites.
- **Keep pairs intact.** Drop whole turns. A tool result without its tool call, or the reverse, is rejected by most APIs.
- **Pin what must not be lost.** The original task, hard constraints, ids and decisions go in a verbatim "facts" block, not into a summary of a summary.
- **Fix tool results at the source.** Return the top rows plus a handle to fetch more, strip HTML boilerplate, return ids instead of blobs. It's cheaper than trimming later.
- **Use server-side features where they fit.** Several providers now offer context editing (clearing old tool results) and server-side compaction. They save you code but follow the same trade-offs.

Compression beyond summaries: extractive compression keeps only sentences relevant to the query. LLMLingua (Jiang et al., 2023) prunes low-information tokens using a small model's perplexity. Both trade fidelity for size, and numbers and identifiers are the first things to break, so evaluate on your tasks. Durable memory across sessions is covered in [Agent memory]({{ '/llm/agent-memory/' | relative_url }}).

## Long context vs RAG

| | Long context (load it all) | RAG (retrieve top-k) |
|---|---|---|
| Recall | Everything is present | Capped by retriever recall |
| Precision | Distractors included | Focused |
| Cost per query | High; caching helps for a fixed corpus | Low |
| Latency | High time to first token from prefill | Retrieval adds about 10-100 ms; small prefill |
| Scale | Corpus must fit (up to about 1M tokens as of 2025-2026) | Millions of documents |
| Freshness, access control | Re-send per change; whole doc visible | Index updates; per-user filters at retrieval |
| Best for | One long document, cross-references, small static corpora | Large, changing, multi-tenant corpora |

Worked numbers: a 500k-token corpus loaded on every query at $3 per million input tokens costs $1.50 per query. With cached reads at 10%, that's about $0.15. RAG with 8k tokens of retrieved context costs about $0.024. That's 6x cheaper than the cached long-context version and 60x cheaper than uncached. Xu et al. (2023) found that retrieval with a short window could match a much longer window, and that retrieval helped long-context models too.

The common middle path: retrieve to find the few relevant documents, then load those whole documents into a long context instead of small chunks. Measure both on your own evaluation set. See [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}).

## In practice: a token budgeter

An explicit budgeter makes truncation a decision you log instead of an accident. The one below reserves output tokens, always includes the fixed parts, gives memory and documents capped shares, keeps the newest history turns, and replaces dropped turns with a summary. It also computes the agent-loop cost with and without prefix caching. `chars / 4` stands in for a tokenizer here. In production, count with the provider's tokenizer or token-counting endpoint and keep a 5% to 10% margin.

```python
def tokens(text: str) -> int:
    return max(1, len(text) // 4)   # rough English estimate


def fit(items, budget):
    """Keep items in order until the next one does not fit."""
    kept, used = [], 0
    for item in items:
        cost = tokens(item)
        if used + cost > budget:
            break
        kept.append(item)
        used += cost
    return kept, used


def edge_order(ranked):
    """Best docs first and last, weakest in the middle: 1,3,5,4,2."""
    return ranked[0::2] + ranked[1::2][::-1]


def build_context(system, tools, memory, docs, history, question, *,
                  window, reserve_output, summarize, summary_budget=150):
    budget = window - reserve_output
    fixed = tokens(system) + tokens(tools) + tokens(question)
    if fixed > budget:
        raise ValueError("fixed parts exceed the budget: trim system or tools")
    flexible = budget - fixed

    mem, used_mem = fit(memory, flexible * 10 // 100)
    kept_docs, used_docs = fit(docs, flexible * 60 // 100)  # docs in rank order
    left = flexible - used_mem - used_docs

    recent, used_hist = fit(reversed(history), left)        # newest first
    recent.reverse()
    dropped = history[:len(history) - len(recent)]
    summary = []
    if dropped:
        recent, _ = fit(reversed(recent), left - summary_budget)
        recent.reverse()
        dropped = history[:len(history) - len(recent)]
        summary = [summarize(dropped)[: summary_budget * 4]]

    parts = ([tools, system] + mem + summary + recent
             + edge_order(kept_docs) + [question])
    report = {"dropped_turns": len(dropped), "docs_kept": len(kept_docs),
              "docs_dropped": len(docs) - len(kept_docs)}
    return parts, report


def loop_cost(turns, static, per_turn, usd_per_m, read=0.1, write=1.25):
    """Total input cost of an agent loop that re-sends its history."""
    plain = cached = 0.0
    for t in range(1, turns + 1):
        prompt = static + per_turn * t
        new = prompt if t == 1 else per_turn
        plain += prompt
        cached += (prompt - new) * read + new * write
    return plain * usd_per_m / 1e6, cached * usd_per_m / 1e6


if __name__ == "__main__":
    history = [f"turn {i}: " + "x" * 400 for i in range(40)]   # ~100 tok each
    docs = [f"doc rank {r}: " + "y" * 2000 for r in range(1, 9)]  # ~500 each
    parts, report = build_context(
        system="You are a support agent. " * 20, tools="search(q) " * 50,
        memory=["prefers email", "plan: enterprise"], docs=docs,
        history=history, question="Why was I charged twice?",
        window=8000, reserve_output=1500,
        summarize=lambda turns: f"Summary of {len(turns)} earlier turns.")

    assert sum(tokens(p) for p in parts) <= 8000 - 1500
    assert parts[-1] == "Why was I charged twice?"           # question last
    assert history[-1] in parts and history[0] not in parts  # newest kept
    assert any(p.startswith("Summary of") for p in parts)
    assert report["dropped_turns"] > 0 and report["docs_kept"] >= 3
    assert edge_order([1, 2, 3, 4, 5]) == [1, 3, 5, 4, 2]

    plain, cached = loop_cost(turns=20, static=5000, per_turn=2000, usd_per_m=3)
    assert round(plain, 2) == 1.56 and cached < plain / 4
    print(report, f"loop: ${plain:.2f} plain vs ${cached:.2f} cached")
```

The loop numbers are the point. A 20-turn agent with a 5k-token static prefix that adds 2k tokens per turn sends 520k input tokens in total, about $1.56 at $3 per million. With prefix caching (reads at 0.1x, writes at 1.25x) the same loop costs about $0.31. Caching changes the economics of agents more than any prompt tweak does. See [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).

## Pitfalls

- **Stuffing everything in just in case.** Symptom: answer accuracy falls as k rises, and cost rises with it. Fix: tune k on an eval set, rerank, use a relevance threshold (see [Hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})).
- **Silent truncation.** A framework drops the oldest messages or cuts documents without telling you. Symptom: the model "forgets" constraints mid-session. Fix: your own budgeter that logs what it dropped.
- **No output reservation.** Symptom: `finish_reason` of `length`, answers cut mid-sentence, or empty answers from reasoning models that spent the budget thinking. Fix: reserve output plus reasoning tokens explicitly.
- **Cache misses from a dynamic prefix.** Symptom: cached-token counts stay at zero. Fix: diff the rendered prompts of two consecutive requests and move whatever changed to the end.
- **Summary drift.** Summaries of summaries lose ids, numbers and decisions. Fix: a pinned verbatim facts block and block-wise summarization.
- **Orphaned tool messages after trimming.** Symptom: 400 errors from the API after long sessions. Fix: trim whole turns.
- **Tool definition bloat.** Eighty tools cost 25k tokens and lower tool-selection accuracy. Fix: load tools per task, or use a tool-search step.
- **Tokenizer surprises.** `chars / 4` is off for code, JSON and non-English text. Hindi and other non-Latin scripts can take several times more tokens per word. Fix: count with the real tokenizer (see [Tokenization]({{ '/llm/tokenization/' | relative_url }})).
- **Cross-tenant context.** Shared caches or memory stores keyed wrong put one customer's data in another's prompt. Fix: tenant id in every retrieval filter and cache key.

## Rules of thumb

- Budget explicitly: fixed parts, capped shares for memory and documents, history gets the rest, output and reasoning reserved.
- Stable content first, volatile content last, the question at the very end.
- Fewer, better documents beat more documents. Put the best at the edges.
- Compact at 60% to 80% of the window, summarize in blocks, pin critical facts verbatim.
- Truncate tool output at the tool, not after the fact.
- Verify cache hits from the usage fields after every change to prompt assembly.
- Default to RAG for large or changing corpora. Use long context for one big document or a small static set, and measure.

## Interview questions

**What is context engineering, and how is it different from prompt engineering?** Prompt engineering writes the instructions. Context engineering decides what information fills the window on each call (documents, history, memory, tools, tool results), how much of each, and in what order, under quality, latency and cost budgets.

**What is "lost in the middle", and how do you design around it?** Models use information at the start and end of a long context better than in the middle. Retrieve fewer, better documents, put the strongest at the edges, put the question last, and restate key constraints after long material.

**How do you keep a long chat within the window?** A sliding window of recent turns, a running summary of older turns updated in blocks, a pinned block of facts that must survive verbatim, and long-term memory retrieved per turn. Trim whole turns so tool calls and results stay paired.

**How do you lay out a prompt for caching, and what breaks the cache?** Tools and system prompt first, then static examples, then per-user and per-request content, with the question last. Timestamps, request ids, nondeterministic tool ordering, unsorted JSON or any edit to an early message invalidate everything after it.

**Long context or RAG for a 300-page product manual?** About 150k tokens fits in many windows. For a single manual with heavy cross-referencing and low query volume, long context with caching is reasonable. For high query volume, many manuals or frequent updates, use RAG, possibly retrieving whole sections. Decide with an eval set and the cost math.

**Why is agent cost quadratic in turns, and how do you fix it?** Each turn re-sends the whole history, so total input is the sum of a growing prefix. Prefix caching makes the repeated part cheap. Tool-result clearing and compaction keep the prefix from growing without bound.

**How do you budget for a reasoning model?** Reserve the answer plus the expected reasoning tokens (use the p95 from logs, not the mean) out of the window. Set `max_tokens` high enough to cover both, or you get empty answers.

## Further reading

- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (2023)
- Hsieh et al., "RULER: What's the Real Context Size of Your Long-Context Language Models?" (2024)
- Xu et al., "Retrieval meets Long Context Large Language Models" (2023)
- Jiang et al., "LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models" (2023)
- Packer et al., "MemGPT: Towards LLMs as Operating Systems" (2023)
- Prompt caching documentation from OpenAI and Anthropic
