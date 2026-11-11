---
date: 2026-11-11 14:00:00 +0530
title: "Why typing 'nort' found nothing, and a cache nobody cleared"
summary: "Two fixes on our spend report cache: prefix search with a GIN index and a 10-word cap, and a has-data check batched into one query."
tags: [postgresql, full-text-search, django, caching]
---

A user opened a spend report, typed `nort` into the search box to find Northwind Supplies, and got an empty table. Northwind was on the first page. Typing `northwind` worked. Typing `north` did not.

The second bug was quieter. When a client started filling one of their custom columns, that column could stay hidden in the report builder for up to a week, then appear on its own, which is the most suspicious kind of fix.

Both bugs lived in the same layer: the cache in front of our spend analytics rollup. I fixed both, and each one had a performance problem hiding behind the visible bug.

## Where these bugs live

Spend analytics lets a procurement team slice the money it paid suppliers by supplier, category, business unit or diversity status. Reports run against a monthly rollup of the client's invoice data, and every generated report is cached as rows: one row per report line, the line stored as JSONB, plus a `tsvector` so the user can search inside the report. (The rollup and the report generator are covered in "The spend report that took 239 seconds, and the index that fixed it".)

Two pieces of that layer matter here:

- **Search inside a report** filters the cached lines with PostgreSQL full-text search.
- **The "has data" check** decides which columns the report builder offers. A client that never fills a custom column should not see it in the dropdown. The check is expensive, so its answers are cached for a week.

## Part 1: search that only matched whole words

### Why "nort" matched nothing

The search used Django's plain `SearchQuery`, which compiles to `plainto_tsquery`. That turns the input into whole, stemmed words (lexemes) and matches them exactly:

```sql
SELECT to_tsvector('english', 'Northwind Supplies Ltd');
-- 'ltd':3 'northwind':1 'suppli':2

SELECT to_tsvector('english', 'Northwind Supplies Ltd')
       @@ plainto_tsquery('english', 'nort');   -- false

SELECT to_tsvector('english', 'Northwind Supplies Ltd')
       @@ to_tsquery('english', 'nort:*');      -- true
```

`nort` is not a lexeme in that document, so it never matches. `nort:*` is a prefix query: it matches any lexeme that starts with `nort`. (`Supplies` became `suppli`. That is the stemmer, and it is why a whole-word search for "supply" still finds it.)

### The second problem: no index at all

While reading the migrations to see how the search column was built, I found it had been created without an index. Every search had to test every line's vector, and the count of matching lines was a plain sequential scan. The cost grew with the number of cached lines, not with the number of matches.

### The fix: prefix terms, a GIN index, and a filter on input

```text
  user types:   nort sup!
                    |
  keep \w+ only     v      ["nort", "sup"]     (operators gone,
                    |                            at most 10 words)
  prefix + AND      v      nort:* & sup:*
                    |
  OR plain query    v      (nort:* & sup:*) | plainto_tsquery(text)
                    |
  GIN on vector     v      bitmap of matching lines, this report only
```

The query builder is small. A simplified version:

```python
import re

MAX_WORDS = 10

def prefix_tsquery(text: str) -> str:
    """'nort sup!' -> 'nort:* & sup:*'. No operator survives."""
    words = re.findall(r"\w+", text)[:MAX_WORDS]
    return " & ".join(f"{w}:*" for w in words)
```

And the SQL it feeds, with the index that makes it fast. `||` between two tsqueries means OR:

```sql
CREATE INDEX cached_line_search_gin
    ON cached_report_line USING gin (search_doc);

SELECT line
  FROM cached_report_line
 WHERE report_id = %(report_id)s
   AND search_doc @@ (to_tsquery(%(prefix)s)
                      || plainto_tsquery(%(text)s))
 ORDER BY line ->> 'supplier'
 LIMIT 25 OFFSET %(offset)s;
```

In Django this is `SearchQuery(prefix, search_type="raw") | SearchQuery(text)`, and blank input skips the filter entirely. Each line of the builder is there for a reason:

- **`\w+` keeps only word characters.** A raw query goes to `to_tsquery` as-is, so `&`, `|`, `!`, `:` and parentheses in user input would be syntax. Bad syntax raises an error, which is a 500. A stray `!` would quietly invert the search. Extracting words removes every operator before anything reaches the database.
- **Every word is a prefix, joined with AND.** `nort sup` finds Northwind Supplies. The vector covers every text value of a line, so the words can match different columns of the same line.
- **The plain query is OR'd back in.** The PostgreSQL parser keeps some tokens whole. An email like `ap@northwind.example` is a single lexeme. `\w+` splits it into `ap`, `northwind` and `example`, and `example:*` matches nothing in that line, so the prefix query alone would miss it. Without the OR, email and URL searches that worked before would have silently broken.
- **The cap.** Ten words. That one has its own section below.

