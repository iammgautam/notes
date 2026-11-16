---
title: FastAPI fundamentals
section: ai-stack
order: 2
phase: "Phase 1 · FastAPI"
summary: Build a typed REST API with FastAPI and Pydantic v2, covering ASGI, routers, validation, response filtering, errors and OpenAPI, tested with TestClient.
---

After this note you can build a small, typed, documented CRUD API in FastAPI, explain what happens between the socket and your function, and use Pydantic v2 to validate finance data at the boundary. In this role FastAPI is how RAG pipelines and agents get exposed to other services, so every later note in this phase (dependencies, async, databases, security, streaming) builds on the invoices API you write here.

## Prerequisites

Python type hints and one REST framework, such as DRF. Read [the study plan]({{ '/ai-stack/study-plan/' | relative_url }}) first to see where this phase fits.

## How a request reaches your function

There are four layers, and each one has a single job:

- **ASGI** is the contract between the server and the app. An ASGI app is one async callable, `app(scope, receive, send)`. WSGI (Django's default) is a sync call per request, so a request holds a thread until it finishes. ASGI lets one process interleave thousands of requests that are waiting on I/O, and it supports streaming and WebSockets.
- **Uvicorn** is the ASGI server. It owns the socket and the event loop, parses HTTP, and calls your app.
- **Starlette** is the toolkit underneath FastAPI. It provides routing, `Request`/`Response`, middleware, background tasks, lifespan and `TestClient`.
- **FastAPI** subclasses Starlette's app. At startup it inspects every endpoint signature once and builds a plan: where each parameter comes from, which dependencies to run, how to validate the input and how to serialize the output. **Pydantic v2**, which has a Rust core, does the validating and serializing.

```text
client --HTTP--> uvicorn (ASGI server, event loop)
                   | app(scope, receive, send)
                   v
                 middleware stack (CORS, GZip, request ID, ...)
                   v
                 router: match method + path ------> 404 / 405
                   v
                 FastAPI route
                   |- resolve dependencies
                   |- parse path, query, header, cookie, body
                   |- validate with Pydantic --------> 422
                   |- call endpoint (async def: event loop,
                   |                 def: threadpool)
                   '- validate + serialize return value
                        through the response model --> JSON
```

At the bottom, an ASGI app is only this. FastAPI generates the same `send` messages for you:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient


# A raw ASGI app: one async callable, three arguments, no framework.
async def raw_app(scope, receive, send):
    assert scope["type"] == "http"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": b'{"status":"ok"}'})


# The same endpoint in FastAPI: routing, parsing and docs come for free.
app = FastAPI(title="Finance API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    assert TestClient(raw_app).get("/anything").json() == {"status": "ok"}
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/missing").status_code == 404
    assert "/health" in client.get("/openapi.json").json()["paths"]
    print("ok")
```

`TestClient` drives any ASGI app in-process through httpx, with no server and no port.

## Parameters and the Annotated style

FastAPI works out where each parameter comes from using a few rules:

| Declared as | Read from |
|---|---|
| Name that appears in the path, e.g. `{ledger_id}` | Path |
| Scalar (`int`, `str`, `bool`, `date`, enum) not in the path | Query string |
| `list[str]` marked `Query()` | Repeated query param |
| Pydantic model | JSON body |
| `Header()` | Header (`x_request_id` matches `X-Request-ID`) |
| `Cookie()` | Cookie |

Write constraints as `Annotated[type, Path(gt=0)]`. That keeps the default as a plain Python default, and the annotated type can be reused as an alias. The older `limit: int = Query(20)` form still works, but new code should use `Annotated`.

```python
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import Cookie, FastAPI, Header, Path, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class GLEntry(BaseModel):
    account: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    posted_on: date


@app.post("/ledgers/{ledger_id}/entries")
async def post_entry(
    ledger_id: Annotated[int, Path(gt=0)],              # from the URL path
    entry: GLEntry,                                       # JSON body
    dry_run: bool = False,                                # ?dry_run=true
    tags: Annotated[list[str], Query()] = [],             # ?tags=a&tags=b
    x_request_id: Annotated[str | None, Header()] = None,  # X-Request-ID
    session_id: Annotated[str | None, Cookie()] = None,
) -> dict:
    return {
        "ledger_id": ledger_id,
        "account": entry.account,
        "dry_run": dry_run,
        "tags": tags,
        "request_id": x_request_id,
        "session": session_id,
    }


if __name__ == "__main__":
    client = TestClient(app)
    client.cookies.set("session_id", "s-1")
    r = client.post(
        "/ledgers/7/entries?dry_run=true&tags=accrual&tags=q3",
        json={"account": "4000-Revenue", "credit": "250.00",
              "posted_on": "2026-09-30"},
        headers={"X-Request-ID": "req-42"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ledger_id": 7, "account": "4000-Revenue",
                        "dry_run": True, "tags": ["accrual", "q3"],
                        "request_id": "req-42", "session": "s-1"}
    bad = client.post("/ledgers/0/entries", json={"account": "x"})
    assert bad.status_code == 422
    locs = {tuple(e["loc"]) for e in bad.json()["detail"]}
    assert ("path", "ledger_id") in locs and ("body", "posted_on") in locs
    print(bad.json()["detail"][0])
    print("ok")
```

A 422 lists every failure at once, and `loc` gives the source and field of each. Clients parse this shape, so it is part of your API contract.

## Pydantic v2 models

A model is a parser with a schema: raw input in, typed object out, and back to JSON. A DRF serializer does both jobs too, but Pydantic is not tied to an ORM. The v1 to v2 renames that matter:

| v1 | v2 |
|---|---|
| `.dict()`, `.json()` | `.model_dump()`, `.model_dump_json()` |
| `parse_obj()` | `model_validate()` |
| `class Config:` | `model_config = ConfigDict(...)` |
| `@validator`, `@root_validator` | `@field_validator`, `@model_validator` |
| `orm_mode = True` | `from_attributes=True` |

```python
# invoice_models.py
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      computed_field, field_validator, model_validator)

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
CURRENCIES = {"USD", "EUR", "GBP", "INR"}
Status = Literal["draft", "approved", "paid"]


class InvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str = Field(min_length=1, max_length=120)
    currency: str = "USD"
    amount: Money
    tax: Money = Decimal("0.00")
    issue_date: date
    due_date: date

    @field_validator("currency")
    @classmethod
    def known_currency(cls, value: str) -> str:
        value = value.upper()
        if value not in CURRENCIES:
            raise ValueError(f"unsupported currency {value!r}")
        return value

    @model_validator(mode="after")
    def due_not_before_issue(self) -> Self:
        if self.due_date < self.issue_date:
            raise ValueError("due_date is before issue_date")
        return self


class InvoiceOut(InvoiceCreate):
    id: int
    status: Status = "draft"

    @computed_field
    @property
    def total(self) -> Decimal:
        return self.amount + self.tax


if __name__ == "__main__":
    inv = InvoiceCreate(vendor="  Acme Corp ", currency="eur", amount="1200.50",
                        issue_date="2026-10-01", due_date="2026-10-31")
    assert inv.vendor == "Acme Corp" and inv.currency == "EUR"
    assert inv.amount == Decimal("1200.50")          # str -> Decimal, exact

    out = InvoiceOut(id=1, **inv.model_dump() | {"tax": "216.09"})
    assert out.model_dump(mode="json")["total"] == "1416.59"  # Decimal as str

    for bad in [{"amount": "10.999"}, {"currency": "XYZ"},
                {"due_date": "2026-09-01"}, {"discount": "5"}]:
        data = {"vendor": "Acme", "amount": "10", "issue_date": "2026-10-01",
                "due_date": "2026-10-31"} | bad
        try:
            InvoiceCreate.model_validate(data)
        except ValidationError as exc:
            print(exc.errors()[0]["type"], "-", exc.errors()[0]["msg"])
        else:
            raise AssertionError(f"accepted {bad}")
    print("ok")
```

What each piece does:

- **`Money` alias.** `Decimal` with `max_digits` and `decimal_places` rejects `10.999` instead of rounding it quietly. Never use `float` for money. In JSON mode a `Decimal` serializes as a string (`"1416.59"`), so clients have to parse it as a decimal, not a float.
- **`extra="forbid"`.** A misspelled or unexpected field (`discount`) fails loudly. The default is to ignore it, so a typo would pass silently.
- **`field_validator`.** Runs after type coercion, on one field. Use it to normalize and enforce allowed values.
- **`model_validator(mode="after")`.** Sees the whole validated object, which is where cross-field rules go (dates, debit equals credit).
- **`computed_field`.** Derived values appear in the output and in the OpenAPI output schema, and clients can never send them.
- **Lax mode.** By default `"1200.50"` is coerced to `Decimal`. If a field must arrive with the exact type, set `strict=True` on that `Field` or on `ConfigDict`.

## Response models and filtering

The return annotation is the response model. FastAPI validates the returned value against it, serializes it, and documents it. Only the fields the model declares are sent, which gives you a safe pattern built from three models:

- `InvoiceCreate` is what clients may send.
- `InvoiceOut` is what clients may see.
- `InvoiceRecord(InvoiceOut)` is what you store. It adds internal fields, and returning it from an endpoint annotated `-> InvoiceOut` strips them.

If you return something unrelated to the type you want documented (a dict or an ORM row), put the model in the decorator as `response_model=InvoiceOut`. That setting wins over the annotation. `response_model_exclude_none=True` drops null fields. ORM objects also need `from_attributes=True`, which [Databases with FastAPI and SQLAlchemy 2]({{ '/ai-stack/fastapi-databases/' | relative_url }}) covers.

## Status codes and errors

- Set `status_code=201` on create, `204` on delete (no body), and `202` when work is accepted to run later.
- `HTTPException(409, "detail")` covers expected HTTP failures and renders as `{"detail": ...}`.
- Domain exceptions plus `@app.exception_handler(...)` keep HTTP out of your business logic. The service raises `InvoiceNotFound`, and one handler maps it to 404 everywhere. This is FastAPI's version of DRF's `EXCEPTION_HANDLER` setting.
- A `RequestValidationError` becomes a 422. Override its handler if you need a different error format, such as RFC 9457 problem details.
- A Pydantic `ValidationError` raised inside your own endpoint code is not a request error. It becomes a **500**, so convert it yourself, as the PATCH handler below does.

## Routers

`APIRouter` is the equivalent of a Django app's `urls.py`. It groups related endpoints and lets them share a `prefix`, `tags`, `dependencies` and documented `responses`. `app.include_router(router, prefix="/v1")` stacks the prefixes, so `/invoices` becomes `/v1/invoices`. Tags group the endpoints in `/docs`. A layout that scales:

```text
app/
  main.py               create FastAPI(), include routers, handlers
  core/config.py        pydantic-settings Settings
  api/v1/invoices.py    APIRouter: thin path operations
  schemas/invoice.py    Pydantic Create / Update / Out models
  services/invoices.py  business rules, raises domain errors
  db/models.py          SQLAlchemy models
```

## Putting it together: an invoices API

This is a complete CRUD router with list filters, partial updates, a business rule, a domain error and OpenAPI checks. Save the previous block as `invoice_models.py` next to it, then run it.

```python
# main.py: save the previous block as invoice_models.py next to it.
from datetime import date
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from invoice_models import InvoiceCreate, InvoiceOut, Money, Status


class InvoiceRecord(InvoiceOut):
    internal_notes: str = ""            # stored, never returned


class InvoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor: str | None = None
    amount: Money | None = None
    due_date: date | None = None
    status: Status | None = None


class InvoiceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status | None = None
    vendor: str | None = None
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)


class InvoiceNotFound(Exception):
    def __init__(self, invoice_id: int) -> None:
        self.invoice_id = invoice_id


DB: dict[int, InvoiceRecord] = {}
router = APIRouter(prefix="/invoices", tags=["invoices"])


def load(invoice_id: int) -> InvoiceRecord:
    if invoice_id not in DB:
        raise InvoiceNotFound(invoice_id)
    return DB[invoice_id]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invoice(body: InvoiceCreate) -> InvoiceOut:
    record = InvoiceRecord(id=len(DB) + 1, internal_notes="3-way match pending",
                           **body.model_dump())
    DB[record.id] = record
    return record                       # filtered down to InvoiceOut fields


@router.get("")
async def list_invoices(
    filters: Annotated[InvoiceFilters, Query()],
) -> list[InvoiceOut]:
    rows = [r for r in DB.values()
            if filters.status in (None, r.status)
            and filters.vendor in (None, r.vendor)]
    return rows[filters.offset : filters.offset + filters.limit]


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: int) -> InvoiceOut:
    return load(invoice_id)


