---
title: Request validation and error handling
section: backend
order: 24
phase: "Phase 3 · API design"
summary: Validate at the boundary with DRF or Pydantic, cap sizes, reject unknown fields, and return consistent RFC 9457 errors with the right status codes.
---

Every request body is untrusted bytes. The API boundary is where those bytes either become typed values your code can rely on or get rejected with an error the client can act on. Get validation wrong and you ship mass-assignment bugs, 500s for client mistakes, memory blowups from oversized payloads and data that violates invariants. Get errors wrong and every client hand-parses message strings, retries requests that can never succeed, and reads your stack traces. Validation and error handling are two halves of one contract: what you accept and what you say when you don't.

```text
 request
   |
 proxy (nginx)      body and header size, timeouts        -> 413 / 408
   |
 framework          content type, JSON syntax, auth       -> 415 / 400 / 401
   |
 boundary schema    types, ranges, lengths, unknown keys  -> 400 or 422
   |   typed command object: no raw dicts past this line
   v
 service / domain   business rules, permissions, state    -> 403 / 404 / 409
   |
 database           NOT NULL, CHECK, UNIQUE, FK            -> mapped to 409
   |
 error mapper  -->  application/problem+json, code, trace id, no internals
```

## Request validation

### Schema validation

Schema validation checks the **shape** of a request: required fields, types, formats (UUID, ISO 8601 timestamp with offset, email), numeric ranges, string lengths, enums and list sizes. It is mechanical and can be declared rather than coded, which is why it belongs in a schema: a serializer, a Pydantic model, or JSON Schema inside an OpenAPI document that also generates docs and client SDKs.

Semantic validation checks **meaning**: `end` after `start`, the coupon exists for this tenant, the order is still cancellable. It often needs the database or the caller's identity, so it runs after the shape is known to be valid.

The useful mindset is "parse, don't validate": the boundary does not just say yes or no, it returns a typed object (`CreateOrder(currency="EUR", items=(...))`) and nothing downstream touches the raw dict again. Code past the boundary can then assume its inputs are valid instead of re-checking defensively.

Type details that bite in Python:

- `bool` is a subclass of `int`, so `isinstance(True, int)` is true and `"quantity": true` slips through naive checks.
- Python's `json` module accepts `NaN` and `Infinity`, which are not valid JSON. Reject them with a `parse_constant` hook.
- Money as JSON floats loses precision. Use integer minor units or decimal strings.
- Integers above 2^53 lose precision in JavaScript clients. Send 64-bit IDs as strings.

### DRF serializers and Pydantic

DRF serializers integrate with the ORM and the browsable API; Pydantic (and frameworks built on it, such as FastAPI and Django Ninja) is faster, with its core written in Rust, and type-hint driven. Both declare shape, both support field-level and object-level validators, and neither should be trusted with uniqueness on its own.

| | DRF serializer | Pydantic v2 model |
|---|---|---|
| Field rule | `IntegerField(min_value=1)` | `Field(ge=1)` |
| Field hook | `validate_<field>()` | `@field_validator` |
| Cross-field | `validate(self, attrs)` | `@model_validator(mode="after")` |
| Unknown fields | Ignored by default | `extra="ignore"` by default; set `"forbid"` |
| Type coercion | Coerces strings to numbers | Lax by default; `strict=True` to stop it |
| Default error status | 400 | 422 in FastAPI |

A DRF version (illustrative) that also rejects unknown fields at every nesting level:

```python
from rest_framework import serializers

class RejectUnknownFields:
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {k: ["Unknown field."] for k in unknown})
        return super().to_internal_value(data)

class LineItemSerializer(RejectUnknownFields, serializers.Serializer):
    sku = serializers.RegexField(r"^[A-Z0-9-]{1,32}$")
    quantity = serializers.IntegerField(min_value=1, max_value=1000)

class CreateOrderSerializer(RejectUnknownFields, serializers.Serializer):
    currency = serializers.ChoiceField(choices=["USD", "EUR", "INR"])
    items = serializers.ListField(
        child=LineItemSerializer(), min_length=1, max_length=100)
    coupon = serializers.CharField(required=False, max_length=32)

    def validate_coupon(self, value):  # semantic, needs tenant context
        tenant = self.context["request"].tenant
        if not Coupon.objects.filter(tenant=tenant, code=value,
                                     active=True).exists():
            raise serializers.ValidationError(
                "Unknown or expired coupon.", code="invalid_coupon")
        return value
```

The same contract in Pydantic v2 (illustrative):

