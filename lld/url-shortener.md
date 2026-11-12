---
title: Design a URL shortener (LLD)
section: lld
order: 36
phase: "Phase 5 · Intermediate"
summary: Class-level URL shortener with pluggable code generators (counter + base62, random, hash), custom aliases, expiry, click counts and a repository interface.
---

This note is the class design an LLD round expects. For caching, sharding and the read-heavy redirect path at scale, see the [HLD URL shortener]({{ '/hld/url-shortener/' | relative_url }}).

## Requirements

Clarifying questions worth asking:

- How are codes generated, and must they be unguessable? (Pluggable; discuss the trade-offs.)
- Can users pick a custom alias? (Yes, 4–30 characters, letters, digits, `-`, `_`.)
- Do links expire? (Optional TTL per link.)
- Does shortening the same URL twice return the same code? (No by default; each call is a new link with its own stats.)
- Do we count clicks? (Yes, a simple counter per link.)

Functional:

- `shorten(long_url, owner, alias=None, ttl=None)` returns a link with a short code.
- `resolve(code)` returns the long URL and counts the click; unknown or expired codes fail.

Non-functional:

- Two links never get the same code, even under concurrent requests.
- Generators and storage are swappable without touching the service.
- Redirect is the hot path: cheap, no global lock.

Out of scope: analytics beyond a counter, user accounts, the HTTP layer, malware scanning.

## Entities and relationships

```text
+-----------------------------+ uses  +------------------------------+
| Shortener                   |------>| <<abstract>> CodeGenerator   |
|-----------------------------|       | generate(url, attempt) -> str|
| shorten(url, owner,         |       +------------------------------+
|         alias?, ttl?)       |         ^ is-a
| resolve(code) -> url        |         | CounterGenerator (lock, n)
| clock, max_attempts         |         | RandomGenerator (secrets)
+-----------------------------+         | HashGenerator (sha256)
          | uses
          v
+-----------------------------+ stores * +-------------------------+
| <<abstract>> LinkRepository |--------->| Link                    |
|-----------------------------|          | code, long_url, owner   |
| add_if_absent(link) -> bool |          | created_at, expires_at  |
| get(code) -> Link | None    |          | clicks                  |
| increment_clicks(code)      |          | expired(now)            |
+-----------------------------+          +-------------------------+
          ^ is-a
  InMemoryLinkRepository (dict + Lock); a Postgres one later
```

- `add_if_absent` is the one atomic primitive the design relies on. Every generator can collide with a custom alias, so the service always inserts with "only if absent" and retries on collision.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Strategy | `CodeGenerator` | Code generation hard-wired to one scheme, rewritten when requirements change |
| Repository | `LinkRepository` | Service code full of dict or SQL details; no way to test without a database |
| Dependency injection | `repo`, `generator`, `clock` passed in | Tests that wait for real TTLs or need real storage |
| Retry loop with atomic insert | `shorten()` | Check-then-insert races that hand one code to two URLs |

## Code

