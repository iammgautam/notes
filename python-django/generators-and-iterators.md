---
title: Generators and iterators
section: python-django
order: 3
summary: The iterator protocol, how generators pause, yield from, and how to stream millions of rows from Django without loading them into memory.
---

Exporting a year of invoices for a large tenant, backfilling a column, or pushing every order to a search index all share one problem: the data doesn't fit comfortably in a worker's memory. Generators let you process one row (or one batch) at a time with the same readable `for` loop. Combined with `QuerySet.iterator()` they are the difference between a 4 GB task that gets OOM-killed and a 60 MB task that finishes.

## How it works

### The iterator protocol

- An **iterable** has `__iter__()` that returns an iterator. Lists, dicts, querysets, files.
- An **iterator** has `__next__()` that returns the next item or raises `StopIteration`, and `__iter__()` that returns itself.

A `for` loop is just:

```python
it = iter(iterable)
while True:
    try:
        item = next(it)
    except StopIteration:
        break
    ...
```

Iterators are **single-pass**. Once exhausted, they stay exhausted. Iterables like lists can produce fresh iterators every time.

### Generators

A function containing `yield` returns a generator object when called. No code runs until the first `next()`. Each `next()` runs until the next `yield`, hands out the value and **suspends the frame** (locals, instruction pointer, try blocks all preserved). Returning raises `StopIteration`.

```python
def read_lines(path):
    print("opening")
    with open(path) as f:
        for line in f:
            yield line.rstrip("\n")
    print("closed")

gen = read_lines("orders.csv")   # nothing printed yet
next(gen)                        # prints "opening", returns line 1
```

Memory is O(1) in the number of items: only the current frame exists. Generator expressions (`(x * 2 for x in rows)`) are the inline form.

### Closing and cleanup

If a consumer stops early (a `break`, an exception, or the generator is garbage collected), Python calls `gen.close()`, which raises `GeneratorExit` at the paused `yield`. `finally` blocks and `with` exits run then. That's why the `with open(...)` above is safe even if the caller stops after 10 lines, as long as the generator is closed or collected.

### yield from

`yield from sub` delegates to another iterable: it yields every item and, for generators, forwards `send()` / `throw()` and returns the sub-generator's return value.

```python
def all_invoice_rows(tenant_ids):
    for tenant_id in tenant_ids:
        yield from invoice_rows(tenant_id)
```

It's also how you compose pipelines without nested loops leaking into callers.

### send and coroutines (briefly)

`gen.send(value)` resumes the generator and makes `value` the result of the `yield` expression. This was the basis of pre-`async` coroutines. In application code you rarely need it; `async def` replaced that use case.

## In practice

### Pipelines with itertools

```python
import csv
from itertools import batched, islice  # batched: Python 3.12+

def parse(rows):
    for row in rows:
        yield {"sku": row[0], "qty": int(row[1])}

def valid(items):
    return (i for i in items if i["qty"] > 0)

with open("import.csv") as f:
    for chunk in batched(valid(parse(csv.reader(f))), 1000):
        LineItem.objects.bulk_create(
            [LineItem(order_id=order_id, **i) for i in chunk]
        )
```

Each stage pulls one item at a time from the previous one. `batched` turns the stream into tuples of 1000 for efficient bulk inserts. `islice(gen, 100)` is handy for "preview the first 100".

### Streaming a QuerySet with .iterator()

Normal queryset iteration fills `_result_cache` with every model instance, and with psycopg the client receives the full result set before Django builds any objects. For 2 million invoices that's gigabytes.

```python
qs = (
    Invoice.objects
    .filter(tenant_id=tenant_id, issued_at__year=2025)
    .only("id", "number", "total", "issued_at")
    .order_by("id")
)

for invoice in qs.iterator(chunk_size=2000):
    write_row(invoice)
```

What `.iterator()` changes:

