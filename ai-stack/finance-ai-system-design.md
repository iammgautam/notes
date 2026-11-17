---
title: AI system design for finance data
section: ai-stack
order: 30
phase: "Phase 5 · Putting it together"
summary: Design an AI assistant over GL, AP/AR and reconciliation data with exact numbers, scoped access, audit, PII controls, evals and a safe rollout.
---

"Design an AI assistant for our finance data platform" is the system design question this role is built around. After this note you can run that interview: pin down requirements, map ERP and lake data sources, split the problem into policy RAG and guarded analytics tools, defend numeric accuracy and access control, and finish with evals, cost and rollout. The hard constraint, the one to name in the first five minutes, is this: **every number must be correct and traceable, and every user sees only what the ERP already lets them see.**

## Prerequisites

The [capstone]({{ '/ai-stack/capstone-finance-rag-agent/' | relative_url }}) (the same ideas at project scale) and the general framework in [AI system design interviews]({{ '/llm/ai-system-design/' | relative_url }}).

## Requirements

Ask clarifying questions first, then state assumptions out loud.

**Functional**

- Policy and process questions with citations: approval matrix, close calendar, accounting policies, SOPs.
- Numeric questions over GL balances, AP and AR (aging, open items, top vendors), and reconciliation status, with drill-down to source records.
- Variance explanations: "why is March opex 18% over February?" broken down by account, vendor and cost center.
- Draft commentary for monthly flux reports, always reviewed by a person.
- Out of scope for v1: posting journals, approving payments, any write to the ERP.

**Non-functional**

| Concern | Target |
|---|---|
| Numeric accuracy | 100% on the golden numeric set; every number links to a query |
| Access | Same scope as ERP data access sets (entity, ledger, cost center) |
| Audit | Every question, tool call, data version and answer, append-only |
| Latency | p95 under 3 s to first token, under 15 s for multi-step analysis |
| Load | ~500 users, ~10,000 questions a day, 3x at month-end close |
| Residency | Data and model endpoints in-region |

## Data sources and ingestion

| Source | What it holds | How it arrives |
|---|---|---|
| ERP (e.g. Oracle Fusion) | GL journals and balances, AP/AR invoices, payments, suppliers, chart of accounts | Bulk extracts (BI Cloud Connector) to object storage; REST for small lookups |
| Data lake (e.g. Databricks) | Bronze raw, silver cleaned, gold reporting tables | Scheduled incremental jobs on last-update timestamps |
| Reconciliation tool | Match results, exceptions, sign-offs | Daily extract into the lake |
| Document stores | Policies, close checklists, audit memos | Change-driven ingest by content hash |
| Identity provider | Users, groups, ERP data access sets | Synced into an entitlements table |

Two principles shape ingestion:

1. **Structured data is never embedded.** Balances live in gold tables and are queried by tools at answer time. Embedding ledger rows gives fuzzy retrieval of numbers that must be exact.
2. **Every answer carries `as_of`.** Extracts lag the ERP by hours. The tool returns the data timestamp and period status (open or closed), and the answer states both.

Documents follow the usual RAG pipeline (parse, chunk by section, embed, index) with metadata on every chunk: doc id, version, effective date, owner, audience. Deleting or superseding a policy must remove it from the index, not just add the new one; see [document ingestion]({{ '/llm/document-ingestion/' | relative_url }}).

A **semantic layer** sits on top of gold tables: named, finance-reviewed metric definitions (opex, AP aging buckets, DSO, unreconciled count) with their filters and sign conventions. The assistant asks for metrics; it never invents them.

## Architecture

```text
 users (web, Teams) --SSO--> gateway: rate limits, request id
                                 |
+----------------- assistant service (FastAPI) -----------------+
| auth -> Scope(entities, ledgers, cost centers, audiences)     |
| router (small model): policy | metric | variance | refuse     |
|                                                               |
|  policy RAG        metric tool          variance agent        |
|  hybrid + rerank   LLM -> MetricQuery   (LangGraph: metric    |
|  audience filter   code -> scoped SQL    tool x N + RAG)      |
|        \                |                  /                  |
|         +--> composer: tool numbers, citations, as_of,        |
|              grounding check, PII redaction --> SSE stream    |
+-------|-----------------|-----------------------|-------------+
        v                 v                       v
  vector index      SQL warehouse:           audit log + traces
  (policy chunks)   gold tables, row         (append-only)
        ^           filters, column masks
        |                 ^
  doc ingest        ERP extracts -> lake (bronze/silver/gold)
```

