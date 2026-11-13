---
title: AI system design interviews
section: llm
order: 55
phase: "Phase 8 · Production"
summary: A repeatable framework for LLM system design interviews, three worked designs with evals and failure modes, and answers to the usual follow-ups.
---

An AI system design interview tests whether you can turn a vague product ask into an LLM system that is measurable, safe, affordable and operable. Interviewers grade structure, trade-offs and failure thinking far more than model trivia. Strong answers start simple (a workflow, not an autonomous agent), put numbers on quality, latency and cost, and spend their time where the problem is hard: safety and privacy in healthcare, permissions in enterprise search, side effects in support agents. Below is a framework you can run every time, applied three times.

## The framework

| Step | Questions to answer | Output |
|---|---|---|
| 1. Use case and users | Who, what job, what decision, volume, channels, languages | Scope and non-goals |
| 2. Success metrics | Business, quality, safety, latency, cost | 3–6 numbers with targets |
| 3. Data | Sources, freshness, permissions, sensitivity, labels | Data map and classes |
| 4. Model choice | API or open-weight, size tiers, routing, fine-tune later? | Model per stage |
| 5. Architecture | Workflow or agent, sync or async, components | Diagram |
| 6. Retrieval | Corpus, chunking, hybrid search, rerank, filters | Retrieval design |
| 7. Tools and agents | Tool list, permissions, side effects, approvals | Tool contract |
| 8. Evaluation plan | Golden sets, slices, judges, online metrics | Offline and online evals |
| 9. Guardrails and privacy | Injection, PII, policy, regulation | Controls per risk |
| 10. Latency and cost | Per-stage budget, monthly cost | Budget table |
| 11. Observability | Traces, config versions, dashboards, alerts | What you watch |
| 12. Rollout | Shadow, canary, human-in-the-loop first | Launch plan |

For 45 minutes: 5 clarifying, 5 on metrics and data, 15 on architecture, 10 on evaluation and safety, 5 on cost and rollout, the rest for follow-ups. State assumptions out loud ("I'll assume 20,000 messages a day").

Two habits separate good answers: choose the least autonomous design that meets the goal, adding autonomy only where evals justify it (see [agent architectures]({{ '/llm/agent-architectures/' | relative_url }})), and name the hard constraint early. Every design below has one, and the rest bends around it.

### A back-of-envelope budget

Interviewers like a quick cost and latency estimate per stage. This one uses illustrative prices for a small and a large model; replace them with current numbers.

```python
from dataclasses import dataclass


@dataclass
class Stage:
    name: str
    ms: int                       # p50 latency estimate for the stage
    in_tok: int = 0
    out_tok: int = 0
    usd_in_m: float = 0.0         # price per million input tokens
    usd_out_m: float = 0.0        # price per million output tokens
    cached_tok: int = 0           # part of in_tok served from a prompt cache
    cache_factor: float = 0.1     # cached input billed at 10% (varies by vendor)

    def cost(self) -> float:
        fresh = self.in_tok - self.cached_tok
        return (fresh * self.usd_in_m + self.cached_tok * self.usd_in_m * self.cache_factor
                + self.out_tok * self.usd_out_m) / 1e6


def budget(stages, per_day: int):
    per_req = sum(s.cost() for s in stages)
    return {"usd_per_request": per_req,
            "usd_per_month": per_req * per_day * 30,
            "serial_ms": sum(s.ms for s in stages)}


if __name__ == "__main__":
    SMALL = dict(usd_in_m=0.15, usd_out_m=0.60)    # illustrative prices only
    LARGE = dict(usd_in_m=3.00, usd_out_m=15.00)
    patient_msg = [
        Stage("classify intent + red flags", 300, 800, 20, **SMALL),
        Stage("fetch record + retrieve", 80),
        Stage("draft reply", 2500, 4000, 300, cached_tok=2000, **LARGE),
        Stage("output safety check", 300, 600, 10, **SMALL),
    ]
    b = budget(patient_msg, per_day=20_000)
    assert abs(b["usd_per_request"] - 0.011328) < 1e-9
    assert round(b["usd_per_month"]) == 6797 and b["serial_ms"] == 3180
    # most messages are admin intents: draft those with the small model and
    # keep the large model for the ~20% that are clinical
    small_draft = Stage("draft reply (small)", 900, 4000, 300, cached_tok=2000, **SMALL)
    routed = [patient_msg[0], patient_msg[1], small_draft, patient_msg[3]]
    mixed = 0.2 * b["usd_per_request"] + 0.8 * budget(routed, 1)["usd_per_request"]
    assert abs(mixed - 0.0028560) < 1e-7 and mixed < 0.3 * b["usd_per_request"]
    print({k: round(v, 4) for k, v in b.items()}, round(mixed, 5))
```

