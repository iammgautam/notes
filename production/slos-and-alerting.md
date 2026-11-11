---
title: SLOs and alerting
section: production
order: 10
summary: Define SLIs and SLOs, spend an error budget, alert on symptoms with burn-rate alerts, and cut alert fatigue so pages mean something.
---

Without explicit targets, reliability work is driven by whoever complains loudest, and alerting drifts toward "page on anything unusual". SLOs give you a number to defend: how reliable the service needs to be, how much unreliability you can afford, and when to stop shipping features and fix things. Alerting then becomes a question of "are we burning that budget too fast?" instead of "is CPU above 80%?".

## How it works

### SLI, SLO, SLA

- **SLI (indicator)**: a measured ratio of good events to valid events. "Proportion of API requests that return non-5xx in under 500 ms."
- **SLO (objective)**: the target for an SLI over a window. "99.5% over 28 days."
- **SLA (agreement)**: a contractual promise with consequences (credits). Always looser than the SLO, so you notice and react before you owe money.

Good SLIs measure what users experience:

| Service type | SLI | Example definition |
|---|---|---|
| API | Availability | non-5xx responses / all responses (excluding health checks) |
| API | Latency | requests under 500 ms / all requests, per route class |
| Background jobs | Freshness | tasks started within 60 s of enqueue / all tasks |
| Background jobs | Success | tasks finished without terminal failure / all tasks |
| Data pipeline | Freshness | rollups less than 15 min old / checks |
| LLM feature | Quality proxy | answers returned without fallback or timeout / all |

Measure at the load balancer or edge where possible; app-level metrics miss requests that never reached the app.

### Error budget

The budget is `1 - SLO`. At 99.5% over 28 days:

```text
28 days x 24 h x 60 min = 40,320 min
budget = 0.5% x 40,320  = ~202 min of full outage
         or 0.5% of requests failing across the window
```

| SLO | Budget per 28 days |
|---|---|
| 99% | ~6.7 h |
| 99.5% | ~3.4 h |
| 99.9% | ~40 min |
| 99.99% | ~4 min |

Each extra nine costs roughly 10x more engineering effort. Pick the lowest target that keeps users happy. Internal admin tools rarely need 99.9%.

The budget is a decision tool:

- Budget remaining: ship features, take reasonable risks, run experiments.
- Budget exhausted: freeze risky launches, prioritize reliability work, until the window recovers.

Agree on this policy with product before the budget runs out, not during the argument.

### Symptom-based alerts

Alert on what users feel (symptoms), not on what might cause it (causes).