```python
from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sku: Annotated[str, Field(pattern=r"^[A-Z0-9-]{1,32}$")]
    quantity: Annotated[int, Field(ge=1, le=1000)]

class CreateOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: Literal["USD", "EUR", "INR"]
    items: Annotated[list[LineItem], Field(min_length=1, max_length=100)]
    deliver_after: date | None = None
    deliver_before: date | None = None

    @model_validator(mode="after")
    def window_is_ordered(self):
        a, b = self.deliver_after, self.deliver_before
        if a and b and a > b:
            raise ValueError("deliver_after must not be after deliver_before")
        return self

order = CreateOrder.model_validate_json(raw_body)  # raises ValidationError
```

`ValidationError.errors()` returns entries with a `loc` tuple such as `("items", 0, "quantity")`, which maps directly to a JSON Pointer for the error response.

### Validate at the boundary

- **Once, at the edge.** The view or serializer turns the request into a typed command; services and domain objects receive that, never `request.data`.
- **Domain invariants still hold inside.** An `Order.cancel()` that raises when the order has shipped is not duplicate validation; it protects every caller, including Celery tasks and admin scripts.
- **The database is the last line.** `NOT NULL`, `CHECK`, `UNIQUE` and foreign keys catch what code missed. A DRF `UniqueValidator` checks with a `SELECT` and races under concurrency, so also catch `IntegrityError` and map it to 409.
- **Never take authority from the body.** `tenant_id`, `owner_id`, `role`, `price` and `is_staff` come from the authenticated context or the database. Explicit writable-field allowlists (`fields = [...]`, never `"__all__"`) prevent mass assignment, which OWASP's API Top 10 lists under broken object property level authorization.
- **Internal traffic is a boundary too.** Messages from another service or a Kafka topic can come from an older or newer producer. Validate them, ideally against a registered schema.
- Frontend validation is UX, not security.

### Size limits

Unbounded input is a denial-of-service vector. Put a limit at every layer, cheapest first.

| Layer | Limit | Typical setting |
|---|---|---|
| Proxy | Body size, header size, slow bodies | nginx `client_max_body_size` (default 1m), `client_body_timeout` |
| App server | Request line and header limits | Gunicorn `limit_request_line`, `limit_request_fields` |
| Framework | In-memory body, form field count | Django `DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB), `DATA_UPLOAD_MAX_NUMBER_FIELDS` (1000) |
| Schema | String length, list length, nesting depth, page size | `max_length`, `le=`, page size cap of 100 |
| Logic | Regex cost, batch sizes, `?ids=` count | No user-supplied regexes; bound every loop |

Return `413 Content Too Large` when you control the response. Test the limit you think you have: framework settings don't cover every parsing path, so the proxy limit is the one you can count on. Send large uploads straight to object storage with presigned URLs instead of through the app.

### Unknown fields

| | Reject unknown fields | Ignore unknown fields |
|---|---|---|
| Typos (`"quantitiy": 5`) | Caught with a clear error | Silently uses the default |
| `PATCH` with a typo | 4xx | 200 OK and nothing changed |
| Mass-assignment probing | Visible, logged | Silent |
| Version skew (new client, old server) | Fails until the server deploys | Works, field dropped |

Reject unknown fields on writes to public APIs and deploy servers before clients that send new fields. Be a tolerant reader of responses and events you consume, since producers add fields over time. One more trap: JSON objects with duplicate keys. Python keeps the last value; some parsers keep the first. When a gateway and a service disagree on which one wins, a validated value can differ from the used one. Reject duplicates with `object_pairs_hook` on sensitive paths.

## Error handling and status codes

### Status code guide

| Code | Use it for |
|---|---|
| 200 / 201 / 204 | Success with body / created (send `Location`) / success without body |
| 202 Accepted | Async work queued; return a status URL |
| 304 Not Modified | Conditional GET matched `If-None-Match` |
| 400 Bad Request | Malformed syntax, or validation failure if that's your convention |
| 401 Unauthorized | Missing or invalid credentials; must send `WWW-Authenticate` |
| 403 Forbidden | Authenticated but not allowed |
| 404 Not Found | Doesn't exist, or exists but the caller may not know it does |
| 405 Method Not Allowed | Must send `Allow` |
| 409 Conflict | State conflict: duplicate, wrong state, idempotency key in progress |
| 412 Precondition Failed | `If-Match` ETag mismatch on optimistic concurrency |
| 413 Content Too Large | Body over the limit |
| 415 Unsupported Media Type | Wrong `Content-Type` |
| 422 Unprocessable Content | Well-formed but semantically invalid |
| 428 Precondition Required | You require `If-Match` and it was missing |
| 429 Too Many Requests | Rate limited; send `Retry-After` |
| 500 Internal Server Error | A bug; never for client mistakes |
| 502 / 504 | Upstream returned garbage / upstream timed out |
| 503 Service Unavailable | Overloaded or in maintenance; send `Retry-After` |

The 400 versus 422 debate has no winner: DRF uses 400 for validation errors, FastAPI uses 422, and RFC 9110 defines 422 as "Unprocessable Content" in the core spec. Pick one, document it, and use it everywhere. The rule that matters more: client mistakes are 4xx, your mistakes are 5xx. A `KeyError` on a missing field that surfaces as 500 pages your on-call for a client bug.

### RFC 9457 problem details

RFC 9457 (2023, obsoleting RFC 7807) standardizes an error body with media type `application/problem+json`:

- `type`: a URI identifying the problem type, ideally a docs page. It is the primary identifier clients key on. Defaults to `about:blank`, in which case `title` should be the HTTP status phrase.
- `title`: a short, human-readable summary of the type that does not change between occurrences.
- `status`: the HTTP status code, repeated for convenience.
- `detail`: this occurrence, written to help the client fix it, not to debug your server.
- `instance`: a URI reference for this specific occurrence.
- Extension members: anything else, such as `errors`, `code` or `trace_id`. The RFC shows multiple validation errors as an array with a JSON Pointer per entry.

```json
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Invalid request",
  "status": 422,
  "detail": "2 fields failed validation.",
  "errors": [
    {"pointer": "#/items/0/quantity", "detail": "integer from 1 to 1000"},
    {"pointer": "#/currency", "detail": "must be one of USD, EUR, INR"}
  ],
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

