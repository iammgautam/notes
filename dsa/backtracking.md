---
title: Backtracking
section: dsa
order: 15
summary: One choose-explore-unchoose template for subsets, combinations, permutations and constraint search, plus duplicate handling and pruning.
---

## When to use

- "Return **all** subsets / permutations / combinations / partitions".
- "Generate all valid ..." (parentheses, IP addresses, board placements).
- Constraint satisfaction: N-Queens, Sudoku, word search on a grid.
- Small `n` (≤ ~20) with exponential output. If only a **count** or **optimum** is needed, check for DP first.

## Core idea

Build a candidate step by step. At each step, **choose** an option, **explore** recursively, then **unchoose** (undo) so the next option starts from a clean state. Prune branches as early as possible when they cannot lead to a valid answer. The recursion tree is the search space; pruning is what keeps it tractable.

```text
backtrack(state):
    if state is complete: record a copy; return
    for choice in choices(state):
        if not valid(choice): continue
        apply choice
        backtrack(next state)
        undo choice
```

## Templates

### Subsets (include/exclude via start index)

```python
def subsets(nums: list[int]) -> list[list[int]]:
    res, path = [], []

    def backtrack(start: int) -> None:
        res.append(path[:])            # every node is a subset; copy!
        for i in range(start, len(nums)):
            path.append(nums[i])
            backtrack(i + 1)
            path.pop()

    backtrack(0)
    return res
```

### Subsets with duplicates

```python
def subsets_with_dup(nums: list[int]) -> list[list[int]]:
    nums.sort()                        # duplicates must be adjacent
    res, path = [], []

    def backtrack(start: int) -> None:
        res.append(path[:])
        for i in range(start, len(nums)):
            if i > start and nums[i] == nums[i - 1]:
                continue               # skip same value at the same depth
            path.append(nums[i])
            backtrack(i + 1)
            path.pop()

    backtrack(0)
    return res
```

### Combinations of size k

```python
def combine(n: int, k: int) -> list[list[int]]:
    res, path = [], []

    def backtrack(start: int) -> None:
        if len(path) == k:
            res.append(path[:])
            return
        need = k - len(path)
        for i in range(start, n - need + 2):   # prune: enough numbers left
            path.append(i)
            backtrack(i + 1)
            path.pop()

    backtrack(1)
    return res
```

### Combination sum (reuse allowed)

```python
def combination_sum(candidates: list[int], target: int) -> list[list[int]]:
    candidates.sort()
    res, path = [], []

    def backtrack(start: int, remaining: int) -> None:
        if remaining == 0:
            res.append(path[:])
            return
        for i in range(start, len(candidates)):
            if candidates[i] > remaining:
                break                          # sorted: the rest are bigger
            path.append(candidates[i])
            backtrack(i, remaining - candidates[i])  # i, not i + 1: reuse
            path.pop()

    backtrack(0, target)
    return res
```

### Permutations (used array)

```python
def permute(nums: list[int]) -> list[list[int]]:
    res, path = [], []
    used = [False] * len(nums)

    def backtrack() -> None:
        if len(path) == len(nums):
            res.append(path[:])
            return
        for i in range(len(nums)):
            if used[i]:
                continue
            # For duplicates (Permutations II), sort nums and add:
            # if i > 0 and nums[i] == nums[i - 1] and not used[i - 1]: continue
            used[i] = True
            path.append(nums[i])
            backtrack()
            path.pop()
            used[i] = False

    backtrack()
    return res
```

### Grid search (Word Search)

```python
def exist(board: list[list[str]], word: str) -> bool:
    rows, cols = len(board), len(board[0])

    def dfs(r: int, c: int, i: int) -> bool:
        if i == len(word):
            return True
        if not (0 <= r < rows and 0 <= c < cols) or board[r][c] != word[i]:
            return False
        board[r][c] = "#"                       # mark visited in place
        found = any(dfs(r + dr, c + dc, i + 1)
                    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        board[r][c] = word[i]                   # restore
        return found

    return any(dfs(r, c, 0) for r in range(rows) for c in range(cols))
```

## Complexity

| Problem | Time | Output size |
|---|---|---|
| Subsets | `O(n * 2^n)` | `2^n` |
| Permutations | `O(n * n!)` | `n!` |
| Combinations C(n, k) | `O(k * C(n, k))` | `C(n, k)` |
| Word search | `O(R * C * 3^L)` | |
| N-Queens | `O(n!)` | |

Space: `O(n)` recursion depth plus output.

## Pitfalls

- Appending `path` instead of `path[:]`. Every result ends up as the same (empty) list.
- Forgetting to undo the choice, leaking state into sibling branches.
- Duplicate handling: sort first, and skip with `i > start` (same depth), not `i > 0`.
- Passing `i` vs `i + 1`: `i` allows reuse, `i + 1` does not.
- Recomputing validity from scratch. Keep sets (`cols`, `diag`, `anti_diag` for N-Queens) updated incrementally.

## Classic problems

- **Subsets** / **Subsets II**: start index, skip duplicates at the same depth.
- **Combinations**: size-k with pruning.
- **Combination Sum** / **Combination Sum II**: reuse vs no reuse + dedupe.
- **Permutations** / **Permutations II**: used array, dedupe with `not used[i - 1]`.
- **Letter Combinations of a Phone Number**: one digit per depth.
- **Generate Parentheses**: add `(` if `open < n`, `)` if `close < open`.
- **Palindrome Partitioning**: cut at each palindromic prefix.
- **Word Search**: grid DFS, mark and restore.
- **N-Queens**: sets for columns, `r - c`, `r + c`.
- **Sudoku Solver**: try digits in empty cells, row/col/box sets.
- **Restore IP Addresses**: 4 segments, each 0–255 with no leading zero.
