---
title: Decorators and context managers
section: python-django
order: 4
summary: How decorators and with-blocks work underneath, how to write correct ones, and where they show up in Django, DRF and Celery.
---

Half of Django's cross-cutting behavior is a decorator or a context manager: `@transaction.atomic`, `@login_required`, `@shared_task`, `@cache_page`, `override_settings`, `assertNumQueries`. Writing your own is how you add tenant scoping, timing, retries or locking without copying the same `try/finally` into fifty functions. Getting them subtly wrong (lost metadata, swallowed exceptions, state leaking across requests) is also a common source of production bugs.

## How it works

### A decorator is a function applied at definition time

```python
@timed
def sync_invoices(tenant_id): ...

# is exactly
def sync_invoices(tenant_id): ...
sync_invoices = timed(sync_invoices)
```

It runs **once, at import**, and whatever it returns replaces the name. Usually that's a wrapper closure that calls the original.

### Decorators with arguments are factories

`@retry(times=3)` first calls `retry(times=3)`, which returns the real decorator. Three levels of nesting: factory, decorator, wrapper.

### Context managers are the with-protocol

```python
with mgr as value:
    body
```

roughly runs:

```python
value = mgr.__enter__()
try:
    body
except BaseException as exc:
    if not mgr.__exit__(type(exc), exc, exc.__traceback__):
        raise
else:
    mgr.__exit__(None, None, None)
```

If `__exit__` returns a truthy value, the exception is **swallowed**. That's how `contextlib.suppress` works and how buggy managers hide errors.

### contextlib does the plumbing

- `@contextmanager` turns a generator with one `yield` into a context manager. Code before `yield` is `__enter__`, after it is `__exit__`.
- `ContextDecorator` lets one object work as both `with x:` and `@x`. `transaction.atomic` is built this way.
- `ExitStack` manages a dynamic number of contexts.
- `@asynccontextmanager` and `AsyncExitStack` for `async with`.

## In practice

### A correct decorator

```python
import functools
import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

def timed[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            ms = (time.perf_counter() - start) * 1000
            log.info("%s took %.1f ms", func.__qualname__, ms)
    return wrapper
```

- `functools.wraps` copies `__name__`, `__qualname__`, `__doc__`, `__module__` and sets `__wrapped__`. Without it, Celery task names, DRF schema generation, Sentry stack grouping and `inspect.signature` all see `wrapper`.
- `try/finally` records time even when the function raises.
- The PEP 695 `[**P, R]` syntax (Python 3.12) keeps the signature visible to type checkers.

### A decorator with arguments

```python
def retry(times: int = 3, on: tuple[type[Exception], ...] = (ConnectionError,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except on:
                    if attempt == times:
                        raise
                    time.sleep(2 ** attempt * 0.1)
        return wrapper
    return decorator

@retry(times=4, on=(TimeoutError, ConnectionError))
def push_invoice_to_erp(invoice_id): ...
```

Retry only errors you know are transient, and only around idempotent operations. In Celery, use the task's own `autoretry_for` instead (see [Celery in production]({{ '/python-django/celery-in-production/' | relative_url }})).

### Decorating class-based views

Function decorators expect `(request, ...)`, but methods receive `self` first. Use `method_decorator`:

```python
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.views import View

@method_decorator(login_required, name="dispatch")
class InvoiceExportView(View):
    def get(self, request): ...
```

### A tenant-scoping context manager

Use `contextvars` so the value is per-thread and per-asyncio-task, not global:

```python
from contextlib import contextmanager
from contextvars import ContextVar

current_tenant: ContextVar[int | None] = ContextVar("current_tenant", default=None)

@contextmanager
def tenant_context(tenant_id: int):
    token = current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        current_tenant.reset(token)

with tenant_context(order.tenant_id):
    recalculate_totals(order)
```

The `try/finally` around `yield` is mandatory. Without it, an exception in the body skips the reset and the next task on that worker thread runs under the wrong tenant.

### Django's built-in ones

```python
from django.db import transaction
from django.test import override_settings

@transaction.atomic
def close_invoice(invoice_id): ...

with transaction.atomic():
    ...

@override_settings(FEATURE_FLAGS={"new_billing": True})
def test_billing(): ...
```

`atomic`, `override_settings` and `assertNumQueries` all work as both decorator and context manager. Prefer the context-manager form when only part of a function should be covered.

### Dynamic contexts with ExitStack

```python
from contextlib import ExitStack

with ExitStack() as stack:
    files = [stack.enter_context(open(p)) for p in report_paths]
    merge_reports(files)
# all files closed, in reverse order, even if one open() failed
```

## Pitfalls

- **Missing `functools.wraps`.** Celery registers tasks by `__name__`/module; two tasks wrapped by the same decorator can collide. DRF and OpenAPI tools lose docstrings.
- **Decorator order.** Decorators apply bottom-up. `@shared_task` on top of `@transaction.atomic` makes the task body atomic; the reverse wraps the task object, which is not what you want. With `@cache_page` outermost, a cache hit skips every decorator below it, so per-request checks (permissions, rate limits, audit logging) don't run on hits.
- **Import-time side effects.** Code in the decorator body (not the wrapper) runs on import. Don't hit the database or read settings that tests override there.
- **`@lru_cache` / `@cache` on methods.** The cache holds `self` and never releases instances. It also caches across tenants if the key doesn't include the tenant.
- **Swallowing exceptions.** An `__exit__` that returns `True`, or a `@contextmanager` with `except Exception: pass` around `yield`, silently hides failures. Only suppress specific exceptions on purpose.
- **Using `threading.local` or globals for request state.** Leaks across asyncio tasks and between requests if not reset. Use `ContextVar` with `reset(token)`.
- **Catching `IntegrityError` inside `atomic`.** The transaction is broken after a DB error; wrap the risky statement in its own nested `atomic()` (a savepoint).

## Checklist

- Always `functools.wraps`.
- Always `try/finally` around `yield` in `@contextmanager`.
- Type decorators with `ParamSpec` so callers keep their signatures.
- Use `method_decorator` for class-based views.
- Use `ContextVar` for per-request or per-task state, reset with the token.
- Read decorator stacks bottom-up when debugging.

## Interview angle

**What does `@decorator` actually do?** It calls `decorator(func)` once at definition time and rebinds the name to the result.

**Why `functools.wraps`?** It copies the wrapped function's metadata and sets `__wrapped__`, so introspection, logging, frameworks and type tools see the real function.

**How does a context manager suppress an exception?** `__exit__` returns a truthy value. Otherwise the exception propagates after cleanup.

**How would you write something usable as both `@x` and `with x:`?** Subclass `contextlib.ContextDecorator` (or use `@contextmanager`, whose result also works as a decorator), like `transaction.atomic`.
