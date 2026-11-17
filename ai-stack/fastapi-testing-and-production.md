---
title: Testing and running FastAPI in production
section: ai-stack
order: 8
phase: "Phase 1 · FastAPI"
summary: Test FastAPI with TestClient, AsyncClient and overrides, then ship it with settings, request-ID logs, error envelopes, health checks and a checklist.
---

A demo API and a service you can run for a bank differ in what tutorials skip: tests for every failure path, one error format, logs you can join to a single request, health checks the orchestrator can trust, and a process model that survives deploys. After this note you can lay out a mid-size FastAPI service, test it with `TestClient` and `httpx.AsyncClient`, isolate database tests with rolled-back transactions, and run it with Uvicorn or Gunicorn in Docker with logs, metrics and traces wired in. The LangChain and LangGraph phases build on this service shape.

## Prerequisites

The previous FastAPI notes, especially [databases]({{ '/ai-stack/fastapi-databases/' | relative_url }}) and [security]({{ '/ai-stack/fastapi-security/' | relative_url }}). For pytest basics from the Django side, see [testing Django]({{ '/python-django/testing-django/' | relative_url }}).

## Project layout for a mid-size service

```text
invoice-service/
├── app/
│   ├── main.py            create_app(): middleware, handlers, routers
│   ├── core/              config.py, logging.py, errors.py, security.py
│   ├── api/
│   │   ├── deps.py        get_session, get_current_user, get_llm
│   │   └── routes/        invoices.py, reconciliation.py, ask.py
│   ├── services/          business rules, transaction boundaries
│   ├── repositories/      SQLAlchemy queries
│   ├── models/            ORM models
│   ├── schemas/           Pydantic request and response models
│   └── llm/               prompts, chains, graphs, tools
├── migrations/            Alembic
├── tests/                 conftest.py, unit/, api/, integration/
├── pyproject.toml
└── Dockerfile
```

Routes import services, services import repositories, nothing imports routes. LLM code sits behind a dependency so tests swap in a fake model. A `create_app()` factory builds the app from a `Settings` object, so tests can build differently configured apps.

## The service: settings, logging, errors, health

The module below is a compressed version of that layout in one file. It shows each production concern once:

- **Settings** from `APP_*` environment variables, validated at startup: a bad `APP_ENV` fails fast.
- **Request IDs**: a pure ASGI middleware reuses a safe incoming `X-Request-ID` or generates one, stores it in a `ContextVar`, returns it in the response and writes one JSON access-log line. A handler-level logging filter stamps it on every record, including library logs.
- **One error envelope** for domain errors, HTTP errors, validation errors and crashes: an `error` object with `code`, `message` and `request_id`.
- **Liveness and readiness** endpoints.
- **OpenAPI as a contract**: response models, documented error responses and stable operation IDs.