@router.patch("/{invoice_id}")
async def update_invoice(invoice_id: int, patch: InvoiceUpdate) -> InvoiceOut:
    current = load(invoice_id)
    if current.status == "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "paid invoices are immutable")
    merged = current.model_dump(exclude={"total"}) | patch.model_dump(exclude_unset=True)
    try:
        DB[invoice_id] = InvoiceRecord.model_validate(merged)  # re-run validators
    except ValidationError as exc:
        # hide input: the merged record holds internal fields
        errors = exc.errors(include_input=False, include_context=False)
        raise RequestValidationError(errors) from exc
    return DB[invoice_id]


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(invoice_id: int) -> None:
    load(invoice_id)
    del DB[invoice_id]


app = FastAPI(title="Invoices API", version="1.0.0")
app.include_router(router, prefix="/v1")


@app.exception_handler(InvoiceNotFound)
async def invoice_not_found(request: Request, exc: InvoiceNotFound) -> JSONResponse:
    return JSONResponse(status_code=404,
                        content={"error": "invoice_not_found", "invoice_id": exc.invoice_id})


if __name__ == "__main__":
    client = TestClient(app)
    body = {"vendor": "Acme Corp", "amount": "1200.00", "tax": "216.00",
            "issue_date": "2026-10-01", "due_date": "2026-10-31"}
    r = client.post("/v1/invoices", json=body)
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["total"] == "1416.00" and "internal_notes" not in inv
    client.post("/v1/invoices", json=body | {"vendor": "Globex"})

    assert [i["vendor"] for i in client.get("/v1/invoices?vendor=Globex").json()] == ["Globex"]
    assert client.get("/v1/invoices?limit=500").status_code == 422

    assert client.patch("/v1/invoices/2", json={"due_date": "2026-09-01"}).status_code == 422
    assert client.patch("/v1/invoices/1", json={"status": "paid"}).json()["status"] == "paid"
    assert client.patch("/v1/invoices/1", json={"amount": "1.00"}).status_code == 409

    r = client.get("/v1/invoices/999")
    assert r.status_code == 404 and r.json()["error"] == "invoice_not_found"
    assert client.delete("/v1/invoices/2").status_code == 204

    spec = client.get("/openapi.json").json()
    assert "/v1/invoices/{invoice_id}" in spec["paths"]
    out_fields = spec["components"]["schemas"]["InvoiceOut"]["properties"]
    assert "total" in out_fields and "internal_notes" not in out_fields
    print("ok")
