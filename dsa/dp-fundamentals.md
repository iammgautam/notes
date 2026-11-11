---
title: "Dynamic programming: fundamentals"
section: dsa
order: 21
summary: Define state, transition and base case, choose top-down or bottom-up, and apply the core 1D patterns from climbing stairs to LIS.
---

## When to use

- "Number of ways to ...", "minimum / maximum cost to ...", "is it possible to ...".
- Choices at each step, and the brute force recursion **recomputes the same subproblems**.
- Greedy has a counterexample.
- Constraints like `n ≤ 1000` or `n ≤ 10^4` hint at `O(n^2)` or `O(n * k)` DP.

## Core idea

DP is recursion plus caching. Write it in four steps:

1. **State:** what does `dp[i]` mean, in words? ("max money robbing houses `0..i`.")
2. **Transition:** how is `dp[i]` built from smaller states? (the choices at step `i`.)
3. **Base case:** the smallest states you know directly.
4. **Answer:** which state holds the final result (`dp[n]`, `max(dp)`, ...).

If you can write a correct recursive brute force, you are 80% done: add `@cache` (top-down), or fill a table in dependency order (bottom-up).

| | Top-down (memoization) | Bottom-up (tabulation) |
|---|---|---|
| Write | Natural recursion + `@cache` | Loops over a table |
| Computes | Only reachable states | All states |
| Risk | Recursion limit (~1000 deep) | Getting the loop order wrong |
| Space optimization | Hard | Easy (rolling variables) |

## Templates

### Climbing stairs (counting ways)

`dp[i] = dp[i - 1] + dp[i - 2]`: the last step was 1 or 2.

```python
def climb_stairs(n: int) -> int:
    a, b = 1, 1          # ways to reach step 0 and step 1
    for _ in range(n - 1):
        a, b = b, a + b
    return b
```

### House robber (take or skip)

`dp[i] = max(dp[i - 1], dp[i - 2] + nums[i])`.

```python
def rob(nums: list[int]) -> int:
    prev2 = prev1 = 0    # best up to i - 2, best up to i - 1
    for x in nums:
        prev2, prev1 = prev1, max(prev1, prev2 + x)
    return prev1
```

House Robber II (circular): `max(rob(nums[1:]), rob(nums[:-1]))`, handling `len(nums) == 1`.

### Top-down version of the same idea

```python
from functools import cache

def rob_memo(nums: list[int]) -> int:
    @cache
    def best(i: int) -> int:        # best from house i onward
        if i >= len(nums):
            return 0
        return max(best(i + 1), nums[i] + best(i + 2))

    return best(0)
```

### Coin change (unbounded, minimum count)

`dp[a] = min(dp[a - c] + 1 for c in coins)`.

```python
def coin_change(coins: list[int], amount: int) -> int:
    INF = amount + 1                 # more coins than ever needed
    dp = [0] + [INF] * amount
    for a in range(1, amount + 1):
        for c in coins:
            if c <= a and dp[a - c] + 1 < dp[a]:
                dp[a] = dp[a - c] + 1
    return dp[amount] if dp[amount] != INF else -1
```

### Coin change II (number of combinations)

Loop **coins outside** so each combination is counted once (order does not matter).

```python
def change(amount: int, coins: list[int]) -> int:
    dp = [1] + [0] * amount          # one way to make 0
    for c in coins:
        for a in range(c, amount + 1):
            dp[a] += dp[a - c]
    return dp[amount]
```

Swap the loops (amount outside, coins inside) and you count **permutations** instead (Combination Sum IV).

### Longest increasing subsequence

`O(n^2)` DP: `dp[i]` = length of the LIS ending at `i`.

```python
def length_of_lis(nums: list[int]) -> int:
    dp = [1] * len(nums)
    for i in range(len(nums)):
        for j in range(i):
            if nums[j] < nums[i]:
                dp[i] = max(dp[i], dp[j] + 1)
    return max(dp, default=0)
```

`O(n log n)` patience sorting: `tails[k]` = smallest tail of an increasing subsequence of length `k + 1`.

```python
import bisect

def length_of_lis_fast(nums: list[int]) -> int:
    tails = []
    for x in nums:
        i = bisect.bisect_left(tails, x)   # bisect_right for non-decreasing
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(tails)
```

### Word break (prefix-validity)

```python
def word_break(s: str, word_dict: list[str]) -> bool:
    words = set(word_dict)
    dp = [True] + [False] * len(s)       # dp[i]: s[:i] can be segmented
    for i in range(1, len(s) + 1):
        dp[i] = any(dp[j] and s[j:i] in words for j in range(i))
    return dp[len(s)]
```

## Complexity

| Problem | Time | Space |
|---|---|---|
| Climbing stairs, house robber | `O(n)` | `O(1)` |
| Coin change | `O(amount * len(coins))` | `O(amount)` |
| LIS | `O(n^2)` or `O(n log n)` | `O(n)` |
| Word break | `O(n^2)` (plus substring cost) | `O(n)` |

General rule: time = number of states × work per transition.

## Pitfalls

- Fuzzy state definition. Write it as a sentence before coding.
- Wrong base case: `dp[0] = 1` for counting ways, `0` for min cost, `-inf` or `inf` sentinels for max/min.
- Loop order for unbounded knapsack: combinations vs permutations.
- `@cache` on a function taking a `list` argument: lists are unhashable. Pass indices.
- Deep recursion with `@cache` on `n = 10^4`: hits the recursion limit. Go bottom-up.
- In the `O(n log n)` LIS, `tails` is **not** an actual subsequence.

## Classic problems

- **Climbing Stairs** / **Min Cost Climbing Stairs**: Fibonacci-style.
- **House Robber** / **House Robber II**: take or skip; split the circle.
- **Decode Ways**: one-digit and two-digit transitions, watch `"0"`.
- **Coin Change** / **Coin Change II**: unbounded knapsack, min vs count.
- **Combination Sum IV**: permutations, amount loop outside.
- **Longest Increasing Subsequence**: `O(n^2)` DP or patience sorting.
- **Word Break**: `dp[i]` over prefixes.
- **Maximum Product Subarray**: track both max and min ending here.
- **Partition Equal Subset Sum**: 0/1 knapsack on `sum // 2`, see [Dynamic programming: 2D and strings]({{ '/dsa/dp-2d/' | relative_url }}).
- **Palindromic Substrings** / **Longest Palindromic Substring**: expand around centers (`O(n^2)`, `O(1)` space).
- **Best Time to Buy and Sell Stock with Cooldown**: state machine (hold, sold, rest).
