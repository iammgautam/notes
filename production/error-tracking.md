---
title: Error tracking with Sentry
section: production
order: 3
summary: How Sentry groups errors, what context to attach, release and performance tracking, keeping noise down, and wiring it into Celery.
---

Logs tell you an exception happened. An error tracker tells you it is a *new* exception, first seen 12 minutes after the last deploy, hitting 340 users across 9 tenants, with the stack trace, local variables and the request that triggered it. That turns "something is off" into a ticket with a cause.

## How it works

The SDK hooks into the framework (Django middleware, logging, Celery signals) and captures unhandled exceptions plus `logger.error` calls. Each capture becomes an **event** with:

- Stack trace with source context and, optionally, local variables per frame.
- **Tags**: indexed key/values you can search and facet on (`tenant`, `release`, `environment`, `transaction`).
- **Contexts**: structured extra data (runtime, OS, custom blocks).
- **Breadcrumbs**: the trail before the error (SQL queries, HTTP calls, log lines).
- **User**: an ID, so you can count affected users.

### Grouping

Events are grouped into **issues** by a fingerprint. By default the fingerprint comes from the stack trace (in-app frames, function names, normalized) and, without a stack trace, from the message. Consequences:

- Same bug from two call paths can become two issues.
- Different bugs that raise the same exception from one generic helper (`api_client.request`) can collapse into one issue.
- Dynamic values in messages (`"Invoice 8812 not found"`) break message-based grouping into thousands of issues.

You can override the fingerprint in code or with server-side fingerprint rules.

### Releases

When every event carries a `release` (the git SHA or version), Sentry can tell you which release introduced an issue, flag **regressions** (resolved issue that comes back in a newer release) and show crash-free session/user rates per release. Associating commits lets it suggest the suspect commit.

### Performance

With tracing enabled, Sentry records **transactions** (one per request or task) with spans for DB queries, HTTP calls and cache. It surfaces slow endpoints, N+1 query patterns and slow DB spans. It overlaps with a full tracing stack; many teams use Sentry for errors plus a sampled slice of performance data.

## In practice

### Django setup

```python
import os
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
import logging

IGNORED = {"ClientDisconnected", "Http404", "PermissionDenied"}

def before_send(event, hint):
    exc = hint.get("exc_info")
    if exc and exc[0].__name__ in IGNORED:
        return None
    return event

sentry_sdk.init(
    dsn=os.environ["SENTRY_DSN"],
    environment=os.environ.get("ENV", "dev"),
    release=os.environ.get("GIT_SHA"),
    integrations=[
        DjangoIntegration(),
        CeleryIntegration(monitor_beat_tasks=True),
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ],
    traces_sample_rate=0.05,
    send_default_pii=False,
    before_send=before_send,
)
```

- `release` comes from the build (bake `GIT_SHA` into the image).
- `send_default_pii=False` keeps IPs, cookies and request bodies out by default.
- `traces_sample_rate` controls performance data volume. 5% on a busy API is usually plenty. Use `traces_sampler` to sample hot health checks at 0 and important endpoints higher.

### Context that makes an event actionable

```python
class SentryContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant:
            sentry_sdk.set_tag("tenant", tenant.slug)
            sentry_sdk.set_tag("plan", tenant.plan)
        if request.user.is_authenticated:
            sentry_sdk.set_user({"id": request.user.id})
        sentry_sdk.set_tag("request_id", request.headers.get("X-Request-ID"))
        return self.get_response(request)
```

Now you can answer "is this one tenant or everyone?" with the tag facet, and jump from the event to logs via `request_id`.

For domain-specific context:

```python
with sentry_sdk.new_scope() as scope:   # SDK 2.x; push_scope() in 1.x
    scope.set_context("llm", {"provider": "openai", "model": model,
                              "prompt_tokens": usage.prompt_tokens})
    scope.set_tag("llm.provider", "openai")
    sentry_sdk.capture_exception(exc)
```

### Custom fingerprints

Group all timeouts from one upstream together regardless of call site:

```python
def before_send(event, hint):
    exc = hint.get("exc_info")
    if exc and isinstance(exc[1], UpstreamTimeout):
        event["fingerprint"] = ["upstream-timeout", exc[1].service]
    return event
```

### Celery and background workers

`CeleryIntegration` captures task exceptions with the task name as the transaction, and with `monitor_beat_tasks=True` it creates cron monitors so you get alerted when a scheduled task **doesn't run**, which is a failure mode exceptions never catch.

Watch retries: a task with `autoretry_for` that fails 5 times creates 5 events by default. The integration skips events for `Retry` exceptions, but your own retry loop in a homegrown executor won't. Capture on the final attempt only, and tag `attempt`:

```python
def execute(task):
    try:
        run_handler(task)
    except Exception as exc:
        if task.attempts >= task.max_attempts:
            sentry_sdk.set_tag("task", task.name)
            sentry_sdk.set_tag("attempt", task.attempts)
            sentry_sdk.capture_exception(exc)
        schedule_retry_or_fail(task, exc)
```

For long-running workers that don't go through an integration, wrap each unit of work in a fresh scope so tags from task A don't leak into task B.

## Pitfalls

- **Noise kills attention.** If the inbox has 2,000 unresolved issues, nobody looks at issue 2,001, which is the real one. Drop expected errors (`404`, client disconnects, validation errors) in `before_send` or with inbound filters.
- **Logging expected failures at `ERROR`.** A declined card is a business outcome, not a bug. Log it at `INFO`/`WARNING` so it doesn't become a Sentry event.
- **Quota burn.** One loop raising per row can send 100k events in minutes and exhaust the monthly quota. Use rate limits per project and spike protection.
- **Missing release.** Without it you lose regression detection and the "since deploy" signal.
- **PII in local variables.** Frame locals can include tokens and personal data. Keep `send_default_pii=False`, configure server-side data scrubbing, and add field names to the denylist.
- **Grouping by message** with IDs in the string. Pass IDs as extra data, not in the message.

## Noise control workflow

- Triage daily: **resolve** (fixed, auto-reopens on regression), **ignore until** (N occurrences or N users), **assign** an owner.
- Alert on **new issues** and **regressions** in production, not on every event.
- Alert on frequency only for known-critical paths ("payment errors > 10 in 5 min").
- Route by ownership rules (path or tag → team).

## Rules of thumb

- Set `release`, `environment`, tenant tag and user ID on every event.
- Errors you expect are not exceptions to report.
- Capture once per final failure, not per retry.
- Use cron monitors for scheduled jobs.
- Keep the unresolved list short enough that a new issue is noticed the same day.

## Interview angle

- **"How do you find production errors before users report them?"** An error tracker with alerts on new issues and regressions tied to releases, plus tags so you can tell scope (one tenant vs all) immediately.
- **"How do you avoid alert fatigue in Sentry?"** Filter expected errors at the source, fix grouping, alert on new/regressed issues instead of event counts, and triage regularly.

Related: [Structured logging]({{ '/production/structured-logging/' | relative_url }}), [Debugging production issues]({{ '/production/debugging-production/' | relative_url }}).
