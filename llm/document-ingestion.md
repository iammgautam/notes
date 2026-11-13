---
title: Document ingestion pipelines
section: llm
order: 16
phase: "Phase 3 · Retrieval"
summary: Connectors, parsing PDFs, HTML and Office files, cleaning, dedup, ACL propagation, incremental sync and deletes, re-indexing, failure handling and PII.
---

Ingestion is the ETL that feeds retrieval: pull documents from source systems, turn them into clean structured text, attach the metadata and permissions retrieval filters on, and keep it all in sync as sources change. Most RAG quality and security problems start here: a garbled PDF table cannot be fixed by a better reranker, a missed delete keeps serving a revoked policy, and a dropped ACL leaks one customer's data to another. Treat it as a production data pipeline with idempotent stages, versioned outputs and its own metrics.

## How it works

### The pipeline

```text
  sources: uploads, Drive, SharePoint, Confluence, Slack, web
      |  list changes (cursor / delta token / webhook)
      v
  fetch -------------> raw bytes in object store (keyed by sha256)
      |
      v
  parse -------------> text layer | OCR | layout model | VLM
      |                (routed per page by quality checks)
      v
  clean -> dedup -> PII policy -> chunk -> embed (cached by hash)
      |                                       |
      v                                       v
  documents table                    chunks + vectors + BM25
  (source_id, version, hash,         (tenant_id, acl_groups,
   acl, stage versions, status)       ordinal, page, hash)
      ^
      |  ACL sync loop: faster cadence, metadata only
  source permission APIs
```

Each stage persists its output keyed by `(document, content hash, stage version)`. Retries are then safe, unchanged documents are skipped cheaply, and a chunker change re-runs from parsed text instead of re-fetching and re-parsing millions of files.

### Sources and connectors

| Source type | Change detection | Typical trouble |
|---|---|---|
| Uploads, object storage | Event on write, or listing by modified time | Huge files, unsupported formats |
| SaaS (Drive, SharePoint, Confluence, Notion, Jira, Zendesk) | Delta or changes APIs with a cursor; webhooks | Per-tenant OAuth refresh, rate limits, permissions in a separate API |
| Chat (Slack, Teams) | Event streams, history APIs | Threads, edits, channel membership as ACL |
| Web crawl | Sitemaps, `Last-Modified`/`ETag`, recrawl schedule | JS rendering, boilerplate, canonical URLs |
| Databases | `updated_at` cursors, CDC | Rows are records, not documents |

Webhooks are lossy, so pair them with periodic reconciliation, and respect `429` and `Retry-After` from source APIs. Connector libraries (LlamaIndex readers, LangChain document loaders, Unstructured, Airbyte) handle fetching; permissions, deletes and incremental state are left to you.

### Parsing

| Approach | How | Good at | Weak at | Cost |
|---|---|---|---|---|
| Text-layer extraction | pypdf, pdfminer.six, PyMuPDF; native DOCX/HTML parsers | Digital documents, speed | Scans, multi-column order, tables, broken font encodings | Very low |
| OCR | Tesseract or cloud OCR | Scanned pages | Character errors, no structure on its own | Medium |
| Layout-aware parsing | Layout detection + OCR + table-structure models (Unstructured, Docling, Marker, cloud document-AI services) | Headings, reading order, tables | Slower, GPU helps | Medium |
| VLM parsing | Render page to image, ask a vision-language model for Markdown | Complex layouts, charts, forms, handwriting | Can skip or invent content, nondeterministic | High |

Route per page, not per corpus. Try the text layer first, run quality checks (characters per page, share of replacement or non-printable characters, ratio of dictionary words), and send only failing pages to OCR, layout models or a VLM. A corpus of 2M pages where 10% are hard turns a 2B-output-token VLM job (at roughly 1,000 tokens per page) into a 200M-token one.

Format notes:

- **PDF**: multi-column reading order, repeated headers and footers, hyphenation, footnotes, rotated pages, passwords. Plain extraction flattens tables into runs of numbers.
- **Tables**: keep as Markdown or HTML with header rows; a table is often its own chunk (see [chunking]({{ '/llm/chunking/' | relative_url }})).
- **HTML**: extract main content (trafilatura or readability-style algorithms), keep headings, lists, code and tables, convert to Markdown. JS-rendered pages need a headless browser.
- **Office**: DOCX heading styles map to sections; PPTX slides are units and speaker notes often hold the real content; XLSX sheets become tables or row records.
- **Email and chat**: strip quoted replies and signatures; attachments become child documents that inherit the parent's ACL.

