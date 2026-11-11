---
title: "Building a RAG platform on Postgres and pgvector"
summary: "Provider abstraction and multi-model routing, an embeddings pipeline, pgvector retrieval, tools, structured outputs and agents, all on Postgres."
tags: [llm, rag, postgresql, pgvector]
---

At Gainfront I engineered an AI/RAG platform with multi-model LLM routing. The pieces were provider abstractions, embeddings, pgvector similarity search, tool orchestration, structured outputs and multi-step agent workflows. The vector store was not a separate service: it was the PostgreSQL we already ran, with pgvector.

This post walks through how those pieces fit together and why I think Postgres is the right default for this kind of platform inside an existing SaaS backend.

## The problem

Once a product wants "AI features", the requests arrive one at a time: summarize this, answer questions over these documents, extract these fields, take this action. Build each one ad hoc and you end up with several copies of the same LLM client code, different retry logic everywhere, no shared way to switch models and no single place to see cost or failures.

> **TODO (author):** describe, in generic terms, the first one or two product features this platform served.

The goal was a platform: one way to call models, one way to embed and retrieve data, one way to give models tools and get structured answers back, used by every feature.

## Constraints

- **Multi-tenant from the first line.** Retrieval must never cross a tenant boundary. This is a security property, not a relevance tweak.
- **More than one model provider.** Models differ in quality, latency, price and features, and they change constantly. Feature code should not care which one it is talking to.
- **Outputs the code can trust.** Downstream code needs typed data, not prose that it has to parse with regexes.
- **Fits the existing stack.** Django services, PostgreSQL, the existing deployment and monitoring.

> **TODO (author):** list the actual providers/models in use if you are comfortable naming them, or keep this generic.

## Why Postgres and pgvector

The obvious alternative is a dedicated vector database. For this platform the arguments for Postgres win:

| Concern | pgvector in the existing Postgres | Separate vector DB |
|---|---|---|
| Tenant filtering | Same `WHERE tenant_id = ...` as everything else | Metadata filters, a second place to get isolation right |
| Consistency | Chunks and source rows in one transaction | Sync job, eventual consistency, orphaned vectors |
| Joins | Join chunks back to documents, permissions, metadata in SQL | Fetch IDs, then query Postgres anyway |
| Operations | Existing backups, monitoring, migrations | A new stateful service |
| Scale ceiling | Lower; fine for millions of vectors with HNSW | Higher, if you truly need it |

The scale ceiling is real, but most SaaS RAG corpora are per-tenant and modest. Paying the operational cost of a second database before you hit that ceiling is a bad trade.

## What I built

What follows are simplified sketches to show the architecture. They are not the production code.

### Provider abstraction

Every provider is wrapped behind one small interface. Feature code depends on the interface, never on a vendor SDK.

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class ChatResult:
    text: str
    tool_calls: list
    input_tokens: int
    output_tokens: int
    model: str

class ChatProvider(Protocol):
    def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
             response_schema: dict | None = None) -> ChatResult: ...

class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Each adapter translates to and from its vendor's message format, tool-call format and error types. Retries, timeouts and token accounting live in one place instead of in every feature.

### Multi-model routing

Routing picks a concrete model for each call. A simple and effective version routes by *task profile* rather than by feature:

```python
ROUTES = {
    "classify":  ["small-fast-model", "fallback-small-model"],
    "extract":   ["structured-output-model", "fallback-model"],
    "reason":    ["large-model", "fallback-large-model"],
}

def route(profile: str) -> list[ChatProvider]:
    return [PROVIDERS[name] for name in ROUTES[profile]]

def chat(profile: str, messages, **kw) -> ChatResult:
    last_exc = None
    for provider in route(profile):
        try:
            return provider.chat(messages, **kw)
        except (ProviderTimeout, ProviderUnavailable, RateLimited) as exc:
            last_exc = exc   # fall through to the next model
    raise last_exc
```

Two things this buys you: cheap models for cheap tasks, and a fallback when a provider is slow or down. Model names here are placeholders.

> **TODO (author):** describe the actual routing criteria used (task type, tenant, cost, latency, capability) and whether fallback was automatic.

### Embeddings pipeline

Documents are split into chunks, embedded and stored next to the data they came from:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE doc_chunk (
    id          bigserial PRIMARY KEY,
    tenant_id   bigint  NOT NULL,
    document_id bigint  NOT NULL,
    chunk_index int     NOT NULL,
    content     text    NOT NULL,
    embedding   vector(1536) NOT NULL,   -- dimension depends on the model
    model       text    NOT NULL,       -- which embedding model produced it
    UNIQUE (document_id, chunk_index, model)
);

CREATE INDEX doc_chunk_embedding_hnsw
    ON doc_chunk USING hnsw (embedding vector_cosine_ops);

