---
title: Pagination strategies
section: backend
order: 22
phase: "Phase 3 · API design"
summary: Offset vs keyset pagination, opaque signed cursors, page tokens, ties, the cost of total counts, and staying consistent while rows change.
---

Every list endpoint is a pagination design decision, whether you make it or not. The default (`LIMIT 20 OFFSET 40`) works on day one and fails in two predictable ways as data grows: deep pages get linearly slower until a crawler walking page 4,000 shows up on your database CPU graph, and users see duplicated or missing rows whenever data changes between page loads. Keyset pagination with opaque cursors fixes both, at the cost of "jump to page 37".

## Offset and limit

```sql
SELECT id, created_at, title
FROM posts
WHERE tenant_id = 42
ORDER BY created_at DESC, id DESC
LIMIT 20 OFFSET 100000;
```

The database cannot jump to row 100,000. It must produce the first 100,020 rows in order, through an index scan or a sort, and throw away 100,000 of them. Cost is `O(offset + limit)`, so latency grows linearly with page depth. `EXPLAIN ANALYZE` shows it plainly: the `Limit` node's child reports roughly offset + limit actual rows ([EXPLAIN ANALYZE]({{ '/postgresql/explain-analyze/' | relative_url }})).

| Strengths | Weaknesses |
|---|---|
| Trivial to implement in any ORM | Deep pages are slow |
| Jump to any page; "page 3 of 120" UIs | Duplicates and skips when rows change |
| Works with any sort order | Every page re-does the work of the previous ones |

It is fine for small, bounded result sets: admin tables, a tenant's 300 projects, search results where nobody goes past page 10. Cap the maximum offset. Search engines do this; Elasticsearch, for example, rejects `from + size` beyond 10,000 by default (`index.max_result_window`) and points you to `search_after`.

## Keyset (seek) pagination

Instead of counting rows to skip, remember where the last page ended and ask for rows strictly after that position in the sort order:

```sql
-- index matches the ORDER BY, with the tenant as the leading column
CREATE INDEX CONCURRENTLY posts_tenant_created_id
    ON posts (tenant_id, created_at DESC, id DESC);

-- first page
SELECT id, created_at, title FROM posts
WHERE tenant_id = $1
ORDER BY created_at DESC, id DESC
LIMIT 51;                                     -- page size 50, plus one

-- next page: seek past the last row of the previous page
SELECT id, created_at, title FROM posts
WHERE tenant_id = $1
  AND (created_at, id) < ($2, $3)
ORDER BY created_at DESC, id DESC
LIMIT 51;
```

With an index whose columns match the `ORDER BY`, PostgreSQL descends the B-tree straight to `($2, $3)` and reads 51 entries. Page 1 and page 10,000 cost the same: `O(log n + limit)`. Fetching `limit + 1` rows tells you whether another page exists without counting anything. A plain ascending index on `(tenant_id, created_at, id)` also works because B-trees scan backward; see [B-tree indexes]({{ '/postgresql/btree-indexes/' | relative_url }}) and [Indexing strategies]({{ '/backend/indexing-strategies/' | relative_url }}).

Trade-offs: no random access to page N, each supported sort order needs its own index, and "previous page" means flipping the comparison and the sort direction, then reversing the rows in the application. Most feeds, timelines, activity logs, sync endpoints and infinite-scroll UIs never need page N, which is why large public APIs (Slack, Stripe, GitHub's GraphQL connections, DynamoDB's `LastEvaluatedKey`) are cursor-based.

## Seek with ties

The cursor must identify a **unique** position. If you sort by `created_at` alone and seek with `created_at < $last`, every other row sharing the last row's timestamp is skipped; with `<=` they repeat. Bulk imports and batch jobs create thousands of rows with identical timestamps, so this is not hypothetical.

The fix is a unique tie-breaker appended to the sort, almost always the primary key, compared as a **row value**: `(created_at, id) < ($2, $3)` means "earlier timestamp, or same timestamp and smaller id". PostgreSQL evaluates row comparisons lexicographically and can use them directly as B-tree index conditions.