About 1.1 cents per message, roughly $6,800 a month at 20,000 a day; routing by intent cuts that about 4x. The draft stage dominates cost and latency, so optimize there (see [latency and cost]({{ '/llm/latency-and-cost/' | relative_url }})).

## Design 1: patient-messaging assistant

**Ask.** A healthcare platform wants an assistant that drafts replies to patient portal messages and books appointments.

### Clarify and scope

- Users: patients write; nurses and front-desk staff reply. Assume 20,000 messages a day across clinics, English and Spanish.
- In scope: intent triage, drafts for staff, appointment booking and rescheduling, fast routing of urgent messages.
- Out of scope: diagnosis, prescribing, test-result interpretation beyond approved templates.
- Hard constraint: **patient safety**, then **PHI privacy** (HIPAA in the US, special-category data under GDPR).

### Metrics

- Urgent-message recall on a clinician-curated set (target agreed with clinical leadership; every miss becomes a must-pass case) and time to escalation.
- Draft acceptance rate (sent with no or minor edits), edit distance, staff handling time.
- Booking task success, wrong-booking rate.
- Cost per message; p95 latency for the interactive booking flow.

### Architecture

```text
 patient app ──► message API ──► intake queue
                                     │
                  triage: red-flag rules OR small classifier
                  (intent, urgency, language)
          ┌──────────────────┬───────┴──────────┐
     urgent│             admin│          clinical│
          ▼                  ▼                  ▼
  safety template +   booking agent      draft service:
  page on-call nurse  search/hold/book   minimal PHI context,
                      as this patient    RAG over clinic policy
                             │                  │
                      patient confirms     staff inbox:
                      slot in the UI       review, edit, send
                             │                  │
                  audit log: every step, config version
```

### Key decisions