```

Points worth defending in a review:

- **PATCH validates the merged result, not just the patch.** On its own, a patch with only `due_date` can't tell that the date falls before `issue_date`. `exclude_unset=True` keeps "field not sent" separate from "field sent as null". Don't use `model_copy(update=...)` here, because it skips validation.
- **`InvoiceFilters` is a query parameter model** (`Annotated[Model, Query()]`). With `extra="forbid"`, an unknown filter such as `?stauts=paid` returns a 422 instead of quietly returning every invoice.
- **`include_input=False`** keeps internal fields out of the 422 response, because by default Pydantic echoes the input back in each error.

## OpenAPI docs

FastAPI serves `/openapi.json`, Swagger UI at `/docs` and ReDoc at `/redoc`, generated from the same signatures that drive validation, so the docs can't drift from the code. Add a docstring to each endpoint (it becomes the description), `Field(description=..., examples=[...])`, and a `responses` entry on the route for each documented error status, such as 404. Treat the schema as a contract: generate typed clients from it, and diff it in CI to catch breaking changes. For internet-facing services you can turn the docs off with `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.

## Coming from Django

| Django / DRF | FastAPI |
|---|---|
| `urls.py` + `include()` | `APIRouter` + `include_router` |
| `APIView` / `ViewSet` method | Path operation function |
| Serializer (validate + represent) | Input model + output model |
| `validate_<field>()`, `validate()` | `field_validator`, `model_validator` |
| `ModelSerializer` | No equivalent: write models, `from_attributes=True` |
| `raise NotFound` | `HTTPException` or domain error + handler |
| `django-filter` | Typed query params or a query model |
| drf-spectacular | Built in |
| `APIClient` | `TestClient` |

