---
title: Arrays and hashing
section: dsa
order: 3
summary: Use hash maps and sets to turn O(n^2) scans into O(n) lookups, with templates for counting, grouping and complement search.
---

## When to use

- "Have we seen this value before?" (duplicates, complements, pairs).
- Counting frequencies: anagrams, majority element, top k frequent.
- Grouping items by a computed key (sorted string, character count, pattern).
- Any nested loop where the inner loop is a search: replace it with a hash lookup.

## Core idea

Trade `O(n)` extra space for `O(1)` average lookups. Store what you need from the past (value → index, value → count, key → group) in a `dict` or `set` while scanning once. Choose the key carefully: it must be hashable and capture exactly the equivalence you care about.

## Templates

### Complement lookup (Two Sum)

```python
def two_sum(nums: list[int], target: int) -> list[int]:
    seen = {}  # value -> index
    for i, x in enumerate(nums):
        if target - x in seen:
            return [seen[target - x], i]
        seen[x] = i  # store after checking, so x does not pair with itself
    return []
```

### Frequency counting

```python
from collections import Counter

def is_anagram(s: str, t: str) -> bool:
    return Counter(s) == Counter(t)

def top_k_frequent(nums: list[int], k: int) -> list[int]:
    return [x for x, _ in Counter(nums).most_common(k)]
```

### Grouping by canonical key

```python
from collections import defaultdict

def group_anagrams(strs: list[str]) -> list[list[str]]:
    groups = defaultdict(list)
    for s in strs:
        key = [0] * 26
        for ch in s:
            key[ord(ch) - ord("a")] += 1
        groups[tuple(key)].append(s)  # tuple: lists are not hashable
    return list(groups.values())
```

### Set for O(1) membership (Longest Consecutive Sequence)

```python
def longest_consecutive(nums: list[int]) -> int:
    s = set(nums)
    best = 0
    for x in s:
        if x - 1 not in s:  # only start counting at the beginning of a run
            y = x
            while y + 1 in s:
                y += 1
            best = max(best, y - x + 1)
    return best
```

### Bucket sort by frequency (O(n) top k)

```python
def top_k_frequent_linear(nums: list[int], k: int) -> list[int]:
    count = Counter(nums)
    buckets = [[] for _ in range(len(nums) + 1)]  # index = frequency
    for x, c in count.items():
        buckets[c].append(x)
    res = []
    for c in range(len(buckets) - 1, 0, -1):
        for x in buckets[c]:
            res.append(x)
            if len(res) == k:
                return res
    return res
```

## Complexity

| Pattern | Time | Space |
|---|---|---|
| Single pass with dict/set | `O(n)` average | `O(n)` |
| Group anagrams (count key) | `O(n * L)` | `O(n * L)` |
| Group anagrams (sorted key) | `O(n * L log L)` | `O(n * L)` |
| Top k with bucket sort | `O(n)` | `O(n)` |

## Useful array tricks

- **In-place index marking:** values in `1..n` → negate `nums[abs(x) - 1]` to mark seen (Find All Duplicates, First Missing Positive).
- **Product except self:** prefix product pass left to right, suffix product pass right to left, no division.
- **Boyer-Moore voting** for majority element in `O(1)` space.
- **Encode a 2D cell** as a tuple `(r, c)` or `r * cols + c` for set membership.

## Pitfalls

- Storing into the map **before** checking it, so an element pairs with itself.
- Using a `list` as a dict key. Convert to `tuple`.
- `Counter` subtraction drops non-positive counts. Compare with `==` or iterate explicitly if you need negatives.
- `defaultdict` creates keys on read (`d[k]` inserts). Use `k in d` or `d.get(k)` when you only want to check.
- Worst-case hashing is `O(n)`; mention it if the interviewer asks about adversarial input.

## Classic problems

- **Two Sum**: value → index map, check complement first.
- **Contains Duplicate**: `len(set(nums)) != len(nums)`.
- **Valid Anagram**: compare counters or a 26-length array.
- **Group Anagrams**: key = tuple of 26 counts.
- **Top K Frequent Elements**: counter + bucket sort or heap.
- **Product of Array Except Self**: prefix and suffix products.
- **Longest Consecutive Sequence**: set, start only at run beginnings.
- **Valid Sudoku**: sets per row, column and box `(r // 3, c // 3)`.
- **Encode and Decode Strings**: length prefix like `"5#hello"`.
- **First Missing Positive**: index marking in place.
- **Subarray Sum Equals K**: see [Prefix sums]({{ '/dsa/prefix-sums/' | relative_url }}).
