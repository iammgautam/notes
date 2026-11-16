---
title: Dependency injection in FastAPI
section: ai-stack
order: 3
phase: "Phase 1 · FastAPI"
summary: Use Depends for settings, DB sessions, auth, roles and tenant context, get cleanup right with yield and scope, and swap any dependency in tests.
---

After this note you can lay out a FastAPI service around dependencies: one place for settings, a database session that commits, rolls back and closes correctly, plus the current user, the tenant, role checks and test doubles. On a finance data platform, dependencies are where access control and tenant isolation live, which is what the job description means by "security and access control in regulated environments".

## Prerequisites

[FastAPI fundamentals]({{ '/ai-stack/fastapi-fundamentals/' | relative_url }}): routers, parameters, Pydantic models and `TestClient`.

## The mental model

A dependency is any callable. FastAPI resolves its parameters exactly like an endpoint's: query params, headers, body fields, or other dependencies. At startup FastAPI turns each endpoint and its dependencies into a graph. For each request it walks that graph from the leaves, calls each node at most once, and injects the results. After the endpoint returns, it unwinds any `yield` dependencies in reverse order.

```text
GET /v1/invoices   Authorization: Bearer ..., X-Tenant-ID: acme
  |
  |  FastAPI walks the graph leaves-first, each node once
  v
  HTTPBearer -> get_current_user -> get_tenant --+--> audit (router)
                                                 +--> InvoiceRepo
  get_settings -> pagination                     |
       |                                         |
       +-------------> endpoint(repo, page) <----+
                              |
                              v
              response, then yield cleanup (LIFO)
```

This replaces implicit state such as `request.user` set by middleware or thread-locals with typed parameters you can see in the signature. Each one shows up in OpenAPI and can be replaced in a test.

## Depends, aliases and sub-dependencies

```python
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

calls = {"settings": 0, "audit": 0}


@dataclass
class Settings:
    max_page_size: int = 100
    default_currency: str = "USD"


def get_settings() -> Settings:
    calls["settings"] += 1
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass
class Page:
    limit: int
    offset: int


def pagination(settings: SettingsDep, limit: int = 20, offset: int = 0) -> Page:
    # a sub-dependency: needs settings, and reads query params itself
    return Page(limit=min(limit, settings.max_page_size), offset=max(offset, 0))


PageDep = Annotated[Page, Depends(pagination)]


def audit_counter() -> int:
    calls["audit"] += 1
    return calls["audit"]


app = FastAPI()


@app.get("/transactions")
def list_transactions(
    page: PageDep,
    settings: SettingsDep,                       # second use: cached
    a: Annotated[int, Depends(audit_counter, use_cache=False)],
    b: Annotated[int, Depends(audit_counter, use_cache=False)],
) -> dict:
    return {"limit": page.limit, "offset": page.offset,
            "currency": settings.default_currency, "audit_calls": [a, b]}


if __name__ == "__main__":
    client = TestClient(app)
    r = client.get("/transactions?limit=500&offset=40")
    assert r.json() == {"limit": 100, "offset": 40, "currency": "USD",
                        "audit_calls": [1, 2]}
    assert calls["settings"] == 1       # once per request, not twice
    client.get("/transactions")
    assert calls["settings"] == 2       # the cache does not span requests
    params = {p["name"] for p in
              client.get("/openapi.json").json()["paths"]["/transactions"]["get"]["parameters"]}
    assert params == {"limit", "offset"}  # dependency params reach the docs
    print("ok")
```

- **Aliases.** `SettingsDep = Annotated[Settings, Depends(get_settings)]` is the standard idiom. Define the aliases once in `deps.py` and import them everywhere, so endpoints stay short and every endpoint gets the dependency the same way.
- **Sub-dependencies.** `pagination` needs `Settings` and reads `limit` and `offset` from the query string. Those params show up in `/docs` as if the endpoint had declared them.
- **Per-request caching.** `get_settings` runs once even though two consumers ask for it. The cache key is the callable, so `use_cache=False` forces a fresh call. The cache dies with the request. For process-wide singletons, put `@lru_cache` on `get_settings` (the usual pattern with pydantic-settings) or create the object in the lifespan, as covered in [Async and performance in FastAPI]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}).

## Yield dependencies and resource lifecycle

A dependency that `yield`s works like a context manager. The code before `yield` is setup, the yielded value is injected, and the code after `yield` is teardown. If the endpoint raises, the exception is thrown into the generator at the `yield`, so `try/except/finally` behaves exactly as it would around a `with` block.

