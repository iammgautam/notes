---
title: Intervals
section: dsa
order: 13
summary: Sort intervals and sweep to merge, insert, find overlaps, count rooms and pick non-overlapping sets.
---

## When to use

- Input is a list of `[start, end]` pairs: meetings, bookings, ranges, time slots.
- "Merge overlapping", "insert into sorted list", "do any overlap?", "minimum rooms", "remove fewest to make non-overlapping", "intersection of two lists".

## Core idea

Sort first. Sorting by **start** makes overlapping intervals adjacent, so you can merge in one pass. Sorting by **end** is the greedy choice for "keep as many non-overlapping intervals as possible". For "how many overlap at once", split into start and end events and sweep.

Two intervals `[a, b]` and `[c, d]` overlap iff `a <= d and c <= b` (closed ranges). Their intersection is `[max(a, c), min(b, d)]` when non-empty.

> **Gotcha:** Clarify whether touching intervals (`[1, 3]` and `[3, 5]`) overlap. For merging, usually yes; for meetings, a meeting ending at 3 frees the room for one starting at 3.

## Templates

### Merge intervals

```python
def merge(intervals: list[list[int]]) -> list[list[int]]:
    intervals.sort(key=lambda iv: iv[0])
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)  # extend, end may be inside
        else:
            merged.append([start, end])
    return merged
```

### Insert interval (input already sorted, non-overlapping)

```python
def insert(intervals: list[list[int]], new: list[int]) -> list[list[int]]:
    res = []
    i, n = 0, len(intervals)
    while i < n and intervals[i][1] < new[0]:      # entirely before
        res.append(intervals[i])
        i += 1
    while i < n and intervals[i][0] <= new[1]:     # overlapping: absorb
        new = [min(new[0], intervals[i][0]), max(new[1], intervals[i][1])]
        i += 1
    res.append(new)
    res.extend(intervals[i:])                      # entirely after
    return res
```

### Non-overlapping intervals (greedy by end)

```python
def erase_overlap_intervals(intervals: list[list[int]]) -> int:
    intervals.sort(key=lambda iv: iv[1])
    kept = 0
    prev_end = float("-inf")
    for start, end in intervals:
        if start >= prev_end:      # touching is fine here
            kept += 1
            prev_end = end
    return len(intervals) - kept
```

Same template answers **Minimum Number of Arrows to Burst Balloons** (use `start > prev_end`, since touching balloons share an arrow) and **Maximum number of meetings in one room**.

### Meeting rooms (sweep line)

```python
def min_meeting_rooms(intervals: list[list[int]]) -> int:
    starts = sorted(iv[0] for iv in intervals)
    ends = sorted(iv[1] for iv in intervals)
    rooms = best = 0
    e = 0
    for s in starts:
        while e < len(ends) and ends[e] <= s:  # free rooms that ended by s
            rooms -= 1
            e += 1
        rooms += 1
        best = max(best, rooms)
    return best
```

Generic event sweep: build `(time, +1)` for starts and `(time, -1)` for ends, sort, and track the running sum. Sorting tuples puts `-1` before `+1` at the same time, which treats touching intervals as non-overlapping.

### Interval list intersections (two pointers)

```python
def interval_intersection(A: list[list[int]], B: list[list[int]]) -> list[list[int]]:
    i = j = 0
    res = []
    while i < len(A) and j < len(B):
        lo = max(A[i][0], B[j][0])
        hi = min(A[i][1], B[j][1])
        if lo <= hi:
            res.append([lo, hi])
        if A[i][1] < B[j][1]:   # advance the one that ends first
            i += 1
        else:
            j += 1
    return res
```

## Complexity

| Problem | Time | Space |
|---|---|---|
| Merge, non-overlapping, rooms | `O(n log n)` (sort) | `O(n)` |
| Insert (already sorted) | `O(n)` | `O(n)` |
| Intersections of two sorted lists | `O(m + n)` | `O(m + n)` output |

## Pitfalls

- Forgetting `max` when merging: `[1, 10]` then `[2, 3]` must stay `[1, 10]`.
- Sorting by start for the "keep max non-overlapping" greedy. Sort by **end**.
- Inconsistent treatment of touching endpoints across problems. Ask.
- Mutating the input list when the caller still needs it. Mention it, or copy.

## Classic problems

- **Meeting Rooms**: sort by start, check `intervals[i][0] < intervals[i - 1][1]`.
- **Meeting Rooms II**: sweep line or min-heap of end times.
- **Merge Intervals**: sort by start, extend last.
- **Insert Interval**: before / overlap / after.
- **Non-overlapping Intervals**: greedy by end, count removals.
- **Minimum Number of Arrows to Burst Balloons**: greedy by end, strict `>`.
- **Interval List Intersections**: two pointers.
- **Employee Free Time**: merge all, gaps between merged intervals.
- **My Calendar I**: check overlap against booked list (or sorted list + bisect).
- **Car Pooling**: event sweep or difference array on time.
- **Minimum Interval to Include Each Query**: sort queries, heap of interval sizes.
