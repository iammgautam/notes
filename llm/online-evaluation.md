---
title: Online evaluation and A/B testing
section: llm
order: 31
phase: "Phase 5 · Evaluation"
summary: Measure LLM features on real traffic with feedback signals, A/B tests, shadow mode, canaries and interleaving, watch drift, and feed failures back to evals.
---

Offline evals tell you whether a change is likely to be better on the cases you thought of. Production tells you whether it is better on the cases users actually bring: different phrasing, new topics, long conversations, tenants with odd data, and a provider model that changed under you last Tuesday. Online evaluation measures behavior on real traffic, decides launches with controlled experiments, catches drift, and feeds failures back into the offline suite from [Evaluating LLM systems]({{ '/llm/llm-evaluation/' | relative_url }}). The two are one loop: offline evals gate what reaches users, and online evaluation decides what the offline evals should contain.

## Production signals

| Signal | Kind | What it suggests | Caveat |
|---|---|---|---|
| Thumbs up/down, ratings | Explicit | Satisfaction with this response | Few users give it; skews negative and toward strong opinions |
| Free-text feedback, reports | Explicit | A specific failure | Rare; needs triage |
| Copy, accept, insert | Implicit | The output was usable | Copying may precede fixing it elsewhere |
| Edits before sending | Implicit | How much a draft needed fixing | Some edits are style preference |
| Regenerate, retry | Implicit | First answer unsatisfactory | Some users just explore alternatives |
| Rephrased follow-up | Implicit | The question was misunderstood | Needs a classifier to detect |
| Abandonment | Implicit | User gave up | Also happens when the answer was enough |
| Escalation to a human | Outcome | Failure to resolve | Strong but delayed |
| Task completion | Outcome | Business value: ticket resolved, suggestion kept | Delayed; attribution is hard |

Explicit feedback arrives on a small minority of responses, from users who are not representative, so treat it as a source of failure examples rather than a quality estimate. Implicit signals cover all traffic but are proxies; before you optimize one, check on a labeled sample that it actually tracks quality (do regenerated answers get judged worse than kept ones?). For drafts, `difflib.SequenceMatcher(None, draft, sent).ratio()` is a cheap edit-similarity score per response.

**Online judges** fill the gap: run reference-free LLM judges asynchronously on a sample of traffic (faithfulness to the retrieved context, policy compliance, format) and track pass rates by version, tenant and topic. Use the same calibrated judges as offline, and log their inputs so a failing verdict links to a full trace. See [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).

## A/B testing LLM features

An A/B test randomly splits users between control and treatment and compares outcomes. It is the only way to measure causal impact on user behavior, and the right tool for launches whose value is about outcomes (resolution, retention, cost), not just output quality.

### Design

- **Randomize by user or account, not request.** A user who sees both variants contaminates the comparison, and requests from one user are not independent. For B2B products where users in a tenant share workflows, randomize by tenant; your sample size is then the number of tenants, and the analysis must cluster by tenant.
- **Pick metrics before launch:**

| Role | Examples |
|---|---|
| Primary (decides the launch) | Resolution without escalation, task completion, accepted-suggestion rate |
| Secondary (explains it) | Thumbs-up rate, regenerate rate, turns per conversation, online judge pass rate |
| Guardrails (must not degrade) | p95 latency, cost per conversation, error and timeout rate, refusal rate, safety flags |

- **Fix the sample size in advance** from the baseline rate and the smallest effect worth detecting. Small effects on rates need thousands of users per arm.
- **Check sample ratio mismatch (SRM).** If you configured 50/50 and observe 49/51 on 20,000 users, assignment or logging is broken, and the results are not trustworthy until you find out why.

### LLM-specific traps

- **Caches shared across arms.** If the response or [semantic cache]({{ '/llm/llm-caching/' | relative_url }}) key omits the prompt and model version, treatment users get control's cached answers. Put the variant in the cache key.
- **Latency is part of the treatment.** A better but slower model can lose on engagement. Measure it as a guardrail and segment results by latency.
- **Cost is an outcome.** A treatment that wins by 1 point and doubles tokens may not be worth shipping; report cost per successful outcome.
- **Novelty and primacy effects.** A visible change attracts curiosity at first, or meets resistance from habitual users. Plot the effect by day and by new vs returning users, and run at least one or two full weekly cycles.
- **Peeking.** Checking daily and stopping at the first p < 0.05 inflates false positives several-fold. Commit to a horizon, or use sequential methods designed for continuous monitoring.

Variance reduction such as CUPED, which adjusts each user's metric using the same metric from before the experiment, can cut required sample sizes substantially when a pre-period metric exists.

