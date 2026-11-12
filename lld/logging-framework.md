---
title: Design a logging framework
section: lld
order: 19
phase: "Phase 5 · Beginner"
summary: Levels, appenders, formatters, a logger hierarchy as a chain of responsibility, an async appender on a bounded queue, and a singleton registry.
---

## Requirements

Clarifying questions to ask first:

- Which levels, and can each logger and each sink have its own threshold?
- Which sinks: console, file, remote? Can one message go to several?
- Text or structured (JSON) output?
- Named loggers (`app.db`) that inherit config from a parent?
- Can a slow sink block the request thread? What happens when it falls behind?

Functional:

- Levels `DEBUG < INFO < WARNING < ERROR < FATAL`; records below the threshold are dropped.
- Named loggers from a registry; `app.db` inherits its level from `app`, then `root`.
- A record flows to the logger's appenders, then up to its parents (unless propagation is off).
- Appenders have their own min level and formatter (text or JSON).
- An async appender wraps any appender so slow I/O happens off the caller's thread.

Non-functional:

- Thread-safe: no torn or interleaved lines.
- Cheap when disabled: no formatting for a dropped record.
- Logging never raises into the caller.

Out of scope: file rotation, config files, shipping to a log backend.

## Entities and relationships

```text
+----------------------+         +------------------------------+
| LoggerRegistry       | 1     * | Logger                       |
| (one per process)    |-------->| name, level (None = inherit) |
| get(name) -> Logger  | creates | parent: Logger, propagate    |
+----------------------+         | appenders: tuple[Appender]   |
                                 | log(level, msg, *args)       |
                                 +---------------+--------------+
                                                 | has 0..*
                                                 v
                                 +------------------------------+
                                 | Appender (ABC)               |
                                 | append(record), close()      |
                                 +------------------------------+
                                    ^ is-a               ^ is-a
                    +---------------+--------+  +--------+-----------+
                    | StreamAppender         |  | AsyncAppender      |
                    | stream, level, lock    |  | queue, worker      |
                    | formatter              |  | inner: Appender ---+-->
                    +-----------+------------+  +--------------------+
                                | uses                       (wraps)
                                v
                    Formatter (Protocol): format(record) -> str
                      ^ TextFormatter        ^ JsonFormatter

LogRecord (frozen): level, logger, msg, args, extra, ts, thread
```

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---------|-------|------------------------|
| Chain of responsibility | `Logger` → parent → `root` | Each level of the hierarchy handles the record, then passes it on; `propagate = False` stops it. No central routing table to edit. |
| Strategy | `Formatter` | Text vs JSON without touching appenders |
| Decorator | `AsyncAppender` wraps an `Appender` | Adds buffering to any sink with the same interface |
| Singleton (registry) | Module-level `LoggerRegistry` | One logger per name per process, so startup config is seen everywhere |
| Observer | Logger fans out to many appenders | Add a sink without changing callers |

## Code