### Error codes for clients

Status codes are too coarse to branch on: a 409 can mean "duplicate email" or "order already shipped", and the client must react differently. Give every problem type a stable, machine-readable identifier (the `type` URI, or a short `code` such as `insufficient_funds` if your clients prefer switch statements) and treat it as API contract:

- Clients branch on the code, never on `title` or `detail`, which you can reword or localize.
- Never rename a code. Adding codes is safe only if clients fall back on the status class for codes they don't know; say so in the docs.
- List every code per endpoint in the OpenAPI spec.
- Field errors carry a pointer so UIs can highlight the right input.

### Retryable vs not

| Outcome | Retry? | Condition |
|---|---|---|
| Connection error, timeout | Yes | Idempotent request, or one with an idempotency key |
| 408, 425 | Yes | Same request is fine |
| 409 for an in-progress idempotency key | Yes | After a short delay |
| 429, 503 | Yes | After `Retry-After`, with backoff and jitter |
| 502, 504 | Yes | Backoff and jitter; watch the retry budget |
| 500 | Rarely | Idempotent requests only, one or two attempts |
| Other 4xx | No | The same request fails again; fix it |

A request that failed before a response arrived may still have executed, which is why retries and [idempotency]({{ '/backend/idempotency/' | relative_url }}) are designed together. Backoff, jitter and budgets are in [retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}).

### Never leaking internals

- **No stack traces, SQL, ORM messages or file paths.** An `IntegrityError` message names your tables and constraints. Run Django with `DEBUG = False` in every shared environment.
- **No passthrough of upstream bodies.** Translate a provider's error into your own problem type; don't forward its JSON or its HTML 502 page.
- **Generic 500 plus a trace ID.** The client gets `{"title": "Internal Server Error", "trace_id": ...}`; your logs and Sentry get the full exception tagged with that ID.
- **Hide existence across tenants.** Fetching another tenant's invoice returns 404, not 403, so IDs can't be probed.
- **Uniform auth failures.** "Invalid email or password" for both cases; password reset always returns 202. Otherwise the error reveals which accounts exist.
- **Strip version banners** (`Server`, `X-Powered-By`), and never echo raw input into HTML error pages.

## In practice

### Boundary parser to problem details (runnable)

A stdlib version of the whole boundary: content type, size cap, syntax and nesting bombs, unknown fields, the `bool`-is-`int` trap, and RFC 9457 errors with escaped JSON Pointers.