```python
from __future__ import annotations

import hashlib
import re
import secrets
import string
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase  # 62
ALIAS = re.compile(r"[A-Za-z0-9_-]{4,30}")


def base62(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 62)
        out = ALPHABET[r] + out
        if n == 0:
            return out


class NotFound(Exception):
    pass


class AliasTaken(Exception):
    pass


@dataclass
class Link:
    code: str
    long_url: str
    owner: str
    created_at: float
    expires_at: float | None = None
    clicks: int = 0

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class LinkRepository(ABC):
    @abstractmethod
    def add_if_absent(self, link: Link) -> bool:
        """Insert atomically; False if the code exists (DB: unique constraint)."""

    @abstractmethod
    def get(self, code: str) -> Link | None: ...

    @abstractmethod
    def increment_clicks(self, code: str) -> None: ...


class InMemoryLinkRepository(LinkRepository):
    def __init__(self) -> None:
        self._links: dict[str, Link] = {}
        self._lock = threading.Lock()

    def add_if_absent(self, link):
        with self._lock:                # Java: ConcurrentHashMap.putIfAbsent
            if link.code in self._links:
                return False
            self._links[link.code] = link
            return True

    def get(self, code):
        return self._links.get(code)    # one dict read: no lock needed

    def increment_clicks(self, code):
        with self._lock:                # += is read-add-write, not atomic
            self._links[code].clicks += 1


class CodeGenerator(ABC):
    @abstractmethod
    def generate(self, long_url: str, attempt: int) -> str: ...


class CounterGenerator(CodeGenerator):
    """Unique and short, but sequential, so codes are guessable."""

    def __init__(self, start: int = 62 ** 5) -> None:   # first code: 6 chars
        self._next = start
        self._lock = threading.Lock()   # Java: AtomicLong.getAndIncrement()

    def generate(self, long_url, attempt):
        with self._lock:
            n, self._next = self._next, self._next + 1
        return base62(n)


class RandomGenerator(CodeGenerator):
    """Unguessable; collisions are rare and handled by the retry loop."""

    def __init__(self, length: int = 7) -> None:
        self.length = length

    def generate(self, long_url, attempt):
        return "".join(secrets.choice(ALPHABET) for _ in range(self.length))


class HashGenerator(CodeGenerator):
    """Deterministic: the same URL maps to the same first candidate."""

    def __init__(self, length: int = 7) -> None:
        self.length = length

    def generate(self, long_url, attempt):
        digest = hashlib.sha256(f"{long_url}#{attempt}".encode()).digest()
        return base62(int.from_bytes(digest[:8]))[: self.length].rjust(self.length, "0")


class Shortener:
    def __init__(self, repo: LinkRepository, generator: CodeGenerator,
                 clock: Callable[[], float] = time.time, max_attempts: int = 5) -> None:
        self.repo, self.generator = repo, generator
        self.clock, self.max_attempts = clock, max_attempts

    def shorten(self, long_url: str, owner: str, alias: str | None = None,
                ttl: float | None = None) -> Link:
        parts = urlsplit(long_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"not an http(s) URL: {long_url!r}")
        now = self.clock()
        expires = now + ttl if ttl else None
        if alias is not None:
            if not ALIAS.fullmatch(alias):
                raise ValueError("alias: 4-30 of A-Z a-z 0-9 _ -")
            link = Link(alias, long_url, owner, now, expires)
            if not self.repo.add_if_absent(link):
                raise AliasTaken(alias)
            return link
        for attempt in range(self.max_attempts):
            link = Link(self.generator.generate(long_url, attempt),
                        long_url, owner, now, expires)
            if self.repo.add_if_absent(link):
                return link
        raise RuntimeError("no free code after retries; keyspace too full")

    def resolve(self, code: str) -> str:
        link = self.repo.get(code)
        if link is None or link.expired(self.clock()):
            raise NotFound(code)
        self.repo.increment_clicks(code)
        return link.long_url


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor

    now = [1_000.0]
    repo = InMemoryLinkRepository()
    svc = Shortener(repo, CounterGenerator(), clock=lambda: now[0])

    a = svc.shorten("https://example.com/a", "u1")
    b = svc.shorten("https://example.com/b", "u1")
    assert (a.code, b.code) == ("100000", "100001")
    svc.shorten("https://example.com/x", "u2", alias="100002")   # alias in the way
    assert svc.shorten("https://example.com/c", "u1").code == "100003"  # retried

    assert svc.resolve(a.code) == "https://example.com/a"
    assert svc.shorten("https://blog.dev", "u2", alias="my-blog").code == "my-blog"
    for bad in ("my-blog", "no"):
        try:
            svc.shorten("https://other.dev", "u3", alias=bad)
            raise AssertionError("bad alias accepted")
        except (AliasTaken, ValueError):
            pass

    t = svc.shorten("https://sale.dev", "u1", ttl=60)
    now[0] += 61
    try:
        svc.resolve(t.code)
        raise AssertionError("expired link resolved")
    except NotFound:
        pass

    h = HashGenerator()
    assert h.generate("https://x.dev", 0) == h.generate("https://x.dev", 0)
    hashed = Shortener(InMemoryLinkRepository(), h)
    first, second = (hashed.shorten("https://x.dev", "u1") for _ in range(2))
    assert first.code != second.code and len(second.code) == 7

    # Stress: 16 threads shorten 4,000 links with counter and random codes,
    # then 5,000 concurrent clicks on one link.
    for gen in (CounterGenerator(), RandomGenerator(length=3)):  # 3 chars: collisions
        s = Shortener(InMemoryLinkRepository(), gen, max_attempts=50)
        with ThreadPoolExecutor(16) as pool:
            codes = list(pool.map(lambda i: s.shorten(f"https://e.dev/{i}", "u").code,
                                  range(4000)))
        assert len(set(codes)) == 4000
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda _: svc.resolve(b.code), range(5000)))
    assert repo.get(b.code).clicks == 5000
    print("url shortener ok")
```

