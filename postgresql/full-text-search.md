---
title: Full-text search
section: postgresql
order: 11
summary: Build search on tsvector with generated columns and GIN, rank and highlight results, and know when trigram or an external engine fits better.
---

For most SaaS search boxes (tickets, documents, contacts, knowledge-base articles) Postgres full-text search is good enough and has one big advantage: results respect your transactions, tenant filters and permissions with no sync pipeline. You outgrow it when you need typo tolerance at scale, heavy faceting, or multi-language relevance tuning; until then, it's one GIN index away.

## How it works

Full-text search turns text into a `tsvector` of normalized **lexemes** and queries into a `tsquery`, then matches with `@@`.

```sql
SELECT to_tsvector('english', 'The workers were retrying failed jobs');
-- 'fail':5 'job':6 'retri':4 'worker':2

SELECT to_tsquery('english', 'retry & job');
-- 'retri' & 'job'
```

The pipeline:

1. **Parser** splits text into tokens (words, numbers, emails, URLs, hyphenated words).
2. **Dictionaries** (from the text search configuration, e.g. `english`) drop stop words (`the`, `were`) and stem (`retrying` to `retri`).
3. The result keeps lexemes, positions, and optional **weights** `A`–`D`.

Query constructors:

| Function | Input | Use for |
|---|---|---|
| `to_tsquery` | `'retry & !failed'` syntax | Trusted, built queries; errors on bad syntax |
| `plainto_tsquery` | Plain text, ANDs all words | Simple search boxes |
| `phraseto_tsquery` | Plain text, words must be adjacent | Phrase search |
| `websearch_to_tsquery` | `"exact phrase" -exclude or` | User input; never throws |

For a search box, use `websearch_to_tsquery`. It accepts quotes, `-`, and `or`, and doesn't raise syntax errors on stray characters.

### Indexing

A GIN index on the `tsvector` maps each lexeme to the rows containing it. The cleanest way to keep the vector in sync is a **stored generated column** (PG 12+):

```sql
ALTER TABLE articles ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(body,  '')), 'B')
  ) STORED;

CREATE INDEX CONCURRENTLY articles_search_gin ON articles USING gin (search);
```

The configuration must be given explicitly (`'english'`), because the one-argument `to_tsvector(text)` depends on `default_text_search_config` and isn't immutable, so it can't be used in generated columns or index expressions.

Alternative: an expression index on `to_tsvector('english', title || ' ' || body)` with no extra column. It saves storage but forces every query to repeat the exact expression, and `ts_rank` must recompute the vector from the heap.

### Ranking

`ts_rank` scores by lexeme frequency and weights; `ts_rank_cd` also uses proximity (cover density). Neither is BM25, and both must read the `tsvector` for each matching row, so ranking cost grows with match count, not result count.

```sql
SELECT id, title, ts_rank(search, q) AS rank
FROM articles, websearch_to_tsquery('english', $1) AS q
WHERE tenant_id = $2 AND search @@ q
ORDER BY rank DESC
LIMIT 20;
```

Normalization flags adjust for length, for example `ts_rank(search, q, 32)` scales into 0–1. Weight multipliers default to `{0.1, 0.2, 0.4, 1.0}` for D, C, B, A.

### Highlighting

```sql
SELECT ts_headline('english', body, q,
         'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=25')
FROM articles, websearch_to_tsquery('english', $1) q
WHERE id = $2;
```

`ts_headline` reparses the original text and is expensive. Run it only on the final page of results, never in the `WHERE` or over the whole match set.

## In practice

### Multi-tenant search query

```sql
WITH q AS (SELECT websearch_to_tsquery('english', $1) AS q)
SELECT a.id, a.title,
       ts_rank(a.search, q.q) AS rank
FROM articles a, q
WHERE a.tenant_id = $2
  AND a.deleted_at IS NULL
  AND a.search @@ q.q
ORDER BY rank DESC, a.id
LIMIT 20;
```