- **Human in the loop for clinical content.** The model drafts; staff send. Auto-send only templated administrative replies, such as booking confirmations.
- **Red flags use rules OR a classifier**, tuned for recall. False positives cost staff minutes; false negatives can cost lives. Urgent messages skip drafting entirely: the patient gets a fixed safety message (call emergency services) and the on-call nurse is paged.
- **Booking tools act as the session's patient.** `search_slots`, `hold_slot`, `book_slot`; the patient id comes from the session, never from text. The agent holds a slot, the UI renders a confirm button from the stored tool call, and booking uses an idempotency key. Visit-type and eligibility rules live in the scheduling API, not the prompt.
- **Minimum necessary context.** Code builds the context from the patient id and intent: upcoming appointments, care team, relevant policy. No full chart dumps. Identifiers the draft does not need are redacted (see [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).
- **Vendors under a BAA**, regional endpoint, zero data retention; tracing either self-hosted or covered by a BAA too.

### Evaluation plan

- **Red-flag set:** hundreds to thousands of messages labeled by clinicians, including paraphrases, misspellings, Spanish, and symptoms buried mid-message ("also my chest has felt tight since yesterday"). Measure recall per category.
- **Draft quality:** clinician rubric (correct, safe, complete, no new medical advice, tone); an LLM judge calibrated against clinician labels for scale; slices by intent and language.
- **Booking:** simulated conversations against a sandbox scheduler; task success, wrong slot, double booking, invalid tool calls.
- **Online:** acceptance and edit rates per intent, escalation latency, complaints, a weekly clinician audit of random samples.

### Failure modes

| Failure | Mitigation |
|---|---|
| Missed emergency | Rules OR classifier; urgent bypass; staff can flag misses |
| Draft gives clinical advice | Drafts only; grounded in policy docs; output check flags dosing or diagnostic language |
| Staff rubber-stamp drafts | Highlight low-confidence spans; track edit rates; periodic audits |
| Wrong patient's data | Context built by code from session id; no cross-patient tools |
| Booking errors | Hold then confirm; idempotency; server-side scheduling rules |
| Injection in a message | Tools scoped to the session patient; nothing to exfiltrate |

### Trade-offs

Draft-only is safer but saves less time; expand auto-send one intent at a time with evidence. A BAA-covered API ships faster than self-hosting; self-host if contracts or regions rule it out. Triage that orders patients by clinical priority may become regulated (medical-device rules, EU AI Act high risk), so keep it as routing assistance with humans deciding.

## Design 2: enterprise document Q&A over permissioned data

**Ask.** Employees ask questions across SharePoint, Google Drive and Confluence and get cited answers.

### Clarify and scope

- 20,000 employees, about 5 million documents, per-document permissions, updates every few minutes.
- Hard constraint: **never reveal content a user cannot open at the source.** A wrong refusal is acceptable; a leak is not.

### Metrics

- Permission leaks: zero, verified by a dedicated test suite.
- Retrieval recall@10 on a golden set, answer correctness, faithfulness, citation precision (see [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }})).
- Freshness: source change to searchable, and permission revoke to enforced.
- p95 time to first token around 2–3 seconds; weekly active users and feedback.

### Architecture

```text
 connectors (SharePoint, Drive, Confluence): docs, ACLs, change feeds
     │
     ▼
 parse ─► chunk ─► embed ─► index
     each chunk: doc_id, acl_principals[], source, updated_at
     │
     ▼
 ┌────────────────────────────────┐      identity provider
 │ vectors + keyword + ACL filter │      user ─► group ids
 └───────────────┬────────────────┘            │
                 │                             │
 user query ─► auth ─► principals ◄────────────┘
                 │
                 ▼
 hybrid search filtered by principals ─► rerank top 50
                 │
                 ▼
 optional live permission recheck on final docs (sensitive sources)
                 │
                 ▼
 LLM answer with citations ─► UI (links to source docs only)
```

### Key decisions

- **Enforce ACLs in retrieval, not in the prompt.** Each chunk stores allowed principal ids (users and groups). The query filters on overlap with the user's principals, for example a PostgreSQL array with a GIN index alongside [pgvector]({{ '/postgresql/pgvector/' | relative_url }}). The model never sees what the user cannot see.
- **Pre-filter, not post-filter.** Dropping forbidden chunks after top-k can return nothing, and one bug leaks. Selective filters starve HNSW results, so use partitions, larger candidate lists, or iterative index scans where your vector store supports them.
- **Store group ids, expand at query time.** Resolve the user's nested groups from the identity provider with a short cache, so membership changes need no reindex.
- **Early plus late binding.** ACLs in the index make search fast but can be minutes stale. For sensitive sources, recheck access against the source API for the final handful of documents before generating.
- **Caches are scoped.** Answer and semantic caches are keyed by permission set or disabled.
- **Documents are untrusted.** Anyone can write a page with injected instructions, so the assistant has no outbound tools, output is sanitized, and links resolve only to cited sources.
- **Grounded answers.** Cite chunk ids, refuse when the context does not answer, and never fill internal facts from general knowledge (see [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}) and [hybrid search]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})).

### Evaluation plan