```python
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

events: list[str] = []


class FakeSession:
    """Stands in for a SQLAlchemy Session; records what happens to it."""
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("deadlock detected")
        events.append("commit")

    def rollback(self) -> None:
        events.append("rollback")

    def close(self) -> None:
        events.append("close")


def get_tenant():
    events.append("tenant:enter")
    try:
        yield "acme"
    finally:                            # without finally, errors skip cleanup
        events.append("tenant:exit")


def get_db(tenant: Annotated[str, Depends(get_tenant)]):
    db = FakeSession()
    events.append(f"db:open({tenant})")
    try:
        yield db
        db.commit()                     # only reached if the endpoint succeeded
    except Exception:
        db.rollback()
        raise                           # always re-raise
    finally:
        db.close()


app = FastAPI()


@app.post("/journal-entries")
def post_entry(db: Annotated[FakeSession, Depends(get_db)], fail: bool = False,
               fail_commit: bool = False) -> dict:
    events.append("endpoint")
    db.fail_commit = fail_commit
    if fail:
        raise HTTPException(400, "unbalanced entry")
    return {"status": "posted"}


@app.post("/journal-entries-safe")
def post_entry_safe(
    db: Annotated[FakeSession, Depends(get_db, scope="function")],
    fail_commit: bool = False,
) -> dict:
    db.fail_commit = fail_commit
    return {"status": "posted"}


if __name__ == "__main__":
    client = TestClient(app, raise_server_exceptions=False)

    assert client.post("/journal-entries").status_code == 200
    assert events == ["tenant:enter", "db:open(acme)", "endpoint",
                      "commit", "close", "tenant:exit"]  # exits run LIFO

    events.clear()
    assert client.post("/journal-entries?fail=true").status_code == 400
    assert events[-3:] == ["rollback", "close", "tenant:exit"]

    # Default scope ("request"): cleanup runs after the response is sent,
    # so the client sees 200 even though the commit failed.
    events.clear()
    r = client.post("/journal-entries?fail_commit=true")
    print("request scope, failed commit ->", r.status_code)
    assert r.status_code == 200 and "commit" not in events

    # scope="function": cleanup runs before the response, the failure surfaces.
    r = client.post("/journal-entries-safe?fail_commit=true")
    print("function scope, failed commit ->", r.status_code)
    assert r.status_code == 500
    print("ok")
```

Three rules come out of this run:

1. **Teardown order is LIFO.** `get_db` depends on `get_tenant`, so the session closes before the tenant context exits, the same as nested `with` blocks.
2. **Cleanup belongs in `finally`.** When the endpoint raised, a bare `yield` followed by cleanup code would never reach that cleanup. Always re-raise after you handle the error. If a yield dependency swallows the exception, FastAPI has no response to send, and even an `HTTPException(400)` turns into a 500.
3. **`scope` decides when teardown runs.** By default (`"request"`), the code after `yield` runs after the response has been sent. A commit there can fail while the client has already received 200 and assumes the journal entry was posted. Either commit explicitly inside the endpoint or service and keep only rollback and close in the dependency, or declare the session with `Depends(get_db, scope="function")` so teardown finishes before the response goes out. The full SQLAlchemy version is in [Databases with FastAPI and SQLAlchemy 2]({{ '/ai-stack/fastapi-databases/' | relative_url }}).

## Class-based dependencies

Classes come in two forms, and the tenant example below uses both:

- **A class as the dependency.** `repo: Annotated[InvoiceRepo, Depends()]` tells FastAPI to call `InvoiceRepo(...)` with its `__init__` parameters resolved. A bare `Depends()` means "the annotated type".
- **An instance with `__call__`.** `RequireRole("approver")` is configured when the module loads, and FastAPI resolves the parameters of its `__call__`. Use this for any guard that takes an argument: a role, a feature flag, a maximum amount.

## Router- and app-level dependencies

`APIRouter(dependencies=[Depends(audit)])`, `FastAPI(dependencies=[...])` and `@router.get(..., dependencies=[...])` run dependencies for their side effects and throw away the return values. Use them for guards and cross-cutting work that the endpoint never reads: API key checks, audit logs, rate limits. Caching still applies, so if an audit dependency and the endpoint both need `TenantContext`, it is computed once.

## Security dependencies (preview)

`HTTPBearer`, `OAuth2PasswordBearer` and `APIKeyHeader` are dependencies that pull credentials out of the request and register a security scheme in OpenAPI, which adds the "Authorize" button to `/docs`. A missing bearer header returns 401 with `WWW-Authenticate: Bearer`. `Security(dep, scopes=[...])` adds OAuth2 scopes on top. Verifying JWTs, rotating keys and checking scopes are covered in [Security in FastAPI]({{ '/ai-stack/fastapi-security/' | relative_url }}).

## Tenant context via a dependency