{% raw %}
```python
# invoice_service.py
import asyncio
import io
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Numeric, String, UniqueConstraint, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException


# ---------- settings ----------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")
    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "sqlite+aiosqlite:///:memory:"
    log_level: str = "INFO"


# ---------- logging with request IDs ----------
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
log = logging.getLogger("invoice_service")


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    FIELDS = ("request_id", "method", "path", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        out = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out |= {k: getattr(record, k) for k in self.FIELDS if hasattr(record, k)}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def configure_logging(level: str, stream=None) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIDFilter())  # on the handler: covers every library's logs
    logging.basicConfig(level=level, handlers=[handler], force=True)


SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class RequestIDMiddleware:
    """Pure ASGI middleware: works with streaming responses, adds no extra task."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode()
        rid = incoming if SAFE_ID.match(incoming) else uuid.uuid4().hex
        request_id_var.set(rid)
        status, start = 500, time.perf_counter()

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []),
                                      (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info("request", extra={
                "method": scope["method"], "path": scope["path"], "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1)})


# ---------- errors: one envelope for every failure ----------
class DomainError(Exception):
    status, code = 400, "domain_error"


class NotFound(DomainError):
    status, code = 404, "not_found"


class Conflict(DomainError):
    status, code = 409, "conflict"


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def error_response(status: int, code: str, message: str, **extra) -> JSONResponse:
    body = {"code": code, "message": message, "request_id": request_id_var.get(), **extra}
    return JSONResponse({"error": body}, status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain(request: Request, exc: DomainError):
        return error_response(exc.status, exc.code, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http(request: Request, exc: StarletteHTTPException):
        resp = error_response(exc.status_code, f"http_{exc.status_code}", str(exc.detail))
        resp.headers.update(exc.headers or {})
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        details = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]  # no inputs
        return error_response(422, "validation_error", "invalid request", details=details)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")  # full traceback goes to logs, not the client
        resp = error_response(500, "internal_error", "internal server error")
        resp.headers["X-Request-ID"] = request_id_var.get()  # runs outside our middleware
        return resp


# ---------- data ----------
class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class InvoiceIn(BaseModel):
    number: str = Field(max_length=50)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class InvoiceOut(InvoiceIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ---------- dependencies ----------
class User(BaseModel):
    id: str
    tenant_id: str


oauth2 = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)


async def get_current_user(token: Annotated[str | None, Depends(oauth2)]) -> User:
    # stub: plug in JWT verification from the security note; tests override this
    raise StarletteHTTPException(401, "not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


UserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ---------- app factory ----------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sqlite = settings.database_url.startswith("sqlite")
        engine = create_async_engine(
            settings.database_url,
            **({"poolclass": StaticPool} if sqlite else {"pool_pre_ping": True}))
        if sqlite:  # local and tests only; real databases are migrated by Alembic
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Invoice service", version="1.4.0", lifespan=lifespan,
        docs_url=None if settings.env == "prod" else "/docs",
        generate_unique_id_function=lambda route: route.name,  # operationId = function name
    )
    app.state.settings = settings
    app.add_middleware(RequestIDMiddleware)
    install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}  # liveness: the process answers, no dependencies

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        try:
            async with asyncio.timeout(2):
                async with request.app.state.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("readiness check failed", exc_info=True)
            return JSONResponse({"status": "unavailable", "db": "fail"}, status_code=503)
        return {"status": "ready", "db": "ok"}

    @app.post("/invoices", status_code=201, response_model=InvoiceOut,
              responses={409: {"model": ErrorEnvelope}})
    async def create_invoice(data: InvoiceIn, user: UserDep, session: SessionDep):
        invoice = Invoice(tenant_id=user.tenant_id, **data.model_dump())
        session.add(invoice)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise Conflict(f"invoice {data.number} already exists")
        return invoice

    @app.get("/invoices/{invoice_id}", response_model=InvoiceOut,
             responses={404: {"model": ErrorEnvelope}})
    async def get_invoice(invoice_id: int, user: UserDep, session: SessionDep):
        invoice = await session.scalar(select(Invoice).where(
            Invoice.id == invoice_id, Invoice.tenant_id == user.tenant_id))
        if invoice is None:
            raise NotFound(f"invoice {invoice_id} not found")
        return invoice

    return app


if __name__ == "__main__":
    logs = io.StringIO()
    configure_logging("INFO", stream=logs)
    with TestClient(create_app(Settings(env="test"))) as client:
        r = client.get("/healthz", headers={"X-Request-ID": "req-42"})
        assert r.headers["x-request-id"] == "req-42"
        assert client.get("/readyz").json() == {"status": "ready", "db": "ok"}
        r = client.get("/invoices/1")
        assert r.status_code == 401 and r.json()["error"]["code"] == "http_401"
    lines = [json.loads(line) for line in logs.getvalue().splitlines()]
    access = [l for l in lines if l["logger"] == "invoice_service"]
    assert access[0]["request_id"] == "req-42" and access[0]["status"] == 200
    assert access[2]["path"] == "/invoices/1" and access[2]["status"] == 401
    print("ok")
```
{% endraw %}

### Where each piece runs

```text
ServerErrorMiddleware       Exception handler (500) runs here, outermost
 └─ RequestIDMiddleware     sets request_id, adds X-Request-ID, access log
     └─ ExceptionMiddleware HTTPException, DomainError, 422 handlers
         └─ router ─► dependencies ─► route
```

- The `Exception` handler runs in Starlette's outermost middleware, outside anything you add, so it sets `X-Request-ID` itself; the `ContextVar` is still readable because it is the same task. Starlette re-raises after sending the 500, so the server logs it again.
- The middleware is pure ASGI, not `@app.middleware("http")`: `BaseHTTPMiddleware` runs the downstream app in a separate task and wraps the body stream, a known source of streaming and context-variable bugs.
- Validation errors drop the `input` field that FastAPI includes by default. For finance data the rejected input may be an account number; it should not be echoed back or logged.
- Never accept a client-supplied request ID blindly: the regex stops log injection through newlines or megabyte-long headers.

