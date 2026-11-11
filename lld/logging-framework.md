---
title: Design a logging framework
section: lld
order: 15
summary: Logger with levels, pluggable formatters and appenders, chain of responsibility for level routing, a thread-safe singleton, and async writes.
---

## Requirements

- Log messages with levels: `DEBUG < INFO < WARNING < ERROR < FATAL`.
- Configurable minimum level; messages below it are dropped.
- Multiple destinations (appenders/sinks): console, file, remote service, DB.
- Configurable message format (timestamp, level, logger name, message).
- Thread-safe; logging must not corrupt output or block requests heavily.
- Single global access point for the app.
- Extensible: new sinks and formats without changing the logger.

## Entities

| Entity | Responsibility |
|--------|----------------|
| `LogLevel` | Ordered enum |
| `LogRecord` | Immutable: time, level, logger name, message, extras |
| `Formatter` | `LogRecord` → string |
| `Appender` (sink) | Writes formatted string somewhere; has its own min level |
| `LevelHandler` | Chain node: handles one level range, passes on |
| `Logger` | Public API (`info`, `error`…), builds records, dispatches |
| `LoggerConfig` | Min level, appenders, formatter |

## Class sketch

```python
import json
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum


class LogLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    FATAL = 50


@dataclass(frozen=True)
class LogRecord:
    level: LogLevel
    message: str
    logger: str
    ts: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)


class Formatter(ABC):
    @abstractmethod
    def format(self, r: LogRecord) -> str: ...


class TextFormatter(Formatter):
    def format(self, r):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts))
        return f"{t} [{r.level.name}] {r.logger}: {r.message}"


class JsonFormatter(Formatter):
    def format(self, r):
        return json.dumps({"ts": r.ts, "level": r.level.name,
                           "logger": r.logger, "msg": r.message, **r.extra})


class Appender(ABC):
    def __init__(self, formatter: Formatter, min_level=LogLevel.DEBUG):
        self.formatter, self.min_level = formatter, min_level
        self._lock = threading.Lock()

    def append(self, r: LogRecord) -> None:
        if r.level < self.min_level:
            return
        line = self.formatter.format(r)
        with self._lock:                  # no interleaved lines
            self.write(line)

    @abstractmethod
    def write(self, line: str) -> None: ...


class ConsoleAppender(Appender):
    def write(self, line):
        print(line, file=sys.stdout)


class FileAppender(Appender):
    def __init__(self, path: str, formatter: Formatter, min_level=LogLevel.DEBUG):
        super().__init__(formatter, min_level)
        self.f = open(path, "a", buffering=1)   # line-buffered

    def write(self, line):
        self.f.write(line + "\n")
```

## Logger as a thread-safe singleton

```python
class Logger:
    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst.min_level = LogLevel.INFO
                    inst.appenders = []
                    cls._instance = inst
        return cls._instance

    def configure(self, min_level: LogLevel, appenders: list[Appender]) -> None:
        self.min_level, self.appenders = min_level, appenders

    def log(self, level: LogLevel, msg: str, name: str = "root", **extra):
        if level < self.min_level:        # cheap early exit
            return
        record = LogRecord(level, msg, name, extra=extra)
        for a in self.appenders:
            a.append(record)

    def debug(self, msg, **kw): self.log(LogLevel.DEBUG, msg, **kw)
    def info(self, msg, **kw): self.log(LogLevel.INFO, msg, **kw)
    def warning(self, msg, **kw): self.log(LogLevel.WARNING, msg, **kw)
    def error(self, msg, **kw): self.log(LogLevel.ERROR, msg, **kw)
    def fatal(self, msg, **kw): self.log(LogLevel.FATAL, msg, **kw)


log = Logger()
log.configure(LogLevel.INFO, [ConsoleAppender(TextFormatter())])
log.info("user signed up", name="auth")
```

## Chain of responsibility variant

Interviewers often ask for CoR explicitly: each handler owns a level and decides whether to process, then passes the record on.

```python
class LevelHandler:
    def __init__(self, level: LogLevel, appenders: list[Appender],
                 nxt: "LevelHandler | None" = None):
        self.level, self.appenders, self.nxt = level, appenders, nxt

    def handle(self, r: LogRecord) -> None:
        if r.level == self.level:
            for a in self.appenders:
                a.append(r)
        if self.nxt:
            self.nxt.handle(r)


console = ConsoleAppender(TextFormatter())
pager = FileAppender("/var/log/alerts.log", JsonFormatter())
chain = LevelHandler(LogLevel.DEBUG, [console],
        LevelHandler(LogLevel.INFO, [console],
        LevelHandler(LogLevel.ERROR, [console, pager])))
```

- Pass-through (every handler sees the record) vs stop-at-first-match: choose based on whether errors should also go to info sinks.
- In practice, per-appender `min_level` gives the same routing with less code; say both.

## Key flows

1. App calls `log.error("payment failed", name="billing", order_id=42)`.
2. Logger drops it if below global level; else builds an immutable `LogRecord`.
3. Each appender filters by its own level, formats, and writes under its lock.

## Async writes

Slow sinks (network, DB) must not block request threads.

```python
import queue


class AsyncAppender(Appender):
    def __init__(self, inner: Appender, maxsize: int = 10_000):
        super().__init__(inner.formatter, inner.min_level)
        self.inner, self.q = inner, queue.Queue(maxsize)
        threading.Thread(target=self._drain, daemon=True).start()

    def append(self, r):
        try:
            self.q.put_nowait(r)
        except queue.Full:
            pass                             # drop rather than block; count drops

    def _drain(self):
        while True:
            self.inner.append(self.q.get())

    def write(self, line): ...
```

Python's stdlib does this with `QueueHandler` + `QueueListener`.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Singleton | `Logger` | One configured logger for the app |
| Strategy | `Formatter` | Text vs JSON without touching appenders |
| Chain of responsibility | `LevelHandler` | Route by level, add handlers freely |
| Observer | Logger → appenders | Fan out one record to many sinks |
| Decorator | `AsyncAppender` wraps any appender | Adds buffering with same interface |

## Pitfalls

- Formatting the message before checking the level (wasted work). Pass args lazily: `log.debug("x=%s", x)`.
- One global lock across all sinks; a slow file blocks console.
- Unbounded async queues → memory blowup under incident load.
- Logging PII / secrets; add a redaction filter.

## Extensions interviewers ask

- **Hierarchical loggers:** `app.db` inherits config from `app` (stdlib `logging.getLogger` does this).
- **Filters:** predicate objects before appenders (e.g. sample 1% of debug).
- **Rotating files:** size or time based; `RotatingFileHandler`.
- **Context propagation:** request id via `contextvars`, added to every record.
- **Runtime config reload:** observer on a config file or admin endpoint.