For large tenants, the planner combines a B-tree on `tenant_id` with the GIN index via `BitmapAnd`. To filter by tenant inside the GIN index, use `btree_gin`:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE INDEX CONCURRENTLY articles_tenant_search_gin
  ON articles USING gin (tenant_id, search);
```

### Django

```python
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F

q = SearchQuery(term, search_type="websearch", config="english")
(Article.objects
    .filter(tenant_id=tid, search=q)        # search is a SearchVectorField
    .annotate(rank=SearchRank(F("search"), q))
    .order_by("-rank")[:20])
```

Declare the generated column in a migration with `RunSQL` (or `GeneratedField` on Django 5.0+) and map it as `SearchVectorField`.

### Prefix search and autocomplete

```sql
SELECT to_tsquery('english', 'postg:*');   -- prefix match on lexemes
```

Prefix on stemmed lexemes is unintuitive. For type-ahead on names, use trigram or a `text_pattern_ops` B-tree on `lower(name)`.

## Full-text vs trigram

`pg_trgm` splits strings into 3-character chunks and measures overlap. It answers different questions.

| Need | Full-text (`tsvector`) | Trigram (`pg_trgm`) |
|---|---|---|
| Natural-language words, stemming | Yes | No |
| Typo tolerance (`postgers`) | No | Yes (`%`, `similarity`) |
| Substring (`ILIKE '%voic%'`) | No | Yes |
| Names, SKUs, emails, codes | Poor | Good |
| Ranking by relevance | `ts_rank` | `similarity` score |
| Index | GIN | GIN (filter) or GiST (KNN order) |

```sql
CREATE INDEX CONCURRENTLY contacts_name_trgm
  ON contacts USING gin (name gin_trgm_ops);

SELECT id, name, similarity(name, $1) AS sim
FROM contacts
WHERE tenant_id = $2 AND name % $1     -- pg_trgm.similarity_threshold, 0.3
ORDER BY sim DESC LIMIT 10;
```

A common setup: full-text on long content, trigram on titles and names, merged in the application or with a `UNION` and a combined score. For semantic matches, add [pgvector]({{ '/postgresql/pgvector/' | relative_url }}) and do hybrid ranking.

## Pitfalls

- **Wrong configuration.** Indexing with `'english'` and querying with `'simple'` (or the default config) silently returns nothing.
- **Mixed languages.** Stemming is per configuration. Store the language in a `regconfig` column and build the vector with `to_tsvector(language, ...)`. A `text` column cast with `::regconfig` isn't immutable, so it can't be used in a generated column or index.
- **Ranking huge match sets.** A query matching 500k rows ranks all of them before `LIMIT`. Cap with stricter queries, pre-filter by recency, or rank only the top N by a cheap signal.
- **GIN write cost and pending list.** Bulk imports into a table with a big `tsvector` GIN index are slow; see [GIN, GiST and BRIN]({{ '/postgresql/gin-gist-brin/' | relative_url }}).
- **`tsvector` size limit.** About 1 MB per vector and 256 positions per lexeme. Very long documents should be chunked.
- **Expecting Elasticsearch.** No BM25, no built-in fuzzy, no facets at scale. Extensions like ParadeDB `pg_search` add BM25 if you need it inside Postgres.

## Rules of thumb

- Stored generated `tsvector` column, explicit config, weights for title vs body, GIN index.
- `websearch_to_tsquery` for user input.
- `ts_headline` only on the rows you return.
- Trigram for names, codes and typo tolerance; full-text for prose.
- Move to a dedicated engine when relevance tuning, facets or typo tolerance at scale become product requirements.

## Interview angle

**Q: How does Postgres full-text search work?**
Text is parsed and normalized into a `tsvector` of stemmed lexemes; queries become `tsquery`; `@@` matches. A GIN index maps lexemes to rows.

**Q: Full-text vs `ILIKE '%term%'`?**
`ILIKE` matches substrings without understanding words and needs a trigram index to be fast. Full-text matches word forms (stemming), ignores stop words and supports ranking.

**Q: When would you move search out of Postgres?**
When you need BM25-quality relevance tuning, typo tolerance, facets and aggregations over large indexes, or search load that shouldn't compete with OLTP.
