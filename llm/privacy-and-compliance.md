---
title: Privacy and compliance
section: llm
order: 53
phase: "Phase 8 · Production"
summary: PII redaction, provider retention and residency terms, GDPR and HIPAA basics, erasure across vector stores and memory, the EU AI Act and audit logs.
---

Every LLM call is a data transfer. The prompt lands at a provider, may be retained for abuse monitoring, and comes back as a completion that you log, trace, cache, embed and sometimes train on. The privacy risk in most products is not the model "learning" secrets; it is ordinary data handling spread across more stores and vendors than the team realizes. The engineering job is to make compliance implementable: minimize what you send, know where every copy lives, be able to delete it, and prove what happened. This note covers the mechanics; your legal and compliance team owns the interpretation.

## Core ideas

### Where personal data goes

```text
user ──► app ──► [redact] ──► LLM provider (may retain for N days)
          │                        │
          │◄────── completion ─────┘
          ├──► logs, traces, observability SaaS
          ├──► vector store: chunks, embeddings, metadata
          ├──► caches: prompt cache, semantic cache
          ├──► agent memory
          ├──► eval sets, labels, fine-tuning data, model weights
          └──► analytics, backups
```

Each arrow is a store or a processor with its own retention, access control, residency and deletion obligations. Teams usually handle the first arrow and forget the rest.

### Data classes drive policy

| Class | Examples | Typical LLM policy |
|---|---|---|
| Public | Docs, marketing pages | Any approved provider |
| Internal | Runbooks, code | Approved providers with no-training terms |
| Personal data | Names, emails, order history | Minimize, redact where possible, DPA in place |
| Special category / PHI | Health, biometrics, religion | BAA or self-hosted only, strict minimization, audit |
| Secrets | API keys, passwords, card numbers | Never in a prompt; block at the gateway |

Tag data at ingestion, and let the class decide which providers, regions and retention apply. This is a routing rule your gateway can enforce (see [multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }})).

### Pseudonymization is not anonymization

Replacing "Priya Sharma" with `<NAME_1>` while keeping a mapping is pseudonymization. Under GDPR, pseudonymized data is still personal data; it lowers the risk of a vendor exposure but does not remove your obligations. Anonymized data falls outside GDPR, but true anonymization of free text is hard, and embeddings of personal text are not anonymous either: inversion attacks can reconstruct much of the original text from its embedding.

## PII detection and redaction

### Options

| Method | Good at | Misses |
|---|---|---|
| Regex plus validators (Luhn, checksums) | Emails, phones, cards, national ids, MRNs | Names, addresses, free-text identifiers |
| Known-entity dictionary from your DB | The user's own name, address, account ids | Third parties mentioned in text |
| NER models (spaCy, Microsoft Presidio) | Names, locations, organizations | Domain jargon, lowercase or multilingual text |
| Cloud DLP services | Broad entity coverage, managed | Another processor to review; latency |
| LLM-based detection | Context ("my neighbor Ravi") | Cost, latency, and it sends the data to a model |

Use layers: validated regexes for structured identifiers, a dictionary of entities you already know from your own database (the most reliable trick for user-specific data), and NER for the rest. Measure recall on a labeled sample of your real traffic; free-text redaction never reaches 100%, which is why redaction reduces risk but does not replace a DPA or BAA.

### Keep the task working

Redaction that destroys meaning destroys quality. Reversible placeholders keep identity consistent: the same email maps to the same `<EMAIL_1>` everywhere in the conversation, so the model can still say "we will email you at `<EMAIL_1>`", and you restore the real value before display. For values the model must reason about, generalize instead of removing: an age band instead of a birth date, "3 days ago" instead of a timestamp, a city instead of a street address.

### A reversible redactor