The router is a cheap classifier, not an autonomous agent. Most traffic is a single policy lookup or a single metric query; only variance questions need a multi-step LangGraph loop. Choosing the least autonomous design that meets each need keeps latency, cost and failure modes small.

## Numeric questions: tools, not arithmetic

The model translates a question into a structured request. Code turns the request into SQL, applies the user's scope and runs it. The model then writes prose around numbers it copied.

Free-form text-to-SQL is the tempting alternative. It fails in finance in predictable ways: invented joins, wrong sign conventions (credits as positive spend), mixed currencies, missing period filters, and columns the user should not see. The safer design is a metric registry: the LLM fills a `MetricQuery`, and reviewed code compiles it.

```python
"""Guarded analytics: the model picks a metric; code writes and runs the SQL."""
import sqlite3
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

METRICS = {  # reviewed by finance, versioned like code
    "ap_spend": "SUM(amount)",
    "unreconciled_count": "SUM(CASE WHEN reconciled = 0 THEN 1 ELSE 0 END)",
}


class MetricQuery(BaseModel):  # the only thing the LLM is allowed to produce
    metric: Literal["ap_spend", "unreconciled_count"]
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    group_by: Literal["vendor", "cost_center"] | None = None


def compile_sql(q: MetricQuery, entities: list[str]) -> tuple[str, list]:
    if not entities:
        raise PermissionError("user has no entity access")
    dims = f"{q.group_by}, " if q.group_by else ""
    marks = ", ".join("?" * len(entities))
    sql = (f"SELECT {dims}{METRICS[q.metric]} AS value FROM ap_invoices "
           f"WHERE period = ? AND entity IN ({marks})")
    if q.group_by:
        sql += f" GROUP BY {q.group_by} ORDER BY value DESC"
    return sql + " LIMIT 1000", [q.period, *entities]


def read_only(tables: set[str]):
    """sqlite authorizer: SELECT on allowlisted tables, deny everything else."""
    def check(action, arg1, arg2, db_name, trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 in tables:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return check


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    pd.DataFrame({
        "entity": ["IN01", "IN01", "IN01", "US01"],
        "period": ["2026-03"] * 4,
        "vendor": ["Acme Cloud", "Acme Cloud", "Zen Office", "Acme Cloud"],
        "cost_center": ["IT", "IT", "Admin", "IT"],
        "amount": [125_000, 98_000, 15_000, 50_000],
        "reconciled": [1, 0, 1, 0],
    }).to_sql("ap_invoices", conn, index=False)
    pd.DataFrame({"employee": ["e1"], "salary": [1]}).to_sql(
        "payroll", conn, index=False)
    conn.set_authorizer(read_only({"ap_invoices"}))
    return conn


if __name__ == "__main__":
    conn = connect()
    llm_json = '{"metric": "ap_spend", "period": "2026-03", "group_by": "vendor"}'
    sql, params = compile_sql(MetricQuery.model_validate_json(llm_json), ["IN01"])
    out = pd.read_sql(sql, conn, params=params)
    assert out["vendor"].tolist() == ["Acme Cloud", "Zen Office"]
    assert out["value"].tolist() == [223_000, 15_000]  # US01 excluded by scope

    try:  # an injected group_by never reaches SQL
        MetricQuery(metric="ap_spend", period="2026-03", group_by="vendor; DROP")
        raise AssertionError("validation should fail")
    except ValidationError:
        pass

    for bad in ("SELECT * FROM payroll", "DELETE FROM ap_invoices"):
        try:  # even free-form model SQL cannot read other tables or write
            conn.execute(bad)
            raise AssertionError(bad)
        except sqlite3.DatabaseError:
            pass  # "prohibited" / "not authorized"
    print("guarded analytics: all checks passed")
```

Read it as three layers. Pydantic `Literal` and `pattern` fields reject anything outside the registry, so an injected `group_by` never becomes SQL. Scope (`entities`) comes from the authenticated user, never from the model. The database itself refuses reads outside the allowlist and all writes: here via sqlite's authorizer, in production via a read-only role, row-level security or Unity Catalog row filters, `statement_timeout` and a forced `LIMIT`. If you later allow free-form SQL for analysts, those last two layers still hold.