- No result cache: instances are discarded as you go.
- On PostgreSQL it uses a **server-side cursor** (a named cursor, `DECLARE ... CURSOR`) and fetches `chunk_size` rows per round trip (default 2000).
- `prefetch_related` works with `.iterator()` since Django 4.1, but only if you pass `chunk_size`; the prefetch runs per chunk.

> **Gotcha:** server-side cursors break behind PgBouncer in transaction pooling mode, because the cursor lives on one backend connection and the next fetch may land on another. Set `"DISABLE_SERVER_SIDE_CURSORS": True` in that database's settings. `.iterator()` then still skips the result cache but the driver fetches the full result set, so batch by primary key instead.

### Keyset batching (works everywhere)

```python
def iter_in_batches(qs, size=2000):
    last_pk = 0
    while True:
        batch = list(qs.filter(pk__gt=last_pk).order_by("pk")[:size])
        if not batch:
            return
        yield from batch
        last_pk = batch[-1].pk
```

Each query is short and index-friendly (`WHERE id > %s ORDER BY id LIMIT 2000`), no long-running transaction holds a snapshot, and it survives PgBouncer. Avoid `OFFSET` pagination for this: page 500 scans and discards 1 million rows.

### Streaming an HTTP response

```python
import csv
from django.http import StreamingHttpResponse

class Echo:
    def write(self, value):
        return value

def export_invoices(request):
    qs = Invoice.objects.filter(tenant=request.tenant).values_list(
        "number", "total", "issued_at"
    )
    writer = csv.writer(Echo())
    rows = (writer.writerow(r) for r in qs.iterator(chunk_size=2000))
    return StreamingHttpResponse(
        rows,
        content_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoices.csv"'},
    )
```

The response body is consumed by the WSGI server after the view returns. Under ASGI, Django 4.2+ prefers an async iterator for `StreamingHttpResponse`; a sync one still works but Django warns and buffers it through a thread.

## Pitfalls

- **Consuming an iterator twice.** `rows = (r for r in data); total = sum(...); count = len(list(rows))` gives 0 the second time. Materialize once with `list()` if you need multiple passes and it fits.
- **`.iterator()` and long transactions.** A server-side cursor inside `ATOMIC_REQUESTS` or a long `atomic()` holds a snapshot for the whole export, which blocks vacuum on busy tables. Keyset batching avoids this.
- **Generators that never close.** A generator holding a DB cursor or file, abandoned without being exhausted, keeps it open until garbage collection. Use `contextlib.closing(gen)` or exhaust it.
- **`StreamingHttpResponse` and middleware.** Middleware that reads `response.content` (some GZip or logging setups) will fail or buffer the whole stream. Check `response.streaming`.
- **Exceptions mid-stream.** Headers (status 200) are already sent. The client sees a truncated file. Validate inputs before you start streaming, and include a row count or trailer if correctness matters.
- **Lazy evaluation surprises.** A generator that reads `request.tenant` or a transaction-bound value runs later than you think, possibly after the request or transaction ended.

## Checklist

- Large exports and backfills: `.iterator(chunk_size=...)` or keyset batches, never plain iteration.
- Use `.only()` or `.values_list()` to shrink each row.
- Batch writes with `itertools.batched` + `bulk_create` / `bulk_update`.
- Behind PgBouncer transaction pooling: disable server-side cursors and use keyset batching.
- Treat iterators as single-pass.

## Interview angle

**What's the difference between an iterable and an iterator?** An iterable produces iterators via `__iter__`. An iterator yields items via `__next__` and is single-pass.

**How does a generator save memory?** It produces one value at a time and suspends its frame between values, so only the current item and frame are alive.

**How do you process 10 million rows in Django?** `.iterator()` with a server-side cursor, or keyset batches by primary key, with `.only()`/`.values_list()`, batched writes, and no surrounding long transaction.

**What does `yield from` do beyond a loop?** It forwards `send()`/`throw()` to the sub-generator and evaluates to its return value.
