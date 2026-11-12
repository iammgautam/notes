---
title: Design a meeting room scheduler
section: lld
order: 39
phase: "Phase 5 · Intermediate"
summary: Rooms with capacity and features, conflict checks with bisect on sorted intervals, smallest-fit room search, recurring series, cancellation and observers.
---

## Requirements

Clarifying questions worth asking:

- Does the user pick a room, or ask for "any room for 6 people with video"? (Both.)
- Are back-to-back meetings allowed (10–11 then 11–12)? (Yes. A meeting is the half-open range `[start, end)`.)
- Which room do we pick when several fit? (The smallest that fits, so big rooms stay free for big meetings.)
- Recurring meetings? (Weekly series, all-or-nothing in one room. Treat as an extension if time is short.)
- Who can cancel? (Only the organizer.)

Functional:

- Rooms with a capacity and a set of features (`tv`, `vc`, `whiteboard`).
- Book a slot in a given room, or in any room that fits size and features.
- Find free rooms for a slot.
- Cancel a meeting; notify attendees on booking and cancellation.
- Book a weekly series.

Non-functional:

- No two meetings ever overlap in one room, with many users booking at once.
- Conflict check in `O(log n)` per room, not a scan of the whole calendar.
- Booking in one room never blocks booking in another.

Out of scope: attendee free/busy, time zones in the UI, approval workflows.

## Entities and relationships

```text
+------------------------------+  has *  +---------------------------+
| Scheduler                    |-------->| Room                      |
|------------------------------|         |---------------------------|
| rooms (capacity ascending)   |         | id, capacity, features    |
| meetings: {id: Meeting}      |         | meetings: [Meeting]       |
| listeners: [MeetingListener] |         |   sorted by start         |
| book(), book_series()        |         | lock: Lock                |
| find_free(), cancel()        |         | try_book([m]) -> bool     |
+------------------------------+         | is_free(), remove(m)      |
       | notifies *                      +---------------------------+
       v                                            | holds *
+------------------------------+                    v
| <<protocol>> MeetingListener |         +---------------------------+
| on_booked(m)                 |         | Meeting (frozen)          |
| on_cancelled(m)              |         | id, title, organizer      |
+------------------------------+         | attendees, start, end     |
       ^ is-a                            | room_id, series_id        |
  EmailNotifier, CalendarSync            +---------------------------+
```

- Each room keeps its meetings sorted by start and non-overlapping. Because of that invariant, a new interval can only clash with its two neighbors, which `bisect` finds in `O(log n)`.
- The lock lives on the room. "Find a free room and book it" becomes "try rooms in order, each try atomic", with no global lock.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Observer | `MeetingListener` | Scheduler code calling email, calendar sync and Slack directly |
| Per-object lock | `Room.lock` | A global lock serializing every booking in the building |
| Atomic try-operation | `Room.try_book` returns `bool` | A race between `is_free()` and the insert |
| Value object | `Meeting` (frozen) | A booked meeting's time changing without a re-check |

## Code