Normalize every format into one **canonical representation**: Markdown text plus a list of elements (type, text, page, bounding box, heading path). Chunkers, cleaners and citations then work the same for every source. Store raw bytes and parsed output with a `parser_version`.

### Cleaning

- Unicode NFKC normalization, whitespace collapse, control and zero-width characters removed (zero-width and white-on-white text are also prompt-injection carriers).
- De-hyphenate line breaks, fix ligatures and mojibake (the `ftfy` library helps).
- Remove repeated page headers, footers and page numbers: lines that appear on most pages of a document.
- Remove site boilerplate (navigation, cookie banners, legal footers): lines that recur across many documents from the same source.
- Detect and store language; it drives BM25 analyzers and evaluation slices.

### Deduplication

Corpora are full of duplicates: re-uploads, the same policy in Drive and Confluence, seven versions of a contract. They waste embedding spend and crowd the top-k with copies of one passage.

- **Exact**: SHA-256 of normalized text at document and chunk level. Catches re-uploads and formatting-only differences.
- **Near-duplicate with shingles**: represent a text as its set of k-word shingles and compare with Jaccard similarity `|A ∩ B| / |A ∪ B|`. All-pairs comparison is `O(n^2)`, so at scale you use sketches.
- **MinHash**: for a random hash function `h`, `P[min h(A) == min h(B)] = J(A, B)`. A signature of 128 minimums per document estimates Jaccard with a standard error of about `sqrt(J(1 - J) / 128)`, around 0.04.
- **LSH banding**: split the signature into `b` bands of `r` rows; documents sharing any band become candidates. Candidate probability is `1 - (1 - J^r)^b`, a steep S-curve with its threshold near `(1/b)^(1/r)`: about 0.42 for 32 bands of 4 rows.
- **SimHash**: a 64-bit fingerprint built from weighted feature hashes; near-duplicates differ in a few bits. Google's web-crawl work used a Hamming distance of 3 or less on 64-bit fingerprints.
- **Embedding similarity**: catches paraphrases that shingles miss, at a vector query per chunk.

Policy matters more than algorithm. Keep a canonical copy but record every source reference. Never let dedup widen access: dedupe only within the same ACL scope, or keep one chunk listing all its sources and show it only to users who can read at least one of them, citing that one. Mark older versions of a document superseded so retrieval stops returning last year's policy.

### Metadata

Document level: tenant, source and source id, URI, title, MIME type, language, source version and modified time, content hash, ACL, and parser, chunker and embedding-space versions. Chunk level: ordinal, heading path, pages, element type, hash, denormalized tenant and ACL. Capture every filter you will want at query time (product, region, date) now; adding one later means a re-ingest.

### ACLs and permission propagation

1. **Capture ACLs at the source**: users, groups, domain-wide sharing, public links, and inheritance from folders or spaces.
2. **Map principals** to internal identities (email to user id, source group to internal group). Unknown principals map to nothing: deny by default.
3. **Store groups, not expanded users**, denormalized to chunks (`acl_groups bigint[]` with a GIN index), so membership changes need no re-index.
4. **Filter inside the retrieval query**, before ranking. Never retrieve broadly and ask the LLM to ignore what the user cannot see.
5. **Run a separate, faster ACL sync loop.** A permission change updates metadata only.
6. **Bound staleness**: revocations apply at the next sync, so for sensitive sources re-check access against the source API for documents you are about to cite.

