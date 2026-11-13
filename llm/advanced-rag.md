---
title: Advanced RAG patterns
section: llm
order: 20
phase: "Phase 3 · Retrieval"
summary: Contextual retrieval, parent-child, RAPTOR, GraphRAG, agentic and corrective RAG, multimodal and SQL RAG, caching, and which pattern fixes which failure.
---

A well-built baseline (structure-aware chunks, hybrid search, a reranker with a threshold, a budgeted prompt with citations) answers most questions over most corpora. Advanced patterns exist because some failures survive that baseline: chunks that make no sense out of context, questions that need a whole document, answers that sit in a chart or a database, questions that need three lookups. Each pattern fixes one class of failure and adds indexing cost, latency or operational complexity. Diagnose first with the failure taxonomy in [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}), then pick from the decision table at the end of this note.

## Core ideas

There are four places to intervene:

| Where | Patterns | Pays at |
|---|---|---|
| What you index | Contextual retrieval, parent-child, RAPTOR, knowledge graphs, page images | Ingest time, per document |
| How you search | Agentic, iterative and corrective RAG | Query time, per request |
| What you search | Text-to-SQL over structured data, multimodal indexes | Both |
| What you reuse | Embedding, retrieval and answer caches | Saves cost, risks staleness |

Index-time patterns are usually the better trade: you pay once per document instead of once per query, and query latency stays flat.

## Index-time patterns

### Contextual retrieval

A chunk like "Revenue grew 3% over the previous quarter" does not say which company or quarter. Embedded alone, it matches every revenue question weakly. Contextual retrieval has an LLM write a short context (50–100 tokens) for each chunk, given the whole document, and prepends it before both embedding and keyword indexing.

```text
<document>
...full document text...
</document>
Here is a chunk from the document:
<chunk>
...chunk text...
</chunk>
Write one or two sentences situating this chunk within the document
to improve search retrieval. Answer with only the context.
```

Anthropic's 2024 write-up reported that contextual embeddings cut top-20 retrieval failures by roughly a third, adding contextual BM25 by about half, and adding a reranker on top by about two thirds. Your numbers will differ; the direction has held up widely.

Cost is one LLM call per chunk with the whole document in the prompt. Prompt caching makes it affordable: the document prefix is cached once and each chunk call pays mostly for cached reads. Cheaper alternatives, in order:

1. **Deterministic prefixes**: document title, heading path, product and date prepended to every chunk. Free, and often most of the gain for well-structured docs. See [Chunking]({{ '/llm/chunking/' | relative_url }}).
2. **Late chunking** (Günther et al. 2024): run a long-context embedding model over the whole document, then pool token embeddings per chunk span, so each chunk vector has seen its surroundings.
3. **LLM-written context** as above, for messy corpora (reports, transcripts, contracts) where structure does not carry the context.

Version the enrichment (model and prompt) per row, as you version embeddings.

### Parent-child and sentence-window retrieval

Small units embed precisely; large units give the model enough to answer. Decouple the two: index small children, return something bigger.

- **Sentence window**: index sentences; return each hit with ±k neighboring sentences from the same section.
- **Parent-child**: index 100–200 token children; return their parent section (1–2K tokens), deduplicated.
- **Auto-merging**: if several children of one parent hit, return the parent instead of the fragments.
- **Multi-vector**: index a summary or hypothetical questions per chunk; return the original chunk.

Rerank the children (cross-encoders truncate long input), then expand. Watch the token budget: five parents of 2,000 tokens is a 10,000-token prompt.

