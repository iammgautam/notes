---
title: Sliding window
section: dsa
order: 5
summary: Fixed-size and variable-size window templates for contiguous subarray and substring problems in O(n).
---

## When to use

- "Longest / shortest / count of **contiguous** subarray or substring" with some property.
- "Max sum / average of every window of size k".
- The property is **monotonic** as the window grows: adding elements only makes it "more invalid" (e.g. more distinct chars, bigger sum with non-negative numbers).

If numbers can be negative and you need an exact sum, a window does not work. Use [Prefix sums]({{ '/dsa/prefix-sums/' | relative_url }}).

## Core idea

Keep a window `[left, right]`. Extend `right` one step at a time and update the window state (sum, counts). When the window breaks the constraint, shrink from `left` until it is valid again. Each index enters and leaves once, so the total is `O(n)`.

## Templates

### Fixed size k

```python
def max_sum_window(nums: list[int], k: int) -> int:
    window = sum(nums[:k])
    best = window
    for right in range(k, len(nums)):
        window += nums[right] - nums[right - k]  # add new, drop oldest
        best = max(best, window)
    return best
```

### Variable size, longest valid window

```python
def longest_valid(s: str) -> int:
    count = {}
    left = 0
    best = 0
    for right, ch in enumerate(s):
        count[ch] = count.get(ch, 0) + 1          # 1. expand
        while not is_valid(count):                # 2. shrink until valid
            count[s[left]] -= 1
            if count[s[left]] == 0:
                del count[s[left]]
            left += 1
        best = max(best, right - left + 1)        # 3. record (window is valid)
    return best
```

Concrete: Longest Substring Without Repeating Characters.

```python
def length_of_longest_substring(s: str) -> int:
    last = {}  # char -> last index seen
    left = best = 0
    for right, ch in enumerate(s):
        if ch in last and last[ch] >= left:
            left = last[ch] + 1  # jump past the previous occurrence
        last[ch] = right
        best = max(best, right - left + 1)
    return best
```

### Variable size, shortest valid window

Shrink **while valid**, recording inside the loop.

```python
def min_subarray_len(target: int, nums: list[int]) -> int:
    """Shortest subarray with sum >= target. nums are positive."""
    left = total = 0
    best = float("inf")
    for right, x in enumerate(nums):
        total += x
        while total >= target:
            best = min(best, right - left + 1)
            total -= nums[left]
            left += 1
    return 0 if best == float("inf") else best
```

### Minimum window substring (need-count pattern)

```python
from collections import Counter

def min_window(s: str, t: str) -> str:
    need = Counter(t)
    missing = len(t)          # chars of t still not covered
    left = 0
    start, end = 0, 0         # best window s[start:end]
    for right, ch in enumerate(s, 1):  # right is exclusive end
        if need[ch] > 0:
            missing -= 1
        need[ch] -= 1
        if missing == 0:
            while need[s[left]] < 0:   # drop surplus chars from the left
                need[s[left]] += 1
                left += 1
            if end == 0 or right - left < end - start:
                start, end = left, right
            need[s[left]] += 1         # break validity, move on
            missing += 1
            left += 1
    return s[start:end]
```

### Count subarrays with "at most k"

`exactly(k) = at_most(k) - at_most(k - 1)`. Count windows ending at `right`: add `right - left + 1`.

```python
def at_most_k_distinct(nums: list[int], k: int) -> int:
    count = {}
    left = res = 0
    for right, x in enumerate(nums):
        count[x] = count.get(x, 0) + 1
        while len(count) > k:
            count[nums[left]] -= 1
            if count[nums[left]] == 0:
                del count[nums[left]]
            left += 1
        res += right - left + 1
    return res
```

## Complexity

- Time: `O(n)`. Each element is added and removed at most once.
- Space: `O(1)` for sums, `O(k)` or `O(alphabet)` for counts.

## Pitfalls

- Using a window when values can be negative and the target is a sum. The shrink condition stops being monotonic.
- Recording the answer in the wrong place: **longest** → after the shrink loop; **shortest** → inside the shrink loop.
- Off-by-one on window length: inclusive `[left, right]` has length `right - left + 1`.
- Leaving zero counts in the dict, so `len(count)` overcounts distinct chars.
- Fixed window: forgetting `len(nums) < k`.

## Classic problems

- **Best Time to Buy and Sell Stock**: track min so far (degenerate window).
- **Longest Substring Without Repeating Characters**: last-seen index jump.
- **Longest Repeating Character Replacement**: valid while `length - max_freq <= k`.
- **Permutation in String** / **Find All Anagrams**: fixed window of `len(p)`, compare counts.
- **Minimum Window Substring**: need-count with `missing`.
- **Minimum Size Subarray Sum**: shortest-window template.
- **Max Consecutive Ones III**: at most `k` zeros in window.
- **Subarrays with K Different Integers**: `at_most(k) - at_most(k - 1)`.
- **Sliding Window Maximum**: monotonic deque, see [Stacks and monotonic stacks]({{ '/dsa/monotonic-stack/' | relative_url }}).
- **Fruit Into Baskets**: longest window with at most 2 distinct.