On a multi-tenant finance platform, every query has to be scoped to one tenant, and only users with the right role can approve. With dependencies, that is one chain resolved per request:

```python
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

TOKENS = {"tok-alice": ("alice", {"acme": frozenset(["viewer", "approver"])})}
INVOICES = [{"id": 1, "tenant": "acme", "amount": "900.00"},
            {"id": 2, "tenant": "globex", "amount": "50.00"}]
AUDIT: list[str] = []


@dataclass(frozen=True)
class User:
    username: str
    memberships: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user: User
    roles: frozenset[str]


bearer = HTTPBearer()


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> User:
    if creds.credentials not in TOKENS:
        raise HTTPException(401, "invalid token",
                            headers={"WWW-Authenticate": "Bearer"})
    username, memberships = TOKENS[creds.credentials]
    return User(username, memberships)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_tenant(user: CurrentUser,
               x_tenant_id: Annotated[str, Header()]) -> TenantContext:
    if x_tenant_id not in user.memberships:
        raise HTTPException(403, "no access to this tenant")
    return TenantContext(x_tenant_id, user, user.memberships[x_tenant_id])


Tenant = Annotated[TenantContext, Depends(get_tenant)]


class RequireRole:
    """Configurable dependency: the instance is the callable."""

    def __init__(self, role: str) -> None:
        self.role = role

    def __call__(self, tenant: Tenant) -> TenantContext:
        if self.role not in tenant.roles:
            raise HTTPException(403, f"requires role {self.role!r}")
        return tenant


class InvoiceRepo:
    def __init__(self, tenant: Tenant) -> None:   # a class works as a dependency
        self.tenant_id = tenant.tenant_id

    def list(self) -> list[dict]:
        return [i for i in INVOICES if i["tenant"] == self.tenant_id]


def audit(request: Request, tenant: Tenant) -> None:
    AUDIT.append(f"{tenant.user.username}@{tenant.tenant_id} "
                 f"{request.method} {request.url.path}")


router = APIRouter(prefix="/invoices", tags=["invoices"],
                   dependencies=[Depends(audit)])  # runs for every route


@router.get("")
def list_invoices(repo: Annotated[InvoiceRepo, Depends()]) -> list[dict]:
    return repo.list()


@router.post("/{invoice_id}/approve")
def approve(invoice_id: int,
            tenant: Annotated[TenantContext, Depends(RequireRole("approver"))]):
    return {"invoice_id": invoice_id, "approved_by": tenant.user.username}


app = FastAPI()
app.include_router(router)


if __name__ == "__main__":
    client = TestClient(app)
    auth = {"Authorization": "Bearer tok-alice", "X-Tenant-ID": "acme"}

    assert client.get("/invoices", headers=auth).json() == [INVOICES[0]]
    assert client.get("/invoices").status_code == 401
    other = auth | {"X-Tenant-ID": "globex"}
    assert client.get("/invoices", headers=other).status_code == 403
    assert client.post("/invoices/1/approve", headers=auth).status_code == 200
    assert AUDIT == ["alice@acme GET /invoices", "alice@acme POST /invoices/1/approve"]

    # Tests swap the auth dependency instead of minting tokens.
    app.dependency_overrides[get_current_user] = lambda: User(
        "bob", {"globex": frozenset(["viewer"])})
    try:
        r = client.get("/invoices", headers={"X-Tenant-ID": "globex"})
        assert r.json() == [INVOICES[1]]
        r = client.post("/invoices/2/approve", headers={"X-Tenant-ID": "globex"})
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
    print("ok")
```

What to notice:

- **The header is only a selector.** A tenant ID from `X-Tenant-ID` is checked against the user's memberships before anything uses it. Trusting it as-is is a textbook cross-tenant data leak. A tenant claim inside a signed token is stronger still.
- **The repository receives the tenant at construction**, so code that uses it cannot forget the filter. For defense in depth on PostgreSQL, the session dependency can also run `SET LOCAL app.tenant_id = ...` and let row-level security enforce the same rule (see [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }})).
- **`RequireRole` returns the context** it checked, so the endpoint receives the authorized tenant directly. For richer policies (amount limits, the four-eyes rule) see [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}).

## Overrides in tests

`app.dependency_overrides[original] = replacement` swaps a dependency for every route. The key is the original callable, and FastAPI resolves the replacement's own parameters as usual. Typical overrides are the current user, a database session wrapped in a per-test transaction that rolls back, a fake LLM client, and a fixed clock. Always clear overrides in a `finally` block or in fixture teardown, or they leak into the next test. Pytest fixtures for this come in [the testing and production note]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}).

## Capping concurrency of a dependency