```python
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    id: int
    parent: int  # section index
    text: str


def build(sections: list[str]) -> list[Unit]:
    units = []
    for pid, section in enumerate(sections):
        for sent in re.split(r"(?<=[.!?])\s+", section.strip()):
            units.append(Unit(len(units), pid, sent))
    return units


def search(units: list[Unit], query: str, k: int = 3) -> list[int]:
    """Stand-in for vector search over small units (sentences)."""
    q = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = [(len(q & set(re.findall(r"[a-z0-9]+", u.text.lower()))), u.id)
              for u in units]
    return [i for s, i in sorted(scored, key=lambda p: -p[0])[:k] if s > 0]


def sentence_window(units, hits, w=1) -> list[str]:
    """Expand each hit by w sentences inside its section, merge overlaps."""
    keep = {j for h in hits for j in range(h - w, h + w + 1)
            if 0 <= j < len(units) and units[j].parent == units[h].parent}
    passages, run = [], []
    for j in sorted(keep):
        if run and (j != run[-1] + 1 or units[j].parent != units[run[-1]].parent):
            passages.append(run)
            run = []
        run.append(j)
    if run:
        passages.append(run)
    return [" ".join(units[j].text for j in p) for p in passages]


def parent_child(units, sections, hits, max_parents=2) -> list[str]:
    """Match on children, return their parent sections, best first, once."""
    parents = list(dict.fromkeys(units[h].parent for h in hits))
    return [sections[p] for p in parents[:max_parents]]


if __name__ == "__main__":
    sections = [
        "Refunds. Annual plans are refundable for 30 days. "
        "Refunds go to the original card. Processing takes 5 to 10 days.",
        "Seats. Admins can add seats at any time. Removed seats are "
        "credited at the next renewal.",
    ]
    units = build(sections)
    hits = search(units, "how long does refund processing take", k=1)
    assert units[hits[0]].text.startswith("Processing")
    window = sentence_window(units, hits, w=1)
    # Neighbors come along, but never across the section boundary.
    assert window == ["Refunds go to the original card. "
                      "Processing takes 5 to 10 days."]
    assert parent_child(units, sections, hits) == [sections[0]]
    # Two hits in the same section collapse into one parent, not two copies.
    both = search(units, "refunds annual card", k=2)
    assert len(parent_child(units, sections, both)) == 1
    print("ok")
```

### RAPTOR: hierarchical summaries

Some questions are about a whole document or corpus: "What are the recurring themes in these incident reports?" No single chunk answers them. RAPTOR (Sarthi et al. 2024) builds a tree: cluster chunk embeddings, have an LLM summarize each cluster, embed the summaries, and repeat on the summaries until a few top nodes remain.

```text
 level 2              [ summary of summaries ]
                       /                  \
 level 1      [ summary A ]           [ summary B ]
               /    |    \               /     \
 level 0     c1    c2    c3            c4      c5   (original chunks)

 Index every node; retrieve across all levels at once.
```

The paper found that searching all nodes together (the "collapsed tree") beat walking the tree top-down. Broad questions match summaries; specific ones still match leaves. Costs: summarization calls across the corpus, and incremental updates are awkward because a new document can change clusters. Fits large, slow-changing documents (reports, books, regulations), not fast-moving ticket streams.

### GraphRAG and knowledge graphs

GraphRAG (Edge et al. 2024) uses an LLM to extract entities and relationships from every chunk into a graph, detects communities of related entities (Leiden algorithm, hierarchically), and has the LLM summarize each community. Two query modes:

- **Global search**: map-reduce over community summaries to answer corpus-wide questions ("main risks across all vendor contracts").
- **Local search**: find entities named in the query, then gather their neighbors, relationships and source chunks.

When it is worth it: questions about relationships across documents, multi-hop questions over entities (people, systems, companies), and sensemaking over a fixed corpus. When it is not: lookup questions (hybrid search wins at a fraction of the cost), fast-changing data, tight budgets. Indexing costs several LLM calls per chunk, often orders of magnitude more than embedding, and entity resolution ("Acme Corp", "ACME", "Acme Inc.") is a real data-quality problem.

Before extracting a graph with an LLM, check whether you already have one: your relational schema (customer → tickets → product → owning team) is a precise, free knowledge graph. Tag chunks with entity ids at ingest and expand with a recursive CTE:

```sql
-- Chunks linked to entities within 2 hops of the one named in the query.
WITH RECURSIVE hop(entity_id, depth) AS (
    SELECT id, 0 FROM entities WHERE tenant_id = $1 AND name = $2
    UNION
    SELECT e.dst, h.depth + 1
    FROM hop h JOIN edges e ON e.src = h.entity_id
    WHERE h.depth < 2 AND e.tenant_id = $1
)
SELECT DISTINCT c.id, c.text
FROM hop JOIN chunk_entities ce USING (entity_id)
JOIN chunks c ON c.id = ce.chunk_id;
```

## Query-time patterns

### Agentic RAG

Instead of one fixed retrieve-then-generate pass, the model gets retrieval as a tool and decides whether to search, with what query and filters, against which source, and whether the results suffice. It can combine tools (docs search, ticket search, SQL) and iterate. See [Tool calling]({{ '/llm/tool-calling/' | relative_url }}) and [Agents]({{ '/llm/agents/' | relative_url }}).

Ideas from research, usable as prompted or programmatic checks:

- **Iterative retrieval**: retrieve, draft, find gaps, retrieve again. FLARE (Jiang et al. 2023) triggers retrieval when the next sentence would contain low-confidence tokens.
- **Self-RAG** (Asai et al. 2023): a model trained to emit reflection tokens deciding whether to retrieve, whether a passage is relevant, and whether its output is supported. Without training the model, the same checks become grader calls.
- **Corrective RAG** (CRAG, Yan et al. 2024): a lightweight evaluator grades retrieved documents as correct, incorrect or ambiguous; correct results are refined to their relevant parts, incorrect ones trigger a fallback source such as web search, ambiguous ones use both.

A production shape is a small state machine with hard limits:

```text
 query ─► retrieve ─► grade ─relevant─► generate ─► grounded? ─yes─► answer
             ▲          │                               │
             │          │ weak or empty                 │ no
             │          ▼                               ▼
             └── rewrite query (max 2)    regenerate once, else refuse
```

Costs: 2–10× the LLM calls of single-shot RAG, seconds of extra latency, less predictable behavior, harder evaluation (see [Agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }})). Use it for genuinely multi-step questions and many sources, behind a classifier that sends simple lookups down the single-shot path, as in [Query transformation and routing]({{ '/llm/query-transformation/' | relative_url }}). Cap steps, tokens and wall time per request, and trace every tool call.

### Multimodal RAG

Answers often live in charts, scanned pages, slides, diagrams and tables.

| Approach | How | Strengths | Weaknesses |
|---|---|---|---|
| Convert to text at ingest | OCR and layout parsing; tables to Markdown; figures captioned by a vision model | Reuses the text pipeline and hybrid search | Loses layout; captions can hallucinate |
| Shared image-text embeddings | CLIP-style model embeds images and queries into one space | Simple image search | Weak on dense text pages, charts |
| Page-image retrieval | ColPali-style: a vision-language model embeds each page screenshot as patch vectors; late-interaction scoring against query tokens | No OCR pipeline; strong on visually rich documents | Many vectors per page; answering needs a VLM on page images |

ColPali (Faysse et al. 2024) applies ColBERT's MaxSim to image patches, so query terms match regions of the page directly. The retrieved pages go to a vision-capable model as images, which costs far more input tokens than text chunks. Tables deserve special care in every approach: keep them whole, repeat header rows when splitting, and if users ask numeric questions about them, load them into a database and use SQL. See [Document ingestion]({{ '/llm/document-ingestion/' | relative_url }}) and [Multimodal models]({{ '/llm/multimodal-models/' | relative_url }}).

### Structured data: text-to-SQL with guardrails

"How many refunds did we issue last month?" is an aggregation, not a retrieval problem; vector search over rows cannot count. Route these to text-to-SQL:

1. **Schema linking**: retrieve the relevant tables and columns, with descriptions and example values (schema RAG), instead of pasting the whole schema.
2. **Few-shot examples**: retrieve similar question-to-SQL pairs from a curated set.
3. **Generate** one `SELECT`, then **validate and execute** under guardrails.
4. **Answer** from the result rows and show the SQL for transparency.

The guardrails are the part that cannot be skipped. Generated SQL is untrusted code:

- A read-only role on a replica, granted only on curated views with clear names.
- Tenant scoping enforced by the database (RLS, tenant-scoped views), never by the prompt.
- Allowlisted tables, columns and functions; one statement; no DDL, DML, `COPY` or extensions.
- Statement timeout, row cap, and a cost check (`EXPLAIN`) for big tables.
- PII columns excluded from the views entirely.

```sql
-- Illustrative Postgres setup. On PostgreSQL 15+, security_invoker makes
-- the view apply the caller's RLS policies instead of the view owner's.
CREATE VIEW analytics.orders_v WITH (security_invoker = true) AS
    SELECT id, created_at, total, status FROM orders;
GRANT SELECT ON analytics.orders_v TO llm_reader;

BEGIN READ ONLY;
SET LOCAL ROLE llm_reader;
SET LOCAL app.tenant_id = '42';          -- read by RLS on orders
SET LOCAL statement_timeout = '5s';
-- run the generated SELECT here, fetch at most N rows
ROLLBACK;
```

The same layers, runnable with SQLite from the standard library: a tenant-scoped view, an authorizer callback that denies everything except reads through that view and allowlisted functions, a per-query execution budget and a row cap.