Rules that keep numbers right:

- **Never let the LLM do arithmetic,** not even a percentage. Tools return derived values (delta, percent change, share) alongside raw totals.
- **Variance is computed, then narrated.** Code decomposes the change by account, vendor or cost center and returns the top drivers; the model explains them.
- **Units and conventions are explicit.** Currency, sign convention and functional versus reporting currency come back with every result; conversions use the official period rate table in code.
- **Check before sending.** A grounding step confirms every number in the answer appears in tool output, as in the capstone's `grounded()`. Failures retry once, then return the table without narrative.
- **Invariants are data checks.** Debits equal credits per journal and subledger totals tie to GL control accounts. When a check fails, the assistant says the data is inconsistent instead of answering.

## Policy RAG

Hybrid retrieval (BM25 plus dense) matters here because users type exact tokens: GL account numbers, policy ids, vendor names. Rerank the merged list, filter by audience and effective date inside the search, and require citations that are validated against the retrieved ids. When nothing relevant comes back, the correct answer is "no policy found", which is an eval case, not a failure. See [hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}).

## Access control and audit

- **One scope object per request,** built from the identity provider and ERP data access sets, passed to tools as runtime context and never shown to the model.
- **Enforced at the data,** not in prompts: SQL row filters and column masks for structured data, metadata filters for documents. Caches are keyed by scope and data version.
- **Segregation of duties:** the assistant is read-only. Any action (flagging an invoice, requesting a journal) goes through the existing ERP workflow with its own approvals.
- **Audit record per answer:** user, question, router decision, each tool call with arguments, query ids, document ids, data `as_of`, model and prompt versions, final answer, latency and tokens. Store it append-only with the firm's financial-records retention period.

Auditors ask "how was this number produced?" The audit record plus a frozen query id lets you rerun the exact query and show the same result.

## PII

Finance data carries vendor bank accounts, tax ids (PAN, GSTIN), employee names in expense lines and customer contacts. Controls, in order of strength: mask columns at the source so the assistant cannot read them; do not select fields the answer does not need; redact free text (invoice notes, emails) before any model call; keep PII out of logs and traces. A regex pass is the floor, not the ceiling; pair it with a DLP service or Presidio.

```python
import re

PATTERNS = {  # floor, not ceiling: use a DLP service or Presidio as well
    "PAN": r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
    "IFSC": r"\b[A-Z]{4}0[A-Z0-9]{6}\b",
    "ACCT": r"\b\d{9,18}\b",
    "EMAIL": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
}


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Swap PII for stable tokens; keep the map server-side, never in prompts."""
    found: dict[str, str] = {}
    for label, pattern in PATTERNS.items():
        for value in sorted(set(re.findall(pattern, text))):
            token = f"<{label}_{len(found) + 1}>"
            found[token] = value
            text = text.replace(value, token)
    return text, found


def restore(text: str, found: dict[str, str]) -> str:
    for token, value in found.items():
        text = text.replace(token, value)
    return text


if __name__ == "__main__":
    note = ("Vendor PAN ABCDE1234F changed bank to a/c 123456789012, "
            "IFSC HDFC0001234; confirm with ap@acme.in. Invoice INV-77 total 98000.")
    safe, found = redact(note)
    for raw in ("ABCDE1234F", "123456789012", "HDFC0001234", "ap@acme.in"):
        assert raw not in safe
    assert "98000" in safe and "INV-77" in safe  # amounts and ids survive
    assert restore(safe, found) == note
    print(safe)
```

The token map stays on the server. If the answer needs the real value (rare), the composer restores it after the model call, for users entitled to see it.

## Evaluation

Build a golden set before the first demo and grow it from production.

| Slice | Example | Check |
|---|---|---|
| Numeric | "AP spend for IN01 in March by vendor" | Exact match against SQL on a frozen snapshot |
| Metric selection | Same question, paraphrased five ways | `MetricQuery` equals the expected one |
| Variance | "Why did opex rise in March?" | Top drivers match the computed decomposition |
| Policy | "Who approves a 7 lakh invoice?" | Cited id correct; faithfulness score |
| Access | Clerk asks about another entity | Empty or refused, never leaked |
| Out of scope | "Post a reversing journal" | Polite refusal |