Row comparison only works when every column sorts in the same direction. For mixed directions, expand it by hand and keep a redundant range condition so the index still gets a bound:

```sql
-- ORDER BY priority DESC, id ASC, cursor = (last_priority, last_id)
WHERE tenant_id = $1
  AND priority <= $2                          -- index range bound
  AND (priority < $2 OR id > $3)
ORDER BY priority DESC, id ASC
LIMIT 51;
-- index: (tenant_id, priority DESC, id ASC)
```

NULLs break seeks silently: `(NULL, 7) < ($2, $3)` evaluates to NULL, so those rows never appear. Make sort columns `NOT NULL`, or handle the NULL group as an explicit extra branch with `NULLS LAST` in both the index and the query.

## Opaque cursors

Do not expose `?after_created_at=...&after_id=...`. Wrap the position in an opaque token:

- **Freedom to change**: you can add a sort column, switch strategies, or change encoding without breaking clients, because they never parse the token.
- **Bound to the query**: include the filters and sort in the cursor and reject it if the next request's parameters differ. Otherwise a client changes `status=open` to `status=closed` mid-walk and gets nonsense.
- **Tamper-evident**: sign it with HMAC so clients cannot forge positions or fiddle with embedded filters. Signing is not encryption; the payload is base64-readable, so never put anything secret in it. Authorization still comes from the request's credentials, never from the cursor.
- **Versioned**: a `v` field lets you accept old cursors for a while after changing the format.

A runnable encoder/decoder with real SQL (SQLite stands in for PostgreSQL; both support row-value comparison). It also proves the tie problem, the duplicate problem with offsets, and tamper rejection:

```python
import base64
import hashlib
import hmac
import json
import sqlite3

SECRET = b"load-from-settings"      # rotate like any signing key


def encode_cursor(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(SECRET, raw, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(sig + raw).rstrip(b"=").decode()


def decode_cursor(token):
    """Raises ValueError for anything forged, truncated or garbled (-> 400)."""
    blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    sig, raw = blob[:12], blob[12:]
    good = hmac.new(SECRET, raw, hashlib.sha256).digest()[:12]
    if not hmac.compare_digest(sig, good):
        raise ValueError("invalid cursor")
    return json.loads(raw)


FIRST_PAGE = """
    SELECT id, created_at FROM posts WHERE tenant_id = ?
    ORDER BY created_at DESC, id DESC LIMIT ?"""
NEXT_PAGE = """
    SELECT id, created_at FROM posts WHERE tenant_id = ?
      AND (created_at, id) < (?, ?)          -- seek past the last row
    ORDER BY created_at DESC, id DESC LIMIT ?"""


def keyset_page(db, tenant_id, size, cursor=None):
    query = {"tenant": tenant_id, "sort": "-created_at,-id"}
    if cursor is None:
        rows = db.execute(FIRST_PAGE, (tenant_id, size + 1)).fetchall()
    else:
        c = decode_cursor(cursor)
        if c["q"] != query:
            raise ValueError("cursor belongs to a different query")
        rows = db.execute(NEXT_PAGE, (tenant_id, *c["after"], size + 1))
        rows = rows.fetchall()
    has_more = len(rows) > size       # fetched one extra row: no COUNT(*)
    rows = rows[:size]
    nxt = None
    if has_more:
        last_id, last_ts = rows[-1]
        nxt = encode_cursor({"v": 1, "q": query, "after": [last_ts, last_id]})
    return [r[0] for r in rows], nxt


def offset_page(db, tenant_id, size, page):
    rows = db.execute(FIRST_PAGE + " OFFSET ?",
                      (tenant_id, size, page * size)).fetchall()
    return [r[0] for r in rows]


def walk(db, tenant_id, size):
    seen, cursor = [], None
    while True:
        ids, cursor = keyset_page(db, tenant_id, size, cursor)
        seen += ids
        if cursor is None:
            return seen


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, tenant_id INT,"
               " created_at TEXT NOT NULL)")
    db.execute("CREATE INDEX posts_seek ON posts (tenant_id, created_at, id)")
    # 23 rows, timestamps deliberately tied in groups of 4
    db.executemany("INSERT INTO posts VALUES (?, 1, ?)",
                   [(i, f"2026-01-01T00:00:{i // 4:02d}")
                    for i in range(1, 24)])
    db.execute("INSERT INTO posts VALUES (100, 2, '2026-01-01T00:00:59')")
    return db


if __name__ == "__main__":
    db = fresh_db()
    expected = [r[0] for r in db.execute(FIRST_PAGE, (1, 1000))]

    # 1. Keyset with a tie-breaker visits every row exactly once, in order.
    assert walk(db, 1, 5) == expected and len(expected) == 23

    # 2. Seeking on created_at alone silently drops tied rows.
    page1 = expected[:5]
    last_ts = db.execute("SELECT created_at FROM posts WHERE id = ?",
                         (page1[-1],)).fetchone()[0]
    naive = [r[0] for r in db.execute(
        "SELECT id FROM posts WHERE tenant_id = 1 AND created_at < ?"
        " ORDER BY created_at DESC, id DESC LIMIT 5", (last_ts,))]
    assert expected[5:8] == [18, 17, 16]     # share page 1's last timestamp
    assert naive[0] == 15                    # ...and the naive seek skips them

    # 3. A new row arrives between page 1 and page 2.
    off1 = offset_page(db, 1, 5, 0)
    ks1, cur = keyset_page(db, 1, 5)
    db.execute("INSERT INTO posts VALUES (50, 1, '2026-01-01T00:00:30')")
    off2 = offset_page(db, 1, 5, 1)
    ks2, _ = keyset_page(db, 1, 5, cur)
    assert set(off1) & set(off2), "offset shows a row twice"
    assert not set(ks1) & set(ks2), "keyset does not"

    # 4. Deleting the cursor row does not break keyset (it seeks by value).
    db.execute("DELETE FROM posts WHERE id = ?", (ks1[-1],))
    assert keyset_page(db, 1, 5, cur)[0] == ks2

    # 5. Tampered or cross-tenant cursors are rejected.
    raw = base64.urlsafe_b64decode(cur + "=" * (-len(cur) % 4))
    forged = base64.urlsafe_b64encode(raw.replace(b'"tenant":1',
                                                  b'"tenant":2'))
    for bad in (forged.decode().rstrip("="), "not-a-cursor"):
        try:
            keyset_page(db, 1, 5, bad)
            raise AssertionError("bad cursor accepted")
        except ValueError:
            pass
    print("keyset cursor checks passed")
```

Invalid cursors raise `ValueError`, which the API maps to `400` with a clear error code so clients know to restart from the first page.

## Page tokens

Page tokens are the same idea packaged as an API convention. Google's AIP-158 is a widely copied version:

- The request carries `page_size` and `page_token`; the response carries `next_page_token`, which is empty on the last page.
- `page_size` is a maximum. The server may return fewer items, even zero, and still return a token, for example when filtering happens after the fetch. Clients loop until the token is empty, never until a short page.
- A token is only valid with the same other parameters; a mismatch is `INVALID_ARGUMENT` (`400`).
- Tokens may expire, and the docs should say so.

Tokens can also point at **server-side state**: the token is a key into Redis where the server stored a snapshot of result IDs or a search engine's point-in-time handle. That gives a consistent view across pages for expensive queries such as search, at the cost of memory, TTL management, and statefulness. Elasticsearch's `search_after` combined with a point-in-time (PIT) works this way. Slack's Web API (`next_cursor` in `response_metadata`) and DynamoDB (`ExclusiveStartKey` / `LastEvaluatedKey`) are public examples of the stateless kind.

## Total counts and their cost