```python
import sqlite3

EXPOSED = {"orders_v"}  # tenant-scoped views the model may query
FUNCS = {"count", "sum", "avg", "min", "max", "round", "lower", "upper",
         "date", "strftime", "coalesce"}
MAX_ROWS, MAX_STEPS = 100, 5_000


def open_tenant_db(conn: sqlite3.Connection, tenant_id: int):
    # Scope first (as the app), then lock the connection down.
    # Views cannot take bound parameters in SQLite, hence the int() cast.
    conn.execute("CREATE TEMP VIEW orders_v AS SELECT id, created, total, "
                 f"status FROM orders WHERE tenant_id = {int(tenant_id)}")

    def authorizer(action, arg1, arg2, db, via_view):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and (arg1 in EXPOSED
                                              or via_view in EXPOSED):
            return sqlite3.SQLITE_OK  # the view itself, or reads it makes
        if action == sqlite3.SQLITE_FUNCTION and arg2.lower() in FUNCS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY  # writes, PRAGMA, ATTACH, base tables...

    conn.set_authorizer(authorizer)
    return conn


def run_generated_sql(conn, sql: str) -> list[tuple]:
    calls = 0

    def budget():  # abort runaway queries (cross joins) deterministically
        nonlocal calls
        calls += 1
        return calls > MAX_STEPS  # truthy return interrupts the query

    conn.set_progress_handler(budget, 1_000)  # every 1,000 VM steps
    rows = conn.execute(sql).fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise ValueError("too many rows: ask the model to aggregate")
    return rows


if __name__ == "__main__":
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, tenant_id INTEGER,
                             created TEXT, total REAL, status TEXT);
        WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n
                                WHERE i < 300)
        INSERT INTO orders (tenant_id, created, total, status)
        SELECT 1 + i % 2, date('2025-01-01', '+' || i || ' days'), i,
               CASE WHEN i % 10 = 0 THEN 'refunded' ELSE 'paid' END FROM n;
    """)
    db = open_tenant_db(conn, tenant_id=1)

    ok = run_generated_sql(db, "SELECT status, count(*) FROM orders_v "
                               "GROUP BY status ORDER BY status")
    assert ok == [("paid", 120), ("refunded", 30)]  # tenant 1's rows only

    for bad in ["SELECT * FROM orders",                 # base table
                "DELETE FROM orders",                   # write
                "SELECT load_extension('x')",           # unlisted function
                "PRAGMA table_info(orders)",
                "ATTACH DATABASE 'other.db' AS o"]:
        try:
            run_generated_sql(db, bad)
            raise AssertionError(f"allowed: {bad}")
        except sqlite3.DatabaseError as e:
            assert "authorized" in str(e) or "prohibited" in str(e), e

    try:
        run_generated_sql(db, "SELECT 1; DROP TABLE orders")
        raise AssertionError("allowed stacked statements")
    except sqlite3.ProgrammingError:
        pass  # execute() runs exactly one statement

    for heavy in ["SELECT count(*) FROM orders_v a, orders_v b, orders_v c",
                  "SELECT * FROM orders_v"]:
        try:
            run_generated_sql(db, heavy)
            raise AssertionError(f"allowed: {heavy}")
        except (sqlite3.OperationalError, ValueError):
            pass  # interrupted by the step budget, or over the row cap
    print("ok")
```

Accuracy on realistic schemas is well below toy benchmarks (compare Spider and the harder BIRD benchmark). Curated views with business-friendly names and documented columns usually help more than a bigger model.

### Caching retrievals

| Layer | Key | Invalidate on | Risk |
|---|---|---|---|
| Query embedding | Embedding model version + normalized text | Model change | Low |
| Retrieval results | Tenant, ACL fingerprint, normalized query, filters, index version | Index version bump on ingest; TTL | Stale or leaked results if key is incomplete |
| Rerank scores | Reranker version + query + chunk id + chunk hash | Model or chunk change | Low |
| Final answers (exact or semantic) | Tenant, ACL fingerprint, query, index version | Any ingest; short TTL | Wrong answer for a similar-looking question |

Bump a per-tenant index version whenever ingest commits, and put it in every retrieval cache key; that gives exact invalidation without tracking which queries a document affects. Semantic answer caches are the dangerous layer: "refund policy for Pro" and "refund policy for Team" embed almost identically and have different answers. Use a high similarity threshold, scope by tenant and ACL, and prefer caching retrieval results over final answers. See [LLM caching]({{ '/llm/llm-caching/' | relative_url }}) and [Caching]({{ '/hld/caching/' | relative_url }}).