Defense in depth: Postgres row-level security keyed on tenant, per-tenant partitions or namespaces, and caches keyed by permission scope (a shared [semantic cache]({{ '/llm/llm-caching/' | relative_url }}) can leak across users). Keep ACL regression tests: a user without access asks a question answerable only from a restricted document, and no restricted chunk may come back. More in [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

### Incremental updates and deletes

Change detection works in layers:

1. **Source cursor** (modified time, etag, delta token) selects candidates without downloading.
2. **Document hash**: if the normalized text is unchanged, apply metadata or ACL changes only.
3. **Chunk hashes**: re-embed only chunks whose text hash is new; look up the rest in an embedding cache keyed by `(embedding space, chunk hash)`. Structure-aware chunk boundaries keep this diff local; fixed-size boundaries shift after any edit.

Upserts are keyed by `(tenant_id, source, source_id)` and guarded by source version, so a late-arriving old event cannot overwrite a newer one. Replace a document's chunks in one transaction so searches never see half a document.

Deletes are the part teams forget. Delta APIs report most; for the rest, run **reconciliation** (mark and sweep): list all ids in the source and delete what was not seen. Only sweep after a complete listing, and refuse to delete more than a set fraction (say 10–20%) of a source in one run without review, because an expired token looks exactly like "everything was deleted." Deletion must reach chunks, vectors, keyword index, caches, eval sets and fine-tuning data. Erasure requests make this a compliance requirement (see [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).

### Re-indexing

Parser, chunker, metadata and embedding-model changes all force re-processing. Re-run from the earliest changed stage only: a chunker change re-chunks persisted parsed text and embeds only chunk texts missing from the embedding cache. Run it blue-green: build version N+1 alongside N, evaluate on the golden set, flip a per-tenant flag, keep N for rollback. The embedding side of this is covered in [embeddings]({{ '/llm/embeddings/' | relative_url }}).

### Orchestration and failure handling

- **Queues per stage** (Celery or a Postgres-backed queue); a workflow engine (Temporal, Airflow, Dagster) helps for large multi-step backfills.
- **Idempotency**: deterministic ids, upserts, and "skip if output for this hash and stage version exists" (see [idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }})).
- **Classify errors**: transient (`429`, `5xx`, timeouts) retry with exponential backoff and jitter; permanent (encrypted PDF, unsupported type, `403`) fail fast. After N attempts, dead-letter and show the status to the user ("failed: password-protected PDF").
- **Sandbox parsers**: malformed or hostile files can hang a parser or exhaust memory. Parse in a subprocess or container with CPU, memory and wall-clock limits, never in the web process.
- **Fairness and backpressure**: per-tenant concurrency caps so one 200k-file import does not starve others, a priority lane for interactive uploads, rate limits toward sources and embedding providers.
- **Metrics**: throughput and error rate per stage and error class, queue depth and age, freshness lag (source modified to searchable, p95), parse quality (empty-page ratio, OCR fallback rate, characters per page), dedup rate, and documents by status (see [observability]({{ '/production/observability/' | relative_url }})).

### PII handling at ingest

- **Policy per source and data class**: drop, redact (mask), pseudonymize (consistent tokens, reversible through a vault), or keep but restrict with ACLs and tags.
- **Detection**: regexes with validators (emails, phones, card numbers with a Luhn check), NER-based detectors such as Microsoft Presidio, and secret scanners for API keys.
- **Retrieval trade-off**: redacting before embedding removes those terms from search. Consistent pseudonyms keep records joinable without exposing raw values.
- **Embeddings are not anonymization**: inversion attacks can reconstruct much of the original text from embeddings, so vectors get the same classification as the text.
- **Derived data**: logs, traces, caches and eval sets inherit the classification. Do not log chunk text by default.

## In practice

### Runnable: exact and near-duplicate detection, sync planning

Stdlib only: normalized exact hashing, shingle Jaccard, MinHash estimation, LSH banding, and a hash-based sync plan.

