---
title: Celery in production
section: python-django
order: 13
summary: Delivery guarantees, acks_late and idempotency, retries with backoff, visibility timeouts, prefetching, routing, beat and queue monitoring.
---

Celery is easy to start and hard to run. The defaults favor throughput and simplicity, not correctness: tasks are acknowledged before they run, workers prefetch several messages, and a Redis broker silently redelivers anything not acknowledged within an hour. In a multi-tenant SaaS where tasks send invoices, charge cards and sync to ERPs, each of those defaults can produce lost work or duplicate side effects. The fix is to design every task for **at-least-once** delivery.

## How it works

```text
 producer (web)          broker (Redis/RabbitMQ)          worker
 task.delay(id) ──────▶  queue "default"  ──prefetch──▶  pool slot
                         (message stays                  runs task
                          until ack)        ◀────ack──── (early or late)
                                                         result backend
```

- `delay()` serializes the task name and arguments (JSON by default) and publishes a message.
- A worker reserves messages (up to `prefetch_multiplier * concurrency`), hands them to pool slots, and acknowledges them.
- **Early ack** (default, `acks_late=False`): ack when the task starts. A crash mid-task loses the task: at-most-once.
- **Late ack** (`acks_late=True`): ack after the task returns. A crash means redelivery: at-least-once, so the task may run twice.

### Visibility timeout (Redis and SQS)

Redis has no native acks. Celery emulates them: unacked messages are stored with a timestamp and restored to the queue if not acked within `visibility_timeout` (default 1 hour). Consequences:

- A task with `acks_late=True` that runs longer than the timeout is **delivered again to another worker while still running**.
- ETA/countdown tasks scheduled further out than the timeout get redelivered repeatedly, producing duplicates.

Set it above your longest task and longest ETA, or avoid long ETAs on Redis. RabbitMQ uses real acks with a consumer timeout (default 30 minutes in recent versions) instead.

## In practice

### A baseline config

```python
# celery.py / settings
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True     # requeue if the child is killed
CELERY_WORKER_PREFETCH_MULTIPLIER = 1        # fair dispatch for long tasks
CELERY_TASK_TIME_LIMIT = 300                 # hard kill (seconds)
CELERY_TASK_SOFT_TIME_LIMIT = 270            # raises SoftTimeLimitExceeded
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3 * 3600}
CELERY_TASK_IGNORE_RESULT = True             # opt in per task when needed
CELERY_RESULT_EXPIRES = 24 * 3600
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
```

`reject_on_worker_lost` with `acks_late` means an OOM-killed task is retried; make sure that task doesn't OOM every time, or it loops forever.

### Idempotent tasks

At-least-once means "design for duplicates". Patterns:

```python
@shared_task(bind=True, acks_late=True)
def send_invoice_email(self, invoice_id: int) -> None:
    # 1. Atomic state transition as a guard
    claimed = (Invoice.objects
               .filter(pk=invoice_id, email_sent_at__isnull=True)
               .update(email_sent_at=timezone.now()))
    if not claimed:
        return                                   # already done by another run
    try:
        mailer.send(invoice_id, idempotency_key=f"invoice-email-{invoice_id}")
    except Exception:
        Invoice.objects.filter(pk=invoice_id).update(email_sent_at=None)
        raise
```

- **Pass IDs, not objects.** Reload state inside the task; the row may have changed since enqueue.
- **Conditional updates** (`filter(status="pending").update(...)`) or unique constraints as guards.
- **Idempotency keys** for external APIs that support them (Stripe, most payment providers).
- **Upserts** (`update_or_create`, `bulk_create(update_conflicts=True)`) instead of blind inserts.

### Retries with backoff

```python
import httpx

@shared_task(
    bind=True,
    autoretry_for=(httpx.TransportError, httpx.HTTPStatusError),
    retry_backoff=2,          # 2, 4, 8, 16... seconds
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=8,
    acks_late=True,
)
def sync_order_to_erp(self, order_id: int) -> None:
    order = Order.objects.select_related("tenant").get(pk=order_id)
    resp = erp_client(order.tenant).post("/orders", json=serialize(order))
    if resp.status_code == 400:
        mark_sync_failed(order_id, resp.text)    # permanent, don't retry
        return
    resp.raise_for_status()
```

Retry only transient failures (timeouts, connection errors, 429, 5xx). Validation errors are permanent; retrying them wastes capacity and spams the upstream. Jitter avoids a thundering herd when an upstream recovers. After `max_retries`, the task fails with `MaxRetriesExceededError`: log it, alert, or write to a dead-letter table.

