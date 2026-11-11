---
title: Greedy
section: dsa
order: 14
summary: Recognize when a locally optimal choice is globally optimal, justify it with an exchange argument, and apply the common greedy templates.
---

## When to use

- Optimization ("minimum number of", "maximum number of") where a **sort order** or a **running best** suggests itself.
- The problem has an obvious choice at each step that never needs to be undone.
- Intervals: pick by earliest end.
- Reachability: track the farthest point you can reach.
- If you can build a counterexample to the greedy choice in a minute, it is DP instead.

## Core idea

Make the best local choice, commit to it, and never revisit it. Greedy is only correct when you can argue it. The standard argument is the **exchange argument**: take any optimal solution, swap one of its choices for the greedy choice, and show it does not get worse. If you cannot argue it, test it on small cases or fall back to DP.

| Greedy works | Greedy fails (use DP) |
|---|---|
| Activity selection (earliest end) | Weighted interval scheduling |
| Fractional knapsack | 0/1 knapsack |
| Coin change with canonical coins (1, 5, 10, 25) | Coin change with arbitrary coins (1, 3, 4 for 6) |
| Jump Game (reachability) | Jump Game with costs |

## Templates

### Running max reach (Jump Game)

```python
def can_jump(nums: list[int]) -> bool:
    reach = 0
    for i, step in enumerate(nums):
        if i > reach:
            return False      # stuck before index i
        reach = max(reach, i + step)
    return True
```

### Minimum jumps (BFS by layers, greedy)

```python
def jump(nums: list[int]) -> int:
    jumps = cur_end = farthest = 0
    for i in range(len(nums) - 1):   # no jump needed from the last index
        farthest = max(farthest, i + nums[i])
        if i == cur_end:             # must jump to go further
            jumps += 1
            cur_end = farthest
    return jumps
```

### Kadane's algorithm (maximum subarray)

```python
def max_sub_array(nums: list[int]) -> int:
    best = cur = nums[0]
    for x in nums[1:]:
        cur = max(x, cur + x)   # extend, or restart at x
        best = max(best, cur)
    return best
```

### Gas station (reset on failure)

```python
def can_complete_circuit(gas: list[int], cost: list[int]) -> int:
    if sum(gas) < sum(cost):
        return -1
    start = tank = 0
    for i in range(len(gas)):
        tank += gas[i] - cost[i]
        if tank < 0:          # no start in [start, i] works
            start = i + 1
            tank = 0
    return start
```

### Sort then assign (two arrays)

```python
def find_content_children(greed: list[int], cookies: list[int]) -> int:
    greed.sort()
    cookies.sort()
    child = 0
    for c in cookies:
        if child < len(greed) and c >= greed[child]:
            child += 1        # smallest cookie that satisfies the least greedy child
    return child
```

### Partition labels (last occurrence)

```python
def partition_labels(s: str) -> list[int]:
    last = {ch: i for i, ch in enumerate(s)}
    res, start, end = [], 0, 0
    for i, ch in enumerate(s):
        end = max(end, last[ch])
        if i == end:
            res.append(end - start + 1)
            start = i + 1
    return res
```

## Complexity

- Usually `O(n)` for a single scan, or `O(n log n)` when a sort is required.
- Space is typically `O(1)` beyond the sort.

## Pitfalls

- Assuming greedy works without a proof. Always try a small counterexample first (coin change with `[1, 3, 4]`, target 6: greedy gives 3 coins, optimal is 2).
- Picking the wrong sort key (start vs end, ratio vs value).
- Kadane on all-negative arrays: initialize with `nums[0]`, not `0`.
- Off-by-one in Jump Game II: iterate to `len(nums) - 1`, not `len(nums)`.

> **Interview tip:** Say "I think greedy works here because..." and give the exchange argument in one sentence. Interviewers often probe exactly that.

## Classic problems

- **Maximum Subarray**: Kadane.
- **Jump Game** / **Jump Game II**: max reach / layer boundaries.
- **Gas Station**: total check + reset start.
- **Hand of Straights**: sort, start groups from the smallest remaining card (counter).
- **Merge Triplets to Form Target Triplet**: keep only triplets not exceeding target, check coverage.
- **Partition Labels**: last occurrence per char.
- **Valid Parenthesis String**: track `[low, high]` range of open count.
- **Assign Cookies**: sort both, two pointers.
- **Non-overlapping Intervals**: earliest end, see [Intervals]({{ '/dsa/intervals/' | relative_url }}).
- **Best Time to Buy and Sell Stock II**: sum all positive daily differences.
- **Candy**: left-to-right pass, then right-to-left pass with `max`.
- **Boats to Save People**: sort, pair heaviest with lightest.
- **Task Scheduler**: most frequent task dictates idle slots.
