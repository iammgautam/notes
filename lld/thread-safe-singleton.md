---
title: Thread-safe singleton
section: lld
order: 11
phase: "Phase 4 · Concurrency"
summary: Four ways to build a singleton in Python, why double-checked locking needs both checks, how to prove it with a thread test, and the pickling and reset traps.
---

A singleton guarantees one instance per process. Lazy creation is a check-then-act (`if _instance is None: create`), so two threads can both see `None` and build two instances. Every version below is about closing that gap, cheaply.

## Option 1: a module-level instance

The Pythonic default. A module body runs once, on first import, and the import system holds a per-module lock while it runs, so concurrent imports cannot build two.

```python
# settings.py
import os


class Settings:
    def __init__(self) -> None:
        self.db_url = os.environ.get("DATABASE_URL", "postgres://localhost/app")


settings = Settings()   # elsewhere: from settings import settings
```

- Eager, not lazy. Fine unless construction is slow or needs runtime arguments.
- To fake it in tests, patch the name in each module that imported it: `from settings import settings` copies the reference.

## Option 2: double-checked locking

What interviewers mean by "thread-safe singleton". The second half of this block is the many-threads test.

```python
import threading
import time


class NaiveRegistry:
    _instance = None

    def __init__(self) -> None:
        time.sleep(0.01)                  # slow init widens the race window

    @classmethod
    def instance(cls) -> "NaiveRegistry":
        if cls._instance is None:         # check...
            cls._instance = cls()         # ...act: many threads get here
        return cls._instance


class Registry:
    _instance = None
    _lock = threading.Lock()
    created = 0

    def __init__(self) -> None:
        Registry.created += 1             # safe: only runs under _lock
        time.sleep(0.01)

    @classmethod
    def instance(cls) -> "Registry":
        if cls._instance is None:         # 1. fast path, no lock once built
            with cls._lock:
                if cls._instance is None: # 2. another thread may have won
                    cls._instance = cls()
        return cls._instance


def distinct_instances(get, threads: int = 32) -> int:
    start = threading.Barrier(threads)
    got = []                              # holds refs so id()s stay unique

    def worker() -> None:
        start.wait()                      # release every thread at once
        got.append(get())

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return len({id(o) for o in got})


if __name__ == "__main__":
    assert distinct_instances(NaiveRegistry.instance) > 1
    for _ in range(5):
        Registry._instance, Registry.created = None, 0   # reset between runs
        assert distinct_instances(Registry.instance) == 1
        assert Registry.created == 1
    print("double-checked locking ok")
```

Why both checks:

- **Inner check (inside the lock)** is for correctness. Several threads can pass the outer check before the first one finishes building. Without the inner check, each one builds its own after it gets the lock.
- **Outer check (outside the lock)** is for speed. Without it every call, forever, takes the lock, and all callers queue up on it for something that is already built. With it, the lock is only touched during the first few calls.
- Java also needs `volatile` here so other threads never see a half-built object. CPython assigns `cls._instance` only after `cls()` returns, so that problem does not arise.

## Option 3: a metaclass

Reusable across classes, and `Config()` looks like a normal constructor call.

```python
import pickle
import threading


class SingletonMeta(type):
    _instances: dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with SingletonMeta._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class Config(metaclass=SingletonMeta):
    def __init__(self, env: str = "dev") -> None:
        self.env = env

    def __reduce__(self):
        return (type(self), ())           # unpickle by calling Config()


class TestConfig(Config):
    pass


if __name__ == "__main__":
    a = Config("prod")
    assert Config("ignored") is a and a.env == "prod"  # later args are dropped
    assert TestConfig() is not a                      # one per subclass
    assert pickle.loads(pickle.dumps(a)) is a         # thanks to __reduce__
    SingletonMeta._instances.pop(Config)              # reset for a test
    assert Config("test").env == "test"
    print("metaclass ok")
```

`__init__` runs once, because the metaclass skips `type.__call__` after the first time. With a `__new__`-based singleton, `__init__` runs on every `Cls()` call and silently resets state.

## Option 4: the `functools.cache` trick

```python
import functools
import time
from concurrent.futures import ThreadPoolExecutor


class Settings:
    def __init__(self) -> None:
        time.sleep(0.01)


@functools.cache
def get_settings() -> Settings:
    return Settings()


if __name__ == "__main__":
    with ThreadPoolExecutor(16) as pool:
        cold = list(pool.map(lambda _: get_settings(), range(16)))
    assert len({id(s) for s in cold}) > 1      # cold race built several
    assert get_settings() is get_settings()    # warm: always the same one
    get_settings.cache_clear()                 # the test reset hook
```

The cache's dict stays consistent under threads, but the docs say the wrapped function may run more than once on concurrent misses, and each caller gets its own result. Use it when building twice is harmless, or call `get_settings()` once at startup before any threads exist.

## When to use, when not

| Use | Avoid |
|---|---|
| Expensive shared resource: config, connection pool, metrics registry | Anything you want to swap in tests; pass it in (dependency injection) instead |
| Process-wide state that must stay unique | State that must be unique across processes or machines; a singleton is per process |

## Pitfalls

- **Pickling and `copy.deepcopy`** rebuild the object through `__new__`, which skips your guard and creates a second instance. Define `__reduce__` to route through the accessor.
- **Subclassing** with the `_instance` class attribute: if `Registry.instance()` runs first, `Child.instance()` finds the inherited `_instance` and returns the parent object. Key the store by class, as the metaclass does.
- **Tests need a reset**: `cache_clear()`, `SingletonMeta._instances.pop(cls)`, or `Cls._instance = None` in a fixture. Otherwise test order changes results.
- **Arguments on later calls are ignored.** `Config("prod")` after `Config("dev")` returns the dev instance. Raise if they differ, or take no arguments.
- **Multiprocessing**: each worker process gets its own copy. With `fork`, a singleton built before the fork is duplicated, including any open sockets.

## Interview questions

- **Why not just lock every call?** It is correct but serializes every reader on one lock forever. Double-checked locking pays for the lock only during startup.
- **Is the module-level instance thread-safe?** Yes. The module body runs once under the import lock.
- **Does the GIL make the naive version safe?** No. A thread switch between the `None` check and the assignment, or a slow `__init__` that releases the GIL, lets a second thread in.
- **Singleton vs dependency injection?** Build one instance at startup and pass it to whoever needs it. You still get one instance, and tests can pass a fake.
