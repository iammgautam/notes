---
title: RAG architecture
section: llm
order: 6
summary: The end-to-end shape of a production RAG system, from ingest pipeline to retrieval, prompt assembly, citations and multi-tenant isolation.
---

Retrieval-augmented generation answers questions from your data by retrieving relevant passages and putting them in the prompt. The LLM call is the easy part. Most of the work, and most of the failures, live in ingest (getting clean, chunked, permissioned text into an index) and retrieval (getting the right passages back for this user). A RAG system is a search engine with an LLM bolted to the output.

## How it works

### The two pipelines

```text
                         INGEST (async, background workers)
 ┌────────┐   ┌─────────┐   ┌───────┐   ┌───────┐   ┌──────────────────┐
 │ source │──►│ extract │──►│ clean │──►│ chunk │──►│ embed + FTS index│
 │ upload,│   │ PDF/HTML│   │ dedupe│   │ + meta│   │ (Postgres/pgvec) │
 │ sync   │   │ OCR     │   │       │   │ + ACL │   └────────┬─────────┘
 └────────┘   └─────────┘   └───────┘   └───────┘            │
                                                             │
                         QUERY (sync, on the request path)   │
 ┌──────┐   ┌──────────┐   ┌───────────────┐   ┌────────┐    │
 │ user │──►│ rewrite/ │──►│ hybrid search │◄──┤ filters│◄───┘
 │ query│   │ embed    │   │ vector + FTS  │   │ tenant,│
 └──────┘   └──────────┘   └──────┬────────┘   │ ACL    │
                                  ▼            └────────┘
                           ┌────────────┐   ┌──────────┐   ┌──────────┐
                           │  rerank    │──►│ assemble │──►│   LLM    │
                           │ top 50→8   │   │ prompt   │   │ + cites  │
                           └────────────┘   └──────────┘   └────┬─────┘
                                                                ▼
                                                    validate citations,
                                                    stream to client, log
```

### Ingest

- **Extract:** PDF, HTML, DOCX, slides, tickets, wiki pages. Extraction quality caps answer quality. Keep page numbers and section structure.
- **Clean:** strip boilerplate, normalize whitespace, dedupe by content hash.
- **Chunk:** structure-aware splitting with heading paths (see [chunking]({{ '/llm/chunking/' | relative_url }})).
- **Attach metadata:** `tenant_id`, document id, ACL groups, source URL, timestamps, language.
- **Index:** embeddings for semantic search plus a `tsvector` for keyword search.
- **Make it incremental and idempotent:** a re-sync of an unchanged document should do nothing. Content hashes per document and per chunk make this cheap. Deletions in the source must propagate (cascade from `documents`).

Run ingest as background jobs with retries, a per-document status (`pending`, `processing`, `indexed`, `failed`) and an error column. Users upload a file and expect to query it within seconds to minutes; show status honestly.

### Retrieval

1. **Query understanding (optional):** rewrite conversational follow-ups into standalone queries ("what about for enterprise?" becomes "rate limits for the enterprise plan"). One cheap LLM call; large win for chat.
2. **Candidate generation:** hybrid search, vector plus full-text, fused (see [hybrid search]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})). Fetch generously: 30–100 candidates.
3. **Security filtering:** tenant and ACL predicates applied *in the query*, never after.
4. **Rerank:** a cross-encoder narrows to the best 5–10.
5. **Expand:** fetch neighboring chunks or the parent section for each hit if needed.

### Prompt assembly

Order the prompt so stable parts come first (system instructions, format rules) and variable parts last (retrieved passages, then the question). This helps both model attention and provider-side prompt caching.

```text
[system]   Role, rules, citation format, "say you don't know" policy
[user]     <sources>
             <source id="S1" title="Refund policy" page="3">...</source>
             <source id="S2" title="Billing FAQ">...</source>
           </sources>
           Question: Can I get a refund on an annual plan after 45 days?
```

Give each source a short stable id. Tell the model to cite ids inline (`[S1]`) and to answer only from sources, saying so when they do not contain the answer.

### Citations

Citations are how users trust answers and how you debug them. Make them checkable:

- Require ids from the provided set; reject or strip citations to ids you did not send.
- Map ids back to document, page and URL server-side; never let the model produce URLs.
- Optionally verify that a cited passage supports the sentence (a cheap entailment check or an LLM judge on a sample).