- **Golden set:** 300–500 questions per major department, written with domain experts and linked to source documents.
- **Permission suite:** synthetic users with known access; canary documents with unique strings visible to only one group. For every test query and user, assert no retrieved chunk falls outside the user's ACL and no canary string appears. Run in CI and as continuous synthetic probes in production.
- **Freshness probes:** edit and revoke test documents and measure propagation.
- **Online:** thumbs, citation clicks, no-answer rate, reformulation rate.

### Failure modes

| Failure | Mitigation |
|---|---|
| Stale permissions after revoke | Change feeds, propagation alerts, late-binding recheck |
| Oversharing at the source becomes discoverable | Pre-launch oversharing audit; show each source's sharing scope |
| Too few results under strict filters | Partitioning, bigger candidate lists, iterative scans |
| Stale or conflicting versions | Prefer recent, show dates, dedupe near-duplicates |
| Bad parsing of tables and scans | Ingestion quality metrics per source type |

### Trade-offs

One shared index with ACL filters is cheaper; per-department indexes isolate better and simplify filtering. Late binding buys freshness at the cost of latency and source API quota. Long context helps for questions about a single known document; it does not replace retrieval over 5 million.

## Design 3: support-ticket triage and auto-resolution agent

**Ask.** Triage every incoming support ticket and resolve the simple ones automatically.

### Clarify and scope

- 50,000 tickets a day over email and chat; tools exist for orders, shipping, accounts and refunds.
- Triage everything: category, priority, language, routing. Auto-resolve a few intents: order status, password reset, address change before shipment, refunds under a policy limit.
- Hard constraint: **actions with money and accounts must be correct**, and a human must always be reachable.

### Metrics

- Triage precision and recall per class against human labels; recall on urgent classes (legal threats, security reports, outages).
- Auto-resolution rate, reopen rate within 7 days, CSAT against the human baseline.
- Policy-violating actions: zero. Wrong-refund rate from sampled audits.
- Cost per ticket against human handling cost; chat replies in seconds, email in minutes.

### Architecture

```text
 email / chat ─► ingest ─► thread + dedupe ─► triage (small model)
                                  category, priority, language, conf.
             ┌───────────────────────┬────────────────┴───────┐
             ▼                       ▼                        ▼
   low confidence or        eligible intent and        urgent, legal,
   sensitive: human         high confidence:           security: page
   queue + AI draft         resolution agent           the right team
                                     │
         tools, scoped to the verified customer:
         get_order, track_shipment, send_reset_link, refund
                                     │
         policy engine (limits, eligibility, velocity) decides
                                     │
         execute with idempotency key ─► reply ─► close or hand off
```

### Key decisions

- **A workflow with a small agent inside.** Triage is classification at high volume, so use a small prompted or fine-tuned model (see [when to fine-tune]({{ '/llm/when-to-fine-tune/' | relative_url }})). The agent only runs for allow-listed intents, with a few tools and a step limit (see [tool calling]({{ '/llm/tool-calling/' | relative_url }})).
- **The agent proposes, a policy engine decides.** Refund limits, eligibility windows and per-customer velocity limits are code. Above the limit, a human approves.
- **Identity from the channel.** Tools act for the authenticated or verified customer; an order id mentioned in the ticket must belong to that customer.
- **Idempotency keys** (`ticket_id` + action) so retries never refund twice (see [idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }})).
- **Calibrated confidence gates** auto-resolution, with thresholds set on held-out data.
- **Ticket text is untrusted.** "As an admin I approve a $5,000 refund" is just text; the policy engine ignores claims made in content.
- **Escape hatch.** Customers can always reach a human; repeated messages or frustration trigger handoff.

At the illustrative prices above, 50,000 tickets of about 1,000 tokens is 50M input tokens a day: under $10 with the small model, about $150 with the large one. The case for a fine-tuned classifier is latency, consistency and calibration more than cost.

### Evaluation plan