## Which pattern for which failure

| Failure or symptom | Try first | Then | Main cost |
|---|---|---|---|
| Chunks ambiguous out of context ("the company", "this quarter") | Title and heading prefixes | Contextual retrieval, late chunking | LLM call per chunk at ingest |
| Right chunk found, answer needs surrounding text | Sentence window, parent-child | Larger chunks, long context | Prompt tokens |
| Questions about a whole document or corpus | RAPTOR-style summaries | GraphRAG global search | Heavy indexing |
| Relationships and multi-hop across entities | Decomposition, iterative retrieval | Knowledge graph (existing schema first) | Extraction pipeline |
| Retrieval sometimes returns junk | Rerank threshold, grade passages | Corrective fallback source | An extra LLM call |
| Questions need several lookups or sources | Routing plus bounded agentic RAG | Multi-agent research flow | Latency, cost, eval effort |
| Answers in charts, scans, slides | OCR plus vision captions at ingest | ColPali-style page retrieval | Vectors per page, VLM tokens |
| Counts, sums, filters over records | Text-to-SQL with guardrails | Semantic layer, curated views | Safety engineering |
| Repeated queries cost too much | Embedding and retrieval caches | Semantic answer cache | Staleness, leakage risk |

## Pitfalls

- **Pattern before diagnosis.** GraphRAG will not fix a retrieval miss caused by bad PDF extraction. Measure per-stage recall first.
- **Unversioned enrichment.** Contextual prefixes, summaries and graphs are model outputs; when the model or prompt changes, old and new rows disagree. Store versions and re-run as a backfill.
- **Unbounded agent loops.** Symptom: rare requests costing 50× the median. Fix: step, token and time limits, plus per-request cost in traces.
- **Views that bypass RLS.** A Postgres view runs with its owner's privileges by default; use `security_invoker` on 15+, or scope in the view definition.
- **Incomplete cache keys.** Missing ACL fingerprint or index version means leaks or stale answers. Test cache isolation like retrieval isolation.
- **End-to-end-only evaluation.** Fix: evaluate each added stage by ablation on the golden set; see [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

## Rules of thumb

- Get the baseline right first: chunking, hybrid search, reranking, citations.
- Prefer index-time fixes to query-time loops; they cost once, not per request.
- Deterministic context (titles, headings, dates) before LLM-written context.
- Use the graph you already have (your schema) before extracting one.
- Every loop has a budget; every generated query runs with least privilege.
- Cache retrieval results with tenant, ACL and index version in the key.

## Interview questions

**What is contextual retrieval and why does it work?** An LLM writes a short document-aware context for each chunk, prepended before embedding and BM25 indexing, so chunks that depend on surrounding text become retrievable. Prompt caching keeps the per-chunk cost low; deterministic title and heading prefixes are the free first step.

**Parent-child retrieval: why and what can go wrong?** Small chunks match precisely, parents give enough context to answer. Risks: blowing the token budget, duplicate parents, and reranking long parents past the reranker's input limit; rerank children, then expand and dedupe.

**When is GraphRAG worth its cost?** For relationship and multi-hop questions over entities and corpus-wide "what are the themes" questions over a fairly stable corpus. Not for lookups or fast-changing data, where hybrid search plus reranking is cheaper and better.

**How do agentic and corrective RAG differ from a fixed pipeline?** The model decides when and what to retrieve and checks whether results are relevant and supported, retrying or switching sources. Better on multi-step questions; more calls, latency and variance, so bound it and route simple queries around it.

**How do you make text-to-SQL safe?** Read-only role on curated views, tenant scoping in the database, allowlisted tables and functions, one statement, timeouts and row caps, and showing the SQL. The prompt is never the security boundary.

**What do you put in a retrieval cache key?** Tenant, ACL fingerprint, normalized query, filters, embedding model version and a per-tenant index version bumped on every ingest.

## Further reading

- Anthropic, "Introducing Contextual Retrieval" (engineering blog, 2024).
- Sarthi et al., "RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval" (2024).
- Edge et al., "From Local to Global: A Graph RAG Approach to Query-Focused Summarization" (2024).
- Asai et al., "Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection" (2023).
- Yan et al., "Corrective Retrieval Augmented Generation" (2024).
- Faysse et al., "ColPali: Efficient Document Retrieval with Vision Language Models" (2024).