```python
import io
import json
import queue
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol, TextIO


class Level(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    FATAL = 50


@dataclass(frozen=True)
class LogRecord:
    level: Level
    logger: str
    msg: str
    args: tuple = ()
    extra: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    thread: str = field(default_factory=lambda: threading.current_thread().name)

    @property
    def message(self) -> str:                    # formatted lazily, only if emitted
        return self.msg % self.args if self.args else self.msg


class Formatter(Protocol):
    def format(self, r: LogRecord) -> str: ...


class TextFormatter:
    def format(self, r: LogRecord) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(r.ts))
        return f"{t} {r.level.name:<7} [{r.thread}] {r.logger}: {r.message}"


class JsonFormatter:
    def format(self, r: LogRecord) -> str:
        return json.dumps({"ts": r.ts, "level": r.level.name,
                           "logger": r.logger, "msg": r.message, **r.extra})


class Appender(ABC):
    @abstractmethod
    def append(self, r: LogRecord) -> None: ...

    def close(self) -> None:
        pass


class StreamAppender(Appender):
    """Console (sys.stderr), file (open(path, "a")), or io.StringIO in tests."""

    def __init__(self, stream: TextIO, formatter: Formatter,
                 level: Level = Level.DEBUG):
        self.stream, self.formatter, self.level = stream, formatter, level
        self._lock = threading.Lock()

    def append(self, r: LogRecord) -> None:
        if r.level < self.level:
            return
        line = self.formatter.format(r)          # format outside the lock
        with self._lock:                         # one writer at a time: no torn lines
            self.stream.write(line + "\n")
            self.stream.flush()


class AsyncAppender(Appender):
    """Decorator: same interface, the slow write happens on a worker thread."""
    _STOP = object()

    def __init__(self, inner: Appender, maxsize: int = 10_000):
        self.inner, self.dropped = inner, 0
        self._q: queue.Queue = queue.Queue(maxsize)   # bounded: memory is capped
        self._drop_lock = threading.Lock()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

    def append(self, r: LogRecord) -> None:
        try:
            self._q.put_nowait(r)                # never block the request thread
        except queue.Full:
            with self._drop_lock:                # += is read-modify-write, GIL or not
                self.dropped += 1

    def _drain(self) -> None:
        while (r := self._q.get()) is not self._STOP:
            try:
                self.inner.append(r)
            except Exception as e:               # a broken sink must not kill the worker
                print(f"log sink failed: {e!r}", file=sys.stderr)
        self.inner.close()

    def close(self) -> None:
        self._q.put(self._STOP)                  # FIFO: everything queued earlier flushes
        self._worker.join()


class Logger:
    def __init__(self, name: str, parent: "Logger | None" = None):
        self.name, self.parent = name, parent
        self.level: Level | None = None          # None: inherit from parent
        self.propagate = True
        self.appenders: tuple[Appender, ...] = ()

    def add_appender(self, a: Appender) -> None:
        self.appenders = (*self.appenders, a)    # copy-on-write: log() reads a snapshot

    def effective_level(self) -> Level:
        node = self
        while node.level is None:
            node = node.parent
        return node.level

    def log(self, level: Level, msg: str, *args, **extra) -> None:
        if level < self.effective_level():       # cheap exit: nothing built or formatted
            return
        record = LogRecord(level, self.name, msg, args, extra)
        node = self
        while node is not None:                  # chain of responsibility up the tree
            for a in node.appenders:
                try:
                    a.append(record)
                except Exception as e:           # logging must never crash the caller
                    print(f"appender failed: {e!r}", file=sys.stderr)
            node = node.parent if node.propagate else None

    def debug(self, msg, *a, **kw): self.log(Level.DEBUG, msg, *a, **kw)
    def info(self, msg, *a, **kw): self.log(Level.INFO, msg, *a, **kw)
    def warning(self, msg, *a, **kw): self.log(Level.WARNING, msg, *a, **kw)
    def error(self, msg, *a, **kw): self.log(Level.ERROR, msg, *a, **kw)


class LoggerRegistry:
    def __init__(self):
        self.root = Logger("root")
        self.root.level = Level.INFO
        self._loggers = {"root": self.root}
        self._lock = threading.Lock()

    def get(self, name: str = "root") -> Logger:
        with self._lock:                         # check-then-create is not atomic
            return self._get(name)

    def _get(self, name: str) -> Logger:
        if name not in self._loggers:
            parent = self._get(name.rpartition(".")[0] or "root")
            self._loggers[name] = Logger(name, parent)
        return self._loggers[name]


_registry = LoggerRegistry()      # a module is imported once: Python's singleton
get_logger = _registry.get


if __name__ == "__main__":
    out, alerts, audit = io.StringIO(), io.StringIO(), io.StringIO()
    get_logger().add_appender(StreamAppender(out, TextFormatter()))
    db = get_logger("app.db")
    assert db is get_logger("app.db") and db.parent is get_logger("app")
    errors = AsyncAppender(StreamAppender(alerts, JsonFormatter(), Level.ERROR))
    db.add_appender(errors)

    db.debug("hidden %s", "x")                   # below root's INFO: dropped early
    db.info("query took %d ms", 12)              # bubbles up to root's appender
    db.error("deadlock", table="orders")         # db's async sink AND root's sink
    errors.close()                               # flush the queue
    assert "query took 12 ms" in out.getvalue() and "hidden" not in out.getvalue()
    assert json.loads(alerts.getvalue())["table"] == "orders"

    a = get_logger("audit")
    a.propagate = False                          # stop the chain here
    a.add_appender(StreamAppender(audit, TextFormatter()))
    a.info("role changed")
    assert "role changed" in audit.getvalue() and "role" not in out.getvalue()

    sys.setswitchinterval(1e-6)                  # switch threads often so races show up
    # stress: 8 threads x 500 records through one async appender
    sink = io.StringIO()
    s = get_logger("stress")
    s.propagate = False
    async_sink = AsyncAppender(StreamAppender(sink, TextFormatter()))
    s.add_appender(async_sink)

    def work(i: int) -> None:
        for j in range(500):
            s.info("worker %d line %d", i, j)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    async_sink.close()
    lines = sink.getvalue().splitlines()
    assert len(lines) == 4000 and async_sink.dropped == 0
    assert all(" stress: worker " in ln for ln in lines)   # no torn lines
    print("ok")
```

