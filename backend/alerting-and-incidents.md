---
title: Alerting and incident management
section: backend
order: 70
phase: "Phase 9 · DevOps and observability"
summary: Symptom-based alerts, SLO burn-rate alerting with a runnable calculator, paging hygiene, on-call, incident roles and comms, and blameless postmortems.
---

Monitoring is only useful if the right person finds out about the right problem at the right time, and then the team handles it calmly. Two failure modes dominate. Too many alerts: on-call engineers get paged for CPU at 85% and disks at 70%, learn to ignore pages, and miss the real outage. Too few or too late: customers report the outage on social media before any alert fires. The fix is to alert on user-visible symptoms measured against service level objectives, and to run incidents with a small, rehearsed process.

## SLIs, SLOs and error budgets

Alerting decisions start here, so a brief recap:

- **SLI** (indicator): the ratio of good events to valid events. "Share of HTTP requests that return non-5xx in under 300 ms", "share of jobs completed within 10 minutes".
- **SLO** (objective): the target for an SLI over a window. "99.9% of requests good over a rolling 30 days".
- **Error budget**: `1 - SLO`, the amount of failure you're allowed. Spent on incidents, risky deploys and maintenance; when it runs out, reliability work takes priority over features.

| SLO | Budget per 30 days | Budget per 30 days at 100 req/s |
|---|---|---|
| 99% | 7.2 hours | 2.59 million failed requests |
| 99.5% | 3.6 hours | 1.30 million |
| 99.9% | 43.2 minutes | 259,200 |
| 99.95% | 21.6 minutes | 129,600 |
| 99.99% | 4.32 minutes | 25,920 |

Pick SLOs from what users notice and what the business needs, not from what the system currently achieves. An internal batch API doesn't need four nines.

## Symptom-based alerts

Page on **symptoms**, what users experience, rather than **causes**, what might be behind it.

| Symptom (page-worthy) | Cause (dashboard or ticket) |
|---|---|
| Checkout error ratio above budget burn threshold | One pod's CPU at 95% |
| p99 latency of the API above SLO | Postgres replication lag 5 s |
| Jobs older than 15 minutes in the queue | Celery worker restarted |
| Synthetic login probe failing from 3 regions | Disk 75% full |
| Webhook deliveries failing for 10% of tenants | Cache hit ratio dropped |

Why: a cause-based alert fires when nothing is wrong for users (CPU high but latency fine) and stays silent for failures nobody predicted. A symptom-based alert catches every cause that matters, including ones you never thought of. Causes still belong in dashboards for diagnosis, and a few cause-based alerts are justified when they predict an imminent, certain symptom with lead time to act: "disk will be full in 4 hours at the current growth rate", "TLS certificate expires in 7 days", "Postgres transaction ID wraparound approaching".

Measure symptoms as close to the user as possible: load balancer or gateway metrics, not only the app's own metrics (a crashed app reports nothing), plus black-box synthetic probes from outside your network for the critical flows.

## SLO burn-rate alerts

Naive SLO alerting ("error ratio above 0.1% for 5 minutes") either pages for tiny blips that barely touch the budget or, with long durations, detects outages too late. Burn rate fixes this.

**Burn rate** is how fast you're spending the error budget relative to the rate that would spend exactly all of it by the end of the SLO window:

```text
burn_rate          = observed_error_ratio / (1 - SLO)
budget_spent       = burn_rate * alert_window / slo_window
time_to_exhaustion = slo_window / burn_rate
```

With a 99.9% SLO, an error ratio of 1.44% is a burn rate of 14.4: the whole 30-day budget would be gone in about 50 hours, and one hour at that rate spends 2% of it. The Google SRE Workbook recommends **multiwindow, multi-burn-rate** alerts:

| Severity | Long window | Short window | Burn rate | Budget spent when it fires |
|---|---|---|---|---|
| Page | 1 hour | 5 minutes | 14.4 | 2% |
| Page | 6 hours | 30 minutes | 6 | 5% |
| Ticket | 3 days | 6 hours | 1 | 10% |