```python
from __future__ import annotations

import bisect
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True)
class Meeting:
    id: str
    title: str
    organizer: str
    attendees: frozenset[str]
    start: datetime
    end: datetime                    # exclusive: 10-11 and 11-12 both fit
    room_id: str
    series_id: str | None = None


def by_start(m: Meeting) -> datetime:
    return m.start


class Room:
    def __init__(self, id: str, capacity: int, features: set[str]) -> None:
        self.id, self.capacity, self.features = id, capacity, frozenset(features)
        self.meetings: list[Meeting] = []          # sorted by start, no overlaps
        self.lock = threading.Lock()

    def _fits(self, start: datetime, end: datetime) -> bool:
        i = bisect.bisect_left(self.meetings, start, key=by_start)
        if i > 0 and self.meetings[i - 1].end > start:
            return False                           # previous meeting runs into us
        return i == len(self.meetings) or self.meetings[i].start >= end

    def is_free(self, start: datetime, end: datetime) -> bool:
        with self.lock:
            return self._fits(start, end)

    def try_book(self, meetings: list[Meeting]) -> bool:
        """All or nothing: one meeting, or every occurrence of a series."""
        with self.lock:
            if not all(self._fits(m.start, m.end) for m in meetings):
                return False
            for m in meetings:
                bisect.insort(self.meetings, m, key=by_start)
            return True

    def remove(self, meeting: Meeting) -> None:
        with self.lock:
            i = bisect.bisect_left(self.meetings, meeting.start, key=by_start)
            if i < len(self.meetings) and self.meetings[i].id == meeting.id:
                del self.meetings[i]


class MeetingListener(Protocol):
    def on_booked(self, meeting: Meeting) -> None: ...
    def on_cancelled(self, meeting: Meeting) -> None: ...


class NoRoomAvailable(Exception):
    pass


class Scheduler:
    def __init__(self, rooms: list[Room]) -> None:
        self.rooms = sorted(rooms, key=lambda r: r.capacity)   # smallest fit first
        self.by_id = {r.id: r for r in rooms}
        self.meetings: dict[str, Meeting] = {}
        self.listeners: list[MeetingListener] = []
        self._lock = threading.Lock()              # guards self.meetings only

    def _matching(self, size: int, features: frozenset[str],
                  room_id: str | None) -> list[Room]:
        return [r for r in self.rooms if r.capacity >= size
                and features <= r.features and room_id in (None, r.id)]

    def find_free(self, start: datetime, end: datetime, size: int,
                  features: frozenset[str] = frozenset()) -> list[Room]:
        return [r for r in self._matching(size, features, None)
                if r.is_free(start, end)]

    def book(self, title: str, organizer: str, attendees: set[str],
             start: datetime, end: datetime, **kw) -> Meeting:
        return self.book_series(title, organizer, attendees, start, end, **kw)[0]

    def book_series(self, title: str, organizer: str, attendees: set[str],
                    start: datetime, end: datetime, *, count: int = 1,
                    every: timedelta = timedelta(weeks=1),
                    features: frozenset[str] = frozenset(),
                    room_id: str | None = None) -> list[Meeting]:
        if not start < end or count < 1:
            raise ValueError("need start < end and count >= 1")
        if count > 1 and every < end - start:
            raise ValueError("occurrences would overlap each other")
        people = frozenset(attendees) | {organizer}
        series = uuid.uuid4().hex if count > 1 else None
        for room in self._matching(len(people), features, room_id):
            occ = [Meeting(uuid.uuid4().hex, title, organizer, people,
                           start + i * every, end + i * every, room.id, series)
                   for i in range(count)]
            if room.try_book(occ):                 # atomic per room
                with self._lock:
                    self.meetings.update((m.id, m) for m in occ)
                for m in occ:                      # notify outside every lock
                    for listener in self.listeners:
                        listener.on_booked(m)
                return occ
        raise NoRoomAvailable(f"{len(people)} people at {start:%a %H:%M}")

    def cancel(self, meeting_id: str, user: str) -> None:
        with self._lock:
            m = self.meetings.get(meeting_id)
            if m is None:
                return                             # already cancelled: no-op
            if m.organizer != user:
                raise PermissionError("only the organizer can cancel")
            del self.meetings[meeting_id]
        self.by_id[m.room_id].remove(m)
        for listener in self.listeners:
            listener.on_cancelled(m)


if __name__ == "__main__":
    import random
    from concurrent.futures import ThreadPoolExecutor

    class EmailNotifier:
        def __init__(self) -> None:
            self.outbox: list[tuple[str, str]] = []

        def on_booked(self, m):          # list.extend is one call: thread-safe
            self.outbox.extend((p, f"Invite: {m.title}") for p in m.attendees)

        def on_cancelled(self, m):
            self.outbox.extend((p, f"Cancelled: {m.title}") for p in m.attendees)

    def at(h: int, mi: int = 0, day: int = 16) -> datetime:   # 16 Nov 2026: Monday
        return datetime(2026, 11, day, h, mi)

    s = Scheduler([Room("hall", 40, {"vc"}), Room("huddle", 4, {"tv"}),
                   Room("board", 12, {"tv", "vc"})])
    mail = EmailNotifier()
    s.listeners.append(mail)

    m1 = s.book("design", "ann", {"bob", "cy"}, at(10), at(11))
    m2 = s.book("1:1", "dan", {"eve"}, at(10, 30), at(11, 30))
    m3 = s.book("sync", "ann", {"bob"}, at(11), at(12))          # back-to-back
    m4 = s.book("demo", "fay", {"g", "h", "i", "j"}, at(10), at(11),
                features=frozenset({"vc"}))
    assert [m.room_id for m in (m1, m2, m3, m4)] == ["huddle", "board", "huddle", "hall"]
    try:
        s.book("x", "zed", set(), at(10, 15), at(10, 45), room_id="huddle")
        raise AssertionError("overlap booked")
    except NoRoomAvailable:
        pass
    assert s.find_free(at(10), at(11), 2) == []
    assert len(s.find_free(at(12), at(13), 2)) == 3

    s.book("offsite prep", "kim", {"lee"}, at(9, day=30), at(10, day=30), room_id="board")
    standup = s.book_series("standup", "kim", {"lee", "mo"}, at(9), at(9, 15),
                            count=4, features=frozenset({"vc"}))  # board busy week 3
    assert {m.room_id for m in standup} == {"hall"}
    assert len({m.series_id for m in standup}) == 1
    before = len(s.by_id["board"].meetings)
    try:
        s.book_series("retro", "kim", {"lee"}, at(9), at(9, 30), count=3, room_id="board")
        raise AssertionError("series booked over a conflict")
    except NoRoomAvailable:
        assert len(s.by_id["board"].meetings) == before            # nothing partial

    try:
        s.cancel(m1.id, "bob")
        raise AssertionError("non-organizer cancelled")
    except PermissionError:
        pass
    s.cancel(m1.id, "ann")
    s.cancel(m1.id, "ann")                                         # retry: no-op
    assert s.by_id["huddle"].is_free(at(10), at(11))
    assert mail.outbox.count(("cy", "Cancelled: design")) == 1

    # Stress 1: 40 threads want the same slot in any room; exactly 3 win.
    def grab(i: int) -> Meeting | None:
        try:
            return s.book(f"m{i}", f"u{i}", set(), at(15, day=17), at(16, day=17))
        except NoRoomAvailable:
            return None

    with ThreadPoolExecutor(16) as pool:
        won = [m for m in pool.map(grab, range(40)) if m]
    assert sorted(m.room_id for m in won) == ["board", "hall", "huddle"]

    # Stress 2: 400 random requests for one room; it stays sorted and overlap-free.
    def rand(_: int) -> None:
        start = at(8, day=18) + timedelta(minutes=15 * random.randint(0, 40))
        try:
            s.book("r", "u", set(), start,
                   start + timedelta(minutes=random.choice([30, 45, 60])), room_id="board")
        except NoRoomAvailable:
            pass

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(rand, range(400)))
    ms = s.by_id["board"].meetings
    assert all(a.end <= b.start for a, b in zip(ms, ms[1:]))
    print("meeting room scheduler ok")
```

