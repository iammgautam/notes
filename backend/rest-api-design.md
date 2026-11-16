---
title: REST API design
section: backend
order: 18
phase: "Phase 3 · API design"
summary: Resources, method semantics, status codes, PATCH and merge patch, bulk and long-running operations, and ETag concurrency control, with DRF code.
---

REST is the default contract between a backend and everything that talks to it: web and mobile clients, partners, other services. The production problems are rarely about REST purity. They are about semantics: a retried POST that charges a card twice, a PATCH that silently wipes a field, two admins overwriting each other's edits, an export endpoint that dies at the load balancer's 60-second timeout. Get the HTTP semantics right and most of those disappear.

## Resources and URL design

REST (Roy Fielding's 2000 dissertation) constrains a system to be client-server, stateless, cacheable, layered, and to use a uniform interface. Day to day, the uniform interface is what matters: URLs name resources, standard methods act on them, and representations (usually JSON) carry their state. Rules that hold up:

- Collections are plural nouns: `/v1/orders`; items are `/v1/orders/{order_id}`.
- Nest only for real containment and only one level deep: `/v1/orders/{id}/line-items`. Past that, go top-level with a filter: `/v1/line-items?order_id=ord_123`.
- Actions that are not CRUD become a sub-resource or a custom method: `POST /v1/orders/{id}/refunds` (a refund has its own lifecycle, so it is a resource) or Google-style `POST /v1/orders/{id}:cancel`.
- IDs are opaque strings; sequential integers leak volume and invite enumeration. Type prefixes (`ord_`) help debugging.
- In a multi-tenant API the tenant comes from the credential. Another tenant's ID gets `404`, not `403`, so you do not confirm it exists.

## Methods and their semantics

| Method | Safe | Idempotent | Cacheable | Typical use |
|---|---|---|---|---|
| `GET` | yes | yes | yes | Read a resource or collection |
| `HEAD` | yes | yes | yes | Headers only (existence, size, ETag) |
| `OPTIONS` | yes | yes | no | Capabilities, CORS preflight |
| `PUT` | no | yes | no | Replace the full representation |
| `DELETE` | no | yes | no | Remove a resource |
| `POST` | no | no | rarely | Create in a collection, run a process |
| `PATCH` | no | not guaranteed | no | Apply a partial change |

**Safe** means the client does not request any state change, so browsers, crawlers and link previewers may call it freely. **Idempotent** means sending the same request N times has the same intended effect on the server as sending it once. The response can differ: the first `DELETE` returns `204`, the second `404`, and that is still idempotent.

Idempotency is what makes retries safe. A client that times out on `PUT /orders/123` can resend it; a client that times out on `POST /orders` cannot know whether the order exists. The fix for POST is an `Idempotency-Key` header with server-side deduplication, covered in [Idempotency]({{ '/backend/idempotency/' | relative_url }}).

`PUT` replaces: fields the client omits are reset to defaults, which is why clients that only want to change one field should use `PATCH`. `PUT` can also create a resource at a client-chosen URL (`PUT /v1/tenants/acme/settings`).

> **Gotcha:** a `GET` with side effects, such as `GET /unsubscribe?token=...` that unsubscribes immediately, gets triggered by email security scanners and link previewers. Let the `GET` render a confirmation and let a `POST` do the work.

## Status codes overview

| Code | Meaning | Use it when |
|---|---|---|
| `200 OK` | Success with body | Reads, updates that return the resource |
| `201 Created` | Resource created | POST created something; set `Location` |
| `202 Accepted` | Queued, not done | Long-running work; point to a status resource |
| `204 No Content` | Success, no body | DELETE, updates that return nothing |
| `304 Not Modified` | Cached copy is valid | `If-None-Match` matched |
| `400 Bad Request` | Malformed request | Unparseable JSON, unknown filter field |
| `401 Unauthorized` | Not authenticated | Missing or invalid credentials |
| `403 Forbidden` | Authenticated, not allowed | Valid user lacks permission |
| `404 Not Found` | No such resource | Also for resources outside the caller's tenant |
| `409 Conflict` | State conflict | Duplicate unique key, invalid state transition |
| `412 Precondition Failed` | `If-Match` did not match | Optimistic concurrency conflict |
| `415 Unsupported Media Type` | Wrong `Content-Type` | Merge patch sent as plain JSON, for example |
| `422 Unprocessable Content` | Well-formed but invalid | Field validation failures |
| `428 Precondition Required` | Conditional header missing | You require `If-Match` on writes |
| `429 Too Many Requests` | Rate limited | Include `Retry-After` |
| `500` / `502` / `503` / `504` | Server or upstream failure | `503` with `Retry-After` for overload |

The full error body format (RFC 9457 problem details) and validation strategy live in [Validation and error handling]({{ '/backend/validation-and-error-handling/' | relative_url }}). The one rule worth repeating here: never return `200` with `{"error": ...}` in the body. Proxies, retry libraries, monitoring and client SDKs all key off the status code.

## HATEOAS

Hypermedia as the engine of application state: each response carries links to the actions that are valid right now, so clients follow links instead of building URLs and hard-coding state machines. A HAL-style response:

```json
{
  "id": "ord_123",
  "status": "placed",
  "total_cents": 4599,
  "_links": {
    "self": { "href": "/v1/orders/ord_123" },
    "cancel": { "href": "/v1/orders/ord_123:cancel", "method": "POST" },
    "invoice": { "href": "/v1/invoices/inv_77" }
  }
}
```

Full hypermedia-driven clients are rare in public JSON APIs; most clients hard-code paths from an OpenAPI spec. The parts that pay off:

- **Pagination links** (`next`, `prev`) in the body or the `Link` header (RFC 8288). GitHub's REST API paginates this way.
- **`Location` headers** on `201` and `202`.
- **Conditional action links.** Including `cancel` only when the order is cancellable and the caller may cancel it lets the UI hide the button without duplicating business rules in JavaScript.

Use it where it removes duplicated logic, not as dogma.

## Filtering, sorting and field selection

| Need | Convention |
|---|---|
| Equality | `?status=paid` |
| Multiple values | `?status=paid,refunded` or repeated `?status=paid&status=refunded` |
| Ranges | `?created_after=2026-01-01&created_before=2026-02-01` |
| Sorting | `?sort=-created_at,id` (minus means descending) |
| Sparse fields | `?fields=id,status,total_cents` |
| Free text | `?q=blue+shirt` |

Rules:

- **Allowlist** filterable and sortable fields. Each one needs an index story; an arbitrary sort on an unindexed column turns into a sequential scan plus sort on your largest tenant.
- **Reject unknown parameters with `400`.** Silently ignoring `?stauts=paid` returns everything, and a client building a bulk delete from that result will have a bad day.
- **Always append a unique tie-breaker** (`id`) to the sort so [pagination]({{ '/backend/pagination/' | relative_url }}) is deterministic.
- Cap the page size.
- Generic filter languages (OData `$filter`, AIP-160) are expressive, but every combination is a query plan to support; heavy ad hoc search belongs in a search engine.

## Partial updates: PATCH and JSON Merge Patch

`PATCH` applies a change described by a patch document whose meaning is defined by its media type. Two standards cover JSON:

| | JSON Merge Patch | JSON Patch |
|---|---|---|
| Spec | RFC 7396 | RFC 6902 |
| Media type | `application/merge-patch+json` | `application/json-patch+json` |
| Shape | A partial document | A list of operations |
| Delete a field | Set it to `null` | `{"op": "remove", "path": "/note"}` |
| Arrays | Replaced whole | Index-level add/remove/replace |
| Set a value to `null` | Impossible | `replace` with `null` |
| Preconditions | No | `test` op, atomic |
| Idempotent | Yes | Not in general (`add` to `/tags/-` appends again) |

Merge patch rules: objects merge recursively, `null` deletes the key, anything else (scalars, arrays) replaces. It is what most "partial update" endpoints already do informally, so declaring it gives you a precise contract for free. Use JSON Patch when clients must edit one element of a large array or need the `test` operation.

```http
PATCH /v1/orders/ord_123 HTTP/1.1
Content-Type: application/merge-patch+json
If-Match: "7"

{
  "note": null,
  "shipping": { "zip": "411045" }
}
```

DRF's `partial=True` is close to merge patch at the top level, but nested serializers and explicit `null` need deliberate handling; decide and document the behavior.

## Bulk operations

Clients importing 5,000 contacts should not make 5,000 requests. Three shapes:

1. **Batch endpoint per resource**: `POST /v1/contacts:batchCreate` with `{"items": [...]}` and a hard cap (100–1,000 items).
2. **Generic batch envelope** of arbitrary sub-requests (Microsoft Graph's `$batch`). Flexible, but you reimplement routing and auth inside one request.
3. **Async bulk job**: upload a file or large payload, get `202` and a job resource (next section). Required once the work exceeds a few seconds.

The key decision is atomicity:

| | All-or-nothing | Partial success |
|---|---|---|
| Implementation | One transaction | Per-item savepoints or validation first |
| Client logic | Simple: retry everything | Must read per-item results |
| One bad row | Fails the batch | Only that item fails |
| Response | `201` or a single error | `207 Multi-Status` or `200` with per-item status |

A partial-success response:

```json
{
  "results": [
    { "index": 0, "status": 201, "id": "con_91" },
    { "index": 1, "status": 422, "error": "email is invalid" },
    { "index": 2, "status": 409, "error": "duplicate external_id" }
  ]
}
```

Accept an `Idempotency-Key` for the batch, let clients send their own `external_id` per item so retries do not duplicate, rate-limit by item count rather than request count ([Rate limiting]({{ '/backend/rate-limiting/' | relative_url }})), and use `bulk_create` or `COPY` in chunks instead of row-by-row inserts.

## Long-running operations

Anything that may exceed a few seconds (exports, imports, report generation, video processing) should not hold an HTTP request open. Load balancers and proxies cut connections at 30–120 seconds, clients retry, and you end up running the job twice. Return `202 Accepted` with a status resource instead:

```text
client                     api                   queue      worker
  | POST /v1/exports         |                      |          |
  |------------------------->| insert operation     |          |
  |                          | (status=pending)     |          |
  |                          |--- enqueue op_42 --->|          |
  | 202 Accepted             |                      |--------->|
  | Location: /v1/operations/op_42                  |          |
  | Retry-After: 5           |                      |          |
  |<-------------------------|                      |          |
  | GET /v1/operations/op_42 |                      |  running |
  |------------------------->|                      |          |
  | 200 {status: running, progress: 0.4}            |          |
  |<-------------------------|      update op row <------------|
  | GET /v1/operations/op_42 |                      |          |
  | 200 {status: succeeded, result_url: ...}        |          |
  |<-------------------------|                      |          |
```

The operation resource has `id`, `status` (`pending`, `running`, `succeeded`, `failed`, `canceled`), `progress`, `result_url` or `error`, and a retention period; add a cancel endpoint for expensive work. Polling `GET` returns `200` with the status in the body. Offer a webhook or SSE stream for clients that do not want to poll ([Real-time communication]({{ '/backend/real-time-communication/' | relative_url }})), and pair the `POST` with an idempotency key so a retry returns the same operation instead of starting a second export. Google's AIP-151 calls these long-running operations.

## Conditional requests: ETag and If-Match

An `ETag` is an opaque validator for one version of a representation. Strong ETags (`"7"`) promise byte-identical content; weak ones (`W/"7"`) only semantic equivalence. They serve two purposes.

**Cache revalidation.** The client sends `If-None-Match: "7"`; if nothing changed the server answers `304 Not Modified` with no body. This saves bandwidth. It only saves server work if the ETag is cheap to compute, such as a version column, rather than a hash of a freshly rendered body.

**Optimistic concurrency.** The client sends `If-Match: "7"` on `PUT`, `PATCH` or `DELETE`. If the current version differs, the server refuses with `412 Precondition Failed`. To make clients opt in, answer writes without the header with `428 Precondition Required`. This prevents the lost update:

```text
 Alice                     server (version 7)                Bob
   | GET  -> ETag "7"           |                              |
   |                            |          GET  -> ETag "7"    |
   | PATCH If-Match "7"         |                              |
   |--------------------------->| version 7 -> 8, 200 ETag "8" |
   |                            |    PATCH If-Match "7"        |
   |                            |<-----------------------------|
   |                            | 412: Bob re-reads, re-applies|
```

Without `If-Match`, Bob's write silently replaces Alice's. Two details matter. `If-Match` uses strong comparison, so a weak ETag never matches. And the check and the write must be one atomic statement (`UPDATE ... WHERE id = $1 AND version = $2`, zero rows updated means 412); checking in Python and then saving leaves a race window. `Last-Modified` / `If-Unmodified-Since` work the same way but with one-second granularity, which is too coarse for busy rows. See [ACID and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}) and [Locking]({{ '/postgresql/locking/' | relative_url }}) for the database side.

## In practice

### A runnable model of merge patch and If-Match

```python
import hashlib
import json


def merge_patch(target, patch):
    """RFC 7396 JSON Merge Patch. Returns a new value; inputs untouched."""
    if not isinstance(patch, dict):
        return patch                         # scalars and arrays replace
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)            # null means delete
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def etag(resource):
    body = json.dumps(resource, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'


class PreconditionRequired(Exception):       # maps to 428
    pass


class PreconditionFailed(Exception):         # maps to 412
    pass


def conditional_patch(store, key, patch, if_match):
    # In a database the compare and the write are one UPDATE ... WHERE.
    if if_match is None:
        raise PreconditionRequired()
    current = store[key]
    if if_match != etag(current):
        raise PreconditionFailed()
    store[key] = merge_patch(current, patch)
    return store[key], etag(store[key])


if __name__ == "__main__":
    order = {
        "status": "draft",
        "note": "leave at door",
        "tags": ["gift", "fragile"],
        "shipping": dict(city="Pune", zip="411001"),
    }
    patched = merge_patch(order, {
        "status": "placed",
        "note": None,
        "tags": ["gift"],
        "shipping": dict(zip="411045"),
    })
    assert patched["status"] == "placed"
    assert "note" not in patched                   # null deleted it
    assert patched["tags"] == ["gift"]             # array replaced whole
    assert patched["shipping"] == dict(city="Pune", zip="411045")
    assert order["status"] == "draft"              # input not mutated

    store = {"ord_1": order}
    alice_tag = bob_tag = etag(order)              # both read version 1
    _, new_tag = conditional_patch(store, "ord_1", {"status": "placed"},
                                   alice_tag)
    try:
        conditional_patch(store, "ord_1", {"status": "canceled"}, bob_tag)
        raise AssertionError("lost update was not detected")
    except PreconditionFailed:
        pass
    try:
        conditional_patch(store, "ord_1", {"note": "x"}, None)
        raise AssertionError("write without If-Match was accepted")
    except PreconditionRequired:
        pass
    assert new_tag != bob_tag and store["ord_1"]["status"] == "placed"
    print("merge patch and If-Match checks passed")
```

### DRF: filtering, sorting and an ETag-guarded PATCH

Illustrative (Django REST framework 3.14+, django-filter). The ETag comes from an integer `version` column, and the compare-and-set happens in a single `UPDATE`:

```python
from django.db.models import F
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.parsers import JSONParser
from rest_framework.response import Response


class MergePatchParser(JSONParser):
    media_type = "application/merge-patch+json"


def order_etag(order):
    return f'"{order.version}"'


class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = OrderSerializer
    parser_classes = [JSONParser, MergePatchParser]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["status", "customer_id"]     # allowlist
    ordering_fields = ["created_at", "total_cents"]  # allowlist
    ordering = ["-created_at", "-id"]                # unique tie-breaker

    def get_queryset(self):
        return Order.objects.filter(tenant=self.request.tenant)

    def retrieve(self, request, *args, **kwargs):
        order = self.get_object()
        return Response(self.get_serializer(order).data,
                        headers={"ETag": order_etag(order)})

    def partial_update(self, request, *args, **kwargs):
        if_match = request.headers.get("If-Match")
        if not if_match:
            return Response({"detail": "If-Match header is required."},
                            status=status.HTTP_428_PRECONDITION_REQUIRED)
        order = self.get_object()                    # 404 across tenants
        if if_match != order_etag(order):
            return Response(status=status.HTTP_412_PRECONDITION_FAILED)
        serializer = self.get_serializer(order, data=request.data,
                                         partial=True)
        serializer.is_valid(raise_exception=True)
        updated = Order.objects.filter(
            pk=order.pk, version=order.version,
        ).update(**serializer.validated_data, version=F("version") + 1)
        if updated == 0:                             # lost the race
            return Response(status=status.HTTP_412_PRECONDITION_FAILED)
        order.refresh_from_db()
        return Response(self.get_serializer(order).data,
                        headers={"ETag": order_etag(order)})
```

`.update(**validated_data)` handles plain columns only; relations and nested writes need a `select_for_update()` inside a transaction instead.

### DRF: a 202 long-running export

Illustrative, with Celery. `transaction.on_commit` ensures the worker never sees an operation row that was rolled back:

```python
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView


class ExportView(APIView):
    def post(self, request):
        op = Operation.objects.create(
            tenant=request.tenant, kind="orders_export", status="pending",
            params=request.data,
        )
        transaction.on_commit(lambda: run_export.delay(str(op.id)))
        return Response(
            {"id": str(op.id), "status": op.status},
            status=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/v1/operations/{op.id}",
                     "Retry-After": "5"},
        )
```

## Pitfalls

- **Errors with `200`.** Retries, alerts and SDKs stop working. Use the status code; put details in the body.
- **Weak ETags from compression.** nginx turns a strong ETag into a weak one when it gzips a response on the fly, and weak ETags never satisfy `If-Match`. Symptom: every conditional write returns 412. Generate the ETag from a version column and compare it in the app.
- **Check-then-write concurrency.** Comparing the ETag in Python and then calling `save()` still loses updates under load. Compare in the `UPDATE`.
- **Ambiguous PATCH null.** Clients send `null` meaning "clear" and the server ignores it, or the reverse. Pick merge patch semantics and document them.
- **Unbounded collections and bulk endpoints.** No max page size, no max batch size, no timeout. One client takes down the database.
- **Verbs in URLs** (`/getOrders`) and one `POST /api` for everything: no caching, no idempotency semantics, no standard tooling.

## Interview questions

**PUT vs PATCH vs POST?** `PUT` replaces the whole resource at a known URL and is idempotent. `PATCH` applies a partial change; merge patch is idempotent, JSON Patch is not in general. `POST` creates in a collection (server picks the ID) or triggers a process and is not idempotent without an idempotency key.

**Is DELETE still idempotent if the second call returns 404?** Yes. Idempotency is about server state, not identical responses. After one or ten calls the resource is gone.

**How do you prevent two users overwriting each other's edits?** Optimistic concurrency: return an `ETag` (from a version column), require `If-Match` on writes (`428` if missing), and do the compare-and-set in one `UPDATE ... WHERE version = $v`. Zero rows updated means `412`; the client re-reads and re-applies.

**How do you design an endpoint for a 10-minute report?** `POST` returns `202 Accepted` with `Location` pointing at an operation resource and `Retry-After`. A worker updates status and progress. The client polls or receives a webhook. An idempotency key on the `POST` prevents duplicate jobs on retry.

**401 vs 403, and 400 vs 422?** `401` means not authenticated (no or bad credentials); `403` means authenticated but not allowed. `400` is a request the server cannot parse or that uses unknown parameters; `422` is well-formed but fails validation.

**How would you design bulk create?** A batch endpoint with a hard item cap, a choice between atomic and partial success (per-item results with `207` or `200`), client-supplied external IDs for retry safety, rate limiting by item count, and a switch to an async job above a size threshold.

## Further reading

- Roy Fielding, *Architectural Styles and the Design of Network-based Software Architectures* (dissertation, 2000), chapter 5.
- RFC 9110, *HTTP Semantics*: methods, status codes, conditional requests.
- RFC 7396 (*JSON Merge Patch*) and RFC 6902 (*JavaScript Object Notation (JSON) Patch*).
- Google API Improvement Proposals (AIP), especially AIP-136 (custom methods), AIP-151 (long-running operations) and AIP-160 (filtering).
- Django REST framework documentation: viewsets, parsers, filtering.