- The **long window** sets significance: it only fires once a meaningful share of budget is spent, so a 30-second blip doesn't page.
- The **short window** (about 1/12 of the long one) must also be burning, so the alert stops soon after recovery instead of firing for the rest of the hour.
- **Fast burn pages, slow burn tickets.** A total outage trips the 1-hour rule in under a minute; a slow leak at 2x shows up as a ticket within days.

A runnable calculator and evaluator over a simulated per-minute series:

```python
SLO = 0.999
SLO_WINDOW_H = 30 * 24          # 30-day rolling window = 720 hours

# (name, long window h, short window h, budget fraction spent, severity)
RULES = [
    ("fast", 1, 5 / 60, 0.02, "page"),
    ("medium", 6, 0.5, 0.05, "page"),
    ("slow", 72, 6, 0.10, "ticket"),
]


def burn_rate(error_ratio, slo=SLO):
    """How many times faster than 'exactly on budget' errors are arriving."""
    return error_ratio / (1 - slo)


def threshold(budget_fraction, window_h):
    """Burn rate that spends budget_fraction of the budget within window_h."""
    return budget_fraction * SLO_WINDOW_H / window_h


def detection_minutes(threshold_rate, actual_rate, window_h):
    """Time for a sudden, constant burn to push the window average over the line."""
    return window_h * 60 * threshold_rate / actual_rate


class Series:
    """Per-minute (requests, errors) with prefix sums for O(1) window ratios."""

    def __init__(self, minutes):
        self.req, self.err = [0], [0]
        for r, e in minutes:
            self.req.append(self.req[-1] + r)
            self.err.append(self.err[-1] + e)

    def error_ratio(self, window_h):
        n = min(int(round(window_h * 60)), len(self.req) - 1)
        reqs = self.req[-1] - self.req[-1 - n]
        return (self.err[-1] - self.err[-1 - n]) / reqs if reqs else 0.0


def evaluate(series):
    """Multiwindow, multi-burn-rate: both windows must exceed the threshold."""
    firing = []
    for name, long_h, short_h, budget, severity in RULES:
        limit = threshold(budget, long_h)
        if (burn_rate(series.error_ratio(long_h)) > limit
                and burn_rate(series.error_ratio(short_h)) > limit):
            firing.append((name, severity))
    return firing


def minutes(n, rpm=10_000, error_ratio=0.0005):
    return [(rpm, round(rpm * error_ratio))] * n


if __name__ == "__main__":
    assert round(threshold(0.02, 1), 6) == 14.4
    assert round(threshold(0.05, 6), 6) == 6.0
    assert round(threshold(0.10, 72), 6) == 1.0
    assert round((1 - SLO) * SLO_WINDOW_H * 60, 6) == 43.2   # budget in minutes

    three_days = 72 * 60
    # healthy: 0.05% errors is half the budget rate -> silence
    assert evaluate(Series(minutes(three_days))) == []

    # hard outage for the last 10 minutes: page on the fast rule
    s = Series(minutes(three_days - 10) + minutes(10, error_ratio=1.0))
    assert ("fast", "page") in evaluate(s)

    # outage ended 20 minutes ago: 1 h window still high, 5 m window clean
    s = Series(minutes(three_days - 30) + minutes(10, error_ratio=1.0) + minutes(20))
    assert burn_rate(s.error_ratio(1)) > 14.4
    assert ("fast", "page") not in evaluate(s)

    # slow leak: 0.2% errors for three days is burn rate 2 -> ticket, not page
    s = Series(minutes(three_days, error_ratio=0.002))
    assert evaluate(s) == [("slow", "ticket")]

    # a total outage (burn rate 1000) trips the fast rule in under a minute
    assert round(detection_minutes(14.4, burn_rate(1.0), 1), 2) == 0.86
    print("burn rate ok")
```

The third scenario is the reason for the short window: the hour-long average is still far above the threshold 20 minutes after recovery, but nobody should be paged for an incident that is already over.