## Key flows

```text
User        Scheduler            huddle (cap 4)       board (cap 12)
 | book(3 people, 10:30-11:30)      |                       |
 |---------->| rooms matching size & features, smallest first
 |           | try_book([m]) ------>|                       |
 |           |                      | lock; bisect: prev    |
 |           |                      | 10:00-11:00 ends late |
 |           |<----- False ---------|                       |
 |           | try_book([m]) ---------------------------->  |
 |           |                      | lock; neighbors clear; insort
 |           |<----------------------------------- True ----|
 |           | record in meetings; on_booked() per listener |
 |<-- m -----|  (listeners called after every lock is released)
```

```text
Organizer     Scheduler                 Room                Listeners
 | cancel(id)    |                        |                      |
 |-------------->| lock: find, check organizer, delete from map  |
 |               | remove(m) ------------>| bisect to m, del     |
 |               | on_cancelled(m) ------------------------------>|
 |<-- ok --------| (second cancel: id unknown, no-op)            |
```

## Edge cases

- Back-to-back meetings: allowed, because the check is `prev.end > start` and `next.start >= end`.
- `start >= end`: reject. Meetings across midnight work because times are full datetimes.
- The organizer counts toward room size.
- Series with `every` shorter than the meeting: reject, occurrences would overlap.
- A series that clashes in one week: the whole series moves to the next matching room, or fails; nothing is half-booked.
- Cancelling one occurrence of a series: cancel that meeting id; cancelling the series: cancel every id with that `series_id`.
- A room taken out of service: cancel or move its future meetings and notify through the same listeners.