"Showing 1–50 of 48,213" looks free and is not. In PostgreSQL, `COUNT(*)` must visit every matching row or index entry and check visibility under MVCC; an index-only scan helps only when the visibility map is current ([MVCC and vacuum]({{ '/postgresql/mvcc-and-vacuum/' | relative_url }})). On a large tenant it can cost more than the page query itself, on every request.

| Approach | Accuracy | Cost |
|---|---|---|
| Exact `COUNT(*)` per request | Exact | `O(matching rows)` every time |
| `has_more` via `LIMIT n + 1` | Boolean only | Free |
| Capped count, shown as "10,000+" | Exact up to the cap | `O(cap)` |
| Planner estimate | Rough, can be far off with filters | Near zero |
| Counter table updated by triggers or jobs | Exact or near | Write overhead, hot counter rows |
| Cached count with a TTL | Stale | One full count per TTL |

```sql
-- capped: never scans more than 10,001 rows
SELECT count(*) FROM (
  SELECT 1 FROM posts
  WHERE tenant_id = $1 AND status = 'open'
  LIMIT 10001
) AS capped;

-- whole-table estimate from planner statistics
SELECT reltuples::bigint AS estimate
FROM pg_class WHERE oid = 'public.posts'::regclass;
```

For a filtered estimate, read the top node's `Plan Rows` from `EXPLAIN (FORMAT JSON)`. Make totals opt-in (`?include_total=true`) so the default path stays cheap, and prefer `has_more` for infinite scroll.

## Consistency under inserts and deletes

Pagination spans several requests, and the data changes between them.

```text
newest first, page size 3, rows E D C B A

OFFSET
t0  page 1 (offset 0)    [E D C] B A
t1  F inserted           F E D C B A
t2  page 2 (offset 3)    F E D [C B A]    C shown twice
    (a delete before the window instead skips a row entirely)

KEYSET
t0  page 1               [E D C]          cursor = C
t1  F inserted, D deleted
t2  page 2: rows < C     [B A]            no repeats, no gaps
    F is visible on the next refresh from the top
```

Keyset pages are anchored to values, not positions, so inserts and deletes elsewhere do not shift them, and deleting the cursor's own row is harmless because the seek compares values. Problems that remain:

- **Mutable sort keys.** Sorting by `updated_at` or `score` means a row edited mid-walk moves: ahead of the cursor it is missed, behind it it appears twice. Paginate on immutable keys, or accept it and let clients de-duplicate by ID.
- **Out-of-order commits.** Sync endpoints that page by an increasing sequence (`WHERE seq > $last`) can miss rows: sequence values are assigned at insert but become visible at commit, so a reader may see 105 before 104 commits and then move past it. Serve only rows older than a short safety lag, or use a commit-ordered watermark.
- **True snapshots** across pages need server-side state (stored result IDs, a search PIT). A database cursor held open across HTTP requests ties up a connection and a snapshot and does not survive load balancing.

## Choosing a strategy

| | Offset | Keyset / cursor | Server-side snapshot |
|---|---|---|---|
| Deep page cost | `O(offset)` | `O(log n + limit)` | Lookup |
| Jump to page N | Yes | No | Within the snapshot |
| Under concurrent writes | Duplicates and skips | Stable, no duplicates | Fully consistent |
| Sort orders | Any | Indexed ones only | Any |
| Total count | Natural but expensive | Optional | Known |
| Server state | None | None | Yes, with TTL |
| Fits | Small admin tables | Feeds, APIs, sync, infinite scroll | Search, exports of expensive queries |

## In practice

DRF's built-in `CursorPagination` (illustrative). It encodes the first ordering field's value plus a small offset to step over ties, base64-encoded but unsigned; it requires an unchanging, ideally unique ordering and does not return counts:

```python
from rest_framework.pagination import CursorPagination


class FeedPagination(CursorPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
    ordering = ("-created_at", "-id")


class PostViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PostSerializer
    pagination_class = FeedPagination

    def get_queryset(self):
        return Post.objects.filter(tenant=self.request.tenant)
```

Hand-rolled keyset in the Django ORM, using the expanded form plus a redundant bound because the ORM has no general row-comparison lookup:

```python
from django.db.models import Q


def posts_page(tenant, size, after=None):
    qs = Post.objects.filter(tenant=tenant).order_by("-created_at", "-id")
    if after is not None:
        ts, pk = after                      # from the decoded cursor
        qs = qs.filter(created_at__lte=ts).filter(
            Q(created_at__lt=ts) | Q(created_at=ts, id__lt=pk))
    rows = list(qs[: size + 1])
    return rows[:size], len(rows) > size
```

GraphQL APIs usually expose the same mechanism as Relay connections (`edges`, `cursor`, `pageInfo { hasNextPage endCursor }`); see [GraphQL]({{ '/backend/graphql/' | relative_url }}). The response envelope for REST is typically `{"data": [...], "next_cursor": "...", "has_more": true}`, with the cursor also offered as a `Link: <...>; rel="next"` header ([REST API design]({{ '/backend/rest-api-design/' | relative_url }})).

## Pitfalls

- **No tie-breaker.** Rows duplicate or vanish between pages; tests flake depending on insert timing. Always end the sort with the primary key.
- **Index does not match the query.** Wrong column order, missing tenant prefix, or mixed directions give a `Sort` node over thousands of rows. Check the plan for every supported sort.
- **Crawlers on deep offsets.** Bots walking `?page=5000` burn database CPU. Cap the offset or move to cursors.
- **`COUNT(*)` on every request.** Often the slowest query on the page. Make it optional, capped or estimated.
- **Filtering after the `LIMIT`.** Permission or soft-delete filtering in Python produces short or empty pages. Push filters into SQL, or loop on the token, not the page length.
- **Trusting the cursor.** A cursor carrying `tenant_id` that the server believes is a data leak. Sign it, and still filter by the authenticated tenant.
- **Cursor format changes on deploy.** Every in-flight client breaks at once. Version the payload and accept the previous version for a while.
- **Unbounded `page_size`.** Clamp it server-side.

## Interview questions

**Why is `OFFSET 100000` slow?** The database must generate and discard 100,000 ordered rows before returning any; cost grows linearly with depth. Keyset pagination seeks directly to the position through the index.

**How do you paginate by a non-unique column like `created_at`?** Add a unique tie-breaker to the sort (`created_at DESC, id DESC`), index both, and seek with a row comparison `(created_at, id) < ($ts, $id)`. Without the tie-breaker, rows sharing the boundary timestamp are skipped or repeated.

**How do you show a total for a 100-million-row table?** Usually you do not show an exact one: use `has_more`, a capped count ("10,000+"), a planner estimate, or a maintained counter. Exact counts are opt-in and cached.

**What happens to a cursor if the row it points to is deleted?** Nothing bad with keyset: the cursor holds the row's sort values, and the next query seeks to "after those values" whether or not the row still exists.

**Why make cursors opaque and signed?** So the server can change the implementation, bind the cursor to the original filters and sort, and stop clients from forging positions. Authorization still comes from credentials.

**How do clients jump to page 50 with cursors?** They cannot directly. Offer filters that narrow the range instead (date ranges, search, "jump to date"), or keep offset pagination for small bounded tables.

**How do you paginate results sorted by a field that changes, like score?** Accept some movement and let clients de-duplicate by ID, snapshot the ranking server-side behind a page token, or paginate over an immutable key and sort within the page.

**How do you paginate across shards?** Each shard returns its top `limit` rows after the cursor; a merge step picks the global top `limit`, and the cursor records the last sort key (which is global), not per-shard offsets. Search engines use `search_after` the same way.

## Further reading

- Markus Winand, *SQL Performance Explained* and the companion site *Use The Index, Luke*, chapter on paging through results.
- PostgreSQL documentation: *Row and Array Comparisons* (row constructor comparison).
- Google AIP-158: *Pagination*.
- Django REST framework documentation: *Pagination* (`CursorPagination`).
- *Evolving API Pagination at Slack* (Slack engineering blog).
