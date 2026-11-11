---
title: Heaps and top-k
section: dsa
order: 12
summary: Use heapq for top-k, k-way merge, scheduling and running medians, with the max-heap and tie-breaking tricks Python needs.
---

## When to use

- "Top k", "k largest / smallest", "k closest", "kth largest".
- Merge k sorted lists or arrays.
- Repeatedly take the min or max while inserting new items (scheduling, simulation, Dijkstra).
- Running median of a stream.

## Core idea

A binary heap gives `O(log n)` push/pop and `O(1)` peek at the minimum. Python's `heapq` is a **min-heap** on a plain list. For **k largest**, keep a min-heap of size `k`: the root is the smallest of the top k, so anything smaller than it can be dropped. That is `O(n log k)` instead of sorting in `O(n log n)`.

## Python heapq basics

```python
import heapq

h = []
heapq.heappush(h, 5)
smallest = h[0]                     # peek
x = heapq.heappop(h)
heapq.heapify(nums)                 # O(n), in place

heapq.heappush(h, -val)             # max-heap: negate
largest = -h[0]

heapq.heappush(h, (priority, i, item))  # tie-break with a counter
heapq.heappushpop(h, x)             # push then pop, faster than both
heapq.heapreplace(h, x)             # pop then push

heapq.nlargest(k, nums, key=...)    # O(n log k)
```

> **Gotcha:** Tuples compare element by element. If two priorities tie and the next field is a non-comparable object (e.g. `ListNode`), you get `TypeError`. Add a unique counter as the second field.

## Templates

### Kth largest / top k (min-heap of size k)

```python
def find_kth_largest(nums: list[int], k: int) -> int:
    h = []
    for x in nums:
        heapq.heappush(h, x)
        if len(h) > k:
            heapq.heappop(h)   # drop the smallest; h holds the k largest
    return h[0]
```

### K closest points (max-heap of size k)

```python
def k_closest(points: list[list[int]], k: int) -> list[list[int]]:
    h = []
    for x, y in points:
        d = x * x + y * y
        heapq.heappush(h, (-d, x, y))  # max-heap by distance
        if len(h) > k:
            heapq.heappop(h)           # drop the farthest
    return [[x, y] for _, x, y in h]
```

### K-way merge

```python
def merge_k_lists(lists):
    h = [(node.val, i, node) for i, node in enumerate(lists) if node]
    heapq.heapify(h)
    dummy = tail = ListNode()
    while h:
        _, i, node = heapq.heappop(h)
        tail.next = node
        tail = node
        if node.next:
            heapq.heappush(h, (node.next.val, i, node.next))
    return dummy.next
```

For sorted Python iterables, `heapq.merge(*iterables)` does this lazily.

### Two heaps: median of a stream

```python
class MedianFinder:
    def __init__(self):
        self.low = []   # max-heap (negated), the smaller half
        self.high = []  # min-heap, the larger half

    def add_num(self, num: int) -> None:
        heapq.heappush(self.low, -num)
        # Move the largest of low into high to keep ordering.
        heapq.heappush(self.high, -heapq.heappop(self.low))
        # Rebalance: low may have one extra element, never fewer.
        if len(self.high) > len(self.low):
            heapq.heappush(self.low, -heapq.heappop(self.high))

    def find_median(self) -> float:
        if len(self.low) > len(self.high):
            return -self.low[0]
        return (-self.low[0] + self.high[0]) / 2
```

### Scheduling (Task Scheduler / meeting rooms)

```python
def min_meeting_rooms(intervals: list[list[int]]) -> int:
    intervals.sort()
    ends = []  # end times of ongoing meetings
    for start, end in intervals:
        if ends and ends[0] <= start:
            heapq.heapreplace(ends, end)  # reuse the room that freed earliest
        else:
            heapq.heappush(ends, end)
    return len(ends)
```

## Complexity

| Operation | Time | Space |
|---|---|---|
| Push / pop | `O(log n)` | |
| Heapify | `O(n)` | `O(1)` extra |
| Top k with size-k heap | `O(n log k)` | `O(k)` |
| K-way merge, N total items | `O(N log k)` | `O(k)` |
| Median: add / find | `O(log n)` / `O(1)` | `O(n)` |
| Quickselect (alternative for kth) | `O(n)` avg, `O(n^2)` worst | `O(1)` |

## Pitfalls

- Using a max-heap of all `n` elements for top k: `O(n log n)`, no better than sorting.
- Forgetting to negate back when reading a max-heap.
- Non-comparable payloads in tuples. Add an index.
- Removing an arbitrary element is `O(n)`. Use **lazy deletion**: mark it invalid and skip when it surfaces at the top.
- `heapq` does not support decrease-key. Push a new entry and skip stale ones on pop (standard in Dijkstra).

## Classic problems

- **Kth Largest Element in an Array**: size-k min-heap or quickselect.
- **Kth Largest Element in a Stream**: keep a size-k heap across calls.
- **Last Stone Weight**: max-heap simulation.
- **K Closest Points to Origin**: size-k max-heap by distance.
- **Top K Frequent Elements**: counter + `nlargest`, or bucket sort.
- **Merge k Sorted Lists**: k-way merge with index tie-break.
- **Kth Smallest Element in a Sorted Matrix**: k-way merge over rows, or binary search on value.
- **Find Median from Data Stream**: two heaps.
- **Sliding Window Median**: two heaps + lazy deletion.
- **Task Scheduler**: max-heap of counts + cooldown queue, or `max(len(tasks), (max_count - 1) * (n + 1) + ties)`.
- **Meeting Rooms II**: min-heap of end times.
- **Reorganize String**: max-heap, hold back the last used char.
- **IPO**: two heaps, unlock projects by capital.
