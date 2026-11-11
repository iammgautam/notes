---
title: Stacks and monotonic stacks
section: dsa
order: 8
summary: Use stacks for matching and nested structure, and monotonic stacks or deques for next-greater and sliding-max problems in O(n).
---

## When to use

**Plain stack**

- Matching pairs: parentheses, tags.
- Nested structure: decode strings, evaluate expressions, simplify paths.
- Undo / "most recent unresolved item".
- Iterative DFS.

**Monotonic stack**

- "Next greater / next smaller element", "previous smaller".
- "How many days until a warmer temperature".
- Largest rectangle in histogram, trapping rain water.
- Sum of subarray minimums (each element's span of influence).

**Monotonic deque**

- Max/min of every sliding window of size `k`.

## Core idea

A monotonic stack keeps elements in increasing or decreasing order. When a new element breaks the order, you pop: each popped element has just found its "next greater" (or smaller) element, which is the current one. Every element is pushed and popped once, so it is `O(n)` despite the nested loop. Store **indices**, not values, so you can compute distances.

| Want | Stack order (bottom → top) | Pop while |
|---|---|---|
| Next greater | decreasing | `nums[stack[-1]] < x` |
| Next smaller | increasing | `nums[stack[-1]] > x` |

## Templates

### Valid parentheses

```python
def is_valid(s: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in s:
        if ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
        else:
            stack.append(ch)
    return not stack
```

### Next greater element (indices)

```python
def next_greater(nums: list[int]) -> list[int]:
    res = [-1] * len(nums)
    stack = []  # indices, values decreasing from bottom to top
    for i, x in enumerate(nums):
        while stack and nums[stack[-1]] < x:
            res[stack.pop()] = x
        stack.append(i)
    return res
```

Daily Temperatures is the same loop with `res[j] = i - j`. For a **circular** array, loop `i` over `range(2 * n)` and use `nums[i % n]`, pushing only when `i < n`.

### Largest rectangle in histogram

```python
def largest_rectangle(heights: list[int]) -> int:
    stack = []  # indices with increasing heights
    best = 0
    for i, h in enumerate(heights + [0]):  # sentinel 0 flushes the stack
        while stack and heights[stack[-1]] >= h:
            height = heights[stack.pop()]
            left = stack[-1] + 1 if stack else 0  # previous smaller + 1
            best = max(best, height * (i - left))
        stack.append(i)
    return best
```

Each popped bar extends from the previous smaller bar (new stack top) to the current index `i` (next smaller).

### Sliding window maximum (monotonic deque)

```python
from collections import deque

def max_sliding_window(nums: list[int], k: int) -> list[int]:
    dq = deque()  # indices, values decreasing; front is the max
    res = []
    for i, x in enumerate(nums):
        while dq and nums[dq[-1]] <= x:
            dq.pop()               # smaller values can never be a max again
        dq.append(i)
        if dq[0] <= i - k:
            dq.popleft()           # front fell out of the window
        if i >= k - 1:
            res.append(nums[dq[0]])
    return res
```

### Min stack

```python
class MinStack:
    def __init__(self):
        self.stack = []  # (value, min_so_far)

    def push(self, val: int) -> None:
        cur_min = min(val, self.stack[-1][1]) if self.stack else val
        self.stack.append((val, cur_min))

    def pop(self) -> None:
        self.stack.pop()

    def top(self) -> int:
        return self.stack[-1][0]

    def get_min(self) -> int:
        return self.stack[-1][1]
```

## Complexity

- All templates: `O(n)` time (each index pushed and popped once), `O(n)` space.
- Min stack: `O(1)` per operation.

## Pitfalls

- Storing values instead of indices, then needing distances or widths.
- Strict vs non-strict comparison. With duplicates, use `<` on one side and `<=` on the other to avoid double counting (Sum of Subarray Minimums).
- Forgetting to flush the stack at the end. A sentinel (`0` for histogram) avoids a second loop.
- Deque: evicting the out-of-window index **before** reading the front.
- Using a `list` as a queue. Use `collections.deque`.

## Classic problems

- **Valid Parentheses**: push openers, match closers.
- **Min Stack**: store `(value, current_min)`.
- **Evaluate Reverse Polish Notation**: push numbers, pop two on operator; use `int(a / b)` to truncate toward zero.
- **Decode String**: stack of `(prev_string, repeat_count)`.
- **Daily Temperatures**: next greater, store index distance.
- **Next Greater Element II**: circular, iterate `2n`.
- **Car Fleet**: sort by position, stack of arrival times.
- **Largest Rectangle in Histogram**: increasing stack + sentinel.
- **Maximal Rectangle**: histogram per row.
- **Trapping Rain Water**: decreasing stack, or two pointers.
- **Sum of Subarray Minimums**: previous-less and next-less-or-equal spans.
- **Remove K Digits**: increasing stack, pop while `k > 0`.
- **Sliding Window Maximum**: monotonic deque.
- **Asteroid Collision**: stack simulation.
