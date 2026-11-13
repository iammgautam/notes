---
title: The LLMOps lifecycle
section: llm
order: 54
phase: "Phase 8 · Production"
summary: Version prompts and configs as code, gate changes on evals, roll out with shadow and canary, upgrade models safely, and feed production back into datasets.
---

An LLM feature's behavior is not defined by code alone. It is the product of a prompt, a model version, sampling parameters, tool definitions, retrieval settings, guardrail policy and the data behind them. Any of those can change, and some change without you touching anything: a provider updates a floating model alias, the document corpus drifts, user traffic shifts to questions your evals never covered. LLMOps is the discipline of treating that whole bundle as a versioned artifact that is tested before release, rolled out gradually, watched in production, and improved with data production gives back.

## The lifecycle

```text
   ┌─────────────── dataset flywheel ◄──────────────────┐
   ▼                                                    │
 develop ──► offline eval ──► review ──► shadow ──► canary ──► GA
 prompt,     CI gate on       owner,     no user    1→5→25%    │
 config      evals, cost,     approvals  impact     sticky     ▼
             latency                                     monitor,
                                                         online eval,
                                     rollback ◄──────── incidents
```

Every stage exists to catch a different failure. Offline evals catch regressions you anticipated. Shadow catches behavior on real traffic without user impact. Canary catches what only real users reveal, with a bounded blast radius. Monitoring catches drift after launch. The flywheel turns each production failure into a test so it cannot happen twice.

## Core ideas

### Version the whole task config

| Component | Why it changes behavior |
|---|---|
| Prompt template | Wording, examples, order |
| Model id | Different weights, tokenizer, tool-call habits |
| Parameters | Temperature, max tokens, reasoning effort |
| Tools and schemas | Names, descriptions and argument shapes steer calls |
| Output schema | Parser and prompt must change together |
| Retrieval config | Index version, chunking, `top_k`, reranker |
| Guardrail policy | Thresholds change what users see |

Bundle these into one config per task, keep it in the repo, and give it a content-hash version. Log that version on every trace, so any output can be traced back to the exact configuration that produced it (see [observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).

```yaml
# llm_configs/ticket_reply.yaml
task: ticket_reply
owner: support-ai
model: vendor_a/model-2025-06-01     # dated snapshot, never a floating alias
fallbacks: [vendor_b/model-b-2025-04-15]
params: {temperature: 0.2, max_tokens: 600}
prompt: prompts/ticket_reply/v14.j2
output_schema: schemas/ticket_reply.json
tools: [search_kb, get_order]
retrieval: {index: kb_v7, top_k: 8, rerank: true}
guardrails: {input: [injection_flag], output: [pii_check, link_allowlist]}
eval_suite: evals/ticket_reply/
```

The prompt itself is a template file. Jinja-style templates keep variables explicit and diffable:

{% raw %}
```text
You are the support assistant for {{ company }}.
Answer only from the context. Cite sources like [kb_12].
If the context does not answer the question, say so.

Context:
{% for c in chunks %}[{{ c.id }}] {{ c.text }}
{% endfor %}
Question: {{ question }}
```
{% endraw %}

### Prompt management options

| Approach | Pros | Cons |
|---|---|---|
| Prompts as files in the repo | Code review, atomic with parsing code, CI runs automatically | Every change needs a deploy |
| Prompt registry (Langfuse, LangSmith and similar) | Non-engineers edit; change without deploy; labels like `production` | Can bypass review and CI unless gated |
| Hybrid | Registry for authoring, promotion to `production` requires a passing eval run | More moving parts |

Whatever you pick, keep the rules: versions are immutable, the runtime resolves a label to a version and logs it, and a prompt change that alters output shape ships with the parser change in the same release.

### Pin models

Floating aliases ("latest", undated names) can change behavior under you. Pin dated snapshots where the provider offers them, track each model's deprecation date in your inventory, and treat a model change exactly like a code change: eval, review, staged rollout.

## Eval-gated CI/CD

Any change to prompts, configs, tool schemas, retrieval code or model ids triggers the eval suite (see [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }})). Tier it so it stays fast enough to run:

| Suite | Size | When | Purpose |
|---|---|---|---|
| Smoke | 20–50 cases | Every PR | Format, obvious breakage, must-pass cases |
| Regression | Hundreds to low thousands | Pre-merge for config changes, nightly | Quality per slice vs baseline |
| Safety and red-team | Hundreds | Pre-merge for prompt, tool or model changes | Injection, jailbreaks, PII leaks |

Gate on several conditions, not one average:

- **Must-pass cases** never fail: regulatory answers, past incidents, critical flows.
- **Overall score** does not drop more than a small tolerance against the baseline.
- **No slice regresses** beyond its tolerance (a 2-point average gain can hide a 15-point loss on refunds).
- **Cost per case and p95 latency** stay within budget.

Non-determinism is the main source of flaky gates. Temperature 0 does not guarantee identical outputs across runs. Compare candidate and baseline on the same cases in the same run, pin the judge model and judge prompt, sample flaky cases several times, and set tolerances wider than the run-to-run noise you have measured.

The gate, the config hash and sticky rollout bucketing are all small pieces of code:

```python
import hashlib
import json
import statistics


def config_version(cfg: dict) -> str:
    """Content hash of the whole task config: same config, same id."""
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def in_rollout(user_id: str, flag: str, percent: float) -> bool:
    """Sticky bucketing: a user stays in as the percentage ramps up."""
    h = int(hashlib.sha256(f"{flag}:{user_id}".encode()).hexdigest()[:8], 16)
    return h % 10_000 < percent * 100


def pass_rate(rows):
    return sum(r["passed"] for r in rows) / len(rows)


def p95(xs):
    return statistics.quantiles(xs, n=20)[18]


def eval_gate(baseline, candidate, max_drop=0.02, max_slice_drop=0.05,
              min_slice=20, max_cost_up=0.15, max_p95_up=0.20) -> list[str]:
    """Return reasons to block; an empty list means ship."""
    reasons = [f"must-pass failed: {r['id']}"
               for r in candidate if r.get("must_pass") and not r["passed"]]
    b, c = pass_rate(baseline), pass_rate(candidate)
    if c < b - max_drop:
        reasons.append(f"pass rate {c:.1%} vs baseline {b:.1%}")
    for s in sorted({r["slice"] for r in candidate}):
        cs = [r for r in candidate if r["slice"] == s]
        bs = [r for r in baseline if r["slice"] == s]
        if len(cs) >= min_slice and bs and pass_rate(cs) < pass_rate(bs) - max_slice_drop:
            reasons.append(f"slice {s}: {pass_rate(cs):.1%} vs {pass_rate(bs):.1%}")
    bc = sum(r["cost"] for r in baseline) / len(baseline)
    cc = sum(r["cost"] for r in candidate) / len(candidate)
    if cc > bc * (1 + max_cost_up):
        reasons.append(f"cost/case up {cc / bc - 1:.0%}")
    bl, cl = p95([r["ms"] for r in baseline]), p95([r["ms"] for r in candidate])
    if cl > bl * (1 + max_p95_up):
        reasons.append(f"p95 latency up {cl / bl - 1:.0%}")
    return reasons


def make_results(fail_every=10, slice_fail=None, cost=0.002, ms=900):
    rows = []
    for i in range(200):
        s = "refunds" if i % 2 else "billing"
        failed = i % fail_every == 0 or (slice_fail == s and i % 5 == 1)
        rows.append({"id": f"c{i}", "slice": s, "passed": not failed,
                     "must_pass": i in (3, 7), "cost": cost, "ms": ms + i})
    return rows


if __name__ == "__main__":
    cfg = {"task": "ticket_reply", "model": "vendor_a/model-2025-06-01",
           "prompt": "ticket_reply@v14", "temperature": 0.2}
    assert config_version(cfg) == config_version(dict(reversed(cfg.items())))
    assert config_version(cfg) != config_version({**cfg, "temperature": 0.3})

    users = [f"user-{i}" for i in range(10_000)]
    five = {u for u in users if in_rollout(u, "new-model", 5)}
    ten = {u for u in users if in_rollout(u, "new-model", 10)}
    assert five <= ten                      # ramping never kicks users out
    assert 400 < len(five) < 600 and 900 < len(ten) < 1100

    base = make_results()
    assert eval_gate(base, make_results()) == []
    regress = eval_gate(base, make_results(slice_fail="refunds"))
    assert any(r.startswith("slice refunds") for r in regress), regress
    broken = make_results()
    broken[3]["passed"] = False
    assert "must-pass failed: c3" in eval_gate(base, broken)
    assert eval_gate(base, make_results(cost=0.003)) == ["cost/case up 50%"]
    assert eval_gate(base, make_results(ms=1500))[0].startswith("p95 latency")
    print("ok")
```

In CI, the gate is one job that runs on paths that affect behavior. An illustrative GitHub Actions workflow, where `evals.run` and `evals.gate` are your own modules:

{% raw %}
```yaml
name: llm-evals
on:
  pull_request:
    paths: ["prompts/**", "llm_configs/**", "app/llm/**", "evals/**"]
jobs:
  eval:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python -m evals.run --suite smoke --compare-to main --out results.json
        env:
          LLM_API_KEY: ${{ secrets.LLM_EVAL_API_KEY }}
      - run: python -m evals.gate results.json   # non-zero exit blocks merge
```
{% endraw %}

Post the per-slice diff as a PR comment. Reviewers should read the eval delta the way they read a test report, and a handful of changed outputs side by side, before approving a prompt change.

## Rollouts

**Shadow.** Run the candidate config on mirrored production traffic and discard its output. Compare it with the live config using judges and spot review. It costs double for the mirrored share, and tools with side effects must be stubbed: a shadow agent must never send the email.

**Canary.** Route a small, sticky share of users or tenants to the candidate: 1%, then 5%, 25%, 50%, 100%, with a hold time at each step long enough to see real volume. Bucket by a stable hash so users do not flip between versions mid-conversation. Watch the same signals for both arms: error rate, schema-validation failures, refusal rate, escalation or handoff rate, negative feedback, online eval scores (see [online evaluation]({{ '/llm/online-evaluation/' | relative_url }})), tokens and cost per request, p95 latency. Define automatic rollback thresholds before you start.

**Feature flags.** Enable per tenant for design partners, keep a kill switch per feature and per tool, and give every flag an owner and an expiry date.

**Rollback.** Because the config is versioned, rollback is a pointer flip to the previous version, not a redeploy. It only works if the previous model is still available, so keep the old route alive until the new one has been stable for a while. Rollback does not undo actions already taken; emails sent stay sent.

## Model upgrade playbook

1. **Inventory.** Which tasks use the model, their owners, and the deprecation date that sets your deadline.
2. **Read the release notes.** Tokenizer changes (token counts, cost, context use), tool-calling and structured-output behavior, default parameters, safety behavior.
3. **Run the full suite unchanged.** The raw delta shows what the model does with your current prompts.
4. **Adapt prompts.** Newer models often need less scaffolding; remove workarounds written for the old one. Re-run.
5. **Measure real cost and latency** with actual token counts, not price-sheet arithmetic.
6. **Shadow** on production traffic for days, not hours, covering weekly patterns.
7. **Canary** with automatic rollback.
8. **Keep the old config** routable until the new one is proven, then remove it and update the inventory and system card.

Change one thing at a time. Upgrading the model and rewriting the prompt in the same release makes any regression impossible to attribute.

## Dataset flywheel

```text
 production traces
        │ sample: failures, thumbs-down, escalations,
        │ low confidence, judge disagreement, random slice
        ▼
 triage and label ──► scrub PII, check consent, dedupe
        │
        ├──► eval set (versioned, frozen per release)
        └──► training set (never overlaps eval)
                      │
                      ▼
       prompt, retrieval or fine-tuning change ──► CI
```

- **Sample with intent.** Random samples show the traffic mix; targeted samples (failures, negative feedback, disagreement between judge and users) find bugs faster.
- **Label against a rubric.** LLM pre-labels with human verification scale well; measure agreement between labelers and judges.
- **Split by group.** Split eval and training data by tenant, user or source document, not by row, or near-duplicates leak across and inflate scores.
- **Version datasets** with content hashes and keep them in one governed store; the privacy rules for production data still apply (see [privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }})).
- **Refresh slices** as traffic shifts. An eval set that no longer matches production is a green light that means nothing.

## Incident response

| Incident | Detection signal | First action |
|---|---|---|
| Provider outage or slowdown | Error rate, timeouts, p95 | Fail over to the fallback route |
| Silent quality regression | Online eval scores, feedback, escalations | Roll back to the previous config version |
| Harmful or off-brand output | Moderation flags, user reports | Kill switch for the surface; tighten output rail |
| Injection or data leak | Anomalous tool calls, security report | Disable tools or feature; preserve traces; start security IR |
| Cost spike | Spend per tenant, tokens per request | Rate limit or cap; look for agent loops |
| Retrieval failure | Empty or stale results, ingest errors | Pin previous index; re-ingest |

Prepare before you need it: kill switches per feature and per tool, a non-LLM degraded path (plain search results, a human queue), fallback routes tested regularly (see [reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }})), and traces retained long enough for forensics. A data leak may also be a personal-data breach with legal notification deadlines, so the runbook names who decides that. Every postmortem ends with at least one new must-pass eval case; that is how incidents stop recurring.