Low-traffic services need adjustments: at 10 requests per minute, two failures are a 20% error ratio. Use longer windows, require a minimum event count, or add synthetic traffic so the SLI has enough samples.

## Paging hygiene

A page interrupts sleep and work, so every page must earn it:

- **Actionable, urgent, user-impacting.** If the right response is "look at it tomorrow", it's a ticket. If there's no action, it's a dashboard. If it's not affecting users and won't soon, it's not a page.
- **Every alert has an owner and a runbook link**: what it means, how to confirm impact, first mitigation steps, escalation path.
- **Group and deduplicate.** One database failure shouldn't send 40 pages from 40 services. Alertmanager's `group_by`, inhibition rules (suppress dependent alerts while a root alert fires) and routing by `team` and `severity` labels do this.
- **Tune durations.** A `for:` clause or burn-rate windows prevent flapping; don't paper over a noisy signal with a longer wait if the signal itself is wrong.
- **Measure alert quality.** Track pages per shift, the share that led to action, and time to acknowledge. Google's SRE book suggests no more than about two incidents per 12-hour shift so each gets proper follow-up. Review every page weekly: fix, retune or delete.
- **Silence deliberately.** Planned maintenance gets a silence with an expiry and a reason, never a disabled alert that nobody re-enables.

## On-call

- **Rotation**: a primary and a secondary, weekly or shorter shifts, enough people that each is on call at most one week in four or five. Follow-the-sun rotations across time zones avoid night pages entirely for larger teams.
- **Escalation policy**: if the primary doesn't acknowledge within 5–10 minutes, page the secondary, then the team lead.
- **Readiness**: access to production, dashboards, runbooks and a laptop; shadow shifts for new engineers before they go primary.
- **Handoffs**: a short written summary of open issues, ongoing incidents and silences at each shift change.
- **Sustainability**: compensate on-call time, give time off after rough nights, and treat a noisy rotation as a defect to fix. The SRE book also caps operational work at about 50% of an SRE team's time so engineering work keeps reducing toil.
- **Ownership**: the team that builds a service carries its pager ("you build it, you run it"), which aligns incentives for reliability.

## Incident roles

When something serious breaks, an incident is **declared** and a small command structure forms, adapted from the Incident Command System used by emergency services (Google calls its version IMAG).

```text
                    +----------------------+
                    | Incident commander   |  owns decisions, delegates,
                    | (IC)                 |  keeps the big picture
                    +----+-------+-----+---+
                         |       |     |
          +--------------+       |     +----------------+
          v                      v                      v
 +-----------------+   +-----------------+   +--------------------+
 | Operations lead |   | Communications  |   | Scribe             |
 | + responders:   |   | lead: status    |   | timeline, actions, |
 | debug, mitigate |   | page, execs,    |   | decisions for the  |
 +-----------------+   | customers       |   | postmortem         |
                       +-----------------+   +--------------------+
```

- **Incident commander**: coordinates and decides; doesn't debug. Assigns work, sets the update cadence, decides on escalation and when the incident is resolved. The first responder is IC until they hand off explicitly.
- **Operations lead** and responders: investigate and change production. Only they touch prod, to avoid conflicting changes.
- **Communications lead**: internal stakeholder updates, status page, customer success and support.
- **Scribe**: keeps the timeline of observations, actions and decisions.

In a small team one person may hold two roles, but IC and hands-on debugging should be separate as soon as a second person is available.

| Severity | Example | Response |
|---|---|---|
| SEV1 | Core product down, data loss, security breach | Page now, IC, exec and customer comms, updates every 30 minutes |
| SEV2 | Major feature down or large tenants affected | Page, IC, status page |
| SEV3 | Minor degradation with a workaround | Ticket, business hours |
| SEV4 | No user impact | Backlog |

Priorities during the incident: **mitigate first, understand later.** Roll back the deploy, turn off the feature flag, fail over, shed load, block the abusive tenant. Root cause analysis can wait until users are no longer affected. Declare early; an incident downgraded after 10 minutes costs little, one declared an hour late costs a lot.