### Health and readiness

- **Liveness** (`/healthz`): is the process alive. No dependencies, or a database blip restarts every pod at once.
- **Readiness** (`/readyz`): should traffic come here. Checks the database with a short timeout and returns 503 on failure so the load balancer routes around the pod. Do not check the LLM provider; an upstream outage should degrade answers, not empty the pool.
- On shutdown, Uvicorn stops accepting connections, waits up to `--timeout-graceful-shutdown` for in-flight requests, then runs lifespan cleanup. Keep the orchestrator's grace period above your longest stream. See [graceful shutdown]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).

### OpenAPI as the contract

Other teams generate clients from `/openapi.json`; treat it as public. `response_model` plus `responses=` entries pointing at `ErrorEnvelope` document every outcome; `generate_unique_id_function` gives stable operation names (`get_invoice`, not `get_invoice_invoices__invoice_id__get`) that become client method names. In CI, commit the exported schema and fail on unreviewed changes; `oasdiff` flags breaking ones.

## Testing: TestClient, AsyncClient and overrides

Two clients, two jobs:

| Client | Runs the app | Lifespan | Use for |
|---|---|---|---|
| `TestClient` | in a separate thread with its own event loop | yes, inside `with` | most API tests, WebSockets, streams |
| `httpx.AsyncClient` + `ASGITransport` | in the test's event loop | no | async tests that share async fixtures (DB sessions) with the app |

An `AsyncSession` created in a pytest-asyncio fixture is bound to that event loop; `TestClient` would use it from another loop and fail. With `ASGITransport` fixture and app share a loop. If those tests need startup code, wrap them in `async with app.router.lifespan_context(app):`.

`app.dependency_overrides[original] = replacement` swaps any dependency for a test: the current user, the database session, the LLM, the clock. The `app` fixture builds a fresh app per test, so overrides never leak.

The database fixture uses the rollback pattern: begin an outer transaction on a connection, bind the session to it with `join_transaction_mode="create_savepoint"`, and roll back at teardown. The app's `commit()` only releases a savepoint, so every test starts clean without truncating tables.

```python
# test_invoice_service.py (next to invoice_service.py)
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from invoice_service import Base, Settings, User, create_app, get_current_user, get_session

ASHA = User(id="asha", tenant_id="t1")


@pytest.fixture
def app():
    app = create_app(Settings(env="test"))
    app.dependency_overrides[get_current_user] = lambda: ASHA
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    with TestClient(app) as c:  # `with` runs the lifespan (engine, tables)
        yield c


@pytest.fixture(scope="session")
async def engine():
    # with PostgreSQL: point at a test database and run `alembic upgrade head` once
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    # SQLite only: let SQLAlchemy emit BEGIN itself so SAVEPOINTs nest correctly
    @event.listens_for(engine.sync_engine, "connect")
    def _connect(dbapi_conn, _):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine):
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False,
                               join_transaction_mode="create_savepoint")
        yield session  # app code may commit: that only releases a savepoint
        await session.close()
        await outer.rollback()  # undo everything this test wrote


@pytest.fixture
async def aclient(app, db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    transport = ASGITransport(app=app)  # same event loop as the fixtures, no lifespan
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_liveness_and_readiness(client, app):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    app.state.engine = create_async_engine("sqlite+aiosqlite:////no/such/dir/x.db")
    assert client.get("/readyz").status_code == 503


def test_auth_required(client, app):
    del app.dependency_overrides[get_current_user]
    r = client.get("/invoices/1", headers={"X-Request-ID": "t-1"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"] == {"code": "http_401", "message": "not authenticated",
                                 "request_id": "t-1"}


def test_validation_error_hides_input(client):
    r = client.post("/invoices", json={"number": "INV-1", "amount": "-5"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"][0]["loc"] == ["body", "amount"]
    assert "-5" not in r.text


def test_unhandled_error_is_opaque(app):
    @app.get("/boom")
    async def boom():
        raise RuntimeError("db password is hunter2")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in r.text and r.headers["x-request-id"]


@pytest.mark.parametrize("run", [1, 2])
async def test_create_read_and_tenant_isolation(aclient, app, run):
    # passes twice only because each run's commit is rolled back
    r = await aclient.post("/invoices", json={"number": "INV-7", "amount": "99.10"})
    assert r.status_code == 201, r.text
    invoice_id = r.json()["id"]
    assert (await aclient.post("/invoices", json={"number": "INV-7", "amount": "1"})
            ).json()["error"]["code"] == "conflict"
    assert (await aclient.get(f"/invoices/{invoice_id}")).json()["amount"] == "99.10"

    app.dependency_overrides[get_current_user] = lambda: User(id="meera", tenant_id="t2")
    assert (await aclient.get(f"/invoices/{invoice_id}")).status_code == 404


def test_openapi_contract(app):
    op = app.openapi()["paths"]["/invoices/{invoice_id}"]["get"]
    assert op["operationId"] == "get_invoice"
    assert {"200", "404", "422"} <= op["responses"].keys()


if __name__ == "__main__":
    assert pytest.main([__file__, "-q", "-p", "no:cacheprovider",
                        "-W", "ignore::pytest.PytestAssertRewriteWarning",
                        "-o", "asyncio_mode=auto",
                        "-o", "asyncio_default_fixture_loop_scope=session",
                        "-o", "asyncio_default_test_loop_scope=session"]) == 0
```

