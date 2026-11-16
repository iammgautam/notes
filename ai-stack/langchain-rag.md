---
title: RAG with LangChain
section: ai-stack
order: 18
phase: "Phase 3 · LangChain"
summary: Build a grounded RAG pipeline in LangChain 1.x, from Documents and splitters to filtered retrievers, citations, refusals and per-entity access control.
---
After this note you can build a retrieval-augmented generation (RAG) pipeline from LangChain's building blocks, explain its tuning knobs, and defend the two properties a finance reviewer asks about first: answers are grounded in cited sources, and a user never retrieves another entity's documents. This is the "end-to-end RAG" line in the job description and the core of the [capstone]({{ '/ai-stack/capstone-finance-rag-agent/' | relative_url }}).

## Prerequisites

[LCEL and runnables]({{ '/ai-stack/langchain-lcel-and-runnables/' | relative_url }}) and [tools and agents]({{ '/ai-stack/langchain-tools-and-agents/' | relative_url }}). For the framework-free view, read [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}).

## The pipeline in LangChain terms

RAG has an offline half that builds the index and an online half that answers. LangChain gives each stage an interface, so you can swap implementations (in-memory to pgvector, fake embeddings to Azure OpenAI) without touching the rest.

```text
 OFFLINE (Celery task / batch job)
 loader ──► Document ──► text splitter ──► Embeddings ──► VectorStore
 PDF, web,  text +       chunks with      embed_documents  add_documents
 DB rows    metadata     inherited meta                    (stable ids)

 ONLINE (FastAPI request)
 question + user ─► retriever (k, filter, MMR, threshold) ─► Documents
                         │ none above threshold? ─► refuse
                         ▼
               prompt with [S1..Sn] ─► chat model ─► validate citations
```

## Documents and metadata

Everything passes around `langchain_core.documents.Document`: a `page_content` string, a `metadata` dict and an optional `id`. The text gets embedded; the metadata is what you filter, cite and audit on, so design it up front:

| Key | Why |
|---|---|
| `source`, `page`, `section` | Citations a reviewer can open |
| `entity`, `allowed_roles` | Access control at retrieval time |
| `doc_type`, `effective_date`, `version` | Freshness and "which policy wins" |
| `content_hash`, `embedding_model` | Skip unchanged chunks, re-embed on model change |

Splitters copy a parent document's metadata onto every chunk, so stamp access metadata on the source document and it flows down.

## Loaders

Loaders turn a source into Documents via `load()` or the streaming `lazy_load()`. They live in `langchain-community` and partner packages. Illustrative (needs `langchain-community`, `pypdf` and network access):

```python
from langchain_community.document_loaders import PyPDFLoader, WebBaseLoader

pages = PyPDFLoader("policies/vendor-payments.pdf").load()   # one Document per page
for doc in WebBaseLoader("https://intranet.example.com/close-calendar").lazy_load():
    ...  # stream instead of loading everything into memory
```

On a finance platform many "documents" are rows: GL account descriptions, vendor master notes, SOPs from a CMS. Build `Document` objects from a DataFrame or SQL query directly; a loader is just a function that yields Documents.

> **In production:** PDF extraction quality limits RAG quality more than the model does. Tables in policy PDFs often come out as word soup; inspect extracted text before tuning anything downstream. See [document ingestion]({{ '/llm/document-ingestion/' | relative_url }}).

## Text splitters

A chunk should hold one idea with enough context to stand alone. Split on structure first, then cap the size:

- `MarkdownHeaderTextSplitter` splits on headers and records them as metadata, so a chunk knows it came from "Vendor payments > Approvals".
- `RecursiveCharacterTextSplitter` tries separators in order (`"\n\n"`, `"\n"`, `" "`, `""`) and only falls back to a finer one when a piece is still too big. `chunk_size` is measured by `length_function`, which defaults to `len`.
- For token-based sizing, pass a tokenizer as `length_function`, or use `RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name="cl100k_base", chunk_size=400, chunk_overlap=50)` when `tiktoken` is installed.