Django is batteries-included around its ORM; FastAPI is a thin, typed HTTP layer, and you choose the database layer, auth, settings and layout yourself. The next notes cover those choices.

## Common mistakes

- **One model for input and output.** Clients can set `id` or `status`, and internal fields leak out. Split the models into Create, Update and Out.
- **Returning a dict or ORM row with no response model.** Every column goes out. Always annotate the return type or set `response_model`.
- **`float` for amounts.** Use `Decimal` with `max_digits` and `decimal_places`.
- **Raising `ValidationError` in a handler.** The client gets a 500. Convert it to `RequestValidationError` or an `HTTPException`.
- **Inconsistent trailing slashes.** A request to `/invoices/` for a route defined as `/invoices` gets a 307 redirect, and clients that do not follow redirects (httpx by default) see a 307 instead of data. Pick one style.
- **Blocking calls inside `async def`.** One slow sync call stalls every request in the process. See [Async and performance in FastAPI]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}).

## Practice tasks

1. Add `GET /v1/vendors/{vendor}/spend` that returns the total per currency. Done: a test checks two currencies and a vendor with no invoices (empty dict, not 404).
2. Add `lines: list[LineItem]` to `InvoiceCreate` and a validator that requires `amount` to equal the sum of the lines. Done: a mismatch returns 422 with a clear message.
3. Return RFC 9457 problem details (`type`, `title`, `status`, `detail`) for 404, 409 and 422 by overriding the handlers. Done: existing tests are updated and all three shapes match.
4. Split the example into the layout above, with `services/` raising only domain errors. Done: the same tests pass unchanged.
5. Export `openapi.json` in CI and fail the build when a response field is removed. Done: deleting `total` breaks the pipeline.

