---
date: 2026-11-11 12:00:00 +0530
title: "Five designs later, our background jobs live in a Postgres table"
summary: "How our Postgres task queue claims, isolates and joins work without Celery or Redis, and the automation rules I built and extended on top of it."
tags: [postgresql, django, background-jobs]
---

When someone asks why last night's export failed, I don't open a broker dashboard. I run one `SELECT` and read the arguments, the timing and the traceback from a single row. Every slow thing our product does lands in that table: a 20,000-row spend import, a supplier merge, an enrichment run against an outside API. The queue is a Postgres table, the worker is a Django management command, and there is no Celery or Redis in the path.

That was our team's fifth design. This post covers how the executor works, where it is weak, and the two places my own work lives: the automation rules that turn business events into tasks, and the tasks themselves.

## What the jobs are

Gainfront makes supplier lifecycle management software: procurement teams use it to onboard suppliers, track risk and contracts, and analyze who they spend money with. A typical job: a client uploads a year of spend, we enrich every row and rebuild the analytics.

Three facts shaped the queue. Each client runs its own deployment, so every extra service is one more thing to run per client. Jobs are heavy but infrequent: a few hundred a day, some needing gigabytes of memory. And operators need to see what happened afterwards.

## Five designs in about two years

```text
 1  jobs table + worker command + SKIP LOCKED
 2  Celery                           removed two days later
 3  Redis list as a doorbell, process pool runs the tasks
 4  Postgres LISTEN/NOTIFY replaces Redis
 5  plain polling + SKIP LOCKED      what runs today
```

Our team started with a table and a worker that claimed rows with `SKIP LOCKED`, then tried the standard answer, Celery, and took it out two days later. Celery needs a broker in every client's deployment, and it keeps its own copy of task state: ids, retries, results. We already had a table that was the source of truth, and two copies of state that can disagree is worse than one.

The next design used a Redis list only as a doorbell: push an id on submit, pop it to wake the worker. That still meant a Redis per client, so it gave way to `LISTEN/NOTIFY`, the same bell rung from inside Postgres. But a notification sent while the listener is reconnecting is lost, it is only a hint (the worker still has to lock the row), and a long-lived listening connection is one more thing that can quietly break.

So we went back to polling, the simplest design that cannot lose work: after a restart the rows are still there. The price is latency. An idle worker sleeps a minute between polls, fine for jobs that take minutes and wrong for anything a user is staring at.

## A task is a row

A task is a registered Python function. Submitting it inserts a row: task name, keyword arguments as JSON, status, timing, result and, on failure, the traceback. That is the command pattern: a request becomes an object you can store, queue, inspect and re-run. Since the data a queue needs is the data an operator wants, the queue is also the audit log.

Arguments are ids, not pickled objects, so a task loads fresh rows instead of a snapshot that went stale in the queue. And every task accepts a `dry` flag. On submit, the executor first calls it with `dry=True` in the caller's process, so a bad argument fails in the request, while the user is looking, not later on a worker nobody watches.

## Claiming a task with SKIP LOCKED

The worker claims one row per loop:

```sql
BEGIN;

SELECT id, task_name, arguments
FROM jobs
WHERE status = 'queued'
  AND (run_after IS NULL OR run_after <= now())
ORDER BY id
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE jobs SET status = 'claimed' WHERE id = :claimed_id;

COMMIT;
```

Picture two workers running this at once. With plain `FOR UPDATE`, A locks the oldest row and B blocks until A commits, then re-checks the row, sees it is no longer queued and gets nothing; more workers pile up behind the same row. `NOWAIT` turns the wait into an error. `SKIP LOCKED` lets B step over the locked row and take the next: no waiting, no error, a different row per worker. Competing consumers, coordinated by the database.

What makes it safe is that the lock and the status flip share one transaction. Once it commits, the row says `claimed` and no query for `queued` rows sees it again, so there is no gap for a second worker. `run_after` gives delayed tasks for free.

Each client runs one worker today, so `SKIP LOCKED` mostly protects restarts and overlapping deploys. The design is ready for more.

## One forked process per task

After the claim commits, the worker closes its database connections and forks a child to run the task. Simplified:

```python
import multiprocessing
import time

from django.db import connections


def run_job(job_id):
    import django
    django.setup()                   # fresh app state in the child
    execute_job(job_id)              # load row by id, run, record result


def worker_loop():
    ctx = multiprocessing.get_context("fork")
    while True:
        job_id = claim_one()         # the SKIP LOCKED transaction above
        if job_id is None:
            time.sleep(60)
            continue
        connections.close_all()      # before the fork, never after
        child = ctx.Process(target=run_job, args=(job_id,))
        child.start()
        child.join()                 # one task at a time per worker
```

