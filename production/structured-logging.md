---
title: Structured logging
section: production
order: 2
summary: Emit JSON logs with request, tenant and correlation IDs from Django and workers, pick levels that mean something, and keep PII out.
---

A plain-text log line like `Payment failed for invoice 8812` is readable by a human and useless to a machine. When you have 40 pods, three worker pools and 3,000 tenants, you need to ask "show me every error for tenant 512 in the last hour, grouped by endpoint". That only works if each log line is a structured event with consistent fields.

## How it works

A structured log is one JSON object per line on stdout:

```json
{"ts": "2026-03-04T10:12:45.812Z", "level": "error", "event": "payment.failed",
 "service": "billing-api", "env": "prod", "request_id": "c1f0a9e2",
 "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "tenant_id": 512,
 "user_id": 9021, "invoice_id": 8812, "provider": "stripe",
 "error": "card_declined", "duration_ms": 412}
```

The platform (Docker/Kubernetes) captures stdout, a shipper (Fluent Bit, Vector, the Datadog agent) parses the JSON and indexes the fields. You query fields, not regexes.

Three kinds of fields:

- **Static context**: `service`, `env`, `version` (git SHA). Set once at startup.
- **Request-scoped context**: `request_id`, `trace_id`, `tenant_id`, `user_id`. Set at the start of a request or task, attached to every line it emits.
- **Event fields**: `invoice_id`, `provider`, `duration_ms`. Specific to the call site.

The trick is getting request-scoped context onto every line without passing it through every function. In Python that means `contextvars`, which are safe across threads and asyncio tasks.

## In practice

### structlog with Django

```python
# settings.py
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
```

Bind request context in middleware. Accept an incoming `X-Request-ID` from the load balancer or upstream service, otherwise mint one, and echo it back in the response so clients can quote it in support tickets.

```python
import uuid
import structlog
from opentelemetry import trace

class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        ctx = trace.get_current_span().get_span_context()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            trace_id=format(ctx.trace_id, "032x") if ctx.is_valid else None,
            tenant_id=getattr(getattr(request, "tenant", None), "id", None),
            path=request.path,
            method=request.method,
        )
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response
```

Then at the call site, log an **event name** plus fields, not a sentence:

```python
log = structlog.get_logger()

def charge(invoice):
    log.info("payment.attempt", invoice_id=invoice.id, amount=invoice.total)
    try:
        provider.charge(invoice)
    except ProviderError as exc:
        log.error("payment.failed", invoice_id=invoice.id, error=exc.code)
        raise
```

### Carrying the correlation ID into background work

The request ID must survive the hop into a queue, otherwise "what happened to this upload" stops at `enqueued`.

```python
# enqueue
enqueue_task("reports.build", payload={"report_id": rid},
             meta={"request_id": request_id, "tenant_id": tenant.id})

# worker
def execute(task):
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        task_id=task.id, task_name=task.name, attempt=task.attempts,
        **task.meta,
    )
    ...
```

With Celery, do the same with `before_task_publish` (write headers) and `task_prerun` (bind them). Always clear contextvars at the start of each unit of work; worker processes are reused and stale context is worse than none.

### Levels that mean something

| Level | Use for | Who reads it |
|---|---|---|
| `DEBUG` | Internal state while developing. Off in prod by default. | You, locally |
| `INFO` | Business events and lifecycle: request done, task started/finished, flag toggled. | Debugging, audits |
| `WARNING` | Unexpected but handled: retry scheduled, fallback model used, slow query. | Trend dashboards |
| `ERROR` | The operation failed and the user or job saw it. | Sentry, alerts |
| `CRITICAL` | The process can't continue (bad config, can't reach DB at boot). | Pager |

> **Rule of thumb:** If an `ERROR` doesn't need anyone to look at it, it is a `WARNING`. If a `WARNING` fires 10,000 times a day and nobody cares, it is `INFO` or deleted.

One access-log line per request at `INFO` with `status`, `duration_ms`, `db_queries`, `db_ms` is worth more than twenty scattered lines. Make it the canonical request log.

### Keeping PII out

Logs are copied to more places, kept longer and seen by more people than your database. Treat them as a lower-trust store.

- Log IDs, not values: `user_id=9021`, not the email. `invoice_id`, not the card number.
- Never log request bodies, `Authorization` headers, cookies, tokens, passwords or LLM prompts containing customer data by default.
- Add a redaction processor as a safety net:

```python
SENSITIVE = {"password", "token", "authorization", "email", "phone", "api_key"}

def redact(_, __, event_dict):
    for key in list(event_dict):
        if key.lower() in SENSITIVE:
            event_dict[key] = "[redacted]"
    return event_dict
```

- Set retention per index (for example 14 days for app logs, longer for audit logs) and make sure deletion requests cover logs if your compliance regime requires it.

## Pitfalls

- **Interpolated messages.** `log.info(f"user {uid} paid {amt}")` creates a unique message per line. You can't group or count it. Use a fixed event name and fields.
- **Inconsistent field names.** `tenant`, `tenantId`, `org_id` across services means three queries for one question. Agree on a schema and stick to it.
- **Multi-line tracebacks** in plain-text logs get split into separate events by the shipper. Render exceptions inside the JSON (`format_exc_info`).
- **Logging in hot loops.** A log line per row in a 2M-row backfill floods the pipeline and can cost more than the job. Log progress every N rows.
- **Leaked context** across tasks in reused worker processes because contextvars weren't cleared.
- **Volume blowups.** One chatty `INFO` in a hot path can multiply ingest cost. Watch bytes/day per service and sample noisy debug events.

## Rules of thumb

- JSON to stdout, one event per line, fixed event names.
- Every line has `service`, `env`, `version`, `request_id`, `trace_id`, `tenant_id` when available.
- Propagate the correlation ID through queues and outbound HTTP calls.
- Return `X-Request-ID` to clients.
- IDs in logs, never secrets or personal data.
- Levels reflect who needs to act.

## Interview angle

- **"How do you trace a request across services?"** Generate or accept a request ID at the edge, bind it into context for every log line, forward it on outbound calls and task metadata, and include the trace ID so logs link to traces.
- **"Why structured logs?"** They are queryable by field, aggregate cleanly and survive pipeline parsing; text logs need fragile regexes.

Related: [Observability]({{ '/production/observability/' | relative_url }}), [Error tracking with Sentry]({{ '/production/error-tracking/' | relative_url }}).