### Enqueue after commit

```python
transaction.on_commit(lambda: sync_order_to_erp.delay(order.pk))
# Celery 5.4+: sync_order_to_erp.delay_on_commit(order.pk)
```

Otherwise the worker can run before the row exists. See [Transactions in Django]({{ '/python-django/django-transactions/' | relative_url }}).

### Prefetch multiplier

Each worker process reserves `prefetch_multiplier` messages (default 4) per slot. With long tasks, one worker hoards messages that idle workers could run. With `acks_late` and multiplier 1, each process holds only the task it's running. For many tiny tasks (sub-100 ms), a higher multiplier improves throughput.

### Routing and isolation

```python
CELERY_TASK_ROUTES = {
    "billing.tasks.*": {"queue": "billing"},
    "reports.tasks.generate_*": {"queue": "reports"},
    "webhooks.tasks.*": {"queue": "webhooks"},
}
```

```bash
celery -A config worker -Q billing -c 4 --max-tasks-per-child 500
celery -A config worker -Q reports -c 2 --max-memory-per-child 1048576
celery -A config worker -Q webhooks -P threads -c 50
```

Separate queues stop a 20-minute report backlog from delaying password-reset emails. In multi-tenant systems, also guard against one tenant flooding a queue: per-tenant rate limits, chunked fan-out, or a separate queue for bulk imports.

### Beat

Celery beat is a scheduler that enqueues tasks on a schedule. Run **exactly one** beat process; two beats double every periodic task. With `django-celery-beat`, schedules live in the database and can be edited in admin.

Periodic tasks should be idempotent and safe to overlap. A job that runs every 5 minutes but sometimes takes 7 needs a lock:

```python
from django.core.cache import cache

@shared_task
def rollup_usage():
    if not cache.add("lock:rollup_usage", "1", timeout=600):
        return
    try:
        do_rollup()
    finally:
        cache.delete("lock:rollup_usage")
```

### Monitoring

| Signal | Source | Alert when |
|---|---|---|
| Queue depth per queue | Broker (`LLEN` in Redis, RabbitMQ management) | Growing for > N minutes |
| Task latency (enqueue to start) | `task-sent` vs `task-started` events, or a timestamp argument | p95 above SLO |
| Failure and retry rate | Flower, Prometheus exporter, Sentry | Spike per task name |
| Worker liveness | `celery inspect ping`, heartbeats | Missing workers |
| Memory per worker | Container metrics | Approaching limit |

Sentry's Celery integration captures exceptions with task name and args, and can monitor beat schedules (cron monitors) to catch "the nightly job silently stopped".

## Pitfalls

- **Default early ack** loses tasks on deploys and OOM kills. Turn on `acks_late` and make tasks idempotent.
- **Visibility timeout shorter than tasks** on Redis: duplicate concurrent runs. Symptom: same task ID started twice in logs.
- **Large arguments.** Passing serialized querysets or files bloats the broker. Pass IDs and S3 keys.
- **Chords and results** require a result backend and are fragile under retries; prefer explicit state in your database.
- **`task_always_eager` in tests** hides serialization and transaction issues. Call the task function directly in unit tests instead.
- **DB connections.** Each worker process holds connections; long-idle workers hit server timeouts. Celery's Django fixup closes stale connections per task; still budget `concurrency * workers` against `max_connections`.
- **Time limits** don't work on the `threads` or `gevent` pools the same way as prefork; hard limits need prefork.

## Checklist

- `acks_late=True`, `reject_on_worker_lost=True`, idempotent tasks.
- `visibility_timeout` above the longest task and ETA (Redis/SQS).
- `prefetch_multiplier=1` for long tasks.
- Retries only for transient errors, with backoff, jitter and a cap.
- Enqueue with `on_commit` / `delay_on_commit`.
- Separate queues per workload; one beat; locks on periodic jobs.
- Alert on queue depth, latency and failure rate.

## Interview angle

**What delivery guarantee does Celery give?** At-most-once with early ack (default), at-least-once with `acks_late`. Exactly-once processing needs idempotent tasks.

**How do you make a task idempotent?** Guard with an atomic conditional update or unique constraint, pass IDs and reload, use idempotency keys with external APIs.

**Why did my task run twice?** Late ack plus worker restart, a Redis visibility timeout shorter than the task, two beat instances, or a retry after a side effect already happened.

**Why does one worker sit busy while others idle?** Prefetching reserved messages behind a long task. Set `prefetch_multiplier=1` with `acks_late`.