The child buys isolation. CPython doesn't always return freed memory to the OS, but an exiting process returns all of it. A segfault or an OOM kill takes out the child, not the worker. Module caches, open files and stray threads die with it.

Closing connections first is the line that matters. A forked child inherits the parent's open sockets. If both talked to Postgres over one socket, their protocol messages would interleave into errors that make no sense. Closing forces the child to open its own.

The cost: every task pays for a Django boot and a new connection. That is noise next to a job that runs for minutes, and far too much for thousands of tiny jobs a second.

## The life of a task

```text
          submit (dry run passed)
                  |
                  v
            +----------+  worker claim  +----------+
            |  queued  | -------------> | claimed  |
            +----------+                +----------+
                  |                          |
                  | sync mode: inline        | forked child
                  +-----------+  +-----------+
                              v  v
                          +---------+
                          | running |
                          +---------+
            returns           |   task parks itself     raises
         +--------------------+------------+-------------+
         v                                 v             v
    +--------+   all children done    +---------+    +--------+
    |  done  | <--------------------- | waiting | -> | failed |
    +--------+                        +---------+    +--------+
                                       any child failed
```

Any exception sets `failed`, saves the traceback on the row and goes to Sentry. Catching everything is deliberate: every row must end in a state an operator can see.

## Fan-out, join, and the race in the join

A parent submits children that point back at it, then sets itself to `waiting`. A `post_save` hook on the jobs table does the join: one failed child fails the parent at once, and the last child to complete marks it `done`. That save fires the same hook for the grandparent, so trees resolve from the leaves up. Children of one parent run one at a time, because siblings usually touch the same rows: "split, then join", not "run in parallel".

The join has a race, decided by the order of two lines:

```text
 parent task             child task              join hook
 -----------             ----------              ---------
 running
 submit child ---------> queued
                         claimed, runs
                         done ------------------> parent waiting?
                                                  no, still running
                                                  -> return
 set self "waiting"
 nothing left to wake it: waits forever
```

With one worker this is rare. In synchronous mode, where the child runs inline during submit, it happens every time, and one of our merge tasks had exactly this shape. The fix is to flip the parent to `waiting` before submitting any child, in the same transaction, so the last child always finds a parent to wake.

## Pipelines and scheduled jobs

A pipeline is a named list of steps registered in code; a hook starts the next step when one finishes. Step arguments are built when the step starts, because step 3 often needs an id step 2 created. The cursor is derived from the step rows, not stored, so it can't disagree with them after a crash, and a unique constraint on (run, step) makes a double advance harmless. It is a saga without compensation: fine for imports, wrong for anything that must be undone.

Scheduled jobs are rows too: a task, arguments and a repeat rule. A host cron submits whatever one query says is due today. A table beats crontab because each client wants its own times, editable without a deploy.

## Idempotency: what is guaranteed and what isn't

The executor guarantees **at-most-once** per row: the atomic claim means no row runs twice, and nothing retries automatically. The rest lives in tasks: dry runs, a "skip if an identical task is queued" check at submit, unique pipeline steps, guards and natural-key upserts.

The gaps:

- **No retries.** A task that fails on a network blip stays failed until resubmitted.
- **No lease or reaper.** If the child is killed, nothing marks the row; it sits in `running` forever.
- **No time limit** on a task.
- **Dedupe is check-then-insert.** Two simultaneous callers can both pass. It holds because submits are rare.
- **Completion hooks can fire twice**, so every hook must be repeat-safe.

The fixes are boring, which is a compliment: a heartbeat column and a reaper, an attempt counter with backoff written into `run_after`, and a unique partial index over active rows with `INSERT … ON CONFLICT DO NOTHING` for dedupe.

## The automation layer I built on top

Clients kept asking for small rules: when a supplier finishes registration, invite them to a survey; when we send a particular email, set their onboarding date. Each belonged to one client, and in code each cost a ticket, a review and a deploy. I wanted admins to configure them as data.

I built the first version. A rule named the model it watched, a condition as JSON field lookups (`{"status": "registered"}`), and an action: any registered task, picked from a dropdown, with its arguments. I also wrote the first action, inviting a supplier to surveys. The decision I cared about was keeping condition and action apart: the condition decides *whether*, and a new kind of action is just a new task. My version ran the action inline; a teammate soon switched it to submitting a job, which was right, because a slow mail server should never slow the registration page.

JSON lookups on one model can't express joins, and real rules needed them. A teammate redesigned the condition as SQL, run with bound parameters. The task is submitted only if the query returns rows and every row says `submit_task = true`, so one query can check several conditions. Extra columns from the first row flow into the task's arguments.

