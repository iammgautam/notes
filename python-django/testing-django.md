---
title: Testing Django apps
section: python-django
order: 15
summary: pytest-django setup, factories, TestCase vs transactional tests, query-count assertions, on_commit and Celery in tests, and mocking external APIs cleanly.
---

A good Django test suite catches the bugs that cost the most in a multi-tenant SaaS: one tenant seeing another's data, an endpoint regressing from 5 queries to 500, a task enqueued before commit, a payment webhook handled twice. It also has to stay fast enough (a few minutes for thousands of tests) that people run it. Most of that comes down to how you use database transactions in tests, factories, and where you mock.

## How it works

### Test database and isolation

The test runner creates a separate database (`test_<name>`), runs migrations once, and isolates tests in one of two ways:

| | `TestCase` / `@pytest.mark.django_db` | `TransactionTestCase` / `django_db(transaction=True)` |
|---|---|---|
| Isolation | Each test in a transaction, rolled back at the end | Tables truncated after each test |
| Speed | Fast (ms per test) | Slow (truncate every table) |
| `on_commit` callbacks | Never run (no real commit) unless captured | Run normally |
| `select_for_update`, concurrency | Single connection, can't test real locking | Can test with multiple connections/threads |

Default to the transactional-rollback flavor. Use `transaction=True` only for tests that need real commits: concurrency, `on_commit` integration, raw connection behavior.

`setUpTestData` (or class-scoped fixtures) creates data once per class inside an outer transaction, with each test rolling back to a savepoint. Big speedup for read-heavy test classes.

### pytest-django

```toml
# pyproject.toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.test"
addopts = "--reuse-db"
```

Key fixtures:

- `db` / `@pytest.mark.django_db`: database access.
- `client`, `admin_client`, `rf` (RequestFactory).
- `settings`: modify settings for one test, auto-restored.
- `django_assert_num_queries`, `django_assert_max_num_queries`.
- `django_capture_on_commit_callbacks`.

Flags: `--reuse-db` keeps the test DB between runs, `--create-db` forces recreation, `-n auto` with `pytest-xdist` runs in parallel (each worker gets its own database).

### Test settings

```python
# config/settings/test.py
from .base import *

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}
CELERY_TASK_ALWAYS_EAGER = False
```

The default PBKDF2 hasher makes every `create_user` take tens of milliseconds; MD5 in tests removes that.

## In practice

### Factories over fixtures

`factory_boy` builds valid objects with sensible defaults and lets each test override only what matters.

```python
import factory
from factory.django import DjangoModelFactory

class TenantFactory(DjangoModelFactory):
    class Meta:
        model = Tenant
    name = factory.Sequence(lambda n: f"Tenant {n}")
    slug = factory.LazyAttribute(lambda o: o.name.lower().replace(" ", "-"))

class CustomerFactory(DjangoModelFactory):
    class Meta:
        model = Customer
    tenant = factory.SubFactory(TenantFactory)
    email = factory.Faker("email")

class InvoiceFactory(DjangoModelFactory):
    class Meta:
        model = Invoice
    customer = factory.SubFactory(CustomerFactory)
    tenant = factory.SelfAttribute("customer.tenant")   # keep tenants consistent
    amount = factory.Faker("pydecimal", left_digits=4, right_digits=2, positive=True)
    status = "open"
```

- `InvoiceFactory()` saves to the DB; `InvoiceFactory.build()` doesn't (use it for pure logic tests).
- `create_batch(20, ...)` for list endpoints.
- `SelfAttribute` keeps related objects in the same tenant, avoiding cross-tenant data that hides scoping bugs.

### Tenant isolation tests

```python
import pytest

@pytest.mark.django_db
def test_invoice_list_is_tenant_scoped(api_client_for):
    a, b = TenantFactory(), TenantFactory()
    InvoiceFactory.create_batch(3, customer__tenant=a)
    InvoiceFactory.create_batch(2, customer__tenant=b)

    resp = api_client_for(tenant=a).get("/api/invoices/")

    assert resp.status_code == 200
    assert {row["tenant_id"] for row in resp.json()["results"]} == {a.id}
    assert len(resp.json()["results"]) == 3
```

Write one of these for every list and detail endpoint, including a test that fetching tenant B's object by ID from tenant A returns 404.

### Query-count tests

