---
title: Chunking strategies
section: llm
order: 5
summary: How to split documents for retrieval, from fixed and recursive splits to structure-aware and parent-child chunking, and what metadata to keep.
---

Retrieval returns chunks, not documents, so the chunk is the unit of relevance. Chunks that are too small lose context ("it increased by 12%": what did?). Chunks that are too large dilute the embedding with several topics and waste prompt tokens. Chunking is usually the highest-leverage, least-glamorous knob in a RAG system, and it is decided at ingest time, so changing it means re-processing everything.

## How it works

### Why chunk size matters

An embedding compresses a whole chunk into one vector. If a chunk covers three topics, its vector sits somewhere between them and matches none strongly. If a chunk is one sentence, it matches precisely but carries too little context to answer from.

| Chunk size (tokens) | Good for | Risk |
|---|---|---|
| 100–250 | FAQs, precise facts, short answers | Missing context, more chunks to store and rank |
| 300–600 | General docs, knowledge bases (a sane default) | Occasional topic mixing |
| 800–1,500 | Narrative text, legal, long reasoning | Diluted embeddings, fewer chunks fit in the prompt |

### Strategies

**Fixed-size.** Split every `N` tokens with `O` tokens of overlap. Simple and predictable, but it cuts mid-sentence and mid-table.

**Recursive.** Try to split on the strongest boundary first (`\n\n`, then `\n`, then sentence end, then space) and only fall back to a weaker boundary when a piece is still over the limit. This is the practical default for plain text.

**Structural.** Use the document's own structure: Markdown or HTML headings, PDF sections, slide boundaries, code functions and classes (via an AST or tree-sitter), table rows. Each chunk carries its heading path. Best quality when structure is available.

**Semantic.** Embed each sentence, and start a new chunk where similarity between adjacent sentences drops below a threshold. Produces topic-coherent chunks for unstructured prose, at the cost of an embedding call per sentence at ingest and a threshold to tune. Often not better than structural splitting when headings exist.

**Parent-child (small-to-big).** Index small child chunks (~100–300 tokens) for precise matching, but return the larger parent (a section, ~1,000–2,000 tokens) to the LLM. Precision of small chunks, context of big ones.

```text
document
 └── section "Refund policy"             ← parent: sent to the LLM
      ├── child: "Refunds within 30 days…"   ← indexed, matched
      ├── child: "Digital goods are…"        ← indexed
      └── child: "Contact billing@…"         ← indexed
```

### Overlap

Overlap (10–20% of chunk size) protects facts that straddle a boundary. It costs storage and causes near-duplicate hits. With structural or recursive splitting on clean boundaries you need less overlap; with fixed-size splits you need it.

### Contextual headers

A chunk saying "The limit is 100 requests per minute" is useless without knowing which API and which plan. Prepend a short header before embedding:

```text
Document: Public API reference > Rate limits > Enterprise plan
The limit is 100 requests per minute per API key...
```

A heavier variant uses an LLM at ingest to write a one- or two-sentence context for each chunk relative to its document. It measurably improves retrieval for ambiguous chunks but costs one LLM call per chunk; provider-side prompt caching of the full document keeps that affordable.

## In practice

### A structural-then-recursive splitter

```python
import re
from dataclasses import dataclass

@dataclass
class Chunk:
    text: str
    heading_path: list[str]
    ordinal: int
    token_count: int

HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
SEPARATORS = ["\n\n", "\n", ". ", " "]

def split_recursive(text: str, max_tokens: int, count, seps=SEPARATORS) -> list[str]:
    if count(text) <= max_tokens or not seps:
        return [text]
    sep, rest = seps[0], seps[1:]
    out, buf = [], ""
    for part in text.split(sep):
        candidate = f"{buf}{sep}{part}" if buf else part
        if count(candidate) <= max_tokens:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            # part alone may still be too big: recurse with weaker separator
            pieces = split_recursive(part, max_tokens, count, rest)
            out.extend(pieces[:-1])
            buf = pieces[-1]
    if buf:
        out.append(buf)
    return out

def chunk_markdown(md: str, count, max_tokens: int = 500) -> list[Chunk]:
    chunks, path, ordinal = [], [], 0
    sections = HEADING.split(md)        # [pre, '#', 'Title', body, '##', ...]
    bodies = [(path.copy(), sections[0])]
    for i in range(1, len(sections), 3):
        level, title, body = len(sections[i]), sections[i + 1], sections[i + 2]
        path = path[: level - 1] + [title]
        bodies.append((path.copy(), body))
    for heading_path, body in bodies:
        for piece in split_recursive(body.strip(), max_tokens, count):
            if piece.strip():
                header = " > ".join(heading_path)
                text = f"{header}\n{piece}" if header else piece
                chunks.append(Chunk(text, heading_path, ordinal, count(text)))
                ordinal += 1
    return chunks
```