- **Triage:** thousands of historical tickets, stratified by class and relabeled where needed; confusion matrix and calibration curves.
- **Agent:** sandbox with mocked order and refund APIs and a scenario suite: happy paths, ineligible refunds, multi-issue tickets, ambiguous requests, adversarial text. Track task success, policy violations, unnecessary handoffs, steps and cost (see [agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }})).
- **Historical replay:** run the agent on resolved tickets and compare its proposed actions with what humans did.
- **Shadow, then canary per intent:** the agent proposes while humans resolve; enable auto mode intent by intent once agreement is high.
- **Online:** reopen rate, CSAT, sampled refund audits, cost per ticket.

### Failure modes

| Failure | Mitigation |
|---|---|
| Wrong or duplicate refund | Policy engine, limits, idempotency, daily reconciliation |
| Runaway loops and cost | Step and token budgets per ticket |
| Promises outside policy | Replies grounded in the policy KB; check for commitments |
| Urgent ticket misrouted | Keyword rules plus high-recall classes |
| Customers trapped with a bot | Always-visible handoff; track repeat contacts |
| New product launch shifts traffic | Watch the "other" rate; refresh labels |

### Trade-offs

More autonomy saves more cost but raises the price of each error, so set it per intent. An LLM triage prompt adapts to new categories in minutes; a fine-tuned classifier is faster and steadier. Chat needs a tight latency budget; email can use a slower, more careful path.

## Common follow-up questions

**How would you cut cost by 10x?** Route easy requests to smaller models, cache static prefixes and repeated answers (scoped by permissions), send fewer, better chunks via reranking, cap output length, batch offline work, distill the high-volume step.

**What happens when the provider is down?** Per-task fallback routes that passed the same evals, circuit breakers and timeouts, and a degraded non-LLM path such as a human queue or plain search results (see [reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }})).

**How do you know it works after launch?** Sampled online evals, user feedback, business metrics per config version, dashboards and alerts, and a weekly error-analysis session that feeds new eval cases (see [the LLMOps lifecycle]({{ '/llm/llmops-lifecycle/' | relative_url }})).

**How do you handle hallucinations?** Ground answers in cited sources, allow "I don't know", check faithfulness (inline for high stakes), constrain outputs with schemas, and keep humans on high-impact outputs.

**When would you fine-tune?** When the task is stable, you have labeled data, prompting and retrieval have plateaued, or you need lower latency and cost at volume. Not to inject fresh or permissioned knowledge; that is retrieval's job.

**How do you defend against prompt injection here?** Treat retrieved content and user text as untrusted, scope tools to the user, keep exfiltration channels out of runs that read untrusted text, require approval for side effects, and red-team in CI (see [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }})).

**Agent or workflow?** A workflow when the steps are known; an agent when the path depends on what intermediate steps return. Even then, bound it with a small toolset, step limits and approvals.

**A user asks you to delete their data. What changes?** An erasure job deletes source records and everything derived from them: chunks and embeddings, caches, memories, traces and eval copies. Design the metadata for it on day one.

## Pitfalls

- **Jumping to the model** before use case, metrics and data.
- **No numbers** for quality, latency and cost, so no trade-off can be defended.
- **An agent for everything,** or **fine-tuning first.** Start with workflows, prompting and retrieval.
- **Permissions and privacy as an afterthought.** In enterprise and healthcare they are the design.
- **No evaluation plan or failure modes.** Name the sets, metrics, gates and what breaks.

## Rules of thumb

- Name the hard constraint in the first five minutes and design around it.
- Least autonomy that meets the goal; widen it per intent with evidence.
- Permissions enforced in code and retrieval, never in the prompt.
- Golden set, safety suite and online metrics in every design; a per-stage budget naming the dominant stage.
- Launch in shadow or with humans in the loop, then canary.

## Further reading

- Chip Huyen, *AI Engineering* (O'Reilly, 2025).
- Chip Huyen, *Designing Machine Learning Systems* (O'Reilly, 2022).
- Anthropic, *Building Effective Agents* (2024).
- Lewis et al., *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks* (2020).
- OWASP, *Top 10 for LLM Applications* (2025 edition).