In a real repo these options live in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"                          # async tests without markers
asyncio_default_fixture_loop_scope = "session" # one loop shared by fixtures
asyncio_default_test_loop_scope = "session"    # ...and by tests
```

The suite covers readiness failure, 401 with header and envelope, 422 without echoing input, an opaque 500, 409, cross-tenant 404 and the contract. The parametrized test passing twice proves the rollback isolation. Also run integration tests on real PostgreSQL in CI (a service container or Testcontainers): SQLite does not exercise `FOR UPDATE`, row-level security or pgvector.

## Running in production

Uvicorn alone is enough now; its `--workers` supervisor restarts crashed workers. Gunicorn remains an option.

```bash
# one container per pod, scale pods horizontally; 1-2 workers per container
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 2 \
  --proxy-headers --forwarded-allow-ips "10.0.0.0/8" \
  --timeout-keep-alive 5 --timeout-graceful-shutdown 30 \
  --limit-max-requests 5000 --no-access-log   # our middleware writes access logs

# Gunicorn as process manager: the old uvicorn.workers module is deprecated
pip install gunicorn uvicorn-worker
gunicorn "app.main:create_app()" -k uvicorn_worker.UvicornWorker -w 4 \
  --graceful-timeout 30 --max-requests 5000 --max-requests-jitter 500
```

- Workers: start at one per CPU core, not the `2n+1` rule for sync workers, and size the DB pool per worker.
- `--proxy-headers` with a restricted `--forwarded-allow-ips` makes `request.client.host` the real client IP, which rate limiting depends on.
- `--limit-max-requests` (with jitter) recycles workers to contain slow memory leaks.

```dockerfile
# illustrative
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 app
WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", \
     "--port", "8000", "--proxy-headers", "--timeout-graceful-shutdown", "30"]
