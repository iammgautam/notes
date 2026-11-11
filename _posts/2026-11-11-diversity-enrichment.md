---
date: 2026-11-11 16:00:00 +0530
title: "Enriching 20,000 suppliers with one task instead of two"
summary: "How I built an upload-and-enrich flow for supplier diversity data, and why I deleted a fork-join handshake that could hang forever."
tags: [django, background-jobs, concurrency]
---

The spreadsheet had a few thousand rows. Supplier name, sometimes a tax ID, sometimes an address, and an empty column on the right labelled "Diverse?". Someone on a procurement team was filling it in by hand: copy a name, search for it, read the result, type Y or N, next row. For a few hundred suppliers that is a bad afternoon. For ten thousand it is a quarter.

I built the feature that replaces that afternoon. You upload the list, wait a few minutes, and download the same list with diversity columns added.

## What "supplier diversity" means

Gainfront makes supplier lifecycle management software for procurement teams. Those teams track how much they spend with diverse suppliers: businesses that are minority-owned, woman-owned or veteran-owned, usually backed by a certification. Companies report on that spend, so they need to know which suppliers qualify, and their own records rarely say. We have an internal supplier-matching service that takes a name, tax ID or DUNS number and returns what is known about a business, certifications included. My job was to put that service behind a spreadsheet.

## The constraints

- **Up to 20,000 rows per upload.** Each row is one HTTP call to the matching service, so a full file is minutes of work. It can't run inside a web request.
- **The user's file must come back recognizable.** Same columns, same headers, their own extra columns, plus the new ones.
- **A wrong answer is worse than no answer.** A blank "minority-owned" cell reads as "no". If the matching service is down, the user must not get a file saying none of their suppliers are diverse.
- **It runs on our Postgres-backed task queue.** A task is a row, a worker claims it with `SKIP LOCKED`, and each task runs in a freshly forked process.

```text
 browser               web process              worker process
 -------               -----------              --------------
 upload file ───────▶  store in object storage
 "check my file" ───▶  verify, save nothing
 "enrich" ──────────▶  verify again
                       tx: job row + task ────▶ claim (SKIP LOCKED)
                                                fork, then:
                                                  guards
                                                  parse
                                                  16 threads ──▶ matching
                                                  write .xlsx    service
                                                  link result
 poll status ◀──────── job mirrors task ◀────── task row saved
 download ◀──────────  short-lived file URL
```

## One job row, and a status it doesn't own

The data model is one table. A row records who asked, the uploaded file, the result file once there is one, the queue task doing the work, a status, and an error message written for humans.

The user sees four states: Pending, Processing, Complete, Failed. The obvious design gives the job its own state machine, updated by the task at the right moments. I didn't, because two state machines drift: the task dies on an exception nobody anticipated, nothing updates the job, and the user stares at "Processing" for a week. Instead the job status is a projection of the task status, through one mapping:

```text
 task (our queue)            job (what the user sees)
 ----------------            ------------------------
 queued, claimed     ─────▶  Pending
 running, waiting    ─────▶  Processing
 completed           ─────▶  Complete
 failed              ─────▶  Failed + a message a human can act on
```

A simplified version:

```python
TASK_TO_JOB = {"queued": JobStatus.PENDING, "running": JobStatus.PROCESSING,
               "completed": JobStatus.COMPLETE, "failed": JobStatus.FAILED}

def sync_from_task(job, task):
    new_status = TASK_TO_JOB.get(task.status)
    if new_status is None or new_status == job.status:
        return False  # already right: no write
    job.status = new_status
    fields = ["status"]
    if new_status == JobStatus.FAILED:
        job.error_message = "The run failed. Support has been told."
        fields.append("error_message")
    job.save(update_fields=fields)
    return True
```

It is idempotent because our queue saves a finished task row more than once. The user never sees a traceback; that stays on the task row and in our error tracker.

The caller is a `post_save` receiver on the queue's task model. The queue is shared code and must not import feature modules, so my module watches the queue, never the reverse. It fires for every task in the system, so its first line checks the task name before any query.

## Checking the file before any work is queued

Verification runs twice: when the user clicks "check my file", for instant feedback, and again on create, because nothing forces a client to call the first endpoint. A bad file is a normal outcome, so the check answers 200 with `ok: false` and a list of fixes rather than raising.

The bounds come first, cheapest first. The upload sits in object storage, so its size is a HEAD request; reading a 2 GB file into a web worker to learn it is too big is the attack, not the defense. An .xlsx is a zip that can expand to gigabytes, so I check the decompressed size before the spreadsheet library opens it. And the real limit is rows, not bytes, because each row costs an outbound call and memory until the result is written.

