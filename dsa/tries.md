---
title: Tries
section: dsa
order: 20
summary: Build a prefix tree for fast prefix queries, autocomplete, wildcard search and multi-word grid search.
---

## When to use

- "Starts with", "prefix", "autocomplete", "search suggestions".
- Searching **many words** at once (Word Search II): share prefixes instead of searching each word.
- Wildcard matching with `.`.
- Longest common prefix, replacing words by their shortest root.
- Bitwise tries for maximum XOR of two numbers.

## Core idea

A trie is a tree where each edge is a character and each node represents the prefix spelled from the root. A flag marks nodes where a complete word ends. Lookup and insert cost `O(L)` for a word of length `L`, independent of how many words are stored. Shared prefixes are stored once.

## Template

```python
class TrieNode:
    __slots__ = ("children", "is_word")

    def __init__(self):
        self.children = {}      # char -> TrieNode
        self.is_word = False


class Trie:
    def __init__(self):
        self.root = TrieNode()

    def insert(self, word: str) -> None:
        node = self.root
        for ch in word:
            node = node.children.setdefault(ch, TrieNode())
        node.is_word = True

    def _walk(self, s: str) -> TrieNode | None:
        node = self.root
        for ch in s:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def search(self, word: str) -> bool:
        node = self._walk(word)
        return node is not None and node.is_word

    def starts_with(self, prefix: str) -> bool:
        return self._walk(prefix) is not None
```

Use `[None] * 26` instead of a dict when the alphabet is fixed lowercase and memory or speed matters.

### Wildcard search (`.` matches any char)

```python
def search_with_dots(root: TrieNode, word: str) -> bool:
    def dfs(node: TrieNode, i: int) -> bool:
        if i == len(word):
            return node.is_word
        ch = word[i]
        if ch == ".":
            return any(dfs(child, i + 1) for child in node.children.values())
        child = node.children.get(ch)
        return child is not None and dfs(child, i + 1)

    return dfs(root, 0)
```

### Word Search II (trie + grid backtracking)

```python
def find_words(board: list[list[str]], words: list[str]) -> list[str]:
    root = TrieNode()
    for w in words:
        node = root
        for ch in w:
            node = node.children.setdefault(ch, TrieNode())
        node.is_word = True

    rows, cols = len(board), len(board[0])
    found = []

    def dfs(r: int, c: int, node: TrieNode, path: list[str]) -> None:
        ch = board[r][c]
        child = node.children.get(ch)
        if child is None:
            return
        path.append(ch)
        if child.is_word:
            found.append("".join(path))
            child.is_word = False             # avoid duplicates
        board[r][c] = "#"
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and board[nr][nc] != "#":
                dfs(nr, nc, child, path)
        board[r][c] = ch
        path.pop()
        if not child.children and not child.is_word:
            del node.children[ch]             # prune exhausted branches

    for r in range(rows):
        for c in range(cols):
            dfs(r, c, root, [])
    return found
```

### Autocomplete: top suggestions per prefix

Store up to 3 sorted words at each node during insert (Search Suggestions System), or sort the words and use `bisect` on the prefix, which is simpler in Python.

```python
import bisect

def suggested_products(products: list[str], search_word: str) -> list[list[str]]:
    products.sort()
    res, prefix = [], ""
    for ch in search_word:
        prefix += ch
        i = bisect.bisect_left(products, prefix)
        res.append([p for p in products[i:i + 3] if p.startswith(prefix)])
    return res
```

## Complexity

| Operation | Time | Space |
|---|---|---|
| Insert / search / prefix | `O(L)` | `O(L)` new nodes per insert |
| Build from `N` words | `O(total chars)` | `O(total chars)` worst |
| Wildcard search | `O(26^dots * L)` worst | `O(L)` recursion |
| Word Search II | `O(R * C * 4 * 3^(L-1))` worst | `O(total chars)` |

## Pitfalls

- Forgetting the end-of-word flag: `"app"` is not a word just because `"apple"` was inserted.
- `search` vs `starts_with`: only `search` checks `is_word`.
- Word Search II without pruning or de-duplicating found words: TLE or duplicate output.
- Memory: a node per character adds up. Use `__slots__` or arrays for large inputs.
- Python dict per node is fine for interviews; mention arrays as an optimization.

## Classic problems

- **Implement Trie (Prefix Tree)**: template above.
- **Design Add and Search Words Data Structure**: wildcard DFS.
- **Word Search II**: trie + backtracking + pruning.
- **Replace Words**: walk each word until the first `is_word` node.
- **Longest Word in Dictionary**: BFS/DFS through nodes that are all words.
- **Search Suggestions System**: trie with top-3 per node, or sort + bisect.
- **Maximum XOR of Two Numbers in an Array**: bitwise trie, greedily choose the opposite bit.
- **Palindrome Pairs**: trie of reversed words (hard).
- **Stream of Characters**: trie of reversed words, check suffixes.
