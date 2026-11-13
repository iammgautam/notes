---
title: Chunking strategies
section: llm
order: 15
phase: "Phase 3 · Retrieval"
summary: Fixed, recursive, structure-aware, semantic and parent-child chunking, size vs model limits, late chunking, contextual headers, and how to evaluate it.
---

Retrieval returns chunks, not documents, so the chunk is the unit of relevance, of citation and of prompt cost. Chunks that are too small lose context ("it increased by 12%": what did?). Chunks that are too large blend several topics into one vector and waste prompt tokens. Chunking is decided at ingest time, so changing it means re-processing and re-embedding everything, and it is usually the highest-leverage, least-glamorous knob in a RAG system.

## How it works

### Three jobs, one chunk

A chunk has to do three things that pull against each other:

1. **Be findable.** One embedding summarizes the whole chunk. A chunk about one thing produces a sharp vector; a chunk covering three topics lands between them and matches none strongly.
2. **Be sufficient.** Once retrieved, it must carry enough context for the LLM to answer and cite: which product, which plan, which time period.
3. **Fit the budgets.** It must fit the embedding model's input limit, and `k` of them must fit the prompt alongside instructions and history.

Most techniques below decouple these jobs: match on something small and focused, then hand the LLM something larger.

### Chunk size vs embedding model limits

Every embedding model has a maximum input length. Many BERT-family models stop at 512 tokens, and some popular small models less (sentence-transformers' `all-MiniLM-L6-v2` truncates at 256 word pieces). Many newer and hosted models accept around 8k tokens (OpenAI's `text-embedding-3` models take 8,191). Past the limit, input is usually truncated without an error, so the tail of the chunk never influences its vector. Count with the model's own tokenizer, not characters or words.

A long limit is not a reason to embed long chunks. The output is still one vector, and models are mostly trained on shorter passages, so a 6k-token chunk embeds as a vague average.

| Chunk size (tokens) | Good for | Risk |
|---|---|---|
| 100–250 | FAQs, precise facts, short answers | Missing context, more chunks to store and rank |
| 300–600 | General docs and knowledge bases (sane default) | Occasional topic mixing |
| 800–1,500 | Narrative, legal, long reasoning | Diluted vectors, fewer chunks fit the prompt |

Prompt math: top-8 chunks at 500 tokens is 4k tokens of context per question; at 1,500 tokens it is 12k, which costs latency and money on every request and pushes relevant text into the middle of a long context, where models use it less reliably. See [context engineering]({{ '/llm/context-engineering/' | relative_url }}).

## Techniques

### Fixed-size

Split every `N` tokens with `O` tokens of overlap. Simple, predictable sizes, trivially parallel. It cuts mid-sentence, mid-table and mid-code block, and an insertion near the top of a document shifts every later boundary, so incremental re-ingest re-embeds the whole document. Fine for homogeneous prose and as a baseline.

### Recursive