CREATE INDEX doc_chunk_tenant_idx ON doc_chunk (tenant_id);
```

A few design choices that matter more than the chunk size:

- **Store the embedding model name.** You will change models. Vectors from different models are not comparable, so you need to know which rows to re-embed.
- **Make the pipeline idempotent.** The unique key on `(document_id, chunk_index, model)` lets you re-run embedding for a document with an upsert instead of creating duplicates.
- **Embed in batches, in the background.** Embedding is a network call per batch; it does not belong in a web request.

> **TODO (author):** fill in the real chunking strategy (size, overlap, structure-aware or not), the embedding dimension, and how embedding jobs were scheduled.

### Retrieval

Retrieval is plain SQL, which is exactly why Postgres is pleasant here. Tenant isolation is a `WHERE` clause, and you can join back to whatever metadata you need:

```sql
SELECT c.id, c.content, d.title,
       c.embedding <=> %(query_vec)s AS distance
FROM   doc_chunk c
JOIN   document d ON d.id = c.document_id
WHERE  c.tenant_id = %(tenant)s
ORDER  BY c.embedding <=> %(query_vec)s
LIMIT  %(k)s;
```

`<=>` is cosine distance; it must match the operator class the index was built with (`vector_cosine_ops`), or the index is not used.

> **Gotcha:** HNSW is an approximate index and filters are applied to what it returns. With a selective `tenant_id` filter on a shared index, you can get fewer than `k` rows back. Options: raise `hnsw.ef_search`, enable iterative index scans (`hnsw.iterative_scan`, pgvector 0.8+), or partition by tenant so each index only contains one tenant's vectors.

Combining vector similarity with Postgres full-text search (hybrid retrieval) is also just SQL: run both, then merge the ranked lists, for example with reciprocal rank fusion.

### Structured outputs

Feature code asks for a schema, not for text. The model's answer is validated before anything else sees it:

```python
from pydantic import BaseModel, ValidationError

class InvoiceFields(BaseModel):
    vendor: str
    total: float
    currency: str

def extract(text: str) -> InvoiceFields:
    schema = InvoiceFields.model_json_schema()
    for _ in range(2):
        result = chat("extract", build_messages(text), response_schema=schema)
        try:
            return InvoiceFields.model_validate_json(result.text)
        except ValidationError as err:
            text = with_repair_hint(text, err)   # tell the model what was wrong
    raise ExtractionFailed()
```

Providers that support native structured output or JSON schema constraints make this more reliable, but validate anyway. A schema-shaped answer can still be wrong.

### Tool orchestration and multi-step agents

Tools are ordinary Python functions with a JSON schema description. The orchestrator runs the loop: call the model, execute any tool calls it asks for, feed results back, and stop when it answers or hits a limit.

```python
MAX_STEPS = 6

def run_agent(profile, messages, tools, ctx):
    for _ in range(MAX_STEPS):
        result = chat(profile, messages, tools=[t.schema for t in tools])
        if not result.tool_calls:
            return result.text
        messages.append(assistant_message(result))
        for call in result.tool_calls:
            tool = TOOLS_BY_NAME[call.name]
            output = tool.run(call.arguments, tenant_id=ctx.tenant_id)
            messages.append(tool_message(call.id, output))
    raise AgentStepLimit()
```

The parts that matter in production are not in the happy path:

- **Hard step limits** so a confused model cannot loop forever and burn tokens.
- **Tenant context injected by the orchestrator,** never taken from the model's arguments. The model asks for "search documents"; the code decides whose documents.
- **Validated tool arguments,** with errors returned to the model as tool output so it can correct itself.
- **Logging every step** (model, tokens, tool calls, latency) so a bad answer can be traced back to the retrieval or tool result that caused it.

## Results

The platform delivered multi-model LLM routing behind provider abstractions, an embeddings pipeline, pgvector similarity search inside the existing PostgreSQL, tool orchestration, structured outputs and multi-step agent workflows.

> **TODO (author):** add any measured outcomes you can share (retrieval latency, number of features built on the platform, cost or quality changes). Do not estimate.

## What I would do differently

- **Build the evaluation set first.** A few dozen real questions with expected sources makes every change to chunking, embeddings or prompts measurable instead of a matter of taste.
- **Version prompts like code.** Prompts change behavior as much as code does, so they belong in review and in logs.
- **Plan for re-embedding from day one.** Model upgrades are inevitable; storing the model name per row is what makes them cheap.

The broader lesson: most of a RAG platform is ordinary backend engineering. Interfaces, retries, idempotent pipelines, tenant isolation and logging do more for answer quality and reliability than any single model choice. And for a Django and Postgres shop, keeping the vectors next to the data removes a whole category of consistency and security problems.
