---
title: Pagination and versioning
section: networking
order: 4
summary: Offset vs cursor (keyset) pagination with the SQL behind each, plus how to version an API and deprecate old versions without breaking clients.
---

Two API decisions are expensive to change later: how clients page through collections and how you evolve the contract. Offset pagination is easy to ship and degrades with table size; keyset pagination stays fast at any depth. Versioning is how you change response shapes without breaking a mobile app that won't update for six months.

## How it works

### Offset pagination

```sql
SELECT id, title, created_at
FROM tasks
WHERE tenant_id = 7
ORDER BY created_at DESC, id DESC
LIMIT 50 OFFSET 100000;
```

Postgres has to produce and **throw away** 100,000 rows before returning 50. Cost grows linearly with the offset: page 1 takes 1 ms, page 2000 might take 800 ms. Plus `COUNT(*)` for "total pages" is a full scan of the matching rows on every request.

Correctness issue too: if a row is inserted at the top between page 1 and page 2, every row shifts down and the client sees one row twice. Deletes make it skip rows.

### Keyset (cursor) pagination

Remember where you stopped and ask for rows after it:

```sql
SELECT id, title, created_at
FROM tasks
WHERE tenant_id = 7
  AND (created_at, id) < ('2026-11-01 10:00:00+00', 98123)
ORDER BY created_at DESC, id DESC
LIMIT 51;
```

With an index on `(tenant_id, created_at DESC, id DESC)`, Postgres seeks straight to the position and reads 51 rows. Page 1 and page 2000 cost the same. Inserts and deletes elsewhere don't cause duplicates or gaps.

Key details:

- The sort key must be **unique and total**. `created_at` alone has ties, so add `id` as a tiebreaker.
- Row value comparison `(a, b) < (x, y)` works in Postgres and uses the composite index when directions match.
- Fetch `LIMIT n + 1` to know whether a next page exists without a count.
- The cursor is an **opaque** token (base64 of the last row's sort values). Clients must not build it themselves, so you can change its contents.

| | Offset | Cursor / keyset |
|---|---|---|
| Deep page cost | `O(offset + limit)` | `O(limit)` with index |
| Jump to page 37 | yes | no |
| Total count | easy (but expensive) | usually omitted |
| Stable under inserts | no | yes |
| Arbitrary sort fields | easy | each needs an index and a tiebreaker |
| Best for | admin tables, small sets | feeds, sync, APIs, infinite scroll |

## In practice

### Cursor pagination in DRF

```python
from rest_framework.pagination import CursorPagination

class CreatedCursorPagination(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = "limit"
    ordering = ("-created_at", "-id")
```

```sql
CREATE INDEX CONCURRENTLY tasks_tenant_created_id_idx
    ON tasks (tenant_id, created_at DESC, id DESC);
```

Response:

```json
{
  "data": [ {"id": "t_98122", "title": "..."} ],
  "next_cursor": "cD0yMDI2LTExLTAxKzEwJTNBMDA",
  "has_more": true
}
```

### Hand-rolled keyset for a sync or export job

```python
import base64, json
from datetime import datetime
from django.db.models import Q

def encode(created_at, pk):
    raw = json.dumps([created_at.isoformat(), pk]).encode()
    return base64.urlsafe_b64encode(raw).decode()

def decode(cursor):
    ts, pk = json.loads(base64.urlsafe_b64decode(cursor))
    return datetime.fromisoformat(ts), pk

def page(tenant, cursor=None, limit=50):
    qs = Task.objects.filter(tenant=tenant).order_by("-created_at", "-id")
    if cursor:
        ts, pk = decode(cursor)
        qs = qs.filter(Q(created_at__lt=ts) | Q(created_at=ts, id__lt=pk))
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode(rows[-1].created_at, rows[-1].id) if has_more else None
    return rows, nxt
```

The `OR` form is what the ORM produces; Postgres handles it well with the composite index, but check `EXPLAIN` on large tables. The same loop is the right way to iterate a big table in a Celery task; never `OFFSET` through millions of rows.

### Counting cheaply

If the UI really needs "about 12,400 results", cap or estimate:

```sql
-- capped count: stop scanning at 10,001
SELECT count(*) FROM (
  SELECT 1 FROM tasks WHERE tenant_id = 7 LIMIT 10001
) t;
```

Show "10,000+" when the cap is hit.

### Versioning strategies

| Strategy | Example | Pros | Cons |
|---|---|---|---|
| URL path | `/v1/projects` | Obvious, easy routing, cacheable | Coarse; whole API bumps together |
| Header | `Accept: application/vnd.acme.v2+json` | Clean URLs | Harder to test, invisible in logs |
| Query param | `?version=2` | Easy | Easy to forget, cache key noise |
| Date-based | `Api-Version: 2026-10-01` | Fine-grained, per-account pinning | Needs a transformation layer |

URL versioning (`/v1/`) is the pragmatic default. Date-based versions (Stripe style) pin each account to the version it signed up with and run response transformers for older versions; powerful but real engineering effort.

DRF supports these directly:

```python
REST_FRAMEWORK = {
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": ["v1", "v2"],
    "DEFAULT_VERSION": "v1",
}

class ProjectViewSet(viewsets.ModelViewSet):
    def get_serializer_class(self):
        return ProjectSerializerV2 if self.request.version == "v2" else ProjectSerializerV1
```

### What counts as breaking

Non-breaking (ship without a new version): adding an endpoint, adding an optional request field, adding a response field, adding an enum value **if** clients were told to tolerate unknown values.

Breaking: removing or renaming a field, changing a type (`"id": 42` to `"id": "p_42"`), making an optional field required, changing default sorting or pagination, changing error codes, tightening validation.

### Deprecation

1. Announce with a date. Add headers on the old version:

```http
Deprecation: @1767225600
Sunset: Thu, 01 Jan 2027 00:00:00 GMT
Link: <https://docs.example.com/migrate-v2>; rel="deprecation"
```

2. Log usage of deprecated versions and fields per API client and tenant, so you can contact the stragglers.
3. Run both versions side by side. Keep one service and one database; versions differ at the serializer and view edge.
4. Brownouts: return errors for short windows before the sunset so ignored integrations surface.
5. Remove, and return `410 Gone` with a migration link.

## Pitfalls

- **Keyset without a tiebreaker.** Rows with equal `created_at` get skipped or duplicated at page boundaries.
- **Index direction mismatch.** `ORDER BY created_at DESC, id ASC` can't use an index on `(created_at DESC, id DESC)` efficiently. Keep directions consistent.
- **Unbounded `page_size`.** Always enforce `max_page_size`.
- **Counting on every request.** `COUNT(*)` on a large filtered set can cost more than the page itself.
- **Leaking cursor structure.** Clients start constructing cursors and you can't change the format.
- **Versioning the database.** Versions belong at the API edge; one schema, multiple representations.
- **Mobile apps.** Old app builds live for months. Treat any shipped client as a version you must support until its usage is near zero.

## Rules of thumb

- Default to cursor pagination for public and high-volume endpoints; offset only for small admin lists.
- Sort key = business column + unique id, with a matching composite index that starts with `tenant_id`.
- Fetch `limit + 1`, return `has_more` and an opaque `next_cursor`.
- `/v1/` in the path, additive changes within a version, breaking changes only in a new one.
- Deprecate with headers, usage metrics, a published date and a brownout.

## Interview angle

- **"Why is OFFSET slow?"** The database must scan and discard all skipped rows, so cost grows with page depth, and concurrent inserts shift rows between pages.
- **"How does cursor pagination work?"** Order by a unique key, remember the last key, query `WHERE key < last ORDER BY key LIMIT n` using an index. Constant cost per page, stable under writes, no random page jumps.
- **"How do you introduce a breaking change?"** New version alongside the old, migration guide, deprecation and sunset headers, per-client usage tracking, brownouts, then removal.