```python
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UNZIPPED_BYTES = 10 * MAX_UPLOAD_BYTES
MAX_ROWS = 20_000

def check_upload(stored):
    # .size is a metadata call to object storage; nothing is downloaded yet
    if stored.size > MAX_UPLOAD_BYTES:
        return Report.failed("File is too large. Split it and try again.")

    data = stored.read()
    if stored.name.endswith(".xlsx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unpacked = sum(m.file_size for m in archive.infolist())
        if unpacked > MAX_UNZIPPED_BYTES:
            return Report.failed("We couldn't open this file.")

    header, row_count = PARSERS[extension(stored.name)](data)
    if row_count > MAX_ROWS:
        return Report.failed(f"More than {MAX_ROWS:,} rows. Split the file.")
    return compare_headers(header, row_count)
```

Trusting the zip's declared sizes is safe: Python's `zipfile` stops reading each member at its declared size, and the spreadsheet library reads through it.

Then headers. The template match is exact, byte for byte. I nearly made it case-insensitive, then traced the result: a column headed `supplier name` would pass the check, miss the exact-key rename to the engine's field name, and match nothing. The job would finish Complete with an empty enrichment and no error anywhere. So a case variant gets its own message naming the exact spelling. Duplicate detection does fold case, because `City` and `city` in one file conflict either way.

## Creating the job: I/O first, then one short transaction

```python
def create_job(user, upload):
    report = check_upload(upload.file)  # network I/O, no transaction open
    if not report.ok:
        raise ValidationError(report.messages())

    with transaction.atomic():  # fast database writes only
        job = EnrichmentJob.objects.create(requested_by=user, upload=upload)
        job.task = enrich_suppliers.submit(job_id=job.id)
        job.save(update_fields=["task"])
        sync_from_task(job, job.task)
    return job
```

A slow network call inside a transaction holds a database connection and its locks for as long as it takes, so the storage I/O happens first. Inside the transaction, the job, the task and the link commit together. If the submit fails, the job rolls back too, so there is never a job with no task, stuck in Pending.

Our queue dry-runs each task in the submitting process. My task loads its job before the dry-run check, so a bad id fails in the user's request, not later on a worker.

## Fanning out to the matching service

The worker claims the task and forks. Before writing anything, the task checks two guards. If the uploaded file is gone, fail. If the job already has a result file, fail: the task is not re-entrant, and a second run would orphan the first result in storage with nothing pointing at it.

Then it parses the file, renames the template headers to the engine's field names, and hands the rows to our existing matching engine. The engine marks rows with too little to search on as "insufficient data" without a call, and submits one lookup per remaining row to a thread pool.

I didn't split the file into batches or child tasks: the unit of work is a row, and the pool does the chunking. Threads suit work that waits on HTTP, and these never touch the ORM, so they open no extra database connections.

I pinned the pool at 16 threads. Left alone, `ThreadPoolExecutor` sizes itself as `min(32, cpu_count + 4)`, tying our call rate to whatever container the worker lands on. The right number is what the matching service tolerates, not our core count. In practice, 16 concurrent calls is our rate limit.

Each call goes through our shared HTTP session: a 15-second connect timeout, a 60-second read timeout, and exponential backoff (1 s, 2 s, 4 s) on connection errors, 429s (honoring `Retry-After`) and 502/503/504.

## One bad row should not sink 20,000

The engine collects results with `as_completed` and calls `future.result()` on each. If one lookup raises after its retries run out (an SSL error, a malformed body), the exception propagates and the run fails, throwing away every row already matched. So I subclassed the engine for this module and overrode its per-row lookup hook:

```python
class TolerantMatcher(SupplierMatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = 0
        self._lock = threading.Lock()

    def lookup(self, **query):
        try:
            return super().lookup(**query)
        except Exception:
            with self._lock:  # called from 16 threads at once
                self.failures += 1
            log.exception("lookup failed; row left unmatched")
            return None  # same as "no match found"
```

A failed lookup now looks like a lookup with no match, which is what a non-2xx response already produced.

## When every row fails, fail the job

That creates a new problem. If the matching service is down, every row becomes "unmatched" and the job reports success. The user downloads a file claiming we found no diversity data for any supplier: the silent wrong answer from the constraints. So the task compares failures against the rows it actually sent:

```python
matcher = TolerantMatcher(rows, max_workers=16)
results = matcher.run()

attempted = sum(r["match"] != "insufficient_data" for r in results)
if attempted and matcher.failures == attempted:
    raise RuntimeError("every lookup failed; matching service unreachable")
```

Rows skipped as "insufficient data" never called out, so they don't count. If every attempt failed, the task raises instead of writing a file. The rule is blunt: a run with 99% failures still produces a file.