```python
import json
import re
from dataclasses import dataclass

MAX_BODY_BYTES = 64 * 1024
MAX_ITEMS = 100
SKU = re.compile(r"[A-Z0-9-]{1,32}")
VALIDATION = "https://api.example.com/problems/validation-error"


@dataclass(frozen=True)
class Item:
    sku: str
    quantity: int


@dataclass(frozen=True)
class CreateOrder:
    currency: str
    items: tuple


class Problem(Exception):
    def __init__(self, status, title, type_="about:blank", errors=()):
        super().__init__(title)
        self.status, self.title, self.type, self.errors = (
            status, title, type_, list(errors))

    def to_json(self):
        doc = {"type": self.type, "title": self.title, "status": self.status}
        if self.errors:
            doc["errors"] = self.errors
        return doc


def ptr(*parts):
    # JSON Pointer (RFC 6901): escape ~ then /
    return "#" + "".join(
        "/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def parse_create_order(raw: bytes, content_type: str) -> CreateOrder:
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise Problem(415, "Unsupported Media Type")
    if len(raw) > MAX_BODY_BYTES:
        raise Problem(413, "Content Too Large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):  # bad syntax, bad UTF-8, deep nesting
        raise Problem(400, "Bad Request") from None

    errors = []
    def err(where, detail):
        errors.append({"pointer": where, "detail": detail})

    if not isinstance(data, dict):
        raise Problem(422, "Invalid request", VALIDATION,
                      [{"pointer": "#", "detail": "must be an object"}])
    for k in sorted(data.keys() - {"currency", "items"}):
        err(ptr(k), "unknown field")
    if data.get("currency") not in ("USD", "EUR", "INR"):
        err(ptr("currency"), "must be one of USD, EUR, INR")
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        err(ptr("items"), f"must be a list of 1 to {MAX_ITEMS} items")
        items = []
    parsed = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            err(ptr("items", i), "must be an object")
            continue
        for k in sorted(item.keys() - {"sku", "quantity"}):
            err(ptr("items", i, k), "unknown field")
        sku, qty = item.get("sku"), item.get("quantity")
        if not isinstance(sku, str) or not SKU.fullmatch(sku):
            err(ptr("items", i, "sku"), "1 to 32 of A-Z, 0-9 and -")
        if type(qty) is not int or not 1 <= qty <= 1000:  # rejects true/false
            err(ptr("items", i, "quantity"), "integer from 1 to 1000")
        parsed.append(Item(sku, qty))
    if errors:
        raise Problem(422, "Invalid request", VALIDATION, errors)
    return CreateOrder(data["currency"], tuple(parsed))


def status_of(raw, ctype="application/json"):
    try:
        parse_create_order(raw, ctype)
        return 200
    except Problem as p:
        return p.status


if __name__ == "__main__":
    ok = b'{"currency": "EUR", "items": [{"sku": "AB-1", "quantity": 2}]}'
    order = parse_create_order(ok, "application/json; charset=utf-8")
    assert order == CreateOrder("EUR", (Item("AB-1", 2),))

    assert status_of(ok, "text/plain") == 415
    assert status_of(b" " * (MAX_BODY_BYTES + 1)) == 413
    assert status_of(b'{"currency": ') == 400
    assert status_of(b"[" * 50_000) == 400          # nesting bomb
    assert status_of(b"[1, 2]") == 422

    bad = (b'{"currency": "EUR", "is_admin": true, "items": '
           b'[{"sku": "ab", "quantity": true, "price": 1}]}')
    try:
        parse_create_order(bad, "application/json")
        raise AssertionError("should fail")
    except Problem as p:
        body = p.to_json()
        assert body["status"] == 422 and body["type"] == VALIDATION
        assert [e["pointer"] for e in body["errors"]] == [
            "#/is_admin", "#/items/0/price",
            "#/items/0/sku", "#/items/0/quantity"]
    assert ptr("a/b", "c~d") == "#/a~1b/c~0d"
    print("ok")
```

### DRF exception handler emitting problem details (illustrative)

Register it with `REST_FRAMEWORK = {"EXCEPTION_HANDLER": "core.errors.problem_handler"}`. It keeps DRF's status codes and headers (`Retry-After`, `WWW-Authenticate`), maps domain errors, and turns anything unexpected into a logged, opaque 500.

