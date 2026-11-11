---
title: REST API design
section: networking
order: 3
summary: Design resource URLs, error formats, filtering and partial updates that stay consistent across a multi-tenant API and its clients.
---

A REST API is a long-lived contract. Your Vue frontend, mobile apps, partner integrations and other services will hard-code its shape, and every inconsistency becomes a special case in someone else's code. Good design is mostly boring consistency: predictable URLs, one error format, one pagination style, one way to filter.

## How it works

REST models your domain as **resources** identified by URLs and manipulated with a small set of HTTP methods. The server is stateless between requests: each request carries its own auth and context. Representations (usually JSON) are what move over the wire, not your database rows.

The decisions that matter:

1. What the resources are and how they nest.
2. How clients find, filter and page through them.
3. How writes and partial updates behave.
4. How errors look.
5. How it all stays consistent as the API grows.

## In practice

### Resources and naming

```http
GET    /v1/projects                     list
POST   /v1/projects                     create
GET    /v1/projects/{id}                read
PATCH  /v1/projects/{id}                partial update
DELETE /v1/projects/{id}                delete
GET    /v1/projects/{id}/members        sub-collection
POST   /v1/projects/{id}/archive        action (not CRUD)
```

- Plural nouns, lowercase, hyphenated: `/v1/billing-accounts`.
- Nest at most one level. `/projects/{id}/members` is fine; `/orgs/{o}/projects/{p}/tasks/{t}/comments` is not. Give deep resources their own top-level URL and filter: `/v1/comments?task_id=...`.
- Actions that don't map to CRUD get a verb sub-resource with POST: `/archive`, `/send`, `/retry`. Don't bend PATCH into a remote procedure call.
- Use opaque or UUID ids in public APIs. Sequential integers leak volume and invite enumeration.

### Tenant scoping

Derive the tenant from the credential, not from the URL or body. If the URL contains a tenant id, verify it matches the token.

```python
class TenantScopedViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        return super().get_queryset().filter(tenant=self.request.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant, created_by=self.request.user)
```

Filtering in `get_queryset` means a missing object and another tenant's object both return 404.

### One error format

Pick one shape and use it everywhere, including 500s from middleware. RFC 9457 (Problem Details) is a reasonable standard:

```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation failed",
  "status": 422,
  "detail": "2 fields are invalid.",
  "code": "validation_error",
  "request_id": "req_8f2a1c",
  "errors": [
    {"field": "email", "code": "invalid", "message": "Enter a valid email."},
    {"field": "seats", "code": "min_value", "message": "Must be at least 1."}
  ]
}
```

- A stable machine-readable `code`. Clients branch on codes, never on message text.
- A `request_id` that also appears in your logs and Sentry, so support tickets are traceable.
- Never leak stack traces or SQL in the body.

In DRF, wire this through `EXCEPTION_HANDLER`:

```python
from rest_framework.views import exception_handler

def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None  # unhandled -> 500 path, also formatted by middleware
    request = context["request"]
    response.data = {
        "status": response.status_code,
        "code": getattr(exc, "default_code", "error"),
        "detail": response.data if isinstance(response.data, str) else None,
        "errors": response.data if isinstance(response.data, dict) else None,
        "request_id": getattr(request, "request_id", None),
    }
    return response
```

### Filtering, sorting, searching

```http
GET /v1/tasks?status=open&assignee_id=u_12&created_after=2026-01-01
GET /v1/tasks?sort=-created_at,priority
GET /v1/tasks?q=invoice
GET /v1/tasks?fields=id,title,status
```

- Whitelist filterable and sortable fields. Every allowed sort field needs an index, or you have built a DoS endpoint. `django-filter` with an explicit `FilterSet` and DRF `OrderingFilter.ordering_fields` give you this.
- Use one convention for ranges (`_after`/`_before` or `[gte]`) across the API.
- Full-text search (`q=`) is a separate concern; back it with Postgres `tsvector` and a GIN index, not `icontains`.
- Pagination: see [Pagination and versioning]({{ '/networking/pagination-and-versioning/' | relative_url }}).

### Partial updates

`PATCH` with a JSON merge-style body is what most clients expect: fields present are updated, fields absent are untouched, `null` explicitly clears.

```http
PATCH /v1/projects/p_42
Content-Type: application/json
If-Match: "v7"

{"name": "Q4 launch", "due_date": null}
```

```python
serializer = ProjectSerializer(project, data=request.data, partial=True)
serializer.is_valid(raise_exception=True)
serializer.save()
```

- `partial=True` skips required-field checks for missing fields.
- Distinguish "absent" from "null" in your serializer; DRF does this, hand-rolled code often doesn't.
- Protect against lost updates with `If-Match` + ETag or a `version` field and return `412`/`409` on mismatch.
- Read-only and server-owned fields (`id`, `tenant`, `created_at`) must be `read_only_fields`, or clients can overwrite them (mass assignment).

### Response shape

- Return the full resource after create and update so clients don't need a follow-up GET.
- Timestamps in ISO 8601 UTC: `"2026-11-11T09:30:00Z"`.
- Money as integer minor units plus currency: `{"amount": 129900, "currency": "INR"}`.
- Enums as lowercase strings, never integers.
- One field-casing convention (`snake_case` fits Django and Python clients).
- Collections wrapped in an object (`{"data": [...], "next_cursor": "..."}`), so you can add metadata without breaking clients.

### Long-running work

Return `202 Accepted` with a job resource, process in Celery, let clients poll or subscribe:

```http
POST /v1/exports           -> 202  Location: /v1/exports/exp_9
GET  /v1/exports/exp_9     -> 200  {"status": "running", "progress": 0.4}
GET  /v1/exports/exp_9     -> 200  {"status": "done", "download_url": "..."}
```

## Pitfalls

- **Exposing the ORM.** A serializer with `fields = "__all__"` leaks internal columns and couples your API to your schema. List fields explicitly.
- **N+1 in nested serializers.** Every nested object triggers a query per row. Use `select_related` / `prefetch_related` in `get_queryset` and watch query counts in tests (`assertNumQueries`).
- **Inconsistent errors.** Validation errors in one shape, auth errors in another, 500s as HTML. Clients end up with three parsers.
- **Unbounded lists.** A list endpoint without pagination works until a tenant has 200k rows.
- **Breaking changes without versioning.** Renaming a field breaks deployed mobile apps for months.
- **Trusting tenant ids from the client.** Always scope from the authenticated principal.

## Checklist

- Plural nouns, shallow nesting, verb sub-resources for actions.
- Tenant from the credential, every queryset scoped.
- One error format with `code` and `request_id`.
- Whitelisted, indexed filters and sorts.
- Paginated collections, wrapped in an object.
- PATCH as partial update, `If-Match` for concurrency, explicit read-only fields.
- 202 + job resource for slow work.
- An OpenAPI schema generated from code (`drf-spectacular`) and checked in CI.

## Interview angle

- **"Design the API for X."** Name resources and URLs, methods and status codes, request and response bodies, pagination, auth, error format, idempotency for creates, versioning. Interviewers mostly check consistency and whether you thought about failure.
- **"PUT vs PATCH?"** PUT replaces the whole resource and is idempotent; PATCH applies a partial change. Most JSON APIs use PATCH for edits.
- **"How do you prevent one tenant reading another's data?"** Derive tenant from auth, scope every queryset in a base class, return 404 for foreign ids, add tests that try cross-tenant access, and optionally Postgres row-level security as a second line.