## Production follow-ups

**How is booking thread-safe without a global lock?** `try_book` does the bisect check and the insert under the room's lock, so the check can never go stale before the write. The GIL does not make that pair atomic. The scheduler lock and a room lock are never held together, so there is no lock-ordering deadlock, and listeners run after all locks are released so a slow email server cannot block bookings. See [deadlock, livelock and starvation]({{ '/lld/deadlock-livelock-starvation/' | relative_url }}).

**What survives a restart?** Meetings live in a table; each room's sorted list is a cache rebuilt per room (and per day range) on demand with `ORDER BY start_at`. In PostgreSQL the overlap rule moves into the schema: `EXCLUDE USING gist (room_id WITH =, tstzrange(start_at, end_at) WITH &&)`, and a series is inserted in one transaction so it is still all or nothing.

**How do retries stay safe?** The client sends a request id with each booking; a unique constraint on it turns a retried POST into "return the existing meeting". Cancel is already a no-op the second time. Notifications carry `(meeting_id, event)` as a dedup key so a retried cancel never emails twice.

**How do you add a new room-selection rule or integration?** Room choice becomes a strategy (smallest fit, nearest to the organizer's floor, cheapest), passed into `Scheduler`. Slack messages, calendar sync and analytics are new listeners. The booking code does not change for either.

**What breaks at 10× or across servers?** In-memory per-room lists only work in one process. With several servers, the database exclusion constraint (or `SELECT ... FOR UPDATE` on the room row) is the real guard, and an index on `(room_id, start_at)` keeps the neighbor lookup fast. "Find a free room" across hundreds of rooms becomes one query with `NOT EXISTS` over overlapping meetings; shard by building or office.

**What do you log and measure?** Bookings, conflicts, `NoRoomAvailable` by requested size and features (tells facilities which rooms to build), utilization per room and hour, and no-shows (booked, never checked in). Log meeting id, room, organizer and range on every change.

**What about ghost meetings and time zones?** Release a room if nobody checks in within 10 minutes of start, through the same cancel path so attendees are notified. Store times in UTC, but generate recurring occurrences in the organizer's local zone and then convert, or a 9:00 standup becomes 8:00 after a daylight-saving change.

## Interview follow-ups

- **Minimum rooms needed for a list of meetings?** Sort by start and keep a min-heap of end times; pop when the earliest end is at or before the next start. The heap's peak size is the answer, in `O(n log n)`.
- **Free slots for a room on a day?** Walk the sorted list and emit each gap between `prev.end` and `next.start` that is long enough.
- **Why not an interval tree?** A room has tens of meetings a day, so a sorted list with `bisect` is simpler; `insort` is `O(n)` for the shift, which is negligible at that size. Use a balanced tree only when lists get large.
- **A meeting that needs two rooms at once (hybrid)?** Lock both rooms in a fixed order (sorted by room id) before checking, so two such bookings cannot deadlock.
- **Attendee conflicts?** Give each person the same sorted-list calendar and check it alongside the room, under the same lock-ordering rule.