```

Non-root user, pinned dependencies installed before the code is copied (better layer caching), no secrets baked in, and migrations as a separate deploy step, not in `CMD`. See [Docker essentials]({{ '/production/docker-essentials/' | relative_url }}).

## Observability hooks

- **Logs**: JSON to stdout with `request_id`, `tenant_id`, route and duration.
- **Metrics**: rate, errors and duration per route, plus the LLM numbers that drive cost and SLOs: time to first token, input and output tokens per tenant, provider 429s and timeouts, retrieval latency.
- **Traces**: FastAPI 0.16x has native OpenTelemetry support. Once an OpenTelemetry SDK tracer provider is configured (`pip install "fastapi[opentelemetry]"`), it records spans for each request, dependency resolution, the endpoint and serialization; `FastAPI(telemetry={"exclude": ...})` skips paths such as health checks. Earlier versions needed `opentelemetry-instrumentation-fastapi`.
- **LLM traces** in LangSmith with the request ID in run metadata ([observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }})); errors to Sentry, tagged the same way.

## Coming from Django

| Django | FastAPI |
|---|---|
| `settings.py` plus `django-environ` | `pydantic-settings` `BaseSettings`, validated at startup |
| `django.test.Client`, `APIClient` | `TestClient`, `httpx.AsyncClient` with `ASGITransport` |
| `TestCase` transaction rollback | outer transaction plus savepoint-joined session |
| `override_settings`, `mock.patch` | `app.dependency_overrides`, `create_app(Settings(...))` |
| `handler500`, DRF `EXCEPTION_HANDLER` | `@app.exception_handler(...)` |
| `gunicorn project.wsgi` | `uvicorn --factory` or Gunicorn with `uvicorn-worker` |

## Common mistakes

- **Module-level `app = FastAPI()` that reads settings at import.** Tests cannot reconfigure it. Use a factory.
- **Forgetting `with TestClient(app)`.** Without it, the lifespan never runs and `app.state.engine` does not exist.
- **Sharing an async DB fixture with `TestClient`.** Different event loops; use `AsyncClient` with `ASGITransport` for those tests.
- **Liveness that checks the database**, restarting every pod during a database blip.
- **Returning `str(exc)` in 500 responses.** It leaks SQL, paths and secrets. Log it, return a code and request ID.
- **`2n+1` async workers per container**, multiplied by pods, exhausting database connections.

## Production checklist

- [ ] `create_app()` factory; settings validated at startup; secrets as `SecretStr`
- [ ] Request ID in every log line, response header and error body
- [ ] One error envelope; no stack traces or inputs in responses
- [ ] Liveness without dependencies; readiness with timeouts; graceful shutdown tuned
- [ ] Alembic in a deploy step; connection pool sized per worker
- [ ] Auth on every router by default; CORS, rate limits and body limits set
- [ ] OpenAPI exported and diffed in CI; operation IDs stable
- [ ] Tests for 401, 403, 404, 409, 422, 500 and tenant isolation, on real PostgreSQL in CI
- [ ] Metrics, traces and LLM token and cost tracking per tenant
- [ ] Non-root container, pinned dependencies, `/docs` off on public deployments

## Practice tasks

1. Split the single module into the layout above without changing behavior. Done: the same test file passes with only import changes.
2. Add a `get_llm` dependency and an `/invoices/{id}/explain` route; override it with `GenericFakeChatModel` in tests. Done: no test touches the network, and a test asserts the prompt contains the invoice number.
3. Add a contract test that compares `app.openapi()` with a committed `openapi.json`. Done: renaming a response field fails CI with a readable diff.
4. Run the test suite against PostgreSQL in Docker with the same rollback fixture. Done: the parametrized isolation test passes on PostgreSQL, and a `FOR UPDATE` test proves concurrent posting is serialized.

## Interview questions

**`TestClient` or `httpx.AsyncClient`?**
`TestClient` for most tests: it runs lifespan and works with sync tests. `AsyncClient` with `ASGITransport` when the test and the app must share async resources such as a DB session, since it runs in the test's event loop. It skips lifespan and buffers streams.

**How do you test an endpoint that calls an LLM?**
Put the model behind a dependency and override it with a fake chat model. Assert on the prompt you built and on how the response is parsed. Judge answer quality in a separate eval suite.

**How do you isolate database tests?**
Outer transaction per test, session joined with savepoints, rollback at teardown. Fast, and the app's commits still behave like commits.

**How do you make every log line traceable to a request?**
A middleware sets a request ID in a `ContextVar`; a logging filter copies it onto every record; the ID goes back in a response header and in error bodies, and onto trace spans.

**Liveness versus readiness?**
Liveness: is the process healthy, no dependencies, failure means restart. Readiness: can it serve now, checks critical dependencies with timeouts, failure means stop routing traffic.

**How do you run FastAPI in production?**
Uvicorn workers (or Gunicorn with `uvicorn-worker`), one or two per container, proxy headers trusted from the load balancer only, graceful shutdown timeout above the longest request, migrations as a separate step.

## Further reading

- FastAPI docs: "Testing", "Async Tests", "Testing Dependencies with Overrides", "Bigger Applications - Multiple Files", "Settings and Environment Variables", "Handling Errors", "Deployment"
- SQLAlchemy docs: "Joining a Session into an External Transaction (such as for test suites)"
- pytest-asyncio docs: "Configuration"
- Uvicorn docs: "Settings" and "Deployment"
- OpenTelemetry Python docs: "Getting Started"
- Notes: [structured logging]({{ '/production/structured-logging/' | relative_url }}), [observability]({{ '/production/observability/' | relative_url }})
