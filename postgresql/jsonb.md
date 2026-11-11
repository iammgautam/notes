---
title: JSONB in practice
section: postgresql
order: 10
summary: JSONB operators and SQL/JSON path, how to index with GIN jsonb_path_ops or expression B-trees, and when a JSON key should become a real column.
---

JSONB lets a relational schema absorb variable data: per-tenant custom fields, webhook payloads, event properties, LLM tool-call arguments. It is fast, indexable and well supported. It is also the easiest way to build a schema nobody can reason about, with no constraints, poor statistics and queries that can't use indexes. The skill is knowing which keys belong in JSONB and which belong in columns.

## How it works

`jsonb` stores a decomposed binary representation: keys are sorted and deduplicated (last one wins), whitespace is dropped, and lookups by key don't reparse text. `json` stores the original text and reparses on every access; use it only if you must preserve key order or duplicates.

Storage details that matter:

- Values over ~2 KB are compressed and moved out of line by **TOAST**. Reading any key from a TOASTed document de-toasts the whole value. Big documents make every key access expensive.
- Updating one key rewrites the whole document as a new row version. A 50 KB JSONB column updated per request generates a lot of WAL and bloat.
- The planner has **no statistics on keys inside JSONB**. Predicates on `properties->>'plan'` get default selectivity estimates (often 0.5% for equality) unless you create an expression index, which collects stats on that expression.

### Operators

| Operator | Meaning | Example |
|---|---|---|
| `->` | Get field/element as `jsonb` | `payload->'customer'` |
| `->>` | Get field/element as `text` | `payload->>'email'` |
| `#>` / `#>>` | Get by path | `payload #>> '{customer,address,city}'` |
| `@>` | Contains | `props @> '{"plan":"pro"}'` |
| `<@` | Is contained by | |
| `?` | Top-level key exists | `props ? 'trial_ends_at'` |
| `?&` | All keys exist | `props ?& array['a','b']` |
| `-` / `#-` | Remove key / path | `props - 'secret'` |
| `@?` / `@@` | SQL/JSON path exists / predicate | `props @@ '$.score > 80'` |

There's also `?|` (any key exists) and `||` (concatenate / shallow merge). Subscripting works since PG 14: `props['plan']`, including in `UPDATE ... SET props['plan'] = '"pro"'`.

Functions you'll use: `jsonb_build_object`, `jsonb_agg`, `jsonb_set`, `jsonb_strip_nulls`, `jsonb_each`, `jsonb_array_elements`, `jsonb_path_query`. PG 16 added `IS JSON` predicates; PG 17 added `JSON_TABLE`, `JSON_VALUE`, `JSON_QUERY` and `JSON_EXISTS` from the SQL standard.

## In practice

### Per-tenant custom fields

```sql
CREATE TABLE contacts (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   bigint NOT NULL,
  email       text NOT NULL,
  name        text,
  custom      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT custom_is_object CHECK (jsonb_typeof(custom) = 'object')
);
```

Filter on a tenant's custom field with containment, which can use GIN:

```sql
SELECT id, email FROM contacts
WHERE tenant_id = 42
  AND custom @> '{"region": "emea", "tier": "gold"}';
```

### Indexing choices

GIN with `jsonb_path_ops`, for containment over arbitrary keys:

```sql
CREATE INDEX CONCURRENTLY contacts_custom_gin
  ON contacts USING gin (custom jsonb_path_ops);
```

- `jsonb_path_ops` hashes each full path-to-value, so it's smaller and faster for `@>` but can't answer `?` (key exists).
- Default `jsonb_ops` indexes keys and values separately; supports `?`, `?&`, but is larger and less selective for nested containment.

Expression B-tree, for one hot key with equality, range or sort:

```sql
CREATE INDEX CONCURRENTLY contacts_tenant_score_idx
  ON contacts (tenant_id, ((custom->>'score')::int));

SELECT id FROM contacts
WHERE tenant_id = 42 AND (custom->>'score')::int > 80
ORDER BY (custom->>'score')::int DESC LIMIT 20;
```

GIN can't do ranges or sorting. If you need `> 80` or `ORDER BY`, it's an expression index or a column.

> **Gotcha:** `custom->>'region' = 'emea'` doesn't use a GIN index. Only operators in the opclass (`@>`, `?`, `@@`, `@?`) do. Rewrite as `custom @> '{"region":"emea"}'`.

### Updating safely