```python
import hashlib
import random
import re
import unicodedata

def normalize_text(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", text).strip()

def exact_key(text):
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()

def shingles(text, k=5):
    words = re.findall(r"\w+", normalize_text(text))
    return {" ".join(words[i:i + k]) for i in range(max(1, len(words) - k + 1))}

def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0

P = (1 << 61) - 1                       # Mersenne prime for universal hashing

def _h64(s):
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")

def make_hashers(n, seed=1):
    rng = random.Random(seed)
    return [(rng.randrange(1, P), rng.randrange(P)) for _ in range(n)]

def minhash(sh, hashers):
    """Signature: per hash function, the minimum hash over all shingles."""
    xs = [_h64(s) for s in sh]
    return [min((a * x + b) % P for x in xs) for a, b in hashers]

def estimate_jaccard(sig1, sig2):
    # P(min-hashes agree) == Jaccard similarity of the shingle sets.
    return sum(x == y for x, y in zip(sig1, sig2)) / len(sig1)

def lsh_keys(sig, bands):
    """Band the signature; docs sharing any band key become candidates."""
    r = len(sig) // bands
    return {(i, tuple(sig[i * r:(i + 1) * r])) for i in range(bands)}

def plan_sync(stored, current):
    """stored/current: source_id -> content hash. Returns what to do."""
    added = current.keys() - stored.keys()
    deleted = stored.keys() - current.keys()
    changed = {k for k in current.keys() & stored.keys() if current[k] != stored[k]}
    return added, changed, deleted

if __name__ == "__main__":
    base = (
        "Customers on the Pro plan can request a refund within 30 days of the "
        "original purchase. Refunds are issued to the original payment method and "
        "usually arrive within five to ten business days. Annual subscriptions are "
        "refunded pro rata for unused full months. Digital add-ons, including extra "
        "seats and storage packs, are final sale once activated. To start a refund, "
        "open the billing page, choose the invoice, and select request refund. "
        "Enterprise customers should contact their account manager instead, because "
        "their contracts may define different terms. Chargebacks filed while a "
        "refund request is open will cancel the request automatically."
    )
    reformatted = "  " + base.upper().replace(". ", ".\n\n")   # same text, new layout
    edited = (base.replace("30 days", "45 days")
                  .replace("five to ten", "seven to ten")
                  .replace("account manager", "customer success manager")
              + " Last updated March 2025.")
    unrelated = (
        "The public API allows 100 requests per minute per key on the Enterprise "
        "plan. Requests above the limit receive HTTP 429 with a Retry-After header. "
        "Burst traffic is smoothed with a token bucket refilled every second. Use "
        "exponential backoff with jitter, and never retry non-idempotent writes "
        "without an idempotency key."
    )

    # Exact dedup catches formatting-only differences.
    assert exact_key(base) == exact_key(reformatted)
    assert exact_key(base) != exact_key(edited)

    # Near-dup: shingle Jaccard separates edits from unrelated text.
    s_base, s_edit, s_other = shingles(base), shingles(edited), shingles(unrelated)
    j_edit, j_other = jaccard(s_base, s_edit), jaccard(s_base, s_other)
    print(f"jaccard edited={j_edit:.2f} unrelated={j_other:.2f}")
    assert j_edit > 0.6 and j_other < 0.05

    # MinHash estimates Jaccard from 128 integers per document.
    hashers = make_hashers(128)
    sig_base, sig_edit, sig_other = (minhash(s, hashers)
                                     for s in (s_base, s_edit, s_other))
    est = estimate_jaccard(sig_base, sig_edit)
    print(f"minhash estimate={est:.2f}")
    assert abs(est - j_edit) < 0.15

    # LSH banding (32 bands x 4 rows): near-dups collide, unrelated docs do not.
    assert lsh_keys(sig_base, 32) & lsh_keys(sig_edit, 32)
    assert not lsh_keys(sig_base, 32) & lsh_keys(sig_other, 32)

    # Incremental sync: hash diff decides what to parse, embed and delete.
    stored = {"doc-1": exact_key(base), "doc-2": exact_key(unrelated), "doc-3": "x"}
    current = {"doc-1": exact_key(reformatted), "doc-2": exact_key(edited),
               "doc-4": exact_key("new page")}
    added, changed, deleted = plan_sync(stored, current)
    assert (added, changed, deleted) == ({"doc-4"}, {"doc-2"}, {"doc-3"})
    print("sync plan:", sorted(added), sorted(changed), sorted(deleted))
```

Three word edits plus an appended line drop Jaccard to about 0.70, and MinHash estimates it closely. `doc-1` is skipped entirely: its layout changed, its normalized text did not.

### Document state and version-guarded upserts

```sql
CREATE TABLE documents (
    id                 bigserial PRIMARY KEY,
    tenant_id          bigint   NOT NULL,
    source             text     NOT NULL,      -- 'gdrive', 'confluence', 'upload'
    source_id          text     NOT NULL,      -- id in the source system
    source_version     text,                   -- etag / revision
    source_modified_at timestamptz,
    content_hash       bytea,                  -- sha256 of normalized text
    acl_groups         bigint[] NOT NULL DEFAULT '{}',
    parser_version     text,
    status             text     NOT NULL DEFAULT 'pending',  -- indexed, failed, deleted
    error_class        text,
    deleted_at         timestamptz,
    UNIQUE (tenant_id, source, source_id)
);

-- A late, older event cannot overwrite a newer version.
INSERT INTO documents (tenant_id, source, source_id, source_version,
                       source_modified_at, status)
VALUES ($1, $2, $3, $4, $5, 'pending')
ON CONFLICT (tenant_id, source, source_id) DO UPDATE
SET source_version     = EXCLUDED.source_version,
    source_modified_at = EXCLUDED.source_modified_at,
    status             = 'pending'
WHERE documents.source_modified_at IS NULL
   OR documents.source_modified_at < EXCLUDED.source_modified_at;
```

### A pipeline task (illustrative)

A Celery sketch; `connector_for`, `parse_in_sandbox`, `embedding_cache` and the model methods stand in for your own code.

