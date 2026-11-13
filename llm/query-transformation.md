---
title: Query transformation and routing
section: llm
order: 19
phase: "Phase 3 · Retrieval"
summary: Rewrite, expand, decompose and route queries before retrieval, from history rewriting and HyDE to routers and self-query filters.
---

What users type is a poor search query. "What about for enterprise?" has no subject. "Compare the refund rules for Pro and Team" needs two lookups. "Q3 incidents in the payments service" mixes a semantic query with filters a database can apply exactly. Query transformation is the step between the user and the retriever that turns a conversational turn into one or more good searches and picks where to send them. Each technique is usually an LLM call on the critical path, so the engineering question is always the same: does it raise retrieval quality on your eval enough to pay for its latency?

```text
 user turn + history
        │
        ▼
 classify / route ──► chitchat, out of scope ──► answer or decline
        │         └─► aggregate question ─────► text-to-SQL
        ▼ needs documents
 condense with history ──► standalone query
        │
        ├─► expand: multi-query, HyDE, step-back
        ├─► decompose: sub-questions for multi-hop
        └─► extract filters: product, year, doc_type
        ▼
 retrieve each query (filters in SQL) ──► fuse (RRF) ──► rerank
```

## Core ideas

Transformations serve two goals:

- **Raise recall** by closing the vocabulary gap between how users ask and how documents are written: rewriting, multi-query, HyDE, step-back.
- **Narrow scope** so the retriever searches the right place: routing, classification, metadata filters, decomposition into focused sub-queries.

Three principles keep them safe:

1. **Keep the original query** in the candidate set. A transformation should add recall, not replace the user's words.
2. **Pay the LLM latency once.** One structured call can rewrite, classify, extract filters and decompose together.
3. **Evaluate each transformation by ablation** on retrieval metrics (recall@k, MRR); the gains are real for some corpora and negative for others. See [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

## Techniques

### Rewriting with conversation history

In chat, most follow-ups are not standalone: pronouns ("does it support SSO?"), ellipsis ("and for annual?"), corrections ("no, the EU region"). Embedding "and for annual?" retrieves noise. A condense step turns the last turn plus recent history into a standalone query:

```text
Rewrite the user's last message as a standalone search query.
- Resolve pronouns and references using the conversation.
- Keep product names, error codes, versions, quoted phrases and
  negations exactly as written.
- Do not answer the question. Output only the query.
- If the message is already standalone, return it unchanged.

Conversation:
user: What are the API rate limits on the Pro plan?
assistant: Pro allows 600 requests per minute per key...
user: and for enterprise?

Standalone query:
```

Expected output: "API rate limits on the Enterprise plan". Use a small, fast model, pass only the last few turns, and skip the call entirely on the first turn or when a cheap check says the message has no unresolved references. Rewrite-Retrieve-Read (Ma et al. 2023) goes further and trains a small rewriter against retrieval reward.

### Multi-query expansion

Generate 3–5 differently worded versions of the query, retrieve for each, and fuse with reciprocal rank fusion (sometimes called RAG-Fusion). Documents that several phrasings agree on rise; documents only one phrasing finds still enter the candidate set.

Illustrative pipeline (`generate_variants`, `hybrid_search` and `rrf` are your functions; `rrf` is implemented in [Hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})):

```python
import asyncio


async def multi_query_retrieve(question: str, tenant_id: int) -> list[str]:
    variants = await generate_variants(question, n=3)   # one LLM call
    queries = [question, *variants]                     # keep the original
    rankings = await asyncio.gather(
        *(hybrid_search(q, tenant_id, limit=30) for q in queries))
    return rrf([[c.id for c in r] for r in rankings])[:50]  # then rerank
```

Cost: one LLM call plus n cheap retrievals in parallel, and a larger candidate pool for the reranker. It helps most on short, ambiguous queries over heterogeneous corpora; on precise queries it mostly adds noise.

### HyDE

Hypothetical Document Embeddings (Gao et al. 2022): ask the LLM to write a plausible answer passage, embed that passage, and search with its vector. Questions and answers live in different regions of embedding space; a fake answer looks like the real documents, so document-to-document similarity finds them. The facts in the hypothetical can be wrong; only its shape and vocabulary matter.

When it helps: zero-shot retrieval with general-purpose embeddings, natural-language questions over prose. When it hurts: identifiers and exact-match queries (the fake passage dilutes the token), niche domains where the model writes confidently wrong jargon, and latency (generating a 100–200 token passage is slower than a one-line rewrite). Embedding the query and the hypothetical together, or averaging several hypotheticals with the query vector, hedges the risk. Query2doc (Wang et al. 2023) applies the same idea to lexical search by appending the pseudo-document to the query.