## Key flows

`db.error("deadlock", table="orders")` with an async error sink on `app.db` and a console sink on `root`:

```text
caller      Logger app.db     AsyncAppender     worker       root sink
  | error()       |                 |              |              |
  |-------------->| level >= INFO?  |              |              |
  |               | build LogRecord |              |              |
  |               |--append(r)----->| put_nowait   |              |
  |               |<----------------|              |              |
  |               |-- parent "app" (no sinks) -> parent "root" -->|
  |               |                 |              | format, lock |
  |<--------------| returns         |              | write        |
  |                                 |--get()------>|              |
  |                                 |              |--inner.append(r)
```

Shutdown flush:

```text
main            AsyncAppender        worker              inner sink
  | close()           |                 |                     |
  |------------------>| put(_STOP)      |                     |
  |                   |                 | drain earlier items |
  |                   |                 |-------------------->|
  |                   |                 | sees _STOP          |
  |                   |                 |--close()----------->|
  |<----- join() returns when the worker exits                |
```

## Edge cases

- `log.debug(f"x={expensive()}")` formats even when DEBUG is off. Pass args: `log.debug("x=%s", x)`.
- Queue full: `put_nowait` drops and counts. Blocking would turn a slow disk into slow requests.
- Process exit without `close()`: the daemon worker dies and queued records are lost. Register `close` with `atexit`.
- `extra` keys that collide with parameters (`level=`, `msg=`) raise `TypeError`; reserve those names.
- `add_appender` from two threads at once can lose one (the tuple swap is not atomic). Configure at startup.
- A logger created before its parent (`a.b.c` first) still gets the right chain: `_get` creates the parents.

## Production follow-ups

**Is it thread-safe?** Each `StreamAppender` has its own lock, so a slow file never blocks the console sink. The registry lock makes get-or-create atomic, and the appender tuple is copy-on-write so `log()` iterates a stable snapshot. The GIL does not save you: `dropped += 1` and "check name, then insert" are multi-step and can interleave. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}) for the queue mechanics.

**What survives a crash or restart?** Records still in the in-memory queue are lost on a hard crash. If that matters, write synchronously to a local file and let an agent (Fluent Bit, Vector) tail it with a checkpointed offset, so shipping resumes after restart without loss.

**What about retries and duplicates for a remote sink?** Retry with exponential backoff on the worker thread, never on the request thread. Retries can deliver a record twice, so stamp each record with a UUID and let the backend dedupe. After N failures, spill to a local file instead of dropping silently.

**How do you add a sink or format without editing old code?** A Kafka sink is a new `Appender` subclass; logfmt output is a new class with `format()`. Filters (sampling, PII redaction) slot in as another wrapping appender. The logger never changes.

**What breaks at 10× volume, or across machines?** The async queue fills and you start dropping. Sample DEBUG and INFO, keep 100% of ERROR, and rate-limit repeated identical messages. Across machines, each process writes JSON to stdout or a file and a collector ships it centrally; app servers should not each hold connections to the log store.

**How do you observe the logger itself?** Export `dropped`, queue depth, sink write latency and sink error count as metrics. A non-zero drop rate during an incident is exactly when you most need to know logs are missing.

**How do you get a request id on every line?** Store it in a `contextvars.ContextVar` set by middleware, and copy it into `extra` when building the record. It works across threads and asyncio tasks without passing it through every call.

**What about forking servers (gunicorn, Celery prefork)?** Threads do not survive `fork()`. Start the async worker after fork (a post-fork hook), or every child inherits a queue that nobody drains.

## Interview follow-ups

- **Why not the classic level-based handler chain (DEBUG handler → INFO handler → ERROR handler)?** It works, but a per-appender min level gives the same routing with less code. The hierarchy chain is what the stdlib uses and supports per-module config.
- **How does this map to Python's `logging`?** `getLogger` is the registry, `Handler` is the appender, `Formatter` and `Filter` are the same idea, and `QueueHandler` + `QueueListener` is the async appender.
- **Drop or block when the queue is full?** Drop for app logs (availability first). Block, or write to disk, for audit logs that must never be lost.
- **Why is `LogRecord` frozen?** It is shared across threads and sinks; immutability means no sink can change what another sink sees.
