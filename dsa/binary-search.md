---
title: Binary search
section: dsa
order: 7
summary: Find a boundary in a monotonic space in O(log n) with lower and upper bound templates, plus binary search on the answer.
---

## When to use

- Sorted array: find a value, first/last occurrence, insert position.
- Rotated sorted array, peak element, matrix with sorted rows.
- `O(log n)` is explicitly required.
- **Search on the answer:** "minimum capacity / speed / time such that X is possible", "minimize the maximum", "maximize the minimum". The check `feasible(x)` is monotonic: once true, it stays true.

## Core idea

Do not think "find the target". Think "find the **first index where a predicate becomes true**" over a `F F F T T T` sequence. Keep the invariant that the answer is always inside `[lo, hi]` (or `[lo, hi)`), and shrink the range by half each step. Most off-by-one bugs disappear once you pick one template and stick to it.

## Templates

### Lower bound: first index with `nums[i] >= target`

Half-open range `[lo, hi)`. Returns `len(nums)` if no such index. Same as `bisect.bisect_left`.

```python
def lower_bound(nums: list[int], target: int) -> int:
    lo, hi = 0, len(nums)
    while lo < hi:
        mid = (lo + hi) // 2
        if nums[mid] < target:
            lo = mid + 1   # mid is too small, answer is to the right
        else:
            hi = mid       # mid could be the answer, keep it
    return lo
```

### Upper bound: first index with `nums[i] > target`

Same as `bisect.bisect_right`. Only the comparison changes.

```python
def upper_bound(nums: list[int], target: int) -> int:
    lo, hi = 0, len(nums)
    while lo < hi:
        mid = (lo + hi) // 2
        if nums[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo
```

Derived answers:

| Question | Expression |
|---|---|
| Does `target` exist? | `i = lower_bound(...)`; `i < n and nums[i] == target` |
| First occurrence | `lower_bound` (if it matches) |
| Last occurrence | `upper_bound - 1` (if it matches) |
| Count of `target` | `upper_bound - lower_bound` |
| Insert position | `lower_bound` |
| Largest element `< target` | index `lower_bound - 1` |

```python
import bisect
bisect.bisect_left(nums, target)   # lower bound
bisect.bisect_right(nums, target)  # upper bound
bisect.bisect_left(nums, target, key=lambda x: x[0])  # Python 3.10+
```

### Generic predicate search

```python
def first_true(lo: int, hi: int, pred) -> int:
    """Smallest x in [lo, hi] with pred(x) True. pred is F..F T..T.
    Returns hi + 1 if pred is never True."""
    hi += 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pred(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo
```

### Search on the answer (Koko Eating Bananas)

```python
def min_eating_speed(piles: list[int], h: int) -> int:
    def can_finish(speed: int) -> bool:
        return sum((p + speed - 1) // speed for p in piles) <= h  # ceil division

    lo, hi = 1, max(piles)  # speed max(piles) always works
    while lo < hi:
        mid = (lo + hi) // 2
        if can_finish(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo
```

For "maximize x such that `ok(x)`" (`T T T F F`), find the first `False` and subtract one, or flip the predicate.

### Rotated sorted array

```python
def search_rotated(nums: list[int], target: int) -> int:
    lo, hi = 0, len(nums) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if nums[mid] == target:
            return mid
        if nums[lo] <= nums[mid]:               # left half sorted
            if nums[lo] <= target < nums[mid]:
                hi = mid - 1
            else:
                lo = mid + 1
        else:                                   # right half sorted
            if nums[mid] < target <= nums[hi]:
                lo = mid + 1
            else:
                hi = mid - 1
    return -1
```

## Complexity

- Index search: `O(log n)` time, `O(1)` space.
- Search on answer: `O(log(range) * cost(feasible))`, e.g. `O(n log max)` for Koko.

## Pitfalls

- **Infinite loop:** with `lo = mid` you must use `mid = (lo + hi + 1) // 2` (round up). The templates above never set `lo = mid`, so they are safe.
- Mixing closed `[lo, hi]` with `lo < hi`. Pick one style per template.
- Wrong search bounds on the answer: make sure `hi` is always feasible and `lo` is the smallest possible value.
- Ceil division: `(a + b - 1) // b` or `-(-a // b)`. Avoid `math.ceil(a / b)` with huge ints (float precision).
- Python ints do not overflow, but mention `lo + (hi - lo) // 2` for other languages.
- Rotated arrays with duplicates degrade to `O(n)` worst case.

## Classic problems

- **Binary Search**: plain lower bound.
- **Search Insert Position**: lower bound.
- **Find First and Last Position of Element**: lower and upper bound.
- **Search a 2D Matrix**: treat as a flat array, `r, c = divmod(mid, cols)`.
- **Find Minimum in Rotated Sorted Array**: compare `nums[mid]` with `nums[hi]`.
- **Search in Rotated Sorted Array**: find the sorted half.
- **Find Peak Element**: move toward the larger neighbor.
- **Koko Eating Bananas**: search on speed.
- **Capacity To Ship Packages Within D Days**: search on capacity, `lo = max(weights)`.
- **Split Array Largest Sum**: minimize the maximum, same as above.
- **Time Based Key-Value Store**: `bisect_right` on timestamps, minus one.
- **Median of Two Sorted Arrays**: binary search the partition of the shorter array.