```python
@pytest.mark.django_db
def test_invoice_list_queries(api_client_for, django_assert_num_queries):
    t = TenantFactory()
    InvoiceFactory.create_batch(25, customer__tenant=t)
    client = api_client_for(tenant=t)
    with django_assert_num_queries(3):
        client.get("/api/invoices/")
```

Use more than one row so an N+1 changes the count. When the assertion fails, pytest-django prints the captured SQL. `django_assert_max_num_queries` is less brittle when unrelated queries (auth, feature flags) vary.

### on_commit and Celery

```python
@pytest.mark.django_db
def test_invoice_creation_enqueues_pdf(django_capture_on_commit_callbacks, mocker):
    delay = mocker.patch("billing.tasks.render_invoice_pdf.delay")
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        invoice = create_invoice(OrderFactory())
    assert len(callbacks) == 1
    delay.assert_called_once_with(invoice.pk)
```

For the task itself, call the function directly, not through the broker:

```python
@pytest.mark.django_db
def test_render_invoice_pdf_is_idempotent(mocker):
    upload = mocker.patch("billing.tasks.storage.upload")
    inv = InvoiceFactory()
    render_invoice_pdf(inv.pk)
    render_invoice_pdf(inv.pk)
    assert upload.call_count == 1
```

Avoid `CELERY_TASK_ALWAYS_EAGER=True` globally: it runs tasks synchronously inside the caller's transaction, skips serialization (so passing a model instance "works"), and hides `on_commit` bugs.

### Mocking external APIs

Mock at the boundary, where your code calls out, not deep inside libraries.

```python
import httpx
import respx

@respx.mock
def test_erp_sync_retries_on_503():
    route = respx.post("https://erp.example.com/orders").mock(
        side_effect=[httpx.Response(503), httpx.Response(201, json={"id": "E-1"})]
    )
    result = push_order(order_payload())
    assert result == "E-1"
    assert route.call_count == 2
```

- `respx` for `httpx`, `responses` for `requests`. Both fail the test on unexpected calls, which catches accidental real network access.
- `unittest.mock.patch` / `mocker.patch` must target where the name is **looked up**: `patch("billing.services.stripe_client")`, not `patch("stripe_client_module.stripe_client")`, if `billing.services` imported it by name.
- Prefer injecting a fake (a class matching a `Protocol`) for your own adapters; it's clearer than patching.
- Freeze time with `time-machine` or `freezegun` for due-date and expiry logic.

## Pitfalls

- **Testing with one row.** Hides N+1 and ordering bugs.
- **Factories that create cross-tenant graphs.** A `SubFactory` creating a fresh tenant for every related object means tenant-scoping bugs never surface. Derive tenant from the parent.
- **`transaction=True` everywhere.** Suite time balloons. Use it only where real commits matter.
- **Shared state between tests.** Module-level caches, `lru_cache`, and the real cache backend persist across tests. Clear the cache in an autouse fixture.
- **Mocking too much.** Tests that mock the ORM pass while the real query is broken. Use the test database; mock only network and time.
- **Order-dependent tests.** Install `pytest-randomly` to shuffle test order and flush them out.
- **Slow migrations on every run.** Use `--reuse-db`; squash old migrations if creating the DB takes minutes.

## Checklist

- pytest-django with `--reuse-db` and `-n auto`.
- Fast password hasher and locmem email in test settings.
- Factories with tenant-consistent `SubFactory`/`SelfAttribute`.
- A tenant-isolation test and a query-count test per endpoint.
- `django_capture_on_commit_callbacks` for on-commit side effects; call task functions directly.
- `respx`/`responses` for HTTP; patch where names are looked up.

## Interview angle

**TestCase vs TransactionTestCase?** TestCase wraps each test in a rolled-back transaction (fast, but no real commits). TransactionTestCase truncates tables (slow), needed when you want real commits: multi-connection, locking or end-to-end `on_commit` tests.

**How do you test that a Celery task is enqueued only after commit?** Patch `.delay`, wrap the action in `captureOnCommitCallbacks(execute=True)`, and assert the call.

**How do you prevent N+1 regressions?** `assertNumQueries` / `django_assert_num_queries` on list endpoints with multiple rows.

**Where do you patch?** Where the name is looked up at call time, which is the importing module, not the defining module.