## Incident communication

- **One channel per incident** (`#inc-2026-11-16-checkout-errors`) with a pinned summary: impact, IC, current status, next update time.
- **Regular cadence even with no news**: every 30 minutes for SEV1, every hour for SEV2. Silence makes stakeholders start their own investigation in your channel.
- **Customer-facing updates state impact, not internals**: what is affected, since when, what you're doing, when the next update comes. Don't speculate on cause or promise times you can't meet.
- **Multi-tenant SaaS**: identify affected tenants from logs and metrics (tenant ID in telemetry pays off here) and give targeted updates to the large ones.

```text
[Investigating] 10:42 UTC - Checkout errors for some customers
Impact: About 15% of checkout attempts in the EU region have failed
        since 10:21 UTC. Browsing and order history are unaffected.
Action: We identified a faulty release and are rolling it back.
Next update: 11:15 UTC, or sooner if anything changes.
```

## Postmortems

A postmortem turns an incident into lasting improvements. Write one for every SEV1 and SEV2, for any incident that consumed a large share of the error budget, and for near misses worth learning from.

- **Blameless**: assume everyone acted reasonably with the information they had. Ask "how did the system allow this?" rather than "who did this?". Blame produces hidden mistakes; blamelessness produces honest timelines.
- **Prompt**: draft within a few days while memory is fresh, review it in a meeting, publish it widely.
- **Contributing factors, not one root cause.** Outages usually need several conditions at once: a bug, a missing test, an alert that didn't fire, a runbook that was out of date. "Five whys" helps but can stop at a convenient single answer.
- **Action items** with owners, priorities and due dates, split into prevent, detect faster and mitigate faster. Track them like product work; a postmortem whose actions never close is theater.

```text
Title, date, severity, authors, status
Summary          two or three sentences: what broke, impact, how it ended
Impact           duration, users and tenants affected, failed requests,
                 error budget consumed, revenue or data impact
Timeline (UTC)   detection, escalation, decisions, mitigation, resolution
Contributing     technical and process factors; why defenses didn't catch it
factors
Detection        how we found out, and how we could have found out sooner
What went well / what went poorly / where we got lucky
Action items     owner, priority, ticket, due date
```

Useful aggregate metrics are time to detect, time to mitigate and time to resolve, and repeat incidents by cause. Treat them as trends to discuss, not targets; teams gamed into lowering MTTR start declaring incidents later.

## In practice

Illustrative Prometheus recording and alerting rules for the two page-level burn-rate alerts on a 99.9% SLO. The long windows for the slow ticket rule follow the same pattern.

```yaml
groups:
  - name: orders-api-slo
    rules:
      - record: slo:error_ratio:rate5m
        expr: |
          sum(rate(http_requests_total{job="orders-api",code=~"5.."}[5m]))
          / sum(rate(http_requests_total{job="orders-api"}[5m]))
      - record: slo:error_ratio:rate30m
        expr: |
          sum(rate(http_requests_total{job="orders-api",code=~"5.."}[30m]))
          / sum(rate(http_requests_total{job="orders-api"}[30m]))
      - record: slo:error_ratio:rate1h
        expr: |
          sum(rate(http_requests_total{job="orders-api",code=~"5.."}[1h]))
          / sum(rate(http_requests_total{job="orders-api"}[1h]))
      - record: slo:error_ratio:rate6h
        expr: |
          sum(rate(http_requests_total{job="orders-api",code=~"5.."}[6h]))
          / sum(rate(http_requests_total{job="orders-api"}[6h]))

      - alert: OrdersApiErrorBudgetBurn
        expr: |
          (slo:error_ratio:rate1h > (14.4 * 0.001) and slo:error_ratio:rate5m > (14.4 * 0.001))
          or
          (slo:error_ratio:rate6h > (6 * 0.001) and slo:error_ratio:rate30m > (6 * 0.001))
        labels:
          severity: page
          team: orders
        annotations:
          summary: orders-api is burning its 30-day error budget too fast
          runbook_url: https://runbooks.example.com/orders-api/error-budget
```

