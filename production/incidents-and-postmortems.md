---
title: Incidents and postmortems
section: production
order: 5
summary: How to run an incident (roles, comms cadence, mitigation before root cause) and write a blameless postmortem that produces real fixes.
---

An incident is any unplanned event that degrades service enough that someone has to drop what they're doing. The technical fix is often the easy part. What goes wrong is coordination: three engineers changing things at once, nobody updating customers, and a root-cause hunt while users are still down. A lightweight process fixes that.

## How it works

### Severity

Define levels up front so nobody debates them at 2 a.m.

| Sev | Meaning | Example | Response |
|---|---|---|---|
| SEV1 | Core function down or data at risk for many customers | Login fails for all tenants; data written to the wrong tenant | Page immediately, all hands, status page |
| SEV2 | Major degradation or one large customer down | Reports time out for enterprise tenants; task queue stalled 30 min | Page on-call, status page if customer-visible |
| SEV3 | Minor, workaround exists | One export format broken | Ticket, fix in business hours |

Cross-tenant data exposure is always SEV1, regardless of how many rows.

### Roles

Even with two people, separate these:

- **Incident commander (IC)**: owns the incident, not the keyboard. Decides, delegates, keeps the timeline, declares resolved. Asks "what are we trying next and who is doing it?"
- **Operations / investigator(s)**: hands on systems. Announce every change before making it.
- **Communications lead**: updates stakeholders, support and the status page on a fixed cadence, so engineers aren't interrupted.
- **Scribe** (often the IC on small teams): timestamps every observation and action in the channel.

> **Rule of thumb:** The person debugging should not also be the person answering "any update?" from five channels.

### Lifecycle

```text
detect -> declare -> triage -> mitigate -> resolve -> postmortem -> follow-ups
             |                   ^
             +-- comms every 30 min until resolved
```

### Mitigation first

Your first goal is to stop user impact, not to understand it. Mitigations are usually reversible and fast:

- Roll back the last deploy.
- Turn off the feature flag.
- Scale out workers or the DB connection pool (carefully).
- Fail over to a secondary LLM provider; serve cached results.
- Pause a runaway backfill or a noisy tenant's jobs.
- Rate-limit or block the offending client.
- Put the affected feature into read-only mode.

Root cause comes after users are OK. If a rollback fixes it, you have also learned a lot about the cause for free.

## In practice

### Declaring and running

Open a dedicated channel (`#inc-2026-03-04-reports-timeouts`). First message:

```text
INCIDENT DECLARED  SEV2
Impact:   Report generation timing out for ~40% of tenants since 10:05 UTC
IC:       <name>   Comms: <name>   Ops: <name>, <name>
Status:   Investigating. Last deploy 09:58 (release a1b2c3d).
Next update: 10:45 UTC
```

During the incident:

- Announce before acting: "Rolling back to release 9f8e7d6 at 10:31."
- Record observations with timestamps, including dead ends.
- Keep one decision-maker. Disagreement goes to the IC, who decides and moves on.
- If there's no progress in 30–60 min, escalate: page the owner of the subsystem, the DB expert, the vendor.

### Customer comms

Status page and customer updates follow a pattern: **what's affected, what users see, what you're doing, when the next update is**. No speculation about cause, no promised ETA you can't keep.

```text
10:20 Investigating: Some customers are seeing timeouts when generating
reports. Other features are unaffected. We are investigating and will
update by 10:45 UTC.
```

### Resolve

Declare resolved when metrics are back to baseline for a defined period (for example 30 min), not when the fix is deployed. Note any cleanup still needed: replaying failed tasks, fixing bad data, refunding.

### The blameless postmortem

Blameless means you assume people acted reasonably with the information and tools they had. The question is "what about the system made this easy to get wrong?", not "who did it?". If the answer is "engineer X should have been more careful", the system will produce the same incident with engineer Y.

Write it within a few days while memory is fresh. Template:

```text
Title:       Report timeouts after composite index drop
Date:        2026-03-04       Severity: SEV2       Duration: 10:05-11:12 UTC
Authors:     ...              Status:   Draft / Reviewed

Summary
  2-3 sentences: what happened, impact, how it was fixed.

Impact
  Users/tenants affected, duration, failed requests/jobs, data impact,
  SLO budget consumed, support tickets.

Timeline (UTC)
  09:58 Release a1b2c3d deployed (includes migration 0142)
  10:05 p95 latency on /api/reports/ rises from 0.8s to 28s
  10:12 Alert fires: reports error rate > 5%
  10:15 Incident declared, IC assigned
  10:31 Rollback started
  10:40 Latency still high: rollback does not restore the dropped index
  10:52 Index recreated CONCURRENTLY
  11:12 Metrics at baseline for 20 min, resolved

Root cause and contributing factors
  - Migration dropped an index believed unused (stats reset recently)
  - No check comparing query plans before/after migrations
  - Rollback procedure doesn't reverse schema changes

Detection
  How was it detected? How could it have been detected sooner?

What went well / what went poorly / where we got lucky

Action items
  | Action                                         | Type    | Owner | Due   |
  | Require 30d of idx_scan stats before drop      | Prevent | ...   | 03-15 |
  | Alert on p95 per route, not just error rate    | Detect  | ...   | 03-11 |
  | Runbook: schema changes are not rolled back    | Mitigate| ...   | 03-08 |
```

Good action items are specific, owned, dated and tracked in the same backlog as feature work. Classify them as **prevent**, **detect** or **mitigate**. "Be more careful" is not an action item; "add a CI check that fails migrations dropping an index without a sign-off label" is.

### The "5 whys" trap

Asking "why" repeatedly is useful, but incidents rarely have one root cause. Look for **contributing factors** across code, process, tooling and monitoring. A deploy caused it, a missing alert delayed it, a confusing runbook prolonged it. Each is a fix.

## Pitfalls

- **Debugging before mitigating.** 40 minutes of root-cause analysis while a rollback would have taken 5.
- **Everyone changing things.** Two people restarting different services confounds every signal. One IC, announced actions.
- **Silent incidents.** Support learns about the outage from customers. Comms on a fixed cadence even if the update is "no change".
- **Postmortems as blame.** People stop reporting near-misses and hide details. You lose the information you need most.
- **Action items that never ship.** Track completion rate. A postmortem with zero closed actions after a month was a document, not a fix.
- **Skipping near-misses.** An incident that almost happened is a free lesson.

## Checklist

- Severity levels and an IC role defined before you need them.
- Declare early; downgrading is cheap.
- Mitigate first: rollback, flag off, fail over, pause.
- Comms cadence with a "next update by" time.
- Timeline written live, with timestamps.
- Blameless postmortem within a week; owned, dated action items.

## Interview angle

- **"Tell me about handling a production incident."** Structure: detection, scope, mitigation first, root cause, follow-ups that prevent recurrence. Mention communication.
- **"What makes a postmortem blameless?"** Focus on systemic factors and the information people had, not individual fault; the goal is learning and fixes, not punishment.

Related: [Debugging production issues]({{ '/production/debugging-production/' | relative_url }}), [SLOs and alerting]({{ '/production/slos-and-alerting/' | relative_url }}).