Try the strongest boundary first (blank line, then newline, then sentence end, then space) and recurse to a weaker boundary only for pieces still over the limit; then greedily pack small pieces up to the limit. This keeps paragraphs and sentences intact and is the practical default for plain text (it is what LangChain's `RecursiveCharacterTextSplitter` does, with configurable separators).

### Structure-aware

Use the document's own structure, then fall back to recursive splitting inside oversized sections:

- **Headings** (Markdown, HTML `h1`–`h6`, DOCX styles, PDF outline): split on sections and keep the heading path with every chunk.
- **Code**: split on functions and classes using an AST (Python's `ast`, or tree-sitter for many languages). Keep the docstring with its function, and put the file path, class and signature in the chunk header.
- **Tables**: keep a table whole if it fits. Otherwise split by rows and repeat the header row in every chunk; serialize as Markdown so column names stay attached. For spreadsheets, a row rendered as `column: value` pairs is often the right unit. Consider also indexing a one-line summary that points to the full table.
- **Other units**: slides, PDF pages for scanned forms, email threads, chat conversations and support tickets. Chunk by the unit people ask about.

Best quality whenever structure exists, and boundaries stay stable when a document is edited elsewhere. The parsing step that recovers that structure is covered in [document ingestion]({{ '/llm/document-ingestion/' | relative_url }}).

### Semantic chunking

Embed every sentence, compute the cosine distance between adjacent sentences (or small sliding windows), and start a new chunk where the distance exceeds a threshold, typically a high percentile of distances within that document, with minimum and maximum sizes enforced. It produces topic-coherent chunks for unstructured prose such as transcripts. Costs: one embedding per sentence at ingest, a threshold to tune, and unpredictable sizes. Published comparisons have often found it no better than well-tuned recursive splitting, and when headings exist structure-aware splitting usually wins.

### Sentence windows

Index single sentences for very precise matching, and store the surrounding window (say 3 sentences on each side) as metadata. At query time, replace each hit with its window before building the prompt. LlamaIndex ships this as a sentence-window node parser plus a metadata-replacement postprocessor. Good for dense factual text; it multiplies the number of vectors.

### Parent-child (small-to-big)

Index small child chunks (100–300 tokens) for matching, but return the parent (a section of 1,000–2,000 tokens) to the LLM. If several children of one parent hit, send the parent once. LangChain's parent-document retriever and LlamaIndex's auto-merging retriever implement variants.

```text
document
 +-- section "Refund policy"            parent: what the LLM reads
      +-- child "Refunds within 30 days ..."   indexed, matched
      +-- child "Digital goods are final ..."  indexed
      +-- child "Contact billing@ ..."         indexed

query --> match children --> dedupe by parent_id --> send parents
```

Precision of small chunks, context of big ones. The cost is prompt tokens: budget parents, not children.

### Propositions

An LLM rewrites each passage into atomic, self-contained statements ("The Enterprise plan's API limit is 100 requests per minute"), each indexed separately. Research on this ("Dense X Retrieval") showed retrieval gains on open-domain QA. It costs an LLM call per passage at ingest and loses the original wording, so keep a pointer back to the source passage for citation.

### Overlap

Overlap repeats the end of one chunk at the start of the next so that a fact straddling a boundary appears whole in at least one chunk. Typical values are 10–20% of chunk size, or one or two sentences with sentence-aligned splits. It costs storage and produces near-duplicate hits; merge adjacent hits by ordinal when assembling context. Structure-aware and recursive splits need less overlap than fixed-size ones.

### Late chunking

Normal ("early") chunking embeds each chunk in isolation, so "It was released in 2023" has no idea what "it" is. Late chunking runs a long-context embedding model over the whole document (or a large window) first, producing contextualized token embeddings, then mean-pools the token embeddings inside each chunk's span. Each chunk vector now reflects the surrounding document, with no extra LLM calls. Requirements: a long-context model that uses mean pooling, and access to token-level outputs, which usually means self-hosting; most hosted APIs return only pooled vectors.

### Contextual chunk headers

Prepend context before embedding (and often before BM25 indexing):

```text
Document: Public API reference > Rate limits > Enterprise plan
The limit is 100 requests per minute per API key...
```

- **Deterministic headers**: document title plus heading path, product, version, date. Free, and almost always worth it.
- **LLM-generated context**: for each chunk, an LLM reads the whole document and writes one or two sentences situating the chunk. Anthropic's "contextual retrieval" write-up reported this cut top-20 retrieval failures by roughly a third, and by about half when combined with contextual BM25. It costs one LLM call per chunk; putting the document first in the prompt lets provider prompt caching reuse it across all chunks of that document.

Embed `header + body`, but show and cite only `body`.

### Choosing

| Technique | Ingest cost | Quality | Use when |
|---|---|---|---|
| Fixed-size | lowest | baseline | Homogeneous prose, quick start |
| Recursive | low | good | Plain text default |
| Structure-aware | low to medium (parsing) | best when structure exists | Docs, wikis, code, tables |
| Semantic | one embedding per sentence | mixed | Unstructured transcripts |
| Sentence window | many vectors | high precision | Dense factual text |
| Parent-child | 2x storage | precision plus context | Answers need surrounding section |
| Propositions | LLM per passage | high, lossy wording | High-value, slow-changing corpora |
| Late chunking | one long-context pass | better for anaphora | Self-hosted long-context model |
| LLM context headers | LLM per chunk | strong gains | Ambiguous chunks, budget allows |

## In practice

### Runnable: heading-aware recursive chunker

Stdlib only. Splits a Markdown document by headings, recursively splits oversized sections on paragraph, line, sentence and word boundaries, carries sentence-level overlap, and prepends the heading path. The token counter is a whitespace stand-in.

```python
import re
from dataclasses import dataclass

SEPARATORS = ["\n\n", "\n", ". ", " "]
HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)

def count_tokens(text):
    # Stand-in. Use the embedding model's own tokenizer in production.
    return len(text.split())

def _split_keep(text, sep):
    parts = text.split(sep)
    return [p + sep for p in parts[:-1]] + [parts[-1]]

def _merge(parts, max_tokens, overlap):
    """Greedily pack parts into chunks; carry trailing parts as overlap."""
    chunks, window = [], []
    for part in parts:
        if window and count_tokens("".join(window + [part])) > max_tokens:
            chunks.append("".join(window))
            while window and (count_tokens("".join(window)) > overlap or
                              count_tokens("".join(window + [part])) > max_tokens):
                window.pop(0)
        window.append(part)
    if window:
        chunks.append("".join(window))
    return chunks

def recursive_split(text, max_tokens, overlap=0, seps=SEPARATORS):
    """Split on the strongest separator present; recurse into oversize parts."""
    if count_tokens(text) <= max_tokens:
        return [text.strip()] if text.strip() else []
    i = next((i for i, s in enumerate(seps) if s in text), None)
    if i is None:
        return [text.strip()]   # unsplittable; a token-based splitter hard-cuts here
    out, fits = [], []
    for part in _split_keep(text, seps[i]):
        if count_tokens(part) <= max_tokens:
            fits.append(part)
        else:
            out += _merge(fits, max_tokens, overlap)
            fits = []
            out += recursive_split(part, max_tokens, overlap, seps[i + 1:])
    out += _merge(fits, max_tokens, overlap)
    return [c.strip() for c in out if c.strip()]

@dataclass
class Chunk:
    text: str            # what you embed: heading path + body
    body: str            # what you show and cite
    heading_path: list
    ordinal: int

def chunk_markdown(md, max_tokens=60, overlap=15):
    pieces = HEADING.split(md)          # [preamble, '#', 'Title', body, ...]
    sections, path = [([], pieces[0])], []
    for j in range(1, len(pieces), 3):
        level, title, body = len(pieces[j]), pieces[j + 1].strip(), pieces[j + 2]
        path = path[:level - 1] + [title]
        sections.append((list(path), body))
    chunks = []
    for heading_path, body in sections:
        header = " > ".join(heading_path)
        budget = max_tokens - count_tokens(header)
        for piece in recursive_split(body, budget, overlap):
            text = f"{header}\n{piece}" if header else piece
            chunks.append(Chunk(text, piece, heading_path, len(chunks)))
    return chunks

if __name__ == "__main__":
    refunds = " ".join(f"Refund rule {i} says purchases in region {i} are "
                       f"refundable within {i + 10} days." for i in range(1, 13))
    doc = (
        "# Billing guide\n\nThis guide covers refunds and rate limits.\n\n"
        f"## Refunds\n\n{refunds}\n\nDigital goods are final sale.\n\n"
        "## Rate limits\n\n### Enterprise plan\n\n"
        "The limit is 100 requests per minute per API key.\n"
    )
    chunks = chunk_markdown(doc, max_tokens=60, overlap=15)
    for c in chunks:
        print(c.ordinal, c.heading_path, count_tokens(c.text), "tokens")

    # Every chunk fits the budget, heading path included.
    assert all(count_tokens(c.text) <= 60 for c in chunks)
    # Heading paths follow nesting.
    last = chunks[-1]
    assert last.heading_path == ["Billing guide", "Rate limits", "Enterprise plan"]
    assert last.text.startswith("Billing guide > Rate limits > Enterprise plan\n")
    # The long section split on sentence boundaries, with overlap.
    ref = [c for c in chunks if c.heading_path[-1] == "Refunds"]
    assert len(ref) >= 3 and all(c.body.endswith(".") for c in ref)
    assert ref[1].body.split(". ")[0] in ref[0].body
    # Nothing lost: every source sentence lands in some chunk.
    for sentence in re.findall(r"[^.\n]+\.", doc):
        assert any(sentence.strip() in c.body for c in chunks), sentence
```

The output also shows a known weakness: "Digital goods are final sale." becomes its own 9-token chunk. Production splitters merge remainders below a minimum size into the previous chunk.

### Metadata to store per chunk

```sql
CREATE TABLE chunks (
    id              bigserial PRIMARY KEY,
    tenant_id       bigint   NOT NULL,
    document_id     bigint   NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id       bigint   REFERENCES chunks(id),   -- parent-child
    ordinal         int      NOT NULL,                -- position in document
    heading_path    text[]   NOT NULL DEFAULT '{}',
    page_from       int,
    page_to         int,
    element_type    text     NOT NULL DEFAULT 'text', -- text | table | code
    body            text     NOT NULL,                -- shown and cited
    embed_text      text     NOT NULL,                -- header + body, embedded
    token_count     int      NOT NULL,
    content_hash    bytea    NOT NULL,
    acl_groups      bigint[] NOT NULL DEFAULT '{}',
    chunker_version text     NOT NULL
);
CREATE INDEX ON chunks (tenant_id, document_id, ordinal);
```

`tenant_id` and `acl_groups` drive filtering; `ordinal` lets you fetch neighbors (expand a hit with the chunk before and after); `page_*` and `heading_path` power citations; `content_hash` skips unchanged chunks on re-ingest; `chunker_version` tells you which rows need reprocessing after a splitter change.

### LLM context headers (illustrative)

```python
CONTEXT_PROMPT = """<document>
{document}
</document>

<chunk>
{chunk}
</chunk>

Using the document above, write one or two sentences stating what this chunk
is about and where it sits in the document (product, section, period, entities
it refers to). Reply with the sentences only."""

def contextualize(llm, document: str, chunk_bodies: list[str]) -> list[str]:
    out = []
    for body in chunk_bodies:
        # Same document prefix on every call, so prompt caching applies.
        context = llm.complete(CONTEXT_PROMPT.format(document=document, chunk=body))
        out.append(f"{context.strip()}\n\n{body}")
    return out
```

`llm.complete` stands in for your provider client. For a 20-page document with 60 chunks, that is 60 calls reading the same ~15k-token prefix: without caching, ~900k input tokens per document; with caching, most of it bills at the cached rate.

### Late chunking (illustrative)

```python
import torch
from transformers import AutoModel, AutoTokenizer

MODEL = "your-long-context-mean-pooling-embedder"
tok = AutoTokenizer.from_pretrained(MODEL)
model = AutoModel.from_pretrained(MODEL)

def late_chunk(document: str, spans: list[tuple[int, int]]):
    """spans are (start_char, end_char) of each chunk within document."""
    enc = tok(document, return_tensors="pt", return_offsets_mapping=True,
              truncation=True, max_length=8192)
    offsets = enc.pop("offset_mapping")[0]
    with torch.no_grad():
        hidden = model(**enc).last_hidden_state[0]          # (tokens, dim)
    vectors = []
    for start, end in spans:
        inside = (offsets[:, 0] >= start) & (offsets[:, 1] <= end) \
                 & (offsets[:, 1] > offsets[:, 0])          # drop special tokens
        v = hidden[inside].mean(dim=0)
        vectors.append(torch.nn.functional.normalize(v, dim=0))
    return vectors
```

### How to evaluate chunking

Chunking changes are invisible without measurement, and comparing them has two traps.

1. **Label answers as spans of the source document, not chunk ids.** Chunk ids change with every configuration. A golden set of (question, document, answer character span) works across all of them.
2. **Hold the token budget constant, not `k`.** Comparing 256-token chunks at `k = 8` against 1,024-token chunks at `k = 8` gives the big chunks 4x more context. Compare 256 at `k = 16` with 512 at `k = 8`.

Metrics:

| Metric | Question it answers |
|---|---|
| Span recall@budget | Did the retrieved chunks contain the answer span? |
| Token precision | What fraction of retrieved tokens were relevant? |
| Tokens per answer | What does each question cost in prompt? |
| Answer correctness and faithfulness | Does it change the end result? (subset, LLM judge) |
| Ingest cost and chunk count | Embedding calls, LLM calls, storage |

Break results down by content type (prose, tables, code) because a strategy that helps one often hurts another. Run two or three configurations against the same embedding model, then keep the winner and record it in `chunker_version`. See [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

## Pitfalls

- **Counting characters or words instead of tokens.** Chunks exceed the model limit and are truncated silently. Fix: count with the embedding model's tokenizer.
- **No heading context.** Symptom: relevant-looking but ambiguous hits; the model answers about the wrong plan or product. Fix: contextual headers.
- **Boilerplate chunks.** Navigation, cookie banners and legal footers fill the top-k. Fix: strip at ingest; dedupe by `content_hash`.
- **Tiny trailing chunks.** A 10-token remainder gets its own noisy vector. Fix: merge below a minimum size.
- **Tables split mid-row or without headers.** Symptom: numbers retrieved without their column names. Fix: row-based splits with repeated headers.
- **Unstable boundaries.** Fixed-size splitting shifts every later chunk after a small edit, so incremental re-ingest re-embeds whole documents. Fix: structure-aware boundaries and chunk-level content hashes.
- **Duplicate context from overlap.** Adjacent hits repeat text in the prompt. Fix: merge neighbors by ordinal before prompting.
- **Changing the chunker without versioning.** Old and new boundaries coexist with duplicated content. Fix: `chunker_version` and a deliberate re-ingest.
- **Tuning by eye.** Fix: span-based recall at a fixed token budget.

## Rules of thumb

- Start with structure-aware splitting plus recursive fallback, 300–600 tokens, one or two sentences of overlap.
- Prepend the title and heading path to every chunk before embedding.
- Use parent-child or sentence windows when answers need more context than precise matching allows.
- Treat tables and code as their own element types with their own rules.
- Store ordinal, heading path, pages, ACLs, content hash and chunker version.
- Add LLM context headers or late chunking when chunks are ambiguous out of context and the eval shows a gain.
- Measure every change with span recall at a fixed token budget.

## Interview questions

**How do you pick a chunk size?** Start from the content and the answers you need (300–600 tokens for typical docs), stay under the embedding model's limit, then compare two or three sizes on span recall at a fixed token budget.

**Recursive vs semantic chunking?** Recursive splits on the strongest available boundary (paragraph, line, sentence) and packs up to a limit: cheap and predictable. Semantic splits where adjacent-sentence embeddings diverge: topic-coherent but costs an embedding per sentence and a tuned threshold, and often does not beat recursive or structure-aware splitting.

**What is parent-child retrieval?** Index small chunks for precise matching, return their parent section for context, deduplicated by parent.

**What is late chunking?** Embed the whole document with a long-context model to get contextualized token embeddings, then mean-pool per chunk span, so each chunk vector carries document context. It needs token-level outputs, so usually a self-hosted model.

**What are contextual chunk headers?** Text prepended to each chunk before embedding: the title and heading path, or an LLM-written sentence situating the chunk. They resolve ambiguous chunks like "the limit is 100 per minute."

**How do you evaluate a chunking change?** Golden questions labeled with answer spans in source documents, span recall at an equal token budget, token precision and cost, broken down by content type, and an end-to-end check on a subset.

**Why overlap, and what does it cost?** So boundary-straddling facts appear whole somewhere. It costs storage and near-duplicate hits, which you merge by ordinal.

## Further reading

- Günther et al., "Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models" (2024).
- Chen et al., "Dense X Retrieval: What Retrieval Granularity Should We Use?" (2023).
- Anthropic, "Introducing Contextual Retrieval" (engineering blog, 2024).
- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (2023).
- LangChain documentation: text splitters; LlamaIndex documentation: node parsers.