```sql
-- Set one key, creating it if missing
UPDATE contacts
SET custom = jsonb_set(custom, '{tier}', '"platinum"', true)
WHERE id = $1 AND tenant_id = $2;

-- Shallow merge from a request body
UPDATE contacts SET custom = custom || $1::jsonb WHERE id = $2;
```

`jsonb_set` with a `NULL` new value returns `NULL` and wipes the column. Use `jsonb_set_lax` (PG 13+) or `COALESCE`.

### Django

```python
from django.db.models import IntegerField
from django.db.models.fields.json import KT
from django.db.models.functions import Cast

Contact.objects.filter(tenant_id=42, custom__contains={"tier": "gold"})  # @>
Contact.objects.filter(tenant_id=42, custom__has_key="trial_ends_at")    # ?
Contact.objects.annotate(score=Cast(KT("custom__score"), IntegerField())) \
               .filter(score__gt=80)
```

`custom__region="emea"` generates `custom -> 'region' = '"emea"'::jsonb`, which neither GIN nor a `->>` expression index serves. Use `__contains` for GIN, or match the expression index exactly with `KT()`.

### Expanding JSON to rows

```sql
-- PG 17 JSON_TABLE: turn webhook line items into rows
SELECT w.id, li.*
FROM webhooks w,
     JSON_TABLE(w.payload, '$.items[*]'
       COLUMNS (sku text PATH '$.sku',
                qty int  PATH '$.quantity',
                price numeric PATH '$.price')) AS li
WHERE w.tenant_id = 42;
```

## When to promote a key to a column

Move a JSONB key to a real column when any of these is true:

- **It's in most rows** and every query reads it. Columns are smaller (no repeated key names) and faster.
- **You filter, join or sort on it** regularly. Columns get real statistics and simple B-tree indexes.
- **It needs a constraint**: `NOT NULL`, `CHECK`, foreign key, uniqueness.
- **It's updated often**, especially if the document is large.
- **Aggregations** run over it in analytics queries.

Keep in JSONB: genuinely sparse or tenant-defined attributes, raw external payloads you store for audit/replay, and data whose shape you don't control.

A generated column is a cheap middle ground; it's indexable, has statistics and stays in sync:

```sql
ALTER TABLE events
  ADD COLUMN plan text GENERATED ALWAYS AS (properties->>'plan') STORED;
```

Adding a `STORED` generated column rewrites the table under `ACCESS EXCLUSIVE`. On a large table, add a nullable column, backfill in batches, and keep it in sync from the app or a trigger instead. (PG 18 adds virtual generated columns, which avoid the rewrite but can't be indexed.)

## Pitfalls

- **Bad row estimates.** No stats inside JSONB means nested loops on predicates that match millions of rows. Expression indexes or extended statistics on expressions (PG 14+) fix it.
- **TOAST amplification.** Selecting `payload->>'id'` from 100 KB documents reads 100 KB per row. Extract hot keys.
- **Write amplification.** Incrementing a counter inside JSONB rewrites the whole value.
- **Type drift.** `"score": "80"` vs `"score": 80`. The cast `(custom->>'score')::int` throws on `"n/a"` and breaks the whole query. Validate at write time, with `CHECK` or app-level schemas.
- **Numbers.** JSONB numbers are `numeric`; very large integers from JavaScript clients may already have lost precision before they arrive.
- **Default `jsonb_ops` everywhere.** Bigger than needed if you only use `@>`.

## Rules of thumb

- Columns for anything you constrain, join, sort, or read in most queries.
- JSONB for sparse, tenant-defined or externally shaped data.
- `@>` plus GIN `jsonb_path_ops` for flexible filtering; expression B-tree for one hot key.
- Add a `CHECK (jsonb_typeof(col) = 'object')` at minimum.
- Keep documents small; split large blobs into their own table if they're rarely read.

## Interview angle

**Q: `json` vs `jsonb`?**
`jsonb` is parsed binary: faster to query, indexable, dedupes keys, no key order. `json` preserves the exact text and reparses on each access.

**Q: How do you index JSONB?**
GIN (`jsonb_path_ops` for containment, `jsonb_ops` for key existence) for flexible queries; expression B-tree on `(col->>'key')` for equality, ranges and sorting on a specific key.

**Q: When do you move a JSON field to a column?**
When it's queried, constrained, joined, sorted, or present in most rows. Columns get statistics, constraints and cheaper storage.