```python
import logging
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

log = logging.getLogger(__name__)
PROBLEMS = "https://api.example.com/problems/"

class DomainError(Exception):
    status, code, title = 409, "conflict", "Conflict"

class InsufficientFunds(DomainError):
    status, code, title = 422, "insufficient_funds", "Insufficient funds"

def pointers(detail, prefix="#"):
    if isinstance(detail, dict):
        for field, sub in detail.items():
            yield from pointers(sub, f"{prefix}/{field}")
    elif isinstance(detail, list) and detail and not isinstance(detail[0], str):
        for i, sub in enumerate(detail):
            yield from pointers(sub, f"{prefix}/{i}")
    else:
        for msg in detail if isinstance(detail, list) else [detail]:
            yield {"pointer": prefix, "detail": str(msg),
                   "code": getattr(msg, "code", "invalid")}

def problem_handler(exc, context):
    response = exception_handler(exc, context)  # DRF's default mapping
    trace_id = context["request"].headers.get("X-Request-ID", "")
    if isinstance(exc, DomainError):
        set_rollback()
        response = Response(status=exc.status)
        body = {"type": PROBLEMS + exc.code, "title": exc.title,
                "code": exc.code, "detail": str(exc)}
    elif response is None:  # a bug, not a client error
        set_rollback()
        log.exception("unhandled API error", extra={"trace_id": trace_id})
        response = Response(status=500)
        body = {"type": "about:blank", "title": "Internal Server Error"}
    elif isinstance(exc, ValidationError):
        body = {"type": PROBLEMS + "validation-error",
                "title": "Invalid request",
                "errors": list(pointers(response.data))}
    else:
        detail = response.data.get("detail", "")
        body = {"type": "about:blank", "title": response.status_text,
                "code": getattr(detail, "code", "error"), "detail": str(detail)}
    body.update(status=response.status_code, trace_id=trace_id)
    response.data = body
    response.content_type = "application/problem+json"
    return response
```

## Pitfalls

- **200 with `{"error": ...}`.** Breaks HTTP caches, client libraries and your own error-rate dashboards. Use the status code.
- **500 for client mistakes.** A missing field raises `KeyError` deep in a service. Symptom: alert noise and Sentry floods from one bad client. Fix: validate at the boundary.
- **Uniqueness checked only in the serializer.** Two concurrent signups pass the `SELECT`, one hits `IntegrityError` and returns 500. Catch it and return 409.
- **Inconsistent error shapes.** Each service invents its own; the gateway returns HTML on 502. Normalize to problem details, including at the gateway.
- **Clients parsing messages.** Someone rewords "Email taken" and the signup screen breaks. Ship stable codes.
- **Silent coercion.** `"quantity": "5"` or `true` accepted as a number hides client bugs. Use strict types on write endpoints.
- **Limits only in the app.** A 2 GB body still reaches the worker and is read into memory. Cap it at the proxy.
- **429 or 503 without `Retry-After`.** Clients guess, usually too aggressively.
- **Leaking via 403.** A 403 for another tenant's resource confirms it exists.

## Interview questions

**400 or 422 for a validation error?**
Both are defensible: 400 for malformed requests generally, 422 for syntactically valid but semantically invalid content. What matters is one documented convention, a structured body with field pointers, and never 500.

**401 vs 403?**
401 means "I don't know who you are": credentials are missing or invalid, and the response carries `WWW-Authenticate`. 403 means "I know who you are and you can't do this". For resources in another tenant, prefer 404 so existence isn't revealed.

**How do you design an error format for a public API?**
RFC 9457 problem details with `application/problem+json`: `type` URI as the stable identifier, `title`, `status`, `detail` for this occurrence, plus extensions such as an `errors` array with JSON Pointers and a `trace_id`. Codes are contract; messages are not.

**Which errors should a client retry?**
Connection failures and timeouts on idempotent requests, 408, 429 and 503 after `Retry-After`, 502 and 504 with backoff and jitter. Not other 4xx: the same request fails again. Non-idempotent requests are retried only with an idempotency key.

**Where should validation live in a layered service?**
Shape validation at the boundary, producing typed commands. Invariants in the domain. Constraints in the database as the final guard. Each layer protects a different set of callers.

**How do you prevent mass assignment in DRF?**
Explicit `fields` lists with `read_only_fields`, never `"__all__"` on write serializers. Set `tenant`, `owner` and similar fields from `request` in `perform_create`, and reject unknown fields so probing shows up.

**A validation check for a unique email still produces duplicates. Why?**
Check-then-insert races. Two requests both see no row and both insert. Enforce it with a unique constraint (case-insensitive if needed) and map the `IntegrityError` to 409.

## Further reading

- RFC 9457, *Problem Details for HTTP APIs*.
- RFC 9110, *HTTP Semantics*, section 15 "Status Codes".
- OWASP Input Validation Cheat Sheet, and the OWASP API Security Top 10.
- Django REST framework documentation: "Serializers", "Validators" and "Exceptions"; Pydantic documentation: "Models" and "Strict Mode".
- Alexis King, "Parse, don't validate".