Recent FastAPI versions accept `Depends(dep, max_concurrency=n)`. Once `n` calls are running, the next request waits for a slot without holding a thread. The limit belongs to that `Depends` object and applies per event loop, so reuse one alias to share it across routes, and remember that every worker process has its own limit. It is a quick way to cap an expensive LLM-backed endpoint; the explicit semaphore version is in the async note.

## Coming from Django

| Django / DRF | FastAPI |
|---|---|
| Auth middleware sets `request.user` | `get_current_user` dependency |
| `authentication_classes` | Security dependency (`HTTPBearer`, ...) |
| `permission_classes` | Guard dependency, e.g. `RequireRole("approver")` |
| `get_queryset()` filtered by tenant | Repository dependency bound to `TenantContext` |
| `ATOMIC_REQUESTS`, `transaction.atomic` | Session dependency with `yield` |
| `from django.conf import settings` | `get_settings` dependency + `lru_cache` |
| `override_settings`, `mock.patch` | `app.dependency_overrides` |

Middleware still fits concerns that apply to every request and produce no typed value the endpoint needs: request IDs, CORS, compression, timing, access logs. Use a dependency for anything an endpoint consumes, anything that should appear in OpenAPI, or anything you want to swap out in tests.

## Common mistakes

- **`Depends(get_db())`.** That passes a generator object, not the dependency. Pass the function itself: `Depends(get_db)`.
- **Cleanup without `finally`.** When the endpoint raises, the session or lock is never released. Wrap the `yield` in `try/finally`.
- **Swallowing exceptions in a yield dependency.** Clients get 500s and the logs lose the real error. Always re-raise.
- **Committing after `yield` with the default scope.** A failed write still returns 200. Commit in the endpoint or service, or use `scope="function"`.
- **Using the request's session in a background task.** The session is closed or about to close. Open a new session inside the task.
- **Expecting the cache to span requests.** It does not. Use `lru_cache` or lifespan state for singletons.
- **Trusting tenant or user IDs from headers or bodies.** Derive them from the authenticated identity and check membership.

## Practice tasks

1. Build `get_settings` on pydantic-settings with `@lru_cache`, then override it in a test to set `max_page_size=5`. Done: the test shows `limit=50` clamped to 5.
2. Replace `FakeSession` with a SQLAlchemy `Session` on SQLite. Commit in the endpoint, roll back in the dependency. Done: an endpoint that raises after `add()` leaves no row behind.
3. Add a four-eyes rule: the user who created an invoice can't approve it. Done: a self-approval test returns 403 with a clear message.
4. Add an app-level dependency that requires `X-Request-ID` and stores it in a `contextvars.ContextVar` for logging. Done: a missing header returns 400, and log lines carry the ID.
5. With PostgreSQL, add row-level security keyed on `app.tenant_id`, set by the session dependency. Done: a query with the tenant filter deliberately removed still returns only that tenant's rows.

## Interview questions

**What is a dependency in FastAPI?** Any callable whose parameters FastAPI resolves from the request or from other dependencies. Its result is injected into the endpoint, and the graph is built once at startup.

**How does dependency caching work?** Within one request, each dependency callable runs once and its result is reused by every consumer. `use_cache=False` opts out. Nothing is cached across requests.

**In what order do yield dependencies clean up?** Last in, first out, like nested context managers. The code after `yield` runs after the response is sent by default, or before it with `scope="function"`.

**Where should a DB commit go?** Inside the endpoint or service, where a failure can still change the response, or in a function-scoped dependency. Never in default-scoped teardown, because the client may already have its 200.

**How do you enforce tenant isolation?** Resolve the tenant from the authenticated identity, verify membership in a dependency, inject a repository bound to that tenant, and back it with database row-level security.

**Depends vs middleware?** Middleware wraps every request and works on raw request and response objects. Dependencies are typed, per-route, documented in OpenAPI and overridable in tests. Use middleware for cross-cutting transport concerns and dependencies for everything an endpoint consumes.

**How do you test an endpoint that needs auth?** Override `get_current_user` with `app.dependency_overrides` to return a fixed user, and clear it afterwards. Test the real auth dependency separately with valid, expired and malformed tokens.

**When would you use a class-based dependency?** When the guard needs configuration (`RequireRole("approver")`), or when a service object should be built with resolved constructor arguments (`Depends()` on a repository class).

## Further reading

- FastAPI docs: Dependencies (First Steps, Classes as Dependencies, Sub-dependencies, Dependencies in path operation decorators, Global Dependencies, Dependencies with yield).
- FastAPI docs: Advanced Dependencies, and Testing Dependencies with Overrides.
- FastAPI docs: Security - First Steps, and OAuth2 scopes.
- pydantic-settings docs: Settings Management.