```python
from celery import shared_task

class TransientError(Exception):
    """429, 5xx, timeouts: retry with backoff."""

class PermanentError(Exception):
    """Encrypted file, unsupported type, 403: fail fast."""

@shared_task(autoretry_for=(TransientError,), retry_backoff=True,
             retry_jitter=True, max_retries=6, acks_late=True)
def ingest_document(doc_id: int) -> None:
    doc = Document.objects.get(pk=doc_id)
    try:
        raw = connector_for(doc).download(doc.source_id)
        parsed = parse_in_sandbox(raw, timeout_s=120, memory_mb=2048)
    except PermanentError as exc:
        doc.mark_failed(error_class=type(exc).__name__)   # visible to the user
        return
    text_hash = sha256(normalize(parsed.text))
    if text_hash == doc.content_hash and doc.parser_version == PARSER_VERSION:
        doc.sync_metadata_and_acl(parsed)                 # no re-chunk, no re-embed
        return
    chunks = chunk(parsed)                                # structure-aware, stable
    missing = [c for c in chunks if not embedding_cache.has(SPACE, c.hash)]
    embed_in_batches(missing)                             # rate-limited
    replace_chunks_atomically(doc, chunks, text_hash)     # one transaction
```

## Pitfalls

- **Garbled extraction nobody looks at.** Symptom: answers that ignore tables or mix columns. Fix: parse-quality metrics per page and a sampled visual review of parsed output per source.
- **Missed deletes.** Symptom: answers cite documents that no longer exist. Fix: delta deletes plus mark-and-sweep reconciliation, and propagate to every derived store.
- **Mass deletion from a failed listing.** An expired token returns an empty listing and the sweep deletes a tenant's corpus. Fix: sweep only after complete listings, with a deletion-fraction circuit breaker.
- **ACL drift.** Permissions change in the source but not in the index. Fix: a separate, frequent ACL sync and access re-checks for sensitive sources.
- **Dedup widening access.** Merging copies with different ACLs exposes the restricted one's source. Fix: dedupe within ACL scope, or track all sources per chunk.
- **Re-embedding everything on every sync.** Symptom: embedding spend scales with corpus size instead of change rate. Fix: document and chunk hashes plus an embedding cache.
- **Parser hangs taking down workers.** Fix: sandboxed parsing with time and memory limits, dead-letter after N attempts.

## Checklist

- Persist raw, parsed and chunked outputs with stage versions; re-run only from the changed stage.
- Route parsing per page; measure parse quality per source.
- Normalize, strip boilerplate, dedupe exactly and near-exactly, and supersede old versions.
- Capture ACLs as groups, filter inside the retrieval query, and sync permissions faster than content.
- Use cursors, then document hashes, then chunk hashes; reconcile deletes safely and propagate them everywhere.
- Classify errors, retry transient ones with jitter, dead-letter the rest, and show status to users.
- Apply a PII policy per source before anything is embedded or logged.

## Interview questions

**How do you keep the index in sync with source systems?** Incremental sync from source cursors or delta APIs, content hashes to skip unchanged documents, chunk hashes to re-embed only changed chunks, version-guarded upserts, and periodic mark-and-sweep reconciliation for deletes the source never reported.

**How do you enforce permissions in multi-tenant RAG?** Capture source ACLs at ingest, map them to internal groups, denormalize to chunks, and filter by tenant and the user's groups inside the retrieval query. Sync permissions on a faster loop; add RLS and ACL regression tests.

**How do you find near-duplicates in millions of documents?** Shingle each document, compute MinHash signatures whose agreement rate estimates Jaccard similarity, and use LSH banding to compare only candidate pairs. SimHash with a small Hamming threshold is the alternative; exact hashes catch identical copies first.

**Text extraction, OCR or VLM parsing?** Text extraction for digital documents, OCR for scans, layout-aware models when structure and tables matter, and VLMs for the hardest pages. Route per page using quality checks, because VLM parsing is expensive and can hallucinate.

**What makes an ingestion pipeline resilient?** Idempotent, versioned stages; transient vs permanent errors with backoff and dead-lettering; sandboxed parsers; per-tenant fairness; freshness and parse-quality metrics.

**How do you handle PII at ingest?** A policy per data class (drop, redact, pseudonymize, restrict), detection with validated regexes, NER and secret scanners, and treating embeddings, logs and caches as being as sensitive as the text.

## Further reading

- Broder, "On the Resemblance and Containment of Documents" (1997).
- Charikar, "Similarity Estimation Techniques from Rounding Algorithms" (2002).
- Manku, Jain and Das Sarma, "Detecting Near-Duplicates for Web Crawling" (2007).
- Leskovec, Rajaraman and Ullman, "Mining of Massive Datasets", chapter 3, "Finding Similar Items".
- Auer et al., "Docling Technical Report" (2024).
- Morris et al., "Text Embeddings Reveal (Almost) As Much As Text" (2023).