### What the index bought, and what it cost

I measured on a development copy, warm cache, against a report cache with six figures of lines:

| Query | No index | GIN |
|---|---|---|
| Prefix search, first page | 177–192 ms | 2.5–4.5 ms |
| Count of matching lines | 146 ms (seq scan) | 22 ms (bitmap index scan) |
| Count, exact word | 149 ms | 0.04 ms |

Prefix matching is slower than exact matching, because the index has to expand the prefix into every lexeme that starts with it. The worst realistic case I found was a single-letter prefix, `e:*`, which expands to about 1,900 distinct lexemes and still came back in 15–20 ms.

The cost is on the write side. Building the index took 275 ms and about 5.8 MB. Bulk-inserting the lines of a large report went from 1.15 s to 1.71 s, about 48% slower. That extra half second happens once per report generation, and each search saves around 180 ms, so the index pays for itself after about three searches. Report lines are written once and searched many times, so that is an easy trade. I left GIN's `fastupdate` at its default. Turning it off made every rebuild 1.26 s slower to save about 11 ms on the first search after it.

### Why the cap is 10 words

Once the index was in, I tried to break it. The search text is a request parameter, so any logged-in user can send a thousand words. Each `word:*` is a separate prefix expansion in the index, so cost grows with the number of words, and short prefixes are the most expensive kind.

For a 1,000-word input, the old plain query took 13.9 ms. My uncapped prefix version took 1,916 ms, 138 times more work, for a query that returns nothing. Worse, the endpoint evaluates the predicate twice per request, once for the count and once for the page, so one GET cost almost four seconds of database time. With the cap at 10 words, the same input took 32.6 ms.

The cap costs nothing in search quality. Terms are ANDed, so after a few words the result is already exact or empty, and nobody types ten words into a report's search box on purpose. I put the cap in the builder rather than as a length limit on each endpoint, so every caller gets it from one place.

The change shipped with 14 tests: five unit tests for the builder (prefix AND, operator stripping, the cap, blank input, punctuation-only input) and nine endpoint tests against the real column, including partial words, words spread across one line and across two lines, operators, underscores and an email address.

## Part 2: the column that stayed hidden for a week

### What the check cost before

The report builder offers a column only if at least one rollup row has a non-blank value in it. Before my change, that check had four copies in the codebase, and each one worked per column:

- **One cache read per column.** Our cache backend is Django's database cache, so every `cache.get` is itself a SELECT. Two endpoints asked about 46 and 49 columns, so every request made 46 or 49 cache round trips, hit or miss.
- **On a miss, one COUNT per column.** "How many rows have a non-null, non-blank value?" The custom columns have no indexes, so each COUNT read the whole rollup. On a development database with a couple of million rollup rows, just proving that one empty column had no values took a 3.5-second parallel sequential scan.
- **Nothing invalidated the answers.** They were cached for seven days. A load that filled a new column did not touch them, so the column stayed hidden until the cache entry expired. That was the week-long mystery.

```text
  BEFORE: one request, ~50 columns
    for each column:
      cache.get(key) ............. 1 SELECT on the cache table
      on miss: COUNT(non-blank) .. full scan of the rollup
      on miss: cache.set(key) .... a few statements
    => ~50 round trips per request, even when every answer is cached

  AFTER:
    cache.get_many(keys) ......... 1 SELECT
    on any miss: one aggregate ... 1 scan, one FILTER per column
    cache.set_many(misses) ....... only on a miss
```

### One cache round trip, one scan

I replaced the four copies with one function. A simplified version:

```python
from django.core.cache import cache
from django.db.models import Count, Q

WEEK = 7 * 24 * 60 * 60

def flag_key(col: str) -> str:
    return f"colflag:{col}"

def blank(col: str) -> Q:
    return Q(**{f"{col}__isnull": True}) | Q(**{col: ""})

def populated_columns(cols: list[str]) -> set[str]:
    flags = cache.get_many([flag_key(c) for c in cols])
    unknown = [c for c in cols if flag_key(c) not in flags]
    if unknown:
        counts = SpendRollup.objects.aggregate(**{
            f"col_{i}": Count("pk", filter=~blank(c))
            for i, c in enumerate(unknown)
        })
        fresh = {flag_key(c): counts[f"col_{i}"] > 0
                 for i, c in enumerate(unknown)}
        cache.set_many(fresh, timeout=WEEK)
        flags.update(fresh)
    return {c for c in cols if flags[flag_key(c)]}
```

`get_many` is one SELECT for every key. On PostgreSQL the aggregate renders roughly as one `count(...) FILTER (WHERE NOT (col IS NULL OR col = ''))` per column, all computed in a single pass over the table. The aliases are positional (`col_0`, `col_1`) because an annotation cannot share a name with a model field, and a related lookup like `supplier__owner` is not a legal alias.

