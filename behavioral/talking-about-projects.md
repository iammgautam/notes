---
title: Presenting a project deep dive
section: behavioral
order: 4
summary: A 15-minute structure for walking through a technical project, worked through with the Postgres-native task executor as the example.
---

## When to use it

- "Walk me through a project you're proud of."
- "Tell me about the most complex system you've built."
- Dedicated project deep-dive or "past experience" rounds.

The interviewer is testing: can you explain a system clearly, justify decisions, discuss trade-offs honestly, and show what **you** did.

## The structure (15 minutes)

| Min | Section | Goal |
|---|---|---|
| 0–2 | Context and problem | Why this needed to exist |
| 2–4 | Requirements and constraints | What "done" meant |
| 4–9 | Design | Architecture, key mechanisms |
| 9–12 | Decisions and trade-offs | Why this, not that |
| 12–14 | Results and what went wrong | Outcome, honest lessons |
| 14–15 | What you'd do differently | Reflection, room for questions |

Pause after the design section and ask: "Want me to go deeper on any part, or continue to trade-offs?" Let them steer.

## Worked outline: Postgres-native task executor

Facts from the CV are stated; everything marked _Add:_ must come from your memory of the real system.

### 1. Context and problem (2 min)

- Django services in a multi-tenant microservices architecture at Gainfront.
- Needed background task execution with queuing, isolation, fan-out/join and idempotency.
- Built on PostgreSQL only, no Celery or Redis.
- _Add: the workloads that needed it (what kind of tasks, rough volume)._
- _Add: what was there before, or what problem prompted building it._

### 2. Requirements and constraints (2 min)

Functional (from the CV):

- Queuing.
- Task isolation.
- Fan-out/join: one task spawns many, a follow-up runs when all complete.
- Idempotent execution: running a task twice has the same effect as once.

_Add:_ non-functional constraints you actually had: throughput, latency to start, tenant isolation needs, ops constraints (why no extra infrastructure).

### 3. Design (5 min)

Draw it. A simple diagram beats a minute of words.

```text
 Django service                PostgreSQL                 Workers
 --------------              ---------------            ----------
 enqueue(task) ---- INSERT --> tasks table   <-- claim -- worker 1
   (same txn as                (status,          (lock)   worker 2
    business write)            run_after,                 worker N
                               attempts...)
                                   |
 fan-out: parent -> N children     | join: when all children done,
                                   +-> enqueue continuation
```

Talk through each mechanism, in your real implementation's terms:

- **Enqueue:** _Add: how tasks are inserted; whether enqueue happens in the same transaction as the business write._
- **Claiming work:** _Add: how a worker safely claims a task without two workers taking the same one._
- **Task isolation:** _Add: what isolation means here (per tenant? per task type? failure isolation?) and how it's enforced._
- **Fan-out/join:** _Add: how children are tracked and how the join knows all children finished._
- **Idempotency:** _Add: the mechanism (idempotency keys, state checks, unique constraints)._
- **Failures and retries:** _Add: retry policy, what happens when a worker dies mid-task._

### 4. Decisions and trade-offs (3 min)

Have 2–3 decisions ready, each as **options → choice → why → cost**.

| Decision | Options | Prompt |
|---|---|---|
| Queue backend | Celery + Redis/RabbitMQ vs Postgres | _Add: why Postgres won for this system._ |
| Claiming strategy | _Add_ | _Add_ |
| Join mechanism | _Add_ | _Add_ |

Be ready to state the downside of your choice. Generic trade-offs of Postgres-as-queue that interviewers often raise:

- Throughput ceiling vs a dedicated broker.
- Table bloat and vacuum pressure from high update/delete churn.
- Polling latency vs push-based delivery.

_Add: which of these mattered in your case and how you handled them (or why they didn't matter at your scale)._

### 5. Results and what went wrong (2 min)

- _Add: what it runs today and how reliably._
- _Add: one thing that broke or surprised you, and the fix._

### 6. What you'd do differently (1 min)

- _Add: one honest improvement._

## Likely follow-up questions

Prepare an answer for each:

- How do you guarantee a task isn't run twice concurrently?
- What happens if a worker crashes halfway through a task?
- How do you prevent one tenant from starving others?
- How do you monitor it? (queue depth, age of oldest task, failures)
- How far does it scale? What would make you move to a dedicated broker?
- How would you test it?

## Pitfalls

- Starting with implementation details before the problem.
- Saying "we" for every decision. Make your role explicit.
- No trade-offs: every design has costs; claiming none sounds naive.
- Going past 15 minutes without checking in.
- Claiming numbers or scale you can't back up in follow-ups.

## Checklist

- One diagram you can draw in 60 seconds.
- Three decisions with options, choice, reason, cost.
- One thing that went wrong.
- Every _Add:_ filled before the interview.