Run numeric and access slices on every change with a zero-tolerance gate. Score policy answers with RAGAS faithfulness and context precision, with an LLM judge calibrated against finance reviewers. Freeze the data snapshot, or expected numbers drift as new invoices land. Online, give users a "dispute this number" button that routes to an analyst; every dispute becomes a golden case. See [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).

## Latency and cost

| Stage | p95 budget |
|---|---|
| Auth, scope, router (small model) | 400 ms |
| Retrieval and rerank | 300 ms |
| SQL on gold tables | 200 ms to 1.5 s |
| Answer generation to first token | 600 ms |
| Grounding check | 50 ms (code, no model) |

Cost levers: a small model for routing and metric extraction and a larger one only for variance narratives; prompt caching on the stable system prompt and tool schemas; result caching keyed by `(metric, filters, scope, data version)`, which is safe because it caches computed results rather than generated text; and a step cap on the variance agent. At month-end, reserve provider capacity (provisioned throughput on Azure OpenAI) or queue long analyses and notify on completion. More in [latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).

## Rollout

1. **Shadow:** finance analysts ask real questions; answers are compared with their own work, not shown to others.
2. **Pilot:** one team, one entity, policy Q&A plus ten metrics, with thumbs and dispute feedback.
3. **Expand:** more metrics and entities, then variance explanations, after a month-end load test.
4. **Steady state:** feature flags per team, a kill switch, weekly review of disputes and refusals, and model upgrades gated by the eval suite.

Success metrics: share of questions answered without analyst help, dispute rate, analyst hours saved during close, p95 latency.

## Common mistakes

- Embedding ledger rows and asking the model to sum retrieved numbers.
- Free text-to-SQL with a privileged connection "because it's read-only".
- Access control in the system prompt.
- No `as_of` or period status, so users act on stale or open-period numbers.
- Evals with fuzzy matching on numeric answers.

## Practice tasks

1. Add an `ap_aging` metric with buckets (0–30, 31–60, 61–90, 90+) to the registry. Done: exact-match tests per bucket.
2. Write a variance tool that decomposes month-over-month change by vendor and returns the top three drivers with their share. Done: the shares sum to 100% in code and a fake-model test narrates them.
3. Draw your audit table schema and write the query that reproduces any answer from its audit row. Done: the query returns the same numbers.
4. Present this design in 35 minutes to a peer, then answer the follow-ups below without notes.

## Interview questions

Common follow-ups after you present the design:

**Why not fine-tune a model on the ledger?** Ledgers change daily and numbers must be exact. Fine-tuning teaches format and style, not facts you can audit. Query the data at answer time.

**What if the period is still open?** Return period status with the data timestamp, label the numbers provisional, and route close-sensitive questions to closed periods by default.

**How do you handle multi-currency?** Tools convert with the official rate table for that period and return both currencies. The model never converts.

**A user asks for a metric you don't have. What happens?** Say so, offer the closest defined metric, and log the request for the finance data team. Do not fall back to free SQL in v1.

**How do you stop the assistant from becoming an exfiltration path?** Scope at the data, row limits per answer, no bulk export, per-user rate limits, and alerts on unusual query volume in the audit log.

**Databricks or Postgres for this?** Use where the gold data already lives. With Databricks, Unity Catalog row filters and column masks enforce scope, and SQL warehouses serve the queries; Postgres with row-level security and pgvector fits smaller platforms.

**How do you scale for month-end?** Stateless pods that autoscale, reserved model capacity, result caching, and a queue for multi-step analyses with notification on completion.

**How do you convince auditors?** Show the audit trail, reproduce a number from its query id and snapshot, and show the eval reports and change history for prompts and models.

## Further reading

- Oracle Fusion Cloud docs: BI Cloud Connector overview, REST API for Financials.
- Databricks docs: Unity Catalog row filters and column masks, medallion architecture, Vector Search.
- Azure OpenAI docs: data privacy, provisioned throughput, quotas.
- Microsoft Presidio docs: analyzer and anonymizer.
- In this site: [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}), [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}), [interview question bank]({{ '/ai-stack/interview-question-bank/' | relative_url }}).