## Interview questions

**What is ASGI and why does FastAPI need it?** ASGI is the async interface between server and app, `app(scope, receive, send)`. It lets one process interleave many I/O-bound requests and supports streaming and WebSockets. WSGI ties up a thread for each in-flight request.

**How does FastAPI know where a parameter comes from?** From the signature. A name in the path template is a path param, a Pydantic model is the body, other scalars are query params, and `Header()`/`Cookie()` mark the rest. It builds this plan once at startup.

**How do you stop a response from leaking internal fields?** Annotate the endpoint with an output model that only lists public fields. FastAPI serializes through that model, so extra attributes on the returned object are dropped. Keep input, output and stored models separate.

**Pydantic v1 vs v2?** v2 has a Rust core and is much faster. It renamed `.dict()`, `parse_obj` and `@validator` to `model_dump`, `model_validate` and `field_validator`, replaced `Config` with `model_config`, and added `computed_field` and strict mode.

**Why `Decimal`, and what does the client receive?** Binary floats can't represent most cents exactly, and the errors pile up in sums. Pydantic serializes `Decimal` as a JSON string, so clients have to parse it as a decimal.

**400, 409 or 422?** 422 means the request is well formed but fails the schema or validation. 409 means it conflicts with the resource's current state (editing a paid invoice). 400 is for other malformed requests.

**`HTTPException` or a custom exception handler?** `HTTPException` for HTTP-specific failures in the route layer. Domain exceptions plus a handler keep services reusable from workers and CLIs, and keep error formatting in one place.

**When would you pick Django over FastAPI?** For admin-heavy CRUD with Django's ORM, auth and admin, Django is faster to build. For typed JSON APIs, async I/O to LLMs and vector stores, and streaming, FastAPI fits better.

## Further reading

- FastAPI docs: "Tutorial - User Guide" pages on Path Parameters, Query Parameter Models, Request Body, Response Model - Return Type, Handling Errors, and Bigger Applications - Multiple Files.
- Pydantic docs: Models, Fields, Validators, Computed Fields, Strict Mode, and the Migration Guide.
- Starlette docs: Applications, Routing, TestClient.
- The ASGI specification (HTTP and lifespan scopes).