```python
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

POLICY_MD = """# Vendor payments
## Approvals
Invoices above INR 5,00,000 need two approvals: the cost center owner and
the finance controller. New vendors also need a procurement review.
## Payment terms
Standard terms are net 45 days from the invoice date.
# Month-end close
## Accruals
Accruals must be posted by working day 3. Suspense balances above
INR 1,00,000 are escalated to the controller.
"""

# 1. Split on structure: headers become metadata on each section.
by_header = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "policy"), ("##", "section")],
)
sections = by_header.split_text(POLICY_MD)

# 2. Cap size. Separators are tried in order: paragraphs, lines, words.
chunker = RecursiveCharacterTextSplitter(
    chunk_size=100, chunk_overlap=20,  # characters, because length_function=len
    add_start_index=True,              # metadata["start_index"] for highlighting
)
chunks = chunker.split_documents(sections)

# Token-based sizing: same splitter, different length_function.
def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)

token_chunker = RecursiveCharacterTextSplitter(
    chunk_size=25, chunk_overlap=5, length_function=approx_tokens)

for i, c in enumerate(chunks):  # access metadata, stamped before indexing
    c.metadata |= {"source": "policies/finance.md", "entity": "IN01", "chunk": i}

if __name__ == "__main__":
    assert sections[0].metadata == {"policy": "Vendor payments", "section": "Approvals"}
    assert len(chunks) > len(sections)
    assert all(len(c.page_content) <= 100 for c in chunks)
    assert all(approx_tokens(c.page_content) <= 25
               for c in token_chunker.split_documents(sections))
    print(len(sections), "sections ->", len(chunks), "chunks")
```

Real policy chunks are usually 300 to 800 tokens with 10 to 15 percent overlap; the tiny sizes here just force splits. Trade-offs are in [chunking]({{ '/llm/chunking/' | relative_url }}).

## Embeddings

The `Embeddings` interface has `embed_documents(texts)` for the index and `embed_query(text)` for the question; some models embed the two differently. `DeterministicFakeEmbedding` seeds a random vector from a hash of the text. The same text always gives the same vector, which tests plumbing, but similar texts get unrelated vectors, so it cannot test relevance:

```python
import math
from langchain_core.embeddings import DeterministicFakeEmbedding

emb = DeterministicFakeEmbedding(size=256)
q = emb.embed_query("payment terms for vendors")
docs = emb.embed_documents(["Standard terms are net 45 days.", "Hotel cap is INR 6,000."])

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

if __name__ == "__main__":
    assert len(q) == 256 and len(docs) == 2
    assert q == emb.embed_query("payment terms for vendors")  # same text, same vector
    print([round(float(cosine(q, d)), 3) for d in docs])     # near 0: no semantics
```

Illustrative real providers (need API keys):

```python
from langchain.embeddings import init_embeddings
from langchain_openai import AzureOpenAIEmbeddings, OpenAIEmbeddings

emb = OpenAIEmbeddings(model="text-embedding-3-small")   # 1536 dims
emb = AzureOpenAIEmbeddings(                             # reads AZURE_OPENAI_ENDPOINT
    azure_deployment="text-embedding-3-large",           # and AZURE_OPENAI_API_KEY
    api_version="2024-10-21",
)
emb = init_embeddings("openai:text-embedding-3-small")   # provider-agnostic factory
```

> **Gotcha:** vectors from different models or dimensions are not comparable. Store `embedding_model` in metadata and re-embed the whole collection when it changes.

## Vector stores and retrievers

A `VectorStore` stores vectors with documents and answers similarity queries. `InMemoryVectorStore` (in `langchain-core`) suits tests and demos; its `filter` is a Python callable on the Document. `as_retriever()` wraps a store in a `Runnable` with these knobs:

| `search_type` | `search_kwargs` | What it does |
|---|---|---|
| `"similarity"` | `k`, `filter` | Top k by similarity |
| `"mmr"` | `k`, `fetch_k`, `lambda_mult`, `filter` | Fetch `fetch_k`, keep `k` relevant but non-redundant; `lambda_mult` 1 = pure relevance, 0 = max diversity |
| `"similarity_score_threshold"` | `score_threshold`, `k` | Drop results below a relevance score; the store must implement a relevance function |

For meaningful retrieval without an API key, the next block defines a tiny hashed bag-of-words `Embeddings` class, which doubles as a template for wrapping an in-house embedding service. Save it as `rag_index.py`:

```python
import hashlib
import math
import re

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

STOP = {"the", "a", "an", "is", "are", "of", "for", "to", "by", "in", "on",
        "what", "who", "must", "need", "do", "does", "we", "our", "how", "and"}

class KeywordEmbedding(Embeddings):
    """Hashed bag of words. A test double that keeps similarity meaningful."""

    def __init__(self, size: int = 512):
        self.size = size

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.size
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            if w not in STOP:
                w = w[:5]  # crude stemming: "approvals" ~ "approve"
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.size] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

POLICIES = [
    ("IN01", "vendor-payments.md", "Invoices above INR 5,00,000 need two "
     "approvals: the cost center owner and the finance controller."),
    ("IN01", "vendor-payments.md", "Vendor payment terms are net 45 days "
     "from the invoice date."),
    ("IN01", "close.md", "Accruals must be posted by working day 3. Suspense "
     "balances above INR 1,00,000 are escalated to the controller."),
    ("IN01", "travel.md", "Hotel stays are reimbursed up to INR 6,000 per "
     "night. Receipts are required for any expense above INR 500."),
    ("US01", "vendor-payments.md", "Invoices above USD 10,000 need CFO "
     "approval. Vendor payment terms are net 30 days."),
]

def build_store() -> InMemoryVectorStore:
    store = InMemoryVectorStore(KeywordEmbedding())
    docs = [Document(page_content=text, metadata={"entity": e, "source": src})
            for e, src, text in POLICIES]
    # Stable ids make re-ingestion an upsert instead of a duplicate.
    store.add_documents(docs, ids=[f"{e}:{s}:{i}" for i, (e, s, _) in enumerate(POLICIES)])
    return store

def for_entity(entity: str):
    return lambda doc: doc.metadata["entity"] == entity

if __name__ == "__main__":
    store = build_store()
    q = "Who must approve a large invoice?"

    r = store.as_retriever(search_kwargs={"k": 2, "filter": for_entity("IN01")})
    top = r.invoke(q)
    assert top[0].metadata["source"] == "vendor-payments.md"
    assert all(d.metadata["entity"] == "IN01" for d in top)

    mmr = store.as_retriever(search_type="mmr", search_kwargs={
        "k": 2, "fetch_k": 4, "lambda_mult": 0.5, "filter": for_entity("IN01")})
    assert len(mmr.invoke(q)) == 2

    # Threshold by hand: InMemoryVectorStore has no relevance function,
    # so search_type="similarity_score_threshold" raises NotImplementedError.
    hits = store.similarity_search_with_score(q, k=4, filter=for_entity("IN01"))
    kept = [(d.metadata["source"], round(float(s), 2)) for d, s in hits if s >= 0.2]
    assert kept and kept[0][0] == "vendor-payments.md"
    assert not [d for d, s in store.similarity_search_with_score(
        "parental leave rules", k=4) if s >= 0.2]
    print(kept)
```

> **Gotcha:** "score" means different things per store. `InMemoryVectorStore` returns cosine similarity (higher is better); `PGVector.similarity_search_with_score` returns a distance (lower is better). Check before writing `score >= 0.2`, and calibrate thresholds per embedding model.

In production the same code points at Postgres. Illustrative (needs `langchain-postgres`, `psycopg` and a pgvector database):

{% raw %}
```python
from langchain_postgres import PGVector

store = PGVector(
    embeddings=emb,
    collection_name="finance_policies",
    connection="postgresql+psycopg://rag_app@db:5432/rag",
    use_jsonb=True,  # metadata in JSONB so filters can use it
)
store.add_documents(chunks, ids=[c.metadata["chunk_id"] for c in chunks])
retriever = store.as_retriever(search_kwargs={
    "k": 6,
    "filter": {"entity": {"$eq": "IN01"}, "doc_type": {"$in": ["policy", "sop"]}},
})
```
{% endraw %}

Newer `langchain-postgres` releases also ship `PGVectorStore` with a `PGEngine` connection pool and a custom table schema; prefer it for new projects. Index tuning is in [pgvector]({{ '/postgresql/pgvector/' | relative_url }}).

## Access control with metadata filters

In finance the retriever is a security boundary. A user from entity IN01 must not get US01's approval matrix into their prompt, even if the model would never repeat it.

1. Derive the filter from the authenticated principal (JWT claims, DB lookup), never from the question or a model-generated argument.
2. Apply it inside the vector search (pre-filter). Post-filtering returns fewer than `k` results and pulls other entities' text into your process, traces and logs.
3. Stamp `entity` and role metadata at ingestion and reject documents without them.
4. Test it: one assertion per entity that results never cross the boundary.

With pgvector and many tenants, a filtered HNSW search can return too few rows; partial indexes for large tenants or iterative index scans fix that.

## Multi-query and contextual compression

- **Multi-query:** ask a model for two or three paraphrases, search with each, and union the results. It fixes vocabulary mismatch ("sign-off for big bills" versus "invoice approvals") for one extra model call.
- **Contextual compression:** shrink or drop chunks after retrieval with a threshold, a cross-encoder reranker, or an LLM that extracts only relevant sentences. Tighter context costs fewer tokens and gives the model less to misuse.

The 0.x classes `MultiQueryRetriever` and `ContextualCompressionRetriever` now live in `langchain-classic`. In 1.x, write them as a few lines of LCEL, as below, or as a LangGraph node.

## A grounded chain with citations and refusal