## What the user sees while it runs

Pending covers the wait for a worker; our queue polls, so an idle worker can take up to a minute to notice new work. Processing covers the run. The task links the result file to the job before the function returns, and our queue marks a task completed only when the function returns, so a job that reads Complete already has its download. The other order shows a Complete badge next to a broken download button, which is how support tickets get written.

Finer progress lives only in logs, where the engine prints a percentage every hundred rows. I kept per-row progress out of the job row because the threads stay out of the ORM. It is still the first thing users ask for.

## Giving the user their own file back

The engine works in its own field names and tags every column it doesn't use with a suffix. Hand that straight back and the user's own columns come back renamed. So the headers make a round trip:

```text
 uploaded     Supplier Name | City      | Region Code
 to engine    name          | city      | Region Code
 from engine  name          | city_orig | Region Code_orig | owner_type
 returned     Supplier Name | City      | Region Code      | Owner Type
```

Every column the user sent comes back under the exact header they sent. Columns the engine added get readable names, except certification acronyms that title-casing would wreck. If an added name collides with a user header, the added one gets a "(from matching)" suffix; records are dicts, and two keys mapping to one header would silently drop a column. Column order follows the input file, added columns last, or a column blank on row one jumps to the end.

## The fork-join I took out

The first version did not run the matching inline. We already had a task that matched a whole file, so my task submitted it as a child, parked itself in a waiting state, and relied on our queue's join to complete the parent when the child finished. Reuse what exists. It looked obviously right.

The join is a `post_save` receiver on the task model. Simplified:

```python
@receiver(post_save, sender=Task)
def join_parent(sender, instance, **kwargs):
    parent = instance.parent
    if instance.status not in ("completed", "failed"):
        return
    if parent is None or parent.status != "waiting":
        return  # the race lives on this line
    if instance.status == "failed":
        parent.status = "failed"
    elif not parent.children.exclude(status="completed").exists():
        parent.status = "completed"
    else:
        return
    parent.save()  # fires this receiver again for the grandparent
```

The join only works if the parent is already waiting when the child finishes. If the parent submits first and parks second, a fast child gets in between:

```text
 time  parent task          worker                join receiver
 ----  -------------------  --------------------  -------------------
  t0   status = running
  t1   submit(child) ─────▶ claim child
  t2                        run the matching
  t3                        child = completed ──▶ parent waiting?
                                                  no, still running:
                                                  return
  t4   status = waiting
  t5   waits forever. No child is left to wake it.
```

In production this needs a second worker to finish the child before the parent's next line runs, which is rare with one worker per deployment. In synchronous mode, where `submit()` runs the task inline, the child finishes inside the submit call, so t1 to t3 always happen before the parent parks. The race becomes a guarantee.

The cure is known: park first, in the same transaction as the submit. I could have kept the child and been careful about that order. Instead I asked what the child was buying me, and the race turned out to be one problem of three:

1. The child expected its input as a record in our generic file-import pipeline. Creating that record started the pipeline's own parse, so every upload was parsed twice.
2. The child was built for bulk matching runs and sent a "matching finished" email, linking to a page this flow never showed the user.
3. The handshake above, which made my job's correctness depend on two lines staying in the right order, with every future editor knowing why.

The child only needed a list of rows and a filename. So I called the matching engine directly inside one task and deleted the child, the waiting state and the handshake. One task now has one life cycle, the same as the user's job. I replaced three handshake tests with one end-to-end test asserting the task creates no child tasks, so the design can't drift back.

## Results

- A row-by-row manual lookup became an upload and a download. Files of up to 20,000 rows finish in a few minutes, mostly spent waiting on the matching service.
- A broken row costs one row. An outage costs a failed job with a clear message, not a confident file full of blanks.
- Complete always comes with a file, and no job waits on a task that doesn't exist.
- About 90 backend tests cover the module, and none call the real matching service.

The caveat: "a few minutes" depends on a service I don't control, and nothing here adapts when it gets slow.

## What I'd do differently

- **A circuit breaker.** If the matching service is slow rather than down, all 20,000 calls still try, and each retries.
- **A failure threshold.** The outage guard fires only when every lookup fails. Flagging runs above, say, 20% failures would catch a partial outage.
- **A heartbeat.** A long run should prove it is alive every few seconds, so a worker that dies mid-run is noticed and the job is marked failed quickly instead of waiting for someone to look.
- **Input order in the output.** Rows come back in completion order. Carrying each row's index through its future and sorting at the end is a small fix I should have made first time.
- **Real progress.** A counter written every few hundred rows by the loop that collects results, not the threads, would give users a percentage.
