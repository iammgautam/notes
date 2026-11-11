---
title: Union-find
section: dsa
order: 19
summary: Track connected components under merges in near O(1) per operation with path compression and union by size.
---

## When to use

- "Are `a` and `b` connected?" after a series of merges.
- Count connected components, especially when edges arrive one by one.
- Detect a cycle in an **undirected** graph (an edge whose ends are already connected).
- Kruskal's minimum spanning tree.
- Grouping by equivalence: accounts with shared emails, similar strings, equations.

Not a fit for: directed graphs, or when you need to split components apart.

## Core idea

Each element points to a parent; following parents leads to the component's **root**, which is its representative. `union(a, b)` links one root under the other. Two optimizations make it nearly constant time:

- **Path compression:** during `find`, point nodes directly at the root.
- **Union by size (or rank):** attach the smaller tree under the larger.

With both, each operation is `O(α(n))`, where `α` is the inverse Ackermann function (≤ 4 for any practical `n`).

## Template

```python
class DSU:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.size = [1] * n
        self.components = n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False                  # already connected: cycle edge
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra              # attach smaller under larger
        self.size[ra] += self.size[rb]
        self.components -= 1
        return True

    def connected(self, a: int, b: int) -> bool:
        return self.find(a) == self.find(b)
```

Path halving is iterative (no recursion limit) and has the same complexity as full compression. The recursive full-compression version:

```python
def find(self, x: int) -> int:
    if self.parent[x] != x:
        self.parent[x] = self.find(self.parent[x])
    return self.parent[x]
```

### Dict-based DSU for non-integer keys

```python
parent = {}

def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x

def union(a, b):
    parent[find(a)] = find(b)  # no size heuristic: fine for interview-sized inputs
```

## Usage patterns

### Redundant connection / cycle detection

```python
def find_redundant_connection(edges: list[list[int]]) -> list[int]:
    dsu = DSU(len(edges) + 1)            # nodes are 1-indexed
    for u, v in edges:
        if not dsu.union(u, v):
            return [u, v]
    return []
```

### Kruskal's MST

```python
def kruskal(n: int, edges: list[tuple[int, int, int]]) -> int:
    dsu = DSU(n)
    total = used = 0
    for w, u, v in sorted(edges):        # edges as (weight, u, v)
        if dsu.union(u, v):
            total += w
            used += 1
            if used == n - 1:
                break
    return total if used == n - 1 else -1  # -1: graph not connected
```

### Grid cells as nodes

Map `(r, c)` to `r * cols + c`. Union each land cell with its land neighbors (right and down are enough).

## Complexity

| Operation | Time | Space |
|---|---|---|
| `find`, `union` (both optimizations) | `O(α(n))` amortized | |
| Only one optimization | `O(log n)` | |
| Neither | `O(n)` worst | |
| Whole structure | | `O(n)` |

## Union-find vs DFS/BFS

| | Union-find | DFS / BFS |
|---|---|---|
| Static graph, count components | Fine | Fine, simpler |
| Edges arrive over time (online) | Best | Re-run each time |
| Need actual paths | No | Yes |
| Directed graph | No | Yes |

## Pitfalls

- Comparing `parent[a] == parent[b]` instead of `find(a) == find(b)`.
- Forgetting 1-indexed nodes: allocate `n + 1`.
- Decrementing the component count even when `a` and `b` were already joined.
- Union-find on a directed graph for cycle detection: wrong. Use colors or Kahn's.

## Classic problems

- **Number of Connected Components in an Undirected Graph**: `components` counter.
- **Graph Valid Tree**: exactly `n - 1` edges and no failed union.
- **Redundant Connection**: first edge whose union fails.
- **Number of Provinces**: union over the adjacency matrix.
- **Accounts Merge**: union emails, group by root.
- **Longest Consecutive Sequence**: union `x` with `x + 1` (set approach is simpler).
- **Number of Islands II**: add land online, union with neighbors.
- **Min Cost to Connect All Points**: Kruskal (or Prim with a heap).
- **Satisfiability of Equality Equations**: union all `==`, then check `!=`.
- **Evaluate Division**: weighted union-find, or BFS on a graph.
- **Most Stones Removed with Same Row or Column**: `n - components`.