Grounding has three layers, and the code enforces two of them instead of trusting the prompt:

1. **No context, no call.** If nothing passes the threshold, return a fixed refusal without calling the model.
2. **Prompt contract.** Numbered sources, "use only these", cite like `[S1]`, an exact refusal sentence.
3. **Output check.** The answer must cite at least one source, and every cited number must exist; otherwise refuse.

This block imports `rag_index.py` and runs end to end with a fake model:

```python
import re

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from rag_index import build_store, for_entity

REFUSAL = "I could not find this in the policies you have access to."

prompt = ChatPromptTemplate.from_messages([
    ("system", "You answer finance policy questions. Use only the numbered "
               "sources below and cite every claim like [S1]. If the sources "
               "do not answer the question, reply exactly: {refusal}"),
    ("human", "Sources:\n{context}\n\nQuestion: {question}"),
]).partial(refusal=REFUSAL)

def retrieve(store, question: str, entity: str, k: int = 4, min_score: float = 0.2):
    hits = store.similarity_search_with_score(question, k=k, filter=for_entity(entity))
    return [doc for doc, score in hits if score >= min_score]

def format_docs(docs) -> str:
    return "\n".join(f"[S{i}] ({d.metadata['source']}) {d.page_content}"
                     for i, d in enumerate(docs, start=1))

def make_rag(store, llm, rewriter=None):
    generate = prompt | llm | StrOutputParser()

    def run(inp: dict) -> dict:
        queries = [inp["question"]]
        if rewriter is not None:  # multi-query: search with paraphrases too
            queries += rewriter.invoke({"question": inp["question"]}).splitlines()
        found = {}
        for q in queries:
            for doc in retrieve(store, q, inp["entity"]):
                found.setdefault(doc.id, doc)  # dedupe across queries
        docs = list(found.values())
        if not docs:  # refuse before paying for a model call
            return {"answer": REFUSAL, "sources": []}
        text = generate.invoke({"context": format_docs(docs), "question": inp["question"]})
        cited = {int(n) for n in re.findall(r"\[S(\d+)\]", text)}
        if not cited or not cited <= set(range(1, len(docs) + 1)):
            return {"answer": REFUSAL, "sources": []}  # uncited or invented source
        return {"answer": text,
                "sources": sorted({docs[i - 1].metadata["source"] for i in cited})}

    return RunnableLambda(run, name="policy_rag")

if __name__ == "__main__":
    store = build_store()
    # Real model (illustrative): llm = init_chat_model("openai:gpt-4.1-mini", temperature=0)
    llm = FakeListChatModel(responses=[
        "Two approvals: the cost center owner and the finance controller [S1].",
        "CFO approval is required above USD 10,000 [S1].",
        "Approval is needed from the treasury head [S7].",  # bad citation
        "The cost center owner and the finance controller approve it [S1].",
    ])
    rag = make_rag(store, llm)
    q = "Who must approve a large invoice?"

    india = rag.invoke({"question": q, "entity": "IN01"})
    assert india["sources"] == ["vendor-payments.md"] and "[S1]" in india["answer"]

    us = rag.invoke({"question": q, "entity": "US01"})  # same question, other entity
    assert "USD" in us["answer"]
    assert all(d.metadata["entity"] == "US01" for d in retrieve(store, q, "US01"))

    calls_before = llm.i
    assert rag.invoke({"question": "parental leave rules", "entity": "IN01"})["sources"] == []
    assert llm.i == calls_before  # no context, no model call

    assert rag.invoke({"question": q, "entity": "IN01"})["answer"] == REFUSAL  # [S7]

    vague = {"question": "sign-off for big bills?", "entity": "IN01"}
    assert make_rag(store, llm).invoke(vague)["answer"] == REFUSAL
    rewriter = (ChatPromptTemplate.from_template("Rephrase two ways: {question}")
                | FakeListChatModel(responses=["Who approves invoices?\nInvoice approval rules"])
                | StrOutputParser())
    assert make_rag(store, llm, rewriter).invoke(vague)["sources"] == ["vendor-payments.md"]
    print(india, us, sep="\n")
```

Wrapping the function in `RunnableLambda` keeps it a first-class runnable with `batch`, `ainvoke` and a named span in traces. In a FastAPI route, `entity` comes from the auth dependency and the response carries `sources` so the UI can link each citation.

## Retrieval tuning knobs

Tune against an eval set ([observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }})), one knob at a time:

| Knob | Raise it when | Cost of raising |
|---|---|---|
| `chunk_size` | Answers need surrounding context | Diluted embeddings, more tokens |
| `chunk_overlap` | Facts split across chunk edges | Duplicate chunks in results |
| `k` | Context recall is low | Tokens, noise, "lost in the middle" |
| `fetch_k` (MMR) | Top results are near-duplicates | Search latency |
| Score threshold | Irrelevant chunks reach the prompt | More refusals |
| Multi-query | Users phrase things unlike the docs | One model call |
| Reranker | Right chunk is in top 50, not top 5 | 50 to 200 ms per query |
| Hybrid (BM25 + vector) | Codes, IDs, GL account numbers | Second index |

Exact identifiers such as invoice numbers and GL codes are where pure vector search is weakest; see [hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}).

## Coming from Django

| Django | LangChain RAG |
|---|---|
| Management command or Celery task importing data | Loader, splitter and `add_documents` ingestion job |
| `QuerySet.filter(tenant=request.tenant)` | Retriever `filter` built from the authenticated user |
| Custom manager that always scopes by tenant | A `make_retriever(user)` factory used by every route |
| Template rendering a context dict | Prompt template rendering sources and question |

The mental shift: Django queries are exact, retrieval is ranked and fuzzy. You always need a threshold and a "nothing found" path.

## Common mistakes

- **Filtering after retrieval.** Fewer than `k` results and other entities' text in your logs. Pre-filter inside the store query.
- **Letting the model choose the entity.** A prompt injection can switch it. Bind the filter from auth.
- **Tuning on three hand-picked questions.** Build a labeled eval set of 50 first.
- **Trusting prompt-only grounding.** Models cite sources that do not exist. Validate citation ids in code.
- **Duplicates on re-ingestion.** `add_documents` without ids appends. Use stable ids per chunk.
- **Assuming one score direction.** Check similarity versus distance before setting thresholds.

## Practice tasks

1. **Ingest a folder.** Split three Markdown policies by header then size, stamp `entity` and `source`, and index them. Done: every chunk has both keys (asserted).
2. **Threshold sweep.** With 10 answerable and 5 unanswerable questions, sweep the threshold from 0.05 to 0.5. Done: a table of correct answers versus wrong refusals per value, and a chosen threshold.
3. **Role filter.** Add `allowed_roles` and filter on entity and role together. Done: a test proves an analyst cannot retrieve a "controller only" chunk.
4. **Rerank stage.** Re-score the top 8 with a stub cross-encoder (word overlap) and keep 3. Done: a question whose best chunk was rank 4 now gets it at rank 1.
5. **FastAPI endpoint.** `POST /ask` returns `answer` and `sources`, with `entity` from a dependency. Done: `TestClient` tests for an answer, a refusal and a cross-entity attempt.

## Interview questions

**How do you stop a RAG system from answering outside its documents?**
A retrieval threshold with a refusal that skips the model call; a prompt restricted to numbered sources; code that validates citations and refuses when they are missing or invalid; and faithfulness measured on an eval set.

**Where do you enforce document-level access control?**
In the retrieval query, as a metadata pre-filter derived from the authenticated user. Not in the prompt, not in a post-filter, never from model output.

**What does MMR do and when do you use it?**
It picks results that are relevant to the query and dissimilar to those already picked. Use it when top-k returns near-duplicates, such as one clause from five policy versions.

**How do you choose chunk size?**
Follow document structure (headers, clauses), then cap at a size that holds one idea, often 300 to 800 tokens with some overlap. Validate with context recall and faithfulness, not intuition.

**What changed in LangChain 1.x for retrieval?**
The core interfaces (`Document`, `Embeddings`, `VectorStore`, retrievers) are unchanged in `langchain-core`. Legacy chains such as `RetrievalQA` and `create_retrieval_chain`, plus `MultiQueryRetriever` and `ContextualCompressionRetriever`, moved to `langchain-classic`. New code composes retrieval with LCEL, as a tool for `create_agent`, or as a LangGraph node.

**Your RAG answers are correct but slow. Where do you look?**
The trace: embedding call, vector search, reranker, generation. Generation usually dominates, so cut context (lower `k`, compression) and stream. Cache query embeddings and frequent answers per entity and policy version.

**How do you handle policy versions?**
Store `effective_date` and `version` in metadata, filter to the version in force by default, and cite the version so auditors can trace it.

## Further reading

- LangChain docs: "Retrieval" and "Build a RAG agent" (Python, 1.x)
- LangChain reference: `langchain_text_splitters`, `InMemoryVectorStore`, `VectorStoreRetriever`
- `langchain-postgres` README: `PGVector` and `PGVectorStore`
- Related notes: [embeddings]({{ '/llm/embeddings/' | relative_url }}), [vector search]({{ '/llm/vector-search/' | relative_url }}), [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }})