| Cause alert (avoid paging) | Symptom alert (page) |
|---|---|
| CPU > 80% | p95 latency SLO burning fast |
| DB connections > 90 | Error rate SLO burning fast |
| One worker pod restarted | Task freshness: oldest pending task > 5 min |
| Disk 75% full | (ticket, not page, until it's days from full) |

Cause metrics belong on dashboards and in tickets. High CPU with happy users is efficiency, not an incident. Users suffering with normal CPU is still an incident, and a cause-based alert would have missed it.

The exception: alerts that predict certain failure with enough lead time, such as disk filling in under 24 hours, certificates expiring in 7 days, transaction ID wraparound approaching. Those can be tickets or low-urgency notifications.

### Burn-rate alerting

**Burn rate** is how fast you're spending budget relative to plan. Burn rate 1 spends exactly the budget over the window. Burn rate 14.4 spends 2% of a 30-day budget in one hour.

Multi-window, multi-burn-rate alerts (from the Google SRE workbook) catch fast outages quickly and slow leaks eventually, with few false positives:

| Severity | Long window | Short window | Burn rate | Budget spent |
|---|---|---|---|---|
| Page | 1 h | 5 min | 14.4 | 2% |
| Page | 6 h | 30 min | 6 | 5% |
| Ticket | 3 d | 6 h | 1 | 10% |

Both windows must exceed the threshold: the long window proves it's significant, the short window proves it's still happening (so the alert resolves quickly after a fix).

## In practice

### Prometheus recording rules and alert

```yaml
groups:
  - name: api-slo
    rules:
      - record: sli:api_errors:ratio_rate5m
        expr: |
          sum(rate(http_requests_total{job="api",status=~"5.."}[5m]))
          /
          sum(rate(http_requests_total{job="api"}[5m]))
      - record: sli:api_errors:ratio_rate1h
        expr: |
          sum(rate(http_requests_total{job="api",status=~"5.."}[1h]))
          /
          sum(rate(http_requests_total{job="api"}[1h]))

      - alert: ApiErrorBudgetFastBurn
        expr: |
          sli:api_errors:ratio_rate1h > (14.4 * 0.005)
          and
          sli:api_errors:ratio_rate5m > (14.4 * 0.005)
        labels:
          severity: page
        annotations:
          summary: "API burning error budget 14x (SLO 99.5%)"
          runbook: "https://runbooks.internal/api-error-budget"
```

### Queue freshness SLI for a task executor

For a Postgres-backed queue, expose the age of the oldest ready task:

```sql
SELECT queue,
       count(*) AS ready,
       extract(epoch FROM now() - min(run_after)) AS oldest_age_s
FROM tasks
WHERE status = 'pending' AND run_after <= now()
GROUP BY queue;
```

Export it as a gauge and alert when `oldest_age_s` stays above the freshness target for 10 minutes. This catches stuck workers, poison tasks and capacity problems that error rates never show.

### Every page needs

- A clear title stating the user impact.
- A link to a runbook: what it means, first checks, mitigations.
- A link to the relevant dashboard.
- An owner team.

If the on-call can't do anything about it, it shouldn't page them.

### Fighting alert fatigue

Alert fatigue is when pages are so frequent or so often false that people stop reacting. It is a reliability risk in itself: the real page gets ignored.

- Review every page weekly: was it actionable? Did it need a human now? If no, delete it, downgrade it to a ticket, or fix the threshold.
- Track pages per on-call shift. More than a couple per shift is a signal to fix the system or the alerts.
- Deduplicate and group: one incident should produce one page, not 40 (one per pod).
- Add `for:` durations so a 30-second blip doesn't page.
- Route by severity: page (wake someone), ticket (next business day), dashboard only.
- Silence during planned maintenance, and expire silences automatically.

## Pitfalls

- **SLO at 100%.** No budget means no deploys and no risk-taking, and you'll miss it anyway.
- **Averaging latency into the SLI.** Use "proportion of requests under threshold", which maps cleanly to a budget.
- **Too many SLOs.** Three to five per service is plenty. Nobody tracks 30.
- **Per-tenant blind spots.** A global 99.9% can hide one large tenant at 90%. For B2B, add a per-tenant view for top customers or alert on "any tenant with error ratio above X with at least N requests".
- **Static thresholds on noisy metrics** that page every night during the batch window.
- **Alerts without runbooks,** so each on-call rediscovers the same steps.

## Rules of thumb

- SLIs as good/valid ratios measured close to the user.
- Pick the lowest SLO users accept; write down the error budget policy.
- Page on symptoms with multi-window burn rates; tickets for slow burns and predictive causes.
- Every page actionable, with runbook and dashboard links.
- Review pages weekly and delete the noisy ones.

## Interview angle

- **"What's an error budget?"** `1 - SLO` over a window; the allowed unreliability. While it remains you ship; when it's spent you prioritize reliability.
- **"Why symptom-based alerts?"** They fire when users are affected regardless of cause, and don't fire for harmless resource spikes, so pages stay actionable.
- **"How do you reduce alert fatigue?"** Delete or downgrade non-actionable alerts, use burn-rate alerts with duration windows, deduplicate, and review pages regularly.

Related: [Observability]({{ '/production/observability/' | relative_url }}), [Incidents and postmortems]({{ '/production/incidents-and-postmortems/' | relative_url }}).