Illustrative Alertmanager routing: group related alerts, page only for `severity="page"`, and suppress service-level pages while a cluster-wide alert is firing:

```yaml
route:
  receiver: slack-orders
  group_by: [alertname, team]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="page"']
      receiver: pager-orders
    - matchers: ['severity="ticket"']
      receiver: tickets-orders
inhibit_rules:
  - source_matchers: ['alertname="KubernetesClusterUnreachable"']
    target_matchers: ['severity="page"']
    equal: [cluster]
```

Burn-rate alerts also make good automatic rollback triggers: if the fast-burn alert fires within 30 minutes of a deploy, the pipeline can roll back first and ask questions later (see [deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }})).

## Pitfalls

- **Cause-based paging.** Symptom: pages for high CPU with no user impact, on-call ignores pages. Fix: page on SLO burn and symptoms, move causes to dashboards.
- **Static thresholds on ratios at low traffic.** Symptom: pages at 3 a.m. for two failed requests. Fix: minimum event counts, longer windows, synthetic traffic.
- **Alerts only from inside the app.** Symptom: the app crashed, so its error metrics went quiet and nothing fired. Fix: measure at the load balancer, alert on absent metrics and failed scrapes, black-box probes.
- **No runbooks.** Symptom: every page starts with 20 minutes of figuring out what the alert means. Fix: runbook link required to merge an alert.
- **Hero culture.** Symptom: the same senior engineer fixes every incident, burns out, and knowledge doesn't spread. Fix: rotate IC duty, shadowing, written runbooks, game days.
- **Debugging instead of mitigating.** Symptom: a 2-hour outage that a 2-minute rollback would have ended. Fix: make "what changed recently, can we roll it back?" the first question.
- **Postmortems without follow-through.** Symptom: the same incident three months later. Fix: tracked action items with owners, reviewed in planning.

## Interview questions

**Why alert on symptoms rather than causes?** Symptoms are what users experience, so every symptom alert is relevant and catches unknown causes. Cause alerts fire when users are fine and miss failure modes nobody anticipated. Keep causes for diagnosis, plus a few predictive alerts like disk-full-in-hours.

**What is an error budget burn rate?** The observed error ratio divided by the allowed error ratio (`1 - SLO`). Burn rate 1 spends the budget exactly over the SLO window; 14.4 against a 30-day window spends 2% of the budget in one hour.

**Why use two windows per burn-rate alert?** The long window ensures enough budget was spent to matter, filtering blips. The short window confirms the burn is still happening, so the alert resolves quickly after recovery instead of firing for the rest of the long window.

**What makes a good page?** It's urgent, actionable, tied to user impact, routed to the owning team, deduplicated, and links to a runbook. Anything else is a ticket or a dashboard.

**What does an incident commander do?** Coordinates rather than debugs: assigns roles, keeps a picture of impact and actions, decides on mitigations and escalations, sets communication cadence, and declares resolution. The IC role is handed off explicitly.

**What should be in a postmortem?** Summary, impact with numbers, timeline, contributing factors, how detection and response went, what went well and badly, and owned, prioritized action items. Written blamelessly and tracked to completion.

**Your on-call gets 30 pages a week. What do you do?** Categorize every page by action taken. Delete alerts with no action, convert non-urgent ones to tickets, fix flapping thresholds, group duplicates, replace cause alerts with SLO burn-rate alerts, and fix the top recurring underlying problems. Track pages per shift as a team metric.

## Further reading

- *Site Reliability Engineering* (Google, 2016): "Monitoring Distributed Systems", "Being On-Call", "Managing Incidents" and "Postmortem Culture".
- *The Site Reliability Workbook* (Google, 2018): "Alerting on SLOs".
- *My Philosophy on Alerting*, Rob Ewaschuk (Google).
- *Implementing Service Level Objectives*, Alex Hidalgo (O'Reilly, 2020).
- PagerDuty Incident Response documentation (open-sourced process guide).
