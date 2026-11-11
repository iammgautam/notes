---
title: Prefix sums
section: dsa
order: 6
summary: Answer range-sum queries in O(1) and count subarrays with a target sum using prefix sums plus a hash map.
---

## When to use

- Many queries for "sum of `nums[i..j]`".
- "Count / find subarrays with sum equal to `k`", especially when **negatives** are allowed (sliding window fails).
- "Subarray sum divisible by `k`", "equal number of 0s and 1s".
- 2D grids: sum of a sub-rectangle.
- Range updates applied offline: difference arrays.

## Core idea

Let `prefix[i]` be the sum of the first `i` elements, with `prefix[0] = 0`. Then `sum(nums[i:j]) == prefix[j] - prefix[i]`. A subarray ending at `j` sums to `k` exactly when some earlier prefix equals `prefix[j] - k`, so a hash map of prefix counts finds them in one pass.

## Templates

### 1D prefix array

```python
from itertools import accumulate

nums = [3, 1, 4, 1, 5]
prefix = [0, *accumulate(nums)]   # len n + 1: [0, 3, 4, 8, 9, 14]

def range_sum(i: int, j: int) -> int:
    """Sum of nums[i..j], inclusive."""
    return prefix[j + 1] - prefix[i]
```

### Subarray sum equals k (prefix sum + hash map)

```python
from collections import defaultdict

def subarray_sum(nums: list[int], k: int) -> int:
    seen = defaultdict(int)
    seen[0] = 1          # empty prefix: lets a subarray start at index 0
    total = count = 0
    for x in nums:
        total += x
        count += seen[total - k]  # earlier prefixes that leave sum k
        seen[total] += 1          # add AFTER counting
    return count
```

Variants of the same template:

| Problem | Map key | Map value | Look up |
|---|---|---|---|
| Count subarrays sum = k | prefix sum | count | `total - k` |
| Longest subarray sum = k | prefix sum | **first** index | `total - k`; init `{0: -1}` |
| Subarray sum divisible by k | `total % k` | count | same remainder |
| Contiguous Array (0/1 balance) | running `+1/-1` | first index | same balance |
| Continuous Subarray Sum (len ≥ 2) | `total % k` | first index | same remainder, `i - j >= 2` |

### Longest subarray with sum k

```python
def max_len_sum_k(nums: list[int], k: int) -> int:
    first = {0: -1}  # prefix value -> earliest index
    total = best = 0
    for i, x in enumerate(nums):
        total += x
        if total - k in first:
            best = max(best, i - first[total - k])
        first.setdefault(total, i)  # keep earliest only
    return best
```

### 2D prefix sum

```python
def build_2d(grid: list[list[int]]) -> list[list[int]]:
    rows, cols = len(grid), len(grid[0])
    P = [[0] * (cols + 1) for _ in range(rows + 1)]
    for r in range(rows):
        for c in range(cols):
            P[r + 1][c + 1] = grid[r][c] + P[r][c + 1] + P[r + 1][c] - P[r][c]
    return P

def region_sum(P, r1: int, c1: int, r2: int, c2: int) -> int:
    """Inclusive corners (r1, c1) to (r2, c2)."""
    return P[r2 + 1][c2 + 1] - P[r1][c2 + 1] - P[r2 + 1][c1] + P[r1][c1]
```

### Difference array (range add, then read all)

```python
def apply_updates(n: int, updates: list[tuple[int, int, int]]) -> list[int]:
    diff = [0] * (n + 1)
    for start, end, val in updates:  # inclusive range
        diff[start] += val
        diff[end + 1] -= val
    return list(accumulate(diff[:n]))
```

## Complexity

| Operation | Time | Space |
|---|---|---|
| Build 1D / 2D | `O(n)` / `O(R * C)` | same |
| Range query | `O(1)` | |
| Subarray sum = k | `O(n)` | `O(n)` |
| Difference array | `O(n + q)` | `O(n)` |

## Pitfalls

- Forgetting `seen[0] = 1` (or `{0: -1}`), which misses subarrays starting at index 0.
- Updating the map before the lookup, so `k = 0` counts the empty subarray.
- Mixing inclusive and exclusive indices. Stick to `prefix[j + 1] - prefix[i]` for inclusive `[i, j]`.
- Python `%` with negative numbers returns a non-negative result for positive `k`, which is what you want. In other languages, normalize.
- For "longest", store the **first** index only (`setdefault`).

## Classic problems

- **Range Sum Query - Immutable**: 1D prefix.
- **Range Sum Query 2D - Immutable**: 2D prefix with inclusion-exclusion.
- **Subarray Sum Equals K**: prefix count map.
- **Subarray Sums Divisible by K**: remainder count map.
- **Continuous Subarray Sum**: remainder → first index, length ≥ 2.
- **Contiguous Array**: map 0 to -1, longest zero-sum subarray.
- **Product of Array Except Self**: prefix and suffix products.
- **Find Pivot Index**: `left_sum == total - left_sum - nums[i]`.
- **Corporate Flight Bookings** / **Car Pooling**: difference array.
- **Path Sum III**: prefix sum map on root-to-node paths with backtracking.