## Governance

- **Inventory.** Every task config with its model, provider, data classes, owner, risk tier and deprecation dates.
- **Model and system cards.** For models you fine-tune, a model card: intended use, training data, eval results by slice, limitations. For features, a system card covering the whole pipeline, including guardrails and human oversight.
- **Approvals by risk.** A prompt wording tweak needs PR review and a green gate. A new model, a new data class or a tool with side effects adds security and privacy review. High-risk domains (health, hiring, credit) need formal sign-off and an updated impact assessment.
- **Frameworks.** NIST AI RMF (govern, map, measure, manage) and ISO/IEC 42001 give auditors a shared structure; the evidence they ask for is the inventory, eval reports, approvals and audit logs you already produce.

## Team workflows

- One owning team and on-call rota per task config.
- Prompt changes reviewed like code, with the eval diff and sample outputs in the PR.
- Domain experts author in a registry or branch; engineers promote after the gate.
- A weekly error-analysis session: read 50 real traces, tag failure modes, file the top ones.
- Shared eval tooling and judge prompts, versioned and validated against human labels.
- A change calendar so a model upgrade does not land in the same week as a retrieval rewrite.

## Pitfalls

- **Floating model aliases.** Symptom: quality shifts with no deploy. Fix: pin snapshots, alert on model id changes in traces.
- **Prompts edited in production.** Symptom: nobody can say what changed. Fix: immutable versions, promotion gated on evals, version on every trace.
- **Gates on one average.** Symptom: a slice silently regresses. Fix: per-slice thresholds and must-pass cases.
- **Flaky gates that get ignored.** Fix: measure noise, widen tolerances, repeat unstable cases; a gate people override is no gate.
- **Shadow with live tools.** Symptom: duplicate emails or refunds. Fix: stub side-effecting tools in shadow.
- **Non-sticky canaries.** Symptom: users see two personalities in one conversation. Fix: hash on user or tenant.
- **Stale eval sets.** Symptom: offline scores rise while user feedback falls. Fix: refresh from production samples every cycle.
- **Train/eval leakage.** Symptom: fine-tune looks great offline, flat online. Fix: group splits and dedupe.

## Checklist

- One versioned config per task; content hash logged on every trace.
- Dated model snapshots; deprecation dates in the inventory.
- Smoke, regression and safety suites; gate on must-pass, overall, slices, cost and latency.
- Shadow with stubbed tools, then sticky canary with predefined rollback thresholds.
- Rollback is a config pointer flip; old routes kept alive until the new one is proven.
- Production failures sampled, labeled, scrubbed and added to versioned eval sets.
- Kill switches, degraded paths and runbooks per incident type; postmortems add eval cases.
- Approvals scale with risk; system cards and inventory kept current.

## Interview questions

**How do you version and deploy prompts?** As immutable, content-hashed versions inside a task config that also pins model, parameters, tools, schema and retrieval settings. Changes go through review and an eval gate, roll out behind a sticky flag, and every trace records the config version.

**What does an eval-gated CI pipeline check?** Must-pass cases, overall score versus a same-run baseline within tolerance, per-slice regressions, safety suite results, and cost and latency budgets. It runs on any change to prompts, configs, tool schemas, retrieval code or model ids.

**How do you upgrade to a new model version safely?** Inventory usage, read release notes, run the full suite unchanged, adapt prompts, measure real tokens and latency, shadow on production, canary with automatic rollback, and keep the old route available until the new one is stable.

**How do you handle non-determinism in evals?** Compare candidate and baseline on the same cases in the same run, pin the judge, sample unstable cases multiple times, use pass rates with tolerances above measured noise, and rely on large enough slices.

**What is a data flywheel?** A loop that samples production traces (especially failures), labels them, scrubs and versions them, and feeds them into eval sets and training data, so the system improves on the traffic it actually sees.

**What is different about incident response for LLM features?** Failures are often silent quality or safety regressions rather than errors, so detection relies on online evals and feedback. Response uses config rollback, per-tool kill switches and degraded non-LLM paths, and the postmortem adds regression cases.

## Further reading

- Sculley et al., *Hidden Technical Debt in Machine Learning Systems* (2015).
- Mitchell et al., *Model Cards for Model Reporting* (2019).
- Gebru et al., *Datasheets for Datasets* (2018).
- Shankar et al., *Who Validates the Validators? Aligning LLM-Assisted Evaluation of LLM Outputs with Human Preferences* (2024).
- NIST, *AI Risk Management Framework (AI RMF 1.0)* (2023).