Rules can only react to events the code announces, and that is where my later work went. I added four triggers (registration through a partner portal, an email sent to a supplier, a supplier becoming registered, an approval level approved) and an on/off switch. A simplified version of the runtime, with one of my triggers wired in:

```python
became_registered = Signal()


def fire_rules(event, obj, params):
    params = {**params, "object_id": obj.pk}
    rules = TriggerRule.objects.filter(
        event=event, model=obj._meta.label_lower, enabled=True
    ).order_by("rank")
    for rule in rules:
        with connection.cursor() as cur:
            cur.execute(rule.sql, params)       # bound, never formatted
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if rule.task_name and rows and all(r["submit_task"] for r in rows):
            extra = {k: v for k, v in rows[0].items() if k != "submit_task"}
            submit(rule.task_name, automated=True,
                   **{**rule.task_kwargs, **extra})


@receiver(became_registered)
def on_registered(sender, obj, previous_status, current_status, **kw):
    fire_rules("became_registered", obj, {
        "previous_status": previous_status,
        "current_status": current_status,
    })
```

Why a named signal per event instead of one generic "something happened"? Each event carries different context: "email sent" needs the template id, "registered" needs the old and new status. A named signal makes that contract explicit, and each receiver builds exactly the parameters its rules can use.

The previous status is what makes "when a supplier becomes registered, send the welcome email" fire once. Seeing only the current status, it is a level trigger: every later save of a registered supplier sends the email again. With both, it becomes an edge trigger:

```sql
SELECT (
    %(previous_status)s IS DISTINCT FROM 'registered'
    AND %(current_status)s = 'registered'
    AND NOT EXISTS (
        SELECT 1 FROM pending_invitations i
        WHERE i.supplier_id = %(object_id)s AND i.closed_at IS NULL
    )
) AS submit_task;
```

That is idempotency at the business level, which no queue gives you. The switch is smaller: before it, stopping a misbehaving rule meant deleting it, SQL and all.

## Writing tasks that are safe to run here

At-most-once with idempotency left to tasks is a contract, so I write my tasks to it: a savings-data import and the supplier diversity enrichment job among them. The import, simplified:

```python
@task
def import_savings(upload_id, dry=False, job=None):
    upload = Upload.objects.get(pk=upload_id)
    if upload.state != "ready":
        raise ValueError("upload is not ready")  # fails at submit
    rows = upload.parsed_rows()
    if dry:
        return {"rows": len(rows)}
    with transaction.atomic():                   # all or nothing
        for row in rows:
            SavingsInitiative.objects.update_or_create(
                client_ref=row["ref"], period=row["period"],
                defaults=to_fields(row),
            )
    return {"rows": len(rows)}
```

The natural-key upsert makes loading the same file twice harmless. The enrichment job guards before its first write (no uploaded file, or a result already exists, means no run) and fails loudly if every outside lookup failed, because an outage should never look like "no matches".

That job's first version ran its lookups as a child task and parked the parent in `waiting`. I removed the handshake and ran them inline: the child had side effects of its own, and park-then-wait had exactly the race above. One task, one life cycle, matching the user's request. I wrote that up separately in [the diversity enrichment post]({{ '/blog/diversity-enrichment/' | relative_url }}).

## Results

Throughput was never the problem. What we get:

- **One less service per deployment.** Backing up Postgres backs up the queue.
- **"Why didn't this run?" is a query.**
- **Failures stay small.** A 20,000-row import that balloons in memory gives it all back when its child exits.
- **Client rules ship as data**, written by an admin instead of deployed.

The caveats: up to a minute of idle latency, fan-out that runs in sequence, at-most-once with the gaps above, and "multi-worker safe" rather than a cluster.

## When I'd stop using Postgres as a queue

- **Many tiny jobs, fast.** Every claim is a write and every task a fork plus a Django boot.
- **Sub-second pickup.** Polling won't do it; `LISTEN/NOTIFY` as a hint plus polling gets closer.
- **Heavy churn.** We keep finished rows forever. At volume you need retention, a partial index on queued rows and attention to vacuum.
- **Many consumers per event.** That wants a log or pub/sub, not a task table.
- **Rebuilding a broker.** Once you add retries, rate limits, priorities and dead letters, you maintain a worse RabbitMQ.

For our heavy, infrequent jobs, none of those is true yet.

## What I'd change next

On the executor, the boring fixes above: a heartbeat, retries with backoff, and dedupe enforced by a unique index instead of a check. On the automation layer, rules are code that admins write at runtime, so they deserve the same guard rails as code: least-privilege execution with a statement timeout, hooks that log their own failures instead of surfacing them to the user who triggered the event, and a test per trigger that fires the signal and asserts the job row exists. A queue that is just a table makes all of that easy to add, which is the main reason I still like it.