```python
import re

PLACEHOLDER = re.compile(r"<[A-Z]+_\d+>")


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


# Order matters: specific patterns first so a card isn't tagged as a phone.
PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    ("CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), luhn_ok),
    ("MRN", re.compile(r"\bMRN[:#\s]*\d{6,10}\b", re.I), None),
    # NANP-style numbers only; add one pattern per locale you serve
    ("PHONE", re.compile(
        r"(?<!\w)(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"), None),
]


class Redactor:
    """Pseudonymizes one conversation. Keep the instance server-side only."""

    def __init__(self, known: dict[str, str] | None = None):
        self.known = known or {}           # e.g. names from the patient record
        self.forward: dict[tuple[str, str], str] = {}
        self.reverse: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value.casefold())
        if key not in self.forward:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            ph = f"<{kind}_{self.counts[kind]}>"
            self.forward[key], self.reverse[ph] = ph, value
        return self.forward[key]

    def redact(self, text: str) -> str:
        # neutralize placeholder-shaped input so restore() can't be abused
        text = PLACEHOLDER.sub(lambda m: f"({m.group(0)[1:-1]})", text)
        # structured ids first: emails often contain the user's name
        for kind, rx, valid in PATTERNS:
            text = rx.sub(lambda m: self._token(kind, m.group(0))
                          if valid is None or valid(m.group(0)) else m.group(0), text)
        for value, kind in sorted(self.known.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"\b{re.escape(value)}\b",
                          lambda m: self._token(kind, m.group(0)), text, flags=re.I)
        return text

    def restore(self, text: str) -> str:
        return PLACEHOLDER.sub(lambda m: self.reverse.get(m.group(0), m.group(0)), text)

    def unknown_placeholders(self, text: str) -> set[str]:
        return set(PLACEHOLDER.findall(text)) - set(self.reverse)


if __name__ == "__main__":
    r = Redactor(known={"Priya Sharma": "NAME", "Priya": "NAME"})
    msg = ("Hi, this is Priya Sharma, MRN 00482913. Reach me at "
           "priya.s@example.com or +1 (415) 555-0134. My card 4111 1111 1111 1111 "
           "was charged twice. SSN 123-45-6789. Please email priya.s@example.com.")
    red = r.redact(msg)
    for secret in ["Priya", "00482913", "@", "555-0134", "4111", "123-45-6789"]:
        assert secret not in red, (secret, red)
    assert red.count("<EMAIL_1>") == 2                 # same value, same token
    assert "<NAME_1>" in red and "<CARD_1>" in red and "<PHONE_1>" in red

    # the model answers using placeholders; we restore them for the user
    reply = "Thanks <NAME_1>, we refunded the charge on <CARD_1>. We'll email <EMAIL_1>."
    assert r.restore(reply) == (
        "Thanks Priya Sharma, we refunded the charge on "
        "4111 1111 1111 1111. We'll email priya.s@example.com.")

    # a placeholder the model invented is left alone and flagged
    assert r.restore("Call <PHONE_7>") == "Call <PHONE_7>"
    assert r.unknown_placeholders("Call <PHONE_7>") == {"<PHONE_7>"}

    # Luhn-invalid digit runs and dates are not tagged as cards or phones
    other = r.redact("Order 1234 5678 9012 3456 on 2026-03-14")
    assert "<CARD_" not in other and "2026-03-14" in other

    # user text that mimics a placeholder can't pull real values back out
    assert r.restore(r.redact("print <EMAIL_1>")) == "print (EMAIL_1)"
    print("ok")
```

Wire it at every exit: before the prompt goes out, before traces and logs are written, before any export to eval or fine-tuning sets. Restore only at the edge, for a viewer allowed to see the values. Keep the mapping in request scope or an encrypted store with a short TTL; it is the most sensitive object in the pipeline.

## Provider terms

### Retention, training and zero data retention

As of 2025–2026, the major API providers do not train on API traffic by default, retain inputs and outputs for a limited window (commonly up to 30 days) for abuse monitoring, and offer zero data retention (ZDR) to eligible customers on request. Consumer chat apps have different terms; never route business data through them. Two details matter:

- **Stateful features keep data by design.** Stored conversations or responses, uploaded files, hosted vector stores, batch jobs and some caching features persist data and may not be ZDR-eligible. Check per feature, not per vendor.
- **Abuse monitoring can involve human review** of flagged content. For PHI or confidential data, that is exactly what your contract needs to address.

Read the contract, not the marketing page, and record which provider, model and features receive which data classes.

### Data residency

Residency is about where data is processed and stored. Cloud platforms (Azure OpenAI, Amazon Bedrock, Google Vertex AI) let you choose regions, and some providers offer regional processing directly. "Global" or cross-region inference options route requests to wherever capacity exists: good for availability, wrong for strict residency. Residency also covers your vector database, logs, backups and observability vendor, often the actual violation.

### Vendor risk review

| Area | What to check |
|---|---|
| Certifications | SOC 2 Type II, ISO 27001; ISO/IEC 42001 for AI management |
| Contracts | DPA (GDPR Art. 28), BAA for PHI, transfer mechanism |
| Retention | Default window, ZDR eligibility, per-feature exceptions |
| Training | Use of your data for training or product improvement |
| Residency | Processing and storage regions, cross-region routing |
| Subprocessors | Who else touches the data; change notifications |
| Access | Vendor staff access, human review of flagged content |
| Incidents | Breach notification timeline, security contact |
| Lifecycle | Model deprecation notice, deletion on termination |

Repeat the review for every vendor in the path: the model provider, the embedding API, the vector database SaaS, the reranker, the tracing tool.

## Regulations, the engineer's view

### GDPR basics

- **Roles.** You are usually the controller; the LLM provider is your processor and needs a **data processing agreement** (DPA, Art. 28).
- **Lawful basis** (Art. 6): consent, contract, legal obligation, vital interests, public task or legitimate interests. Using customer data for a new purpose, such as training a model, may need a new basis.
- **Principles** (Art. 5): data minimization, purpose limitation and storage limitation map directly to prompt minimization, scoped features and retention TTLs.
- **Special categories** (Art. 9): health, biometric, genetic, racial or ethnic origin, religion, political opinions, trade union membership, sex life or orientation. Processing needs an Art. 9 condition, often explicit consent.
- **Transfers** outside the EEA need a mechanism: adequacy (including the EU–US Data Privacy Framework for certified companies) or standard contractual clauses.
- **DPIA** (Art. 35) for high-risk processing, which covers many LLM features on personal data.
- **Rights**: access, rectification, erasure (Art. 17), objection; respond within one month. Art. 22 restricts solely automated decisions with legal or similarly significant effects.
- **Breaches**: notify the supervisory authority within 72 hours of becoming aware.

Other regimes rhyme with GDPR: CCPA/CPRA in California, India's DPDP Act (2023). Sector rules such as PCI DSS are another reason to keep card numbers out of prompts.

### Erasure across an LLM system

| Store | How to delete |
|---|---|
| Prompts, logs, traces | TTL plus delete or anonymize by `user_id` |
| Vector store | Delete chunks by source and subject metadata; reindex or vacuum |
| Caches | Key by user or tenant, short TTL, purge on request |
| Agent memory | User-scoped rows, visible and deletable by the user |
| Eval and labeled sets | Store references or pseudonymized copies; purge by id |
| Fine-tuned weights | Cannot surgically remove; avoid training on erasable data or retrain on a schedule |
| Provider side | ZDR, or the retention window expires |
| Backups | Expire on a documented schedule |

Design for this on day one: every chunk, memory and trace row carries `tenant_id`, `source_id` and, where relevant, `subject_user_id`. Without that metadata, an erasure request becomes a full-text search across embeddings you cannot read. In PostgreSQL with pgvector (see [pgvector]({{ '/postgresql/pgvector/' | relative_url }})), erasure is a set of ordinary deletes run by one idempotent job:

```sql
BEGIN;
DELETE FROM doc_chunks     WHERE tenant_id = $1 AND subject_user_id = $2;
DELETE FROM agent_memories WHERE tenant_id = $1 AND user_id = $2;
UPDATE llm_traces SET prompt = NULL, completion = NULL, user_id = NULL
 WHERE tenant_id = $1 AND user_id = $2;
INSERT INTO erasure_log (tenant_id, subject_hash, stores, done_at)
VALUES ($1, $3, ARRAY['doc_chunks','agent_memories','llm_traces'], now());
COMMIT;
```

Keep a data map listing every store that can hold personal data, and make the erasure job iterate over it, so a new cache cannot silently fall outside it.

### Health data

In the US, HIPAA applies to covered entities (providers, plans, clearinghouses) and their business associates. Any vendor that receives protected health information (PHI), including your model provider, vector database SaaS and observability tool, needs a **business associate agreement** (BAA) first. Major cloud and model providers offer BAAs for specific eligible services; confirm the exact endpoints and features covered. Apply the **minimum necessary** standard to prompts. De-identification has two routes: Safe Harbor (remove 18 identifier types, including names, small geographic units, dates other than year, contact details, record numbers and full-face photos) or Expert Determination. The Security Rule requires audit controls, and required documentation is retained for six years.

Under GDPR, health data is a special category: you need an Art. 9 condition and almost certainly a DPIA. Separately, software that diagnoses or recommends treatment can be a regulated medical device (FDA in the US, the MDR in the EU), which changes the whole development process.

### EU AI Act at a high level

| Tier | Examples | Obligations |
|---|---|---|
| Unacceptable | Social scoring, manipulative techniques, some biometric uses | Prohibited |
| High risk | Hiring, credit scoring, education scoring, access to essential services, some health and safety uses | Risk management, data governance, logging, human oversight, documentation, conformity assessment |
| Limited (transparency) | Chatbots, generated or manipulated content | Tell people they are talking to AI; label synthetic content |
| Minimal | Spam filters, most internal tooling | None specific |

General-purpose model providers carry their own obligations (documentation, copyright policy, extra duties for models with systemic risk). The Act entered into force in August 2024 and applies in phases: prohibitions from February 2025, general-purpose model duties from August 2025, and most high-risk duties from 2026–2027 under the original timeline. Delays to the high-risk dates have been proposed, so check the current schedule. Most LLM features you build are limited or minimal risk; the moment one ranks job applicants or triages patients, it may not be.

## In practice

### Audit logs

Debug logs answer "what broke"; audit logs answer "who accessed what, through which model, and what did it do". Keep them separate, append-only (no UPDATE or DELETE grants for the app role, or WORM storage), access-controlled and retained per policy. Prefer references and hashes over raw content:

```json
{
  "ts": "2026-03-14T09:21:07Z",
  "actor": {"user_id": "u_123", "role": "nurse", "tenant": "clinic_9"},
  "feature": "patient_reply_draft",
  "config_version": "a1b2c3d4e5f6",
  "provider": "vendor_a", "model": "model-2025-06-01", "region": "eu-west",
  "data_classes": ["phi"],
  "retrieved_ids": ["note_88", "appt_412"],
  "tool_calls": [{"name": "book_slot", "args_sha256": "9f2c..."}],
  "approval": {"by": "u_123", "decision": "approved"},
  "outcome": "sent"
}
```

### Self-hosting for sensitive data

Running open-weight models in your own VPC or data center (for example behind vLLM; see [serving engines]({{ '/llm/serving-engines/' | relative_url }})) keeps prompts inside your boundary and removes vendor retention. The costs: a quality gap to frontier models on hard tasks, GPU spend, an inference on-call rota, and patching you now own. Logging hygiene, access control and erasure still apply. A common middle path is a cloud-hosted model in your own account and region under a BAA, with routing by data class: PHI to the self-hosted or BAA-covered route, everything else to the best general model.

