---
title: Two pointers
section: dsa
order: 4
summary: Walk two indices through a sorted array or string to replace a nested loop with a single O(n) pass.
---

## When to use

- Input is **sorted** (or you can sort it) and you need a pair/triplet with a target sum.
- Palindrome checks, reversing in place.
- In-place array edits: remove duplicates, move zeros, partition.
- Merging two sorted arrays or lists.
- "Container" problems where moving the smaller side is provably safe.

## Core idea

Keep two indices and move one of them based on a comparison, so every step discards candidates that cannot be the answer. Each pointer moves at most `n` times, giving `O(n)`. Three variants: **opposite ends** (converge inward), **same direction** (slow writer, fast reader), and **two sequences** (one pointer per input).

## Templates

### Opposite ends (sorted pair sum)

```python
def two_sum_sorted(nums: list[int], target: int) -> list[int]:
    lo, hi = 0, len(nums) - 1
    while lo < hi:
        s = nums[lo] + nums[hi]
        if s == target:
            return [lo, hi]
        if s < target:
            lo += 1  # need a bigger sum
        else:
            hi -= 1  # need a smaller sum
    return []
```

### Same direction (slow writer, fast reader)

```python
def remove_duplicates(nums: list[int]) -> int:
    """Sorted input. Returns the new length; nums[:k] is deduplicated."""
    if not nums:
        return 0
    write = 1
    for read in range(1, len(nums)):
        if nums[read] != nums[write - 1]:
            nums[write] = nums[read]
            write += 1
    return write
```

### 3Sum (sort + fix one + opposite ends)

```python
def three_sum(nums: list[int]) -> list[list[int]]:
    nums.sort()
    res = []
    n = len(nums)
    for i in range(n - 2):
        if nums[i] > 0:
            break
        if i > 0 and nums[i] == nums[i - 1]:
            continue  # skip duplicate anchors
        lo, hi = i + 1, n - 1
        while lo < hi:
            s = nums[i] + nums[lo] + nums[hi]
            if s < 0:
                lo += 1
            elif s > 0:
                hi -= 1
            else:
                res.append([nums[i], nums[lo], nums[hi]])
                lo += 1
                hi -= 1
                while lo < hi and nums[lo] == nums[lo - 1]:
                    lo += 1  # skip duplicate second values
    return res
```

### Container with most water

```python
def max_area(height: list[int]) -> int:
    lo, hi = 0, len(height) - 1
    best = 0
    while lo < hi:
        best = max(best, (hi - lo) * min(height[lo], height[hi]))
        # The shorter wall limits the area; moving the taller one cannot help.
        if height[lo] < height[hi]:
            lo += 1
        else:
            hi -= 1
    return best
```

### Palindrome with skips

```python
def is_palindrome(s: str) -> bool:
    lo, hi = 0, len(s) - 1
    while lo < hi:
        if not s[lo].isalnum():
            lo += 1
        elif not s[hi].isalnum():
            hi -= 1
        elif s[lo].lower() != s[hi].lower():
            return False
        else:
            lo += 1
            hi -= 1
    return True
```

## Complexity

| Variant | Time | Space |
|---|---|---|
| Opposite ends / same direction | `O(n)` | `O(1)` |
| With initial sort | `O(n log n)` | `O(1)` to `O(n)` (sort) |
| 3Sum | `O(n^2)` | `O(1)` extra |
| kSum | `O(n^(k-1))` | `O(k)` recursion |

## Pitfalls

- Using `lo <= hi` when a pair needs two distinct indices. Use `lo < hi`.
- Forgetting to skip duplicates in 3Sum, producing repeated triplets.
- Applying opposite-end pointers to an **unsorted** array. Sort first, or use a hash map if you need original indices.
- Infinite loops: every branch must move at least one pointer.
- Sorting destroys original indices. Sort `(value, index)` pairs if you must return indices.

## Classic problems

- **Valid Palindrome**: skip non-alphanumerics, compare lowercase.
- **Two Sum II (sorted)**: opposite ends.
- **3Sum** / **3Sum Closest** / **4Sum**: sort, anchor, two pointers.
- **Container With Most Water**: move the shorter wall.
- **Trapping Rain Water**: track `left_max`, `right_max`; move the side with the smaller max.
- **Remove Duplicates from Sorted Array**: slow writer, fast reader.
- **Move Zeroes**: write non-zeros forward, fill the rest with zeros.
- **Sort Colors**: Dutch national flag with three pointers (`lo`, `mid`, `hi`).
- **Merge Sorted Array**: fill from the back to avoid overwriting.
- **Squares of a Sorted Array**: compare absolute values from both ends, fill from the back.
- **Is Subsequence**: one pointer per string.
