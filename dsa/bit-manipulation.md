---
title: Bit manipulation
section: dsa
order: 23
summary: The bitwise identities that show up in interviews, XOR tricks, bitmask subsets, and the Python quirks around negative numbers.
---

## When to use

- "Without extra space" with pairs/duplicates → XOR.
- "Power of two", "count set bits", "reverse bits".
- Enumerate all subsets of a small set (`n ≤ 20`) → bitmask.
- DP over subsets (visited sets in TSP-style problems).
- Add two numbers without `+`.

## Core idea

Integers are bit arrays, and bitwise operators work on all bits in parallel in `O(1)`. A handful of identities cover most problems. XOR is the star: `x ^ x = 0` and `x ^ 0 = x`, and it is commutative, so XOR-ing everything cancels out pairs.

## Operators and identities

| Expression | Meaning |
|---|---|
| `x & 1` | Is `x` odd? (lowest bit) |
| `(x >> i) & 1` | Value of bit `i` |
| `x ^ (1 << i)` | Toggle bit `i` |
| `x & ~(1 << i)` | Clear bit `i` |
| `x & (x - 1)` | Clear the lowest set bit |
| `x & -x` | Isolate the lowest set bit |
| `x > 0 and x & (x - 1) == 0` | `x` is a power of two |
| `x << k`, `x >> k` | Multiply / floor-divide by `2^k` |
| `a ^ b ^ b` | `a` (XOR cancels) |
| `bin(x).count("1")`, `x.bit_count()` (3.10+) | Number of set bits |
| `x.bit_length()` | Index of highest set bit + 1 |

Set bit `i`: `x |= 1 << i` (bitwise OR).

## Templates

### Single number (XOR cancels pairs)

```python
from functools import reduce
from operator import xor

def single_number(nums: list[int]) -> int:
    return reduce(xor, nums, 0)
```

### Missing number

```python
def missing_number(nums: list[int]) -> int:
    res = len(nums)
    for i, x in enumerate(nums):
        res ^= i ^ x          # every present index/value cancels
    return res
```

### Count bits for 0..n (DP on bits)

```python
def count_bits(n: int) -> list[int]:
    res = [0] * (n + 1)
    for i in range(1, n + 1):
        res[i] = res[i >> 1] + (i & 1)   # drop lowest bit, add it back
    return res
```

### Brian Kernighan: count set bits

```python
def hamming_weight(x: int) -> int:
    count = 0
    while x:
        x &= x - 1            # remove one set bit per iteration
        count += 1
    return count
```

### Subsets via bitmask

```python
def subsets(nums: list[int]) -> list[list[int]]:
    n = len(nums)
    return [[nums[i] for i in range(n) if mask >> i & 1] for mask in range(1 << n)]
```

Iterate all submasks of `mask`: `sub = mask; while sub: ...; sub = (sub - 1) & mask` (then handle `0` separately).

### Reverse bits (32-bit)

```python
def reverse_bits(n: int) -> int:
    res = 0
    for _ in range(32):
        res = (res << 1) | (n & 1)
        n >>= 1
    return res
```

### Sum of two integers without + (32-bit emulation in Python)

```python
def get_sum(a: int, b: int) -> int:
    MASK = 0xFFFFFFFF
    while b & MASK:
        a, b = a ^ b, (a & b) << 1      # sum without carry, carry
    a &= MASK
    return a if a <= 0x7FFFFFFF else ~(a ^ MASK)  # back to signed
```

### Single Number II (every other element appears 3 times)

```python
def single_number_ii(nums: list[int]) -> int:
    ones = twos = 0
    for x in nums:
        ones = (ones ^ x) & ~twos
        twos = (twos ^ x) & ~ones
    return ones
```

## Complexity

- Bitwise ops: `O(1)` for fixed-width ints (Python ints are arbitrary precision, `O(bits)` in theory).
- Kernighan: `O(number of set bits)`.
- Bitmask enumeration: `O(n * 2^n)`.
- Bitmask DP: `O(2^n * n)` or `O(2^n * n^2)` states × transitions.

## Pitfalls

- **Python ints are unbounded.** `~x` is `-x - 1`, and right-shifting a negative number never reaches 0. Mask with `0xFFFFFFFF` when the problem assumes 32-bit.
- Operator precedence: in Python, `&`, `^`, `|` bind tighter than `==` (unlike C), but looser than `+`, `-`, `<<`, `>>`. So `1 << n - 1` is `1 << (n - 1)` and `x & 1 + 1` is `x & 2`. Parenthesize when unsure.
- `x & (x - 1) == 0` alone says `0` is a power of two. Check `x > 0`.
- Signed vs unsigned when the problem gives an unsigned 32-bit int.

## Classic problems

- **Single Number**: XOR all.
- **Single Number II**: `ones`/`twos` state, or count bits mod 3.
- **Single Number III**: XOR all, split by lowest set bit (`x & -x`).
- **Number of 1 Bits**: Kernighan.
- **Counting Bits**: `res[i >> 1] + (i & 1)`.
- **Reverse Bits**: shift 32 times.
- **Missing Number**: XOR indices and values, or Gauss sum.
- **Power of Two**: `x > 0 and x & (x - 1) == 0`.
- **Sum of Two Integers**: XOR + carry with 32-bit mask.
- **Subsets**: mask from `0` to `2^n - 1`.
- **Maximum XOR of Two Numbers in an Array**: greedy bit by bit with prefixes or a bitwise trie.
- **Shortest Path Visiting All Nodes**: BFS over `(node, visited_mask)`.