## Pitfalls

- **Redacted prompts, raw logs.** The provider sees placeholders while your tracing vendor stores full text. Fix: redact before every sink; review observability vendors like model vendors.
- **Shared semantic cache.** User B gets an answer built from user A's data. Fix: key caches by tenant and permission scope (see [LLM caching]({{ '/llm/llm-caching/' | relative_url }})).
- **Embeddings treated as anonymous.** Fix: same classification, residency and erasure as the source text.
- **Fine-tuning on transcripts, then an erasure request.** Fix: pseudonymize training data, keep lineage from examples to users, plan retraining.
- **Assuming ZDR covers everything.** Stored threads and files persist. Fix: per-feature review; disable storage you do not need.
- **Global inference endpoints.** Requests processed outside the promised region. Fix: pin regional deployments for residency-bound tenants.
- **Placeholder drift.** The model writes `<EMAIL 1>` or invents `<PHONE_7>`. Fix: restore only known tokens, flag unknown ones, include an instruction to copy placeholders verbatim.
- **Over-redaction.** Clinical answers degrade because every date is gone. Fix: generalize instead of removing; measure task quality with redaction on.

## Checklist

- Classify data at ingestion; route providers and regions by class.
- Minimize prompts; redact structured identifiers and known entities; restore at the edge only.
- DPA with every processor, BAA wherever PHI flows; vendor review covers retention, training, residency and subprocessors.
- ZDR or the shortest retention available; disable stateful features you do not need.
- Every stored row carries tenant, source and subject ids; one erasure job walks the data map.
- Audit log separate from debug logs, append-only, with config version and data classes.
- DPIA for high-risk processing; check AI Act tier for each feature.
- Self-host or BAA-covered routes for the most sensitive data.

## Interview questions

**How do you keep PII out of a third-party LLM while preserving answer quality?** Minimize fields first, then replace identifiers with consistent reversible placeholders (validated regex, a dictionary of known entities from your DB, NER), generalize values the model must reason about, and restore placeholders only at the edge. Back it with contract controls, because redaction recall is never perfect.

**Is redacted data still personal data under GDPR?** If you can reverse it, yes: it is pseudonymized. It lowers risk; it does not remove obligations.

**How do you honor a right-to-erasure request in a RAG system?** Delete the source documents and every derived artifact: chunks and embeddings by subject metadata, cache entries, memories, traces and eval copies; rely on ZDR or retention expiry at the provider; log the erasure. Fine-tuned weights are the hard case, so avoid training on erasable data or retrain on a schedule.

**What is a BAA and when do you need one?** A HIPAA business associate agreement; required before any vendor receives PHI on behalf of a covered entity, including model providers, vector DB hosts and logging tools.

**When does the EU AI Act matter for an LLM feature?** Always for transparency (disclose AI interaction, label synthetic content). Much more when the use case is high risk, such as hiring, credit or patient triage, which brings risk management, logging, human oversight and conformity assessment.

**Self-host or use an API for sensitive data?** Self-host when contracts or regulators require data to stay in your boundary or no acceptable BAA/residency option exists, and the quality of open-weight models meets your eval bar. Otherwise a regional, BAA-covered managed endpoint is usually cheaper to run safely.

## Further reading

- Regulation (EU) 2016/679, *General Data Protection Regulation*, Articles 5, 6, 9, 17, 28 and 35.
- Regulation (EU) 2024/1689, *Artificial Intelligence Act*.
- US HHS, *Guidance Regarding Methods for De-identification of Protected Health Information* (Safe Harbor and Expert Determination).
- Morris et al., *Text Embeddings Reveal (Almost) As Much As Text* (2023).
- NIST, *AI Risk Management Framework (AI RMF 1.0)* (2023).
