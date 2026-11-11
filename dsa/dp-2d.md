---
title: "Dynamic programming: 2D and strings"
section: dsa
order: 22
summary: Grid paths, LCS, edit distance and 0/1 knapsack as 2D tables, and how to cut them to one row.
---

## When to use

- Grid with moves only right/down: "number of paths", "minimum path sum".
- **Two sequences** compared: "longest common subsequence", "edit distance", "is interleaving", "distinct subsequences".
- **Items with a capacity**: "pick a subset with sum / weight limit" (0/1 knapsack).
- One string, substring ranges: palindromes, interval DP (`dp[i][j]` over `s[i..j]`).

## Core idea

The state needs two indices: a cell `(r, c)`, a pair of prefixes `(i, j)`, or `(item, capacity)`. Fill the table in an order where every dependency is already computed. For two strings, `dp[i][j]` usually means "answer for `a[:i]` and `b[:j]`", which gives a free empty-prefix row and column at index 0.

## Templates

### Unique paths (grid)

```python
def unique_paths(m: int, n: int) -> int:
    row = [1] * n                      # first row: one way to each cell
    for _ in range(1, m):
        for c in range(1, n):
            row[c] += row[c - 1]       # from above (row[c]) + from left
    return row[-1]
```

Minimum Path Sum: same shape, `dp[c] = grid[r][c] + min(dp[c], dp[c - 1])`. Obstacles: set `dp[c] = 0` on a blocked cell.

### Longest common subsequence

```python
def lcs(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[m][n]
```

### Edit distance

```python
def min_distance(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i                  # delete all of a[:i]
    for j in range(n + 1):
        dp[0][j] = j                  # insert all of b[:j]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # delete a[i-1]
                    dp[i][j - 1],      # insert b[j-1]
                    dp[i - 1][j - 1],  # replace
                )
    return dp[m][n]
```

### 0/1 knapsack

`dp[i][w]` = best value using the first `i` items with capacity `w`.

```python
def knapsack(weights: list[int], values: list[int], cap: int) -> int:
    n = len(weights)
    dp = [[0] * (cap + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        wt, val = weights[i - 1], values[i - 1]
        for w in range(cap + 1):
            dp[i][w] = dp[i - 1][w]                    # skip item
            if wt <= w:
                dp[i][w] = max(dp[i][w], dp[i - 1][w - wt] + val)  # take it
    return dp[n][cap]
```

### Space optimization

Each row depends only on the previous row, so keep one row.

```python
def knapsack_1d(weights: list[int], values: list[int], cap: int) -> int:
    dp = [0] * (cap + 1)
    for wt, val in zip(weights, values):
        for w in range(cap, wt - 1, -1):   # backwards: each item used once
            dp[w] = max(dp[w], dp[w - wt] + val)
    return dp[cap]
```

| Loop direction over capacity | Meaning |
|---|---|
| High → low | 0/1: each item at most once |
| Low → high | Unbounded: items reusable (Coin Change) |

Partition Equal Subset Sum is the boolean version:

```python
def can_partition(nums: list[int]) -> bool:
    total = sum(nums)
    if total % 2:
        return False
    target = total // 2
    dp = [True] + [False] * target       # dp[s]: some subset sums to s
    for x in nums:
        for s in range(target, x - 1, -1):
            dp[s] = dp[s] or dp[s - x]
    return dp[target]
```

LCS in two rows (needs the diagonal, so keep `prev` explicitly):

```python
def lcs_two_rows(a: str, b: str) -> int:
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]
```

### Interval DP (palindromic subsequence)

Fill by increasing length, or iterate `i` from the end so `dp[i + 1]` is ready.

```python
def longest_palindrome_subseq(s: str) -> int:
    n = len(s)
    dp = [[0] * n for _ in range(n)]
    for i in range(n - 1, -1, -1):
        dp[i][i] = 1
        for j in range(i + 1, n):
            if s[i] == s[j]:
                dp[i][j] = dp[i + 1][j - 1] + 2
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j - 1])
    return dp[0][n - 1] if n else 0
```

## Complexity

| Problem | Time | Space (full / optimized) |
|---|---|---|
| Grid paths | `O(m * n)` | `O(m * n)` / `O(n)` |
| LCS, edit distance | `O(m * n)` | `O(m * n)` / `O(min(m, n))` |
| 0/1 knapsack | `O(n * cap)` | `O(n * cap)` / `O(cap)` |
| Interval DP | `O(n^2)` to `O(n^3)` | `O(n^2)` |

Knapsack is **pseudo-polynomial**: it depends on the numeric value of `cap`, not just the input length.

## Pitfalls

- Index shift: `dp[i][j]` refers to `a[i - 1]` and `b[j - 1]` when you use the empty-prefix row.
- 1D knapsack iterated low → high turns 0/1 into unbounded.
- `[[0] * n] * m` creates `m` references to the **same** row. Use a comprehension.
- Interval DP filled in the wrong order (reading `dp[i + 1][...]` before it exists).
- Forgetting base rows in edit distance (`dp[i][0] = i`, `dp[0][j] = j`).

## Classic problems

- **Unique Paths** / **Unique Paths II**: grid sum, obstacles to 0.
- **Minimum Path Sum**: grid min.
- **Longest Common Subsequence**: match → diagonal + 1, else max.
- **Edit Distance**: three operations.
- **Distinct Subsequences**: `dp[i][j] = dp[i-1][j] + (dp[i-1][j-1] if match else 0)`.
- **Interleaving String**: `dp[i][j]` from `s1[i-1]` or `s2[j-1]`.
- **Partition Equal Subset Sum**: boolean 0/1 knapsack.
- **Target Sum**: count subsets with sum `(total + target) / 2`.
- **Ones and Zeroes**: 0/1 knapsack with two capacities.
- **Longest Palindromic Subsequence**: interval DP or `LCS(s, reversed(s))`.
- **Burst Balloons**: interval DP, pick the **last** balloon in `(i, j)`.
- **Regular Expression Matching**: 2D DP, `*` means zero or more of the previous char.
- **Maximal Square**: `dp[r][c] = 1 + min(top, left, diag)`.