Pass a real tokenizer's count function, not `len(text.split())`. Use the embedding model's tokenizer when you can.

### Metadata to store per chunk

```sql
CREATE TABLE chunks (
    id            bigserial PRIMARY KEY,
    tenant_id     bigint  NOT NULL,
    document_id   bigint  NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id     bigint  REFERENCES chunks(id),     -- parent-child
    ordinal       int     NOT NULL,                  -- position in document
    heading_path  text[]  NOT NULL DEFAULT '{}',
    page_from     int,
    page_to       int,
    text          text    NOT NULL,
    token_count   int     NOT NULL,
    content_hash  bytea   NOT NULL,
    acl_groups    bigint[] NOT NULL DEFAULT '{}',
    chunker_version text  NOT NULL
);
CREATE INDEX ON chunks (tenant_id, document_id, ordinal);
```

Every field earns its place: `tenant_id` and `acl_groups` for filtering, `ordinal` to fetch neighbors (expand a hit with the chunk before and after), `page_*` and `heading_path` for citations, `content_hash` for incremental re-ingest, and `chunker_version` so you know which rows need reprocessing after you change the splitter.

### Special content

- **Tables:** keep a table in one chunk if it fits; otherwise split by rows and repeat the header row in each chunk. Consider also storing a one-line LLM summary of the table.
- **Code:** split on functions and classes; include the file path and class name in the header.
- **PDFs:** extraction quality dominates. Two-column layouts, headers and footers, and hyphenation need cleaning before chunking. Garbage in, garbage embedded.
- **Chat logs and tickets:** chunk by conversation or thread, not by size.

## Pitfalls

- **Splitting on characters, not tokens.** Chunks exceed the embedding model's limit and get truncated silently. Fix: count tokens.
- **No heading context.** Symptom: retrieved chunks are relevant-looking but ambiguous; the model answers about the wrong product or plan. Fix: contextual headers.
- **Boilerplate chunks.** Navigation, cookie banners and legal footers repeated in every page fill the top-k. Fix: strip boilerplate; dedupe by `content_hash`.
- **Tiny trailing chunks.** A 12-token remainder becomes its own chunk with a noisy embedding. Fix: merge remainders below a minimum size into the previous chunk.
- **Changing the chunker without versioning.** Old and new chunks coexist with different boundaries and duplicate content. Fix: `chunker_version` and a re-ingest job.
- **Tuning by eye.** Fix: compare chunk configurations on retrieval recall@k against a golden set; see [evaluating RAG]({{ '/llm/rag-evaluation/' | relative_url }}).

## Rules of thumb

- Start with structural splitting plus recursive fallback, ~300–600 tokens, ~10–15% overlap.
- Prepend the heading path to every chunk before embedding.
- Use parent-child when answers need more context than precise matching allows.
- Store ordinal, heading path, page numbers, ACLs and a content hash.
- Version the chunker; re-ingest deliberately.
- Measure chunking changes with retrieval metrics, not anecdotes.

## Interview angle

- **How do you pick chunk size?** Start from content type and the answers you need, then measure recall@k on a golden set across two or three sizes.
- **What is parent-child retrieval?** Match on small chunks for precision, return their parent section for context.
- **Why add overlap?** So facts spanning a boundary appear whole in at least one chunk; cost is storage and duplicate hits.