### Runnable: sample size, significance and SRM

```python
from math import ceil, erfc, sqrt
from statistics import NormalDist

Z = NormalDist()


def sample_size_per_arm(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int:
    z_a, z_b = Z.inv_cdf(1 - alpha / 2), Z.inv_cdf(power)
    p_bar = (p1 + p2) / 2
    num = (z_a * sqrt(2 * p_bar * (1 - p_bar)) + z_b * sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return ceil(num / (p1 - p2) ** 2)


def two_proportion_test(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float]:
    """z and two-sided p-value for H0: both arms have the same rate."""
    pooled = (x1 + x2) / (n1 + n2)
    se = sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = (x2 / n2 - x1 / n1) / se
    return z, 2 * (1 - Z.cdf(abs(z)))


def srm_p_value(n_a: int, n_b: int, share_a: float = 0.5) -> float:
    """Chi-square test (1 df) that the observed split matches the configured one."""
    e_a, e_b = (n_a + n_b) * share_a, (n_a + n_b) * (1 - share_a)
    chi2 = (n_a - e_a) ** 2 / e_a + (n_b - e_b) ** 2 / e_b
    return erfc(sqrt(chi2 / 2))


if __name__ == "__main__":
    n = sample_size_per_arm(0.30, 0.32)             # detect 30% -> 32% resolution
    assert 8_000 < n < 8_800
    z, p = two_proportion_test(2_700, 9_000, 2_880, 9_000)
    assert 2.8 < z < 3.0 and p < 0.01               # +2 points with enough users
    _, p_small = two_proportion_test(1_500, 5_000, 1_560, 5_000)
    assert p_small > 0.05                           # +1.2 points, underpowered
    assert srm_p_value(10_000, 10_400) < 0.01       # 50/50 configured: broken split
    assert srm_p_value(10_000, 10_050) > 0.05
    print(f"n per arm={n}, z={z:.2f}, p={p:.4f}")
```

About 8,400 users per arm to detect a 2-point lift on a 30% rate. If your feature sees 1,000 eligible users a day, that is a 17-day test, which is why low-traffic B2B features often rely on shadow evaluation, interleaving and offline pairwise judging instead.

## Shadow mode and canaries

```text
             ┌───────────────► control (serves the user) ──► response
 request ────┤
             └─(mirror, async)─► candidate (shadow) ──► logged only
                                 tools in dry-run          │
                                                           ▼
                                       compare: pairwise judge, diff
                                       rate, latency, cost, errors

 canary:  1% ──► 5% ──► 25% ──► 50% ──► 100%
          hold each stage, check guardrails, roll back automatically
```

**Shadow mode** runs the candidate (new prompt, model or retrieval config) on mirrored production requests without showing its output. Compare the pair with a pairwise judge, measure how often outputs differ materially, and record latency, cost and errors on real inputs. It costs a second inference per mirrored request, so mirror a sample. Any tool with side effects must run in dry-run or against a stub; a shadow agent that sends real emails is an incident. Shadow mode cannot measure user reactions, only output quality and operational metrics.

**Canary releases** route a small, growing share of real traffic to the new version behind a feature flag. Each stage holds long enough to collect guardrail data (error rate, latency, cost, refusal rate, negative feedback, online judge pass rate) and rolls back automatically on a breach. For multi-tenant SaaS, canary by tenant: internal and design-partner tenants first, then a cohort, then everyone. A canary is not an experiment unless assignment is random and you compare against a concurrent control; it is a safety mechanism.

## Interleaving

For ranking components (retrieval, search, recommendations), **interleaving** is far more sensitive than A/B testing. Each user sees one merged list built from both rankers, and clicks credit whichever ranker contributed the clicked item. Every query becomes a head-to-head comparison within the same user and context, so it needs far fewer users than a between-user A/B test. Team-draft interleaving alternates picks like captains choosing teams:

```python
import random


def team_draft(a: list[str], b: list[str], k: int,
               rng: random.Random) -> tuple[list[str], dict[str, str]]:
    merged, team, n_a, n_b = [], {}, 0, 0
    while len(merged) < k:
        next_a = next((d for d in a if d not in team), None)
        next_b = next((d for d in b if d not in team), None)
        if next_a is None and next_b is None:
            break
        if next_b is None:
            pick_a = True
        elif next_a is None:
            pick_a = False
        else:                                        # fewer picks goes next; coin flip on ties
            pick_a = n_a < n_b or (n_a == n_b and rng.random() < 0.5)
        doc = next_a if pick_a else next_b
        team[doc] = "A" if pick_a else "B"
        merged.append(doc)
        n_a, n_b = n_a + pick_a, n_b + (not pick_a)
    return merged, team


def credit(team: dict[str, str], clicked: list[str]) -> str:
    a = sum(team.get(d) == "A" for d in clicked)
    b = sum(team.get(d) == "B" for d in clicked)
    return "A" if a > b else "B" if b > a else "tie"


if __name__ == "__main__":
    rng = random.Random(7)
    merged, team = team_draft(["d1", "d2", "d3", "d4"], ["d3", "d5", "d1", "d6"], 4, rng)
    assert len(set(merged)) == 4
    assert abs(list(team.values()).count("A") - list(team.values()).count("B")) <= 1

    wins = {"A": 0, "B": 0, "tie": 0}
    for q in range(2_000):                           # B ranks the relevant docs higher
        rel = [f"r{q}-{i}" for i in range(2)]
        noise = [f"n{q}-{i}" for i in range(8)]
        a, b = noise[:3] + rel + noise[3:], rel + noise
        merged, team = team_draft(a, b, 5, rng)
        p_click = {d: 0.7 if d in rel else 0.1 for d in merged}
        clicked = [d for d in merged if rng.random() < p_click[d]]
        wins[credit(team, clicked)] += 1
    assert wins["B"] > 3 * wins["A"]
    print(wins)
```

Test the winner with a sign test over non-tied queries. For generated answers, the analogue is showing two responses side by side and letting the user pick. It yields direct pairwise preferences, but it costs two generations, interrupts the user, and suffers the same position and length biases as a judge.

## Monitoring drift

| What drifts | Watch | How |
|---|---|---|
| Inputs | Topic mix, language, query length, new intents | Classify or cluster queries; compare distributions week over week |
| Retrieval | Top-1 score distribution, no-result rate, age of retrieved documents | Score histograms and percentiles per tenant |
| Outputs | Length, refusal rate, format failures, tool-call error rate | Counters per prompt and model version |
| Quality | Online judge pass rates, negative feedback, escalations | Rates per version, tenant, topic |
| The model itself | Behavior under a fixed model name | Run a small fixed eval set daily against production config |

Provider-side changes are real: aliases move to new snapshots and serving stacks change. Pin model versions where the provider allows it, and run a daily canary eval so a change shows up as a dip on a chart rather than a support escalation. See [LLMOps lifecycle]({{ '/llm/llmops-lifecycle/' | relative_url }}).

For categorical distributions such as topic mix, the **population stability index** is a simple drift score: `PSI = sum over categories of (actual - expected) * ln(actual / expected)`. Common rules of thumb, inherited from credit-risk modeling: below 0.1 stable, 0.1 to 0.25 worth a look, above 0.25 a real shift. For embeddings, track the share of queries per cluster (cluster on a reference window) rather than raw vector statistics.

```python
from math import log


def psi(expected: dict[str, float], actual: dict[str, float], eps: float = 1e-4) -> float:
    total = 0.0
    for k in expected.keys() | actual.keys():
        e, a = max(expected.get(k, 0.0), eps), max(actual.get(k, 0.0), eps)
        total += (a - e) * log(a / e)
    return total


if __name__ == "__main__":
    last_month = {"billing": 0.40, "auth": 0.25, "integrations": 0.20, "other": 0.15}
    this_week = {"billing": 0.38, "auth": 0.24, "integrations": 0.21, "other": 0.17}
    after_launch = {"billing": 0.25, "auth": 0.20, "integrations": 0.15, "other": 0.10,
                    "data-export": 0.30}
    assert psi(last_month, this_week) < 0.1
    assert psi(last_month, after_launch) > 0.25      # new intent the evals never covered
    print(round(psi(last_month, this_week), 4), round(psi(last_month, after_launch), 2))
```

A new category appearing is the alarm in itself: it is traffic your golden set does not cover.

## Sampling traffic for human review

| Strategy | What it samples | Use it for |
|---|---|---|
| Uniform random | A fixed share of all traffic | Unbiased quality estimates over time |
| Stratified | Fixed counts per tenant, topic, language | Coverage of small but important segments |
| Signal-targeted | Thumbs down, regenerates, escalations, judge failures, guardrail hits | Finding failures fast |
| Uncertainty | Borderline judge verdicts, low retrieval scores | Improving judges and labels |
| Novelty | New clusters, new tenants, new features | Discovering unknown failure modes |
| Expensive | Longest agent runs, highest cost | Loops and runaway cost |