## Key flows

```text
Client        Shortener        CodeGenerator       LinkRepository
  | shorten(url)  |                   |                    |
  |-------------->| validate URL      |                    |
  |               | generate(url, 0) >|                    |
  |               |<------ "100003" --|                    |
  |               | add_if_absent(link) ------------------>|
  |               |<----------------------- False (taken) -|
  |               | generate(url, 1) >|                    |
  |               |<------ "100004" --|                    |
  |               | add_if_absent(link) ------------------>|
  |               |<------------------------------ True ---|
  |<-- Link ------|                   |                    |
```

```text
Browser       Shortener                        LinkRepository
  | GET /abc123  |                                    |
  |------------->| get("abc123") -------------------->|
  |              |<--------------------- Link --------|
  |              | expired(now)? -> NotFound (404)    |
  |              | increment_clicks("abc123") ------->|
  |<-- 302 to long_url                                |
```

## Edge cases

- Custom alias that equals a future counter code: the counter's insert fails and the loop takes the next number.
- Alias that clashes with a route (`api`, `admin`, `login`): keep a reserved-word blocklist.
- `javascript:` or `ftp:` URLs: rejected; only http(s).
- Expired link: 404 (or 410 Gone). Never reuse its code, or old emails and QR codes point to someone else's page.
- Keyspace nearly full (short random codes): retries climb, then `shorten` fails loudly. Grow the length before that.
- Shortening a short link (redirect loops): reject URLs on your own domain.

## Production follow-ups

**What must be thread-safe?** Three things, each a classic check-then-act: the counter (`AtomicLong` in Java, a lock here), the insert (`putIfAbsent`), and the click counter. The GIL does not make `clicks += 1` or `if code not in d: d[code] = link` atomic. Reads take no lock, so redirects never wait on writers. See [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}).

**What survives a restart?** Links live in a table with `code` as the primary key, and `add_if_absent` becomes `INSERT ... ON CONFLICT (code) DO NOTHING RETURNING code`. The counter must not restart at zero: use a database sequence, or have each server reserve a block of 1,000 ids (`UPDATE counters SET next = next + 1000 RETURNING next`). A crash only wastes the unused part of a block.

**What about client retries?** A retried `shorten` creates a second link unless the client sends an idempotency key, which the server maps to the code it already created. `resolve` is a GET and naturally idempotent; click counts are approximate and at-least-once is acceptable.

**How do you add a new code scheme?** Write another `CodeGenerator`, such as a counter scrambled through a reversible permutation so codes look random but never collide, and pass it in. `Shortener` and the repository stay unchanged; so does everything else when you swap `InMemoryLinkRepository` for a Postgres or DynamoDB one.

**What breaks at 10× and across machines?** A single in-process counter cannot be shared across servers, so use range allocation as above. Redirects outnumber creates about 100:1, so put a cache (Redis or an in-process LRU) in front of `get`. Writing a row on every click becomes the bottleneck: buffer counts in memory and flush every few seconds, or emit click events to a stream. The [HLD note]({{ '/hld/url-shortener/' | relative_url }}) covers the full picture.

**What do you log and measure?** Redirect latency p99, cache hit rate, 404 rate (a spike means scanning or a broken campaign), collision retries per shorten (rising means the keyspace is filling), and links created per user (abuse). Log shorten requests with owner and domain; do not log full URLs if they can contain tokens.

**How do you stop abuse?** Rate-limit link creation per user, check target domains against a malware/phishing list at creation and periodically after, and prefer unguessable codes when links may point to private documents.

## Interview follow-ups

- **301 or 302?** 302 (or 307) if you count clicks or allow edits; a 301 is cached by browsers, so later clicks never reach you.
- **How long should codes be?** 62^7 is about 3.5 trillion. At 100 million links a day that lasts decades, and random codes stay sparse enough that collisions are rare.
- **Which generator would you pick?** Counter with range allocation when guessability does not matter; random with a collision check when it does; hash when you want the same URL to map to the same code (check that the existing row's URL matches before reusing it).
- **Same URL from the same user twice?** If required, look up `(owner, long_url)` first with a unique index, and return the existing link.