## In practice

### Retrieval query with tenant isolation

```sql
-- $1 query embedding, $2 tenant_id, $3 user's ACL group ids
WITH vec AS (
    SELECT c.id, row_number() OVER (ORDER BY e.embedding <=> $1) AS rnk
    FROM chunk_embeddings e
    JOIN chunks c ON c.id = e.chunk_id
    WHERE e.tenant_id = $2
      AND c.acl_groups && $3
    ORDER BY e.embedding <=> $1
    LIMIT 50
)
SELECT c.id, c.document_id, c.heading_path, c.page_from, c.text, vec.rnk
FROM vec JOIN chunks c USING (id)
ORDER BY vec.rnk;
```

### Prompt assembly with a token budget

```python
def assemble(question: str, hits: list[Hit], budget_tokens: int, count) -> tuple[str, dict]:
    sources, id_map, used = [], {}, 0
    for i, h in enumerate(hits, start=1):
        sid = f"S{i}"
        block = (f'<source id="{sid}" title="{h.title}" page="{h.page}">\n'
                 f"{h.text}\n</source>")
        cost = count(block)
        if used + cost > budget_tokens:
            break                       # hits are already sorted by rerank score
        sources.append(block)
        id_map[sid] = h.chunk_id
        used += cost
    user = "<sources>\n" + "\n".join(sources) + f"\n</sources>\n\nQuestion: {question}"
    return user, id_map
```

Then after generation, parse `[S\d+]` citations, drop any not in `id_map`, and resolve the rest to links.

### Multi-tenant isolation

Leaking one customer's document into another customer's answer is the worst RAG bug. Defense in depth:

| Layer | Control |
|---|---|
| Data | `tenant_id` on every chunk and embedding row; never nullable |
| Query | Filter by tenant in the same SQL statement as the vector search |
| Database | Row-level security with `current_setting('app.tenant_id')` as a backstop |
| Index | Partition by tenant for large tenants (also helps filtered ANN recall) |
| Cache | Tenant id in every cache key (query embeddings, retrieval results, answers) |
| Logs and evals | Do not mix tenants' content in shared golden sets without consent |

```sql
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
    USING (tenant_id = current_setting('app.tenant_id')::bigint);
```

> **In production:** Permission changes must reach the index. If a user loses access to a document, their next query must not retrieve it. Store ACLs on chunks and update them on permission-change events, or check document access at query time against the source of truth.

## Pitfalls

- **Filtering after retrieval.** Top-k is computed globally, then other tenants' rows are dropped: you get few results and, if you forget the filter once, a data leak. Fix: filter inside the retrieval query; RLS as a backstop.
- **Stale index.** Edited or deleted documents keep answering. Fix: incremental sync with content hashes and cascading deletes.
- **Stuffing too much context.** More passages raise cost and latency and can lower accuracy. Fix: rerank and keep the top 5–10.
- **No "I don't know" path.** The model answers from its training data when retrieval misses. Fix: instruct it to refuse without sources, and measure that behavior.
- **Model-generated URLs.** Hallucinated or attacker-controlled links. Fix: resolve citation ids to URLs server-side.
- **Debugging only the final answer.** Fix: log retrieved chunk ids, scores and the assembled prompt hash per request, so you can tell retrieval failures from generation failures.

## Checklist

- Ingest is idempotent, incremental, and propagates deletes and ACL changes.
- Tenant and ACL filters are in the retrieval query, with RLS behind them.
- Hybrid retrieval, then rerank, then a token-budgeted prompt.
- Stable source ids; citations validated and resolved server-side.
- Explicit refusal policy when sources do not contain the answer.
- Per-request logs of retrieved ids and scores for debugging and evals.

## Interview angle

- **Design a RAG system for a multi-tenant SaaS.** Walk the diagram: async ingest with status, hybrid retrieval filtered by tenant and ACL, rerank, budgeted prompt, citations, RLS and tenant-scoped caches, evals on retrieval and faithfulness.
- **RAG vs fine-tuning?** RAG for knowledge that changes or must be cited and permissioned; fine-tuning for behavior, format or style. They combine.
- **How do you debug a wrong answer?** Check whether the right chunk was retrieved (retrieval failure) or retrieved but misused (generation failure); fix the stage that failed.