Keep the uniform stream separate from targeted streams, or weight targeted samples back down: a quality estimate from oversampled failures will look worse than reality. Route samples to an annotation queue with the same rubric used offline, double-label a slice to track reviewer agreement, and give reviewers the full trace (retrieved context, tool calls), not just the final answer. Redact PII before review, restrict access, set retention limits, and respect consent and contract terms on customer data. See [Privacy and compliance]({{ '/llm/privacy-and-compliance/' | relative_url }}).

## Closing the loop

```text
 production trace
      │ flagged: thumbs down, regenerate, escalation, judge fail,
      │ guardrail hit, or random sample
      ▼
 review queue ──► label + failure category ──► root cause
                                                   │
      ┌────────────────────────────────────────────┘
      ▼
 new golden case (redacted, expected behavior) ──► fix ──► offline eval
      ──► CI gate ──► canary ──► watch that category's rate in production
```

1. **Triage** each flagged trace into a failure category from a maintained taxonomy (retrieval miss, invented policy, wrong tool, format break, refusal, latency).
2. **Count** categories weekly. The largest and fastest-growing get fixed first.
3. **Convert** representative failures into golden cases with the expected behavior, redacted and tagged with `source: production`.
4. **Fix and verify offline:** the new cases fail before the fix and pass after it, with no regressions elsewhere.
5. **Ship through the gate and a canary**, then confirm the category's production rate actually fell. If it did not, the offline case did not capture the real failure.

## Pitfalls

- **Treating thumbs-up rate as quality.** Feedback rates are tiny and biased. Use feedback to find failures; estimate quality from uniform samples and online judges.
- **Randomizing by request.** Users see both variants and units are correlated. Randomize by user or tenant and analyze at that level.
- **Peeking and stopping early.** Inflated false positives. Fixed horizon or sequential tests.
- **Ignoring SRM.** A broken split invalidates the result. Check it before reading any metric.
- **Cache leakage between arms.** Variant missing from the cache key. Include prompt and model version.
- **Shadow mode with side effects.** Real emails, real refunds. Dry-run all tools.
- **No daily fixed-set eval.** Provider changes surface as customer complaints. Run one against the production config.
- **Production failures that never become cases.** The same bug returns next quarter. Make conversion to golden cases part of triage.

## Checklist

- Explicit and implicit signals logged per response with version, tenant and trace id.
- Online judges on sampled traffic, calibrated like offline ones.
- A/B tests: user or tenant randomization, pre-registered primary and guardrail metrics, sample size computed, SRM checked, weekly cycles covered.
- New prompts and models go through shadow mode or a staged canary with automatic rollback.
- Drift dashboards for inputs, retrieval, outputs and quality, plus a daily fixed-set eval.
- Uniform and targeted review samples, kept separate, with PII redaction.
- Weekly triage that turns production failures into golden cases.

## Interview questions

**How do you know an LLM feature works in production?** Combine outcome metrics (resolution, task completion), implicit signals (regenerates, edits, escalations), online judges on sampled traffic, and human review of uniform and targeted samples. Validate proxies against labels before trusting them.

**How would you A/B test a new prompt?** Randomize by user or tenant, pick a primary outcome metric plus guardrails for latency, cost, errors and refusals, compute the sample size, check SRM, run full weekly cycles without peeking, and put the variant in the cache key.

**What is shadow mode and what can't it tell you?** Running the candidate on mirrored traffic without serving it, then comparing outputs, latency and cost. It cannot measure user behavior, and its tools must not have side effects.

**When would you use interleaving instead of A/B testing?** For ranking components such as retrieval or search, when traffic is limited. Each query compares both rankers for the same user, so it needs far fewer users. It does not measure downstream business outcomes.

**Your answer quality dropped with no deploy. What happened?** Check for input drift (new topics or tenants), retrieval drift (stale or missing documents, score shifts) and provider-side model changes, using the daily fixed-set eval and drift dashboards. Then sample failing traces for review.

**How do production failures improve the offline suite?** Triage into categories, convert representative traces into redacted golden cases with expected behavior, verify the fix offline, ship through CI and a canary, and confirm the category's production rate drops.

## Further reading

- Kohavi, Tang and Xu, "Trustworthy Online Controlled Experiments: A Practical Guide to A/B Testing" (2020).
- Radlinski, Kurup and Joachims, "How Does Clickthrough Data Reflect Retrieval Quality?" (2008).
- Chapelle, Joachims, Radlinski and Yue, "Large-Scale Validation and Analysis of Interleaved Search Evaluation" (2012).
- Deng, Xu, Kohavi and Walker, "Improving the Sensitivity of Online Controlled Experiments by Utilizing Pre-Experiment Data" (2013).
- Johari, Koomen, Pekelis and Walsh, "Peeking at A/B Tests: Why It Matters, and What to Do About It" (2017).