My first step was swapping each COUNT for EXISTS, which stops at the first matching row. That helps populated columns and does nothing for empty ones, where proving "no rows" still means reading everything. Most clients leave most custom columns empty, so the slow case is the normal case. The aggregate always reads the table once, and that beat the per-column EXISTS easily. Measured cold on that development database:

| Columns asked about | Per-column EXISTS | One aggregate |
|---|---|---|
| 20 custom columns | 6.4 s | 1.7 s |
| 33 renameable columns | 12.2 s | 1.7 s |

The aggregate barely grows with the number of columns. The cost is reading the table, and once you are reading a row, one more FILTER is cheap.

### Count with a filter, not NULLIF

The shorter version, and the one suggested in review, is `Count(NullIf(col, Value("")))`: NULLIF turns blanks into NULL, and COUNT skips NULLs. It breaks on one of the probed fields, a nullable boolean on the supplier reached through a join. NULLIF puts a literal `''` next to a boolean column in SQL, and PostgreSQL rejects it. The filtered count builds the blank check through the ORM, which prepares `''` with each field's own type. For a nullable boolean the comparison becomes `IS NULL`, so the check reduces to "not null", which is the right question for that column. I added a test that asks about a related field and a non-text field, so nobody "simplifies" it back.

### Clearing the flags when the data changes

My first version cleared the flags from the function that wraps the rollup refresh. I then moved it onto a signal that the rollup sends after its refresh transaction commits, so any code path that rebuilds the rollup clears the flags, including ones that call the refresh directly instead of going through that wrapper. It must fire after the commit: a request that recounts before the commit would cache the old answer for another week.

```python
from django.dispatch import Signal, receiver

rollup_rebuilt = Signal()   # sent after the refresh commits

@receiver(rollup_rebuilt)
def forget_column_flags(**kwargs):
    cache.delete_many([flag_key(c) for c in PROBED_COLUMNS])
```

The database cache has no delete-by-pattern, so the receiver needs an explicit key list. My first version listed 33 keys, the renameable columns. Callers actually probed 52, so 19 flags would still have ridden the week-long TTL. I widened it to all 52 and added a test for it.

```text
  BEFORE
  load -> rebuild rollup -> (nothing clears the flags)
  "custom_7 is empty" stays cached ............ up to 7 days
  TTL expires -> recount -> column appears

  AFTER
  load -> rebuild rollup -> COMMIT -> rollup_rebuilt
       -> delete_many(all 52 flags)
       -> next request recounts once -> column appears
```

### Validating the same input the query uses

The same work touched an endpoint that groups a supplier's spend by a chosen field. The field name is user input that ends up in a GROUP BY, so it is checked against an allow-list. The parameter is repeatable, for two-level groupings. Validation used a scalar field, which reads only the last value from the query string, while the query used every value. Validation and use disagreed about what the input was, which is the classic shape of this bug. The fix makes validation read the same list the query uses:

```python
from rest_framework import serializers

class SpendByFieldParams(serializers.Serializer):
    group_by = serializers.ListField(
        child=serializers.ChoiceField(choices=GROUPABLE_FIELDS),
        allow_empty=False,
    )
```

`ListField` reads every repeated value from the query string, and each one has to pass the allow-list. While I was there, I capped the year span at 25. A very wide range built one aggregate expression per year and did not come back within ten minutes.

## Results

| Change | Before | After |
|---|---|---|
| Search for a partial word | no match | prefix match |
| Prefix search, first page | 177–192 ms | 2.5–4.5 ms |
| 1,000-word search input | 1,916 ms, twice per request | 32.6 ms |
| Has-data cache reads per request | 46–49 | 1 |
| Has-data, 33 columns, cold | 12.2 s | 1.7 s |
| New column visible after a load | up to 7 days | next request |

These are development-database numbers, measured warm unless marked cold, and production tables are larger. The cold has-data path is still a full scan: one scan instead of dozens, but the first user after every refresh pays for it.

## What I would do next

- **Warm the flags instead of only clearing them.** Running the aggregate in the refresh job right after clearing takes about a second for all 33 renameable columns, off the request path. It also removes a small thundering herd: today several users opening pages right after a refresh can each run the same scan before the first one fills the cache.
- **Partial indexes on the custom columns.** They would make the empty-column probe nearly free. They need a migration that builds each index concurrently on a large table, which did not belong in a bug fix.
- **`set_many` is still a loop.** Django's database cache does not override it, so each missed key is still a few statements. It only runs on a miss, so I left it.
- **Single-letter stop words.** `a`, `i` and `s` are English stop words, so `a:*` compiles to an empty query and matches nothing. It is fast, but a user who types one letter sees an empty report. Using the `simple` configuration for very short input is one way to fix it.