### Step-back prompting

For a specific question whose answer depends on a general rule, first generate a more abstract question, then retrieve for both (Zheng et al. 2023):

- Original: "Can I get a refund if I bought an annual plan on March 3 and cancel on April 20?"
- Step-back: "What is the refund policy for annual plans?"

The specific query matches nothing well; the step-back query finds the policy, and the model applies it to the dates. Good for policy, legal and troubleshooting corpora where documents state principles rather than cases.

### Decomposition for multi-hop questions

Some questions need several retrievals:

| Shape | Example | Execution |
|---|---|---|
| Parallel | "Compare refund rules for Pro and Team" | Independent sub-queries, retrieve concurrently, merge |
| Sequential | "What is the on-call rotation of the team that owns billing?" | Hop 2 needs hop 1's answer ("payments-core"), so retrieve, read, then retrieve again |
| Aggregate | "How many P1 incidents did billing have in Q3?" | Not a retrieval problem; route to SQL |

Parallel decomposition is a single planning call. Sequential decomposition is an interleaved loop of retrieve and reason, as in Self-Ask (Press et al. 2022) and IRCoT (Trivedi et al. 2022); in production it is a small agent with a hop limit, covered in [Advanced RAG patterns]({{ '/llm/advanced-rag/' | relative_url }}) and [Agents]({{ '/llm/agents/' | relative_url }}). Bound it: 2–4 sub-questions, 2–3 hops, and fall back to the original query if planning fails.

### Routing

Routing picks the index, pipeline or model for a query: product docs vs support tickets vs code, retrieval vs no retrieval, cheap vs strong model.

| Router | How | Latency | Strengths | Weaknesses |
|---|---|---|---|---|
| Rules | Keywords, regex, UI context | ~0 ms | Predictable, free | Brittle, misses paraphrase |
| Semantic | Nearest route by embedding similarity to example utterances, with a threshold | 10–50 ms | No LLM call, easy to extend | Needs examples; weak on multi-intent |
| LLM classifier | Structured output constrained to an enum of routes | 200–800 ms | Handles nuance, can explain | Cost, latency, occasional misroutes |
| Trained classifier | Small model fine-tuned on logged queries | 1–20 ms | Fast and accurate once data exists | Needs labels and retraining |

Always include a fallback route ("search everything") for low-confidence cases, and log the route and its score with every request so you can slice quality by route.

### Self-query and metadata filters

Many queries carry constraints a database applies better than embeddings: "pricing changes in 2024 for the Team plan" is a semantic query ("pricing changes") plus filters (`year >= 2024`, `product = 'team'`). A self-query step asks the LLM to split the query into a search string and structured filters, then applies the filters as `WHERE` clauses.

Treat the extracted filters as untrusted input. Validate field names and operators against an allowlist, check types, bind values as parameters, and never let the model set security fields such as `tenant_id`; those come from the session. The demo below does exactly that. Filters also over-restrict: a 2023 document can still be the current answer to a "2024" question. On empty results, retry without the filters, or apply them as ranking boosts instead of hard predicates.

### Query classification

A classifier in front of the pipeline decides how much machinery a query needs:

- **Needs retrieval?** Chitchat and "make that shorter" need none.
- **Question type:** lookup, how-to, comparison (decompose), aggregate (SQL), opinion or out of scope (decline).
- **Complexity:** Adaptive-RAG (Jeong et al. 2024) routes between no retrieval, single-step retrieval and multi-step retrieval by predicted complexity, spending iterations only where they pay.
- **Risk:** sensitive topics routed to stricter prompts or human review; see [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

In practice one structured call returns route, type, standalone query, filters and sub-questions. Illustrative schema, used with any provider's structured-output mode (see [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }})):

```python
from typing import Literal

from pydantic import BaseModel, Field


class Filter(BaseModel):
    field: Literal["product", "year", "doc_type"]
    op: Literal["eq", "in", "gte", "lte"]
    value: str | int | list[str]


class QueryPlan(BaseModel):
    route: Literal["docs", "tickets", "sql", "no_retrieval", "out_of_scope"]
    standalone_query: str
    sub_questions: list[str] = Field(default_factory=list, max_length=4)
    filters: list[Filter] = Field(default_factory=list)

# plan = QueryPlan.model_validate_json(raw_llm_output)
```

Schema validation constrains shape; you still validate semantics (allowed values, security fields) in code.

## In practice

A runnable, stdlib-only version of three guards: a semantic router with a threshold and fallback, a self-query filter validator that produces parameterized SQL, and a rewrite check that rejects rewrites which lose identifiers, quoted phrases or negations. In production `embed()` is your embedding model and the utterance vectors are computed once at startup.

```python
import math
import re
from collections import Counter


# --- 1. Semantic router (swap embed() for your embedding model) -----------
def embed(text: str) -> Counter:
    return Counter(re.findall(r"[a-z]+", text.lower()))


def cosine(a: Counter, b: Counter) -> float:
    dot = sum(a[t] * b[t] for t in a)
    norm = math.sqrt(sum(v * v for v in a.values()))
    norm *= math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


ROUTES = {
    "billing": ["refund my invoice", "change my payment method",
                "why was my card charged twice"],
    "api_docs": ["what is the api rate limit", "how do i authenticate api "
                 "requests", "webhook retry policy"],
    "chitchat": ["hello there", "thanks that helped", "who are you"],
}
ROUTE_VECS = {name: [embed(u) for u in utts] for name, utts in ROUTES.items()}


def route(query: str, threshold: float = 0.4, default: str = "search_all"):
    q = embed(query)
    name, score = max(((n, max(cosine(q, v) for v in vecs))
                       for n, vecs in ROUTE_VECS.items()), key=lambda p: p[1])
    return name if score >= threshold else default


# --- 2. Self-query filters: validate what the LLM extracted ---------------
ALLOWED = {  # field -> (operators, python type); tenant_id is NOT here
    "product": ({"eq", "in"}, str),
    "year": ({"eq", "gte", "lte"}, int),
    "doc_type": ({"eq"}, str),
}
SQL_OPS = {"eq": "= %s", "gte": ">= %s", "lte": "<= %s", "in": "= ANY(%s)"}


def filters_to_sql(raw: list[dict]) -> tuple[str, list]:
    clauses, params = [], []
    for f in raw:
        field, op, value = f.get("field"), f.get("op"), f.get("value")
        if field not in ALLOWED or op not in ALLOWED[field][0]:
            continue  # unknown field or operator: drop, never interpolate
        typ = ALLOWED[field][1]
        values = value if op == "in" and isinstance(value, list) else [value]
        if not all(type(v) is typ for v in values):
            continue
        clauses.append(f"{field} {SQL_OPS[op]}")  # column name from allowlist
        params.append(value)
    return " AND ".join(clauses) or "TRUE", params


# --- 3. Rewrite guard: reject rewrites that drop the hard parts ------------
NEGATIONS = {"not", "no", "without", "never", "except", "excluding"}


def must_keep(text: str) -> set[str]:
    quoted = re.findall(r'"([^"]+)"', text)
    tokens = [t.strip("?,.!:;()'\"") for t in text.split()]
    ids = [t for t in tokens if re.search(r"[\d_]", t)
           or (len(t) > 1 and t.isupper())]
    return {s.lower() for s in quoted + ids}


def rewrite_is_safe(original: str, rewritten: str) -> bool:
    r = rewritten.lower()
    words = lambda s: set(re.findall(r"[a-z']+", s.lower()))
    negation_kept = not (words(original) & NEGATIONS) or words(r) & NEGATIONS
    return all(k in r for k in must_keep(original)) and bool(negation_kept)


if __name__ == "__main__":
    assert route("I was charged twice on my card") == "billing"
    assert route("rate limit for api requests") == "api_docs"
    assert route("quarterly roadmap for the mobile app") == "search_all"

    where, params = filters_to_sql([
        {"field": "year", "op": "gte", "value": 2024},
        {"field": "product", "op": "in", "value": ["pro", "team"]},
        {"field": "tenant_id", "op": "eq", "value": 7},          # injected
        {"field": "year", "op": "eq", "value": "2024; DROP"},     # bad type
        {"field": "doc_type", "op": "like", "value": "%"},        # bad op
    ])
    assert where == "year >= %s AND product = ANY(%s)"
    assert params == [2024, ["pro", "team"]]

    q = 'Why does ERR_CONN_RESET happen on v2.3.1 but not on "edge proxy"?'
    good = ('Causes of ERR_CONN_RESET on v2.3.1, not seen with the '
            '"edge proxy"')
    assert rewrite_is_safe(q, good)
    assert not rewrite_is_safe(q, "connection reset errors after upgrading")
    assert not rewrite_is_safe("plans without SSO", "plans with SSO")
    print("ok")
```

When the guard rejects a rewrite, fall back to the original query rather than failing the request.

### Latency math

Take a follow-up turn with a 400 ms planning call (rewrite, route, filters in one structured output), four parallel hybrid searches at 40 ms, and a 150 ms rerank over 80 fused candidates: about 600 ms before generation starts, versus about 200 ms with no transformation. Ways to claw it back:

- Skip planning on first turns and on messages with no references to resolve.
- Speculate: start retrieval on the raw query while the planner runs; if the plan says "standalone, no filters", use the results already in hand.
- Cache plans keyed by tenant, normalized query and a hash of the recent history, with a short TTL; see [LLM caching]({{ '/llm/llm-caching/' | relative_url }}).
- Use the smallest model that passes your routing and rewrite evals; this is classification, not reasoning.

## Pitfalls: when transformation hurts

| Technique | How it hurts | How you notice | Mitigation |
|---|---|---|---|
| History rewrite | Drops error codes, versions, negations; drifts toward the previous topic | Exact-match queries regress after enabling it | Rewrite guard, keep original in the set |
| Rewrite on standalone queries | Pure latency, occasional drift | Rewrite equals input most of the time | Skip when first turn or no references |
| Multi-query | Off-topic variants add noise; bigger rerank bill | Precision@5 drops, rerank p95 rises | 2–3 variants, rerank, cap pool |
| HyDE | Hallucinated specifics pull wrong neighbors | Wins on prose questions, losses on ids | Combine with query vector; route ids to lexical |
| Decomposition | Latency, compounding errors, over-splitting simple questions | Simple questions get slower and worse | Classify complexity first; hop limits |
| Routing | Confident misroute to the wrong index | Empty or irrelevant results on one route | Threshold plus fallback; multi-route when close |
| Self-query filters | Over-restrictive or hallucinated filter values | Empty result sets with filters applied | Retry without filters; soft boosts |

The common thread: every transformation is a model in your retrieval path that can be wrong. Log the original query, the transformed queries, the route and the filters for each request so a bad answer can be traced to the step that caused it.

## Rules of thumb

- Condense follow-ups with history first; it is the highest-value transformation for chat.
- Merge rewrite, route, filters and decomposition into one structured call.
- Keep the original query in every multi-query or HyDE set.
- Validate extracted filters like user input; security filters come from the session.
- Every router needs a fallback route and a logged confidence score.
- Ship a transformation only after an ablation shows it helps recall on your golden set.

## Interview questions

**A chat RAG system answers follow-up questions badly. What do you change first?** Add a condense step that rewrites the last turn into a standalone query using recent history, keeping identifiers and negations verbatim, and skip it when the message is already standalone.

**What is HyDE and when does it fail?** Generate a hypothetical answer, embed it and search with that vector, since answers resemble documents more than questions do. It fails on identifier and exact-match queries and in niche domains where the generated passage is confidently wrong; it also adds generation latency.

**Multi-query vs HyDE?** Multi-query creates several phrasings and fuses their results (broader recall); HyDE changes what is embedded (answer-shaped vector). Both cost an LLM call; both should keep the original query in the mix.

**How do you handle multi-hop questions?** Decompose: parallel sub-queries for comparisons, an iterative retrieve-then-reason loop for dependent hops, with limits on sub-questions and hops. Aggregations go to SQL, not retrieval.

**Semantic router vs LLM router?** Semantic routers embed the query and match it against example utterances: fast and cheap, but weak on nuanced or multi-intent queries. LLM routers classify into an enum with structured output: more accurate on hard cases, slower. Both need a fallback.

**What are the risks of self-query retrieval?** Over-restrictive or hallucinated filters, and injection if extracted values reach SQL unchecked. Validate against an allowlist, bind parameters, never let the model set tenant or permission filters, and retry without filters on empty results.

## Further reading

- Gao et al., "Precise Zero-Shot Dense Retrieval without Relevance Labels" (HyDE, 2022).
- Zheng et al., "Take a Step Back: Evoking Reasoning via Abstraction in Large Language Models" (2023).
- Ma et al., "Query Rewriting for Retrieval-Augmented Large Language Models" (2023).
- Press et al., "Measuring and Narrowing the Compositionality Gap in Language Models" (Self-Ask, 2022).
- Trivedi et al., "Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions" (IRCoT, 2022).
- Jeong et al., "Adaptive-RAG: Learning to Adapt Retrieval-Augmented Large Language Models through Question Complexity" (2024).
