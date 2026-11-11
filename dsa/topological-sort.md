---
title: Topological sort
section: dsa
order: 17
summary: Order the nodes of a DAG so every edge points forward, with Kahn's BFS and DFS post-order, and detect cycles along the way.
---

## When to use

- "Prerequisites", "dependencies", "build order", "course schedule", "task ordering".
- "Is it possible to finish all tasks?" → is the directed graph acyclic?
- Deriving an order from pairwise constraints (Alien Dictionary).
- DP on a DAG (longest path, number of paths): process nodes in topological order.

## Core idea

A topological order exists only for a **directed acyclic graph**. Two ways to get one:

- **Kahn's algorithm (BFS):** repeatedly take a node with in-degree 0, append it, and decrement the in-degree of its neighbors. If you cannot place all nodes, there is a cycle.
- **DFS post-order:** a node is finished after all its descendants. Reverse the finishing order. A gray (on-stack) neighbor means a cycle.

Edge direction matters: for "to take course `a` you must take `b` first" (`[a, b]`), the edge is `b → a`.

## Templates

### Kahn's algorithm

```python
from collections import deque

def topo_sort_kahn(n: int, edges: list[list[int]]) -> list[int]:
    """edges: [u, v] means u must come before v. Returns [] if there is a cycle."""
    graph = [[] for _ in range(n)]
    indegree = [0] * n
    for u, v in edges:
        graph[u].append(v)
        indegree[v] += 1

    q = deque(i for i in range(n) if indegree[i] == 0)
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in graph[u]:
            indegree[v] -= 1
            if indegree[v] == 0:
                q.append(v)
    return order if len(order) == n else []
```

Course Schedule (LeetCode 207) gives `[course, prereq]`, so add the edge `prereq → course`:

```python
def can_finish(num_courses: int, prerequisites: list[list[int]]) -> bool:
    edges = [[pre, course] for course, pre in prerequisites]
    return len(topo_sort_kahn(num_courses, edges)) == num_courses
```

### DFS post-order

```python
def topo_sort_dfs(n: int, edges: list[list[int]]) -> list[int]:
    graph = [[] for _ in range(n)]
    for u, v in edges:
        graph[u].append(v)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n
    order = []

    def dfs(u: int) -> bool:          # returns False on cycle
        color[u] = GRAY
        for v in graph[u]:
            if color[v] == GRAY:
                return False
            if color[v] == WHITE and not dfs(v):
                return False
        color[u] = BLACK
        order.append(u)               # post-order: after all descendants
        return True

    for u in range(n):
        if color[u] == WHITE and not dfs(u):
            return []
    return order[::-1]
```

### Alien Dictionary (build edges from adjacent words)

```python
def alien_order(words: list[str]) -> str:
    graph = {ch: set() for w in words for ch in w}
    indegree = {ch: 0 for ch in graph}
    for w1, w2 in zip(words, words[1:]):
        for a, b in zip(w1, w2):
            if a != b:
                if b not in graph[a]:
                    graph[a].add(b)
                    indegree[b] += 1
                break                      # only the first difference matters
        else:
            if len(w1) > len(w2):
                return ""                  # "abc" before "ab" is invalid
    q = deque(ch for ch in graph if indegree[ch] == 0)
    order = []
    while q:
        ch = q.popleft()
        order.append(ch)
        for nxt in graph[ch]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                q.append(nxt)
    return "".join(order) if len(order) == len(graph) else ""
```

### Kahn vs DFS

| | Kahn's (BFS) | DFS |
|---|---|---|
| Cycle detection | `len(order) < n` | gray neighbor |
| Lexicographically smallest order | Use a min-heap instead of a deque | Awkward |
| Level / parallel scheduling ("minimum semesters") | Natural: process by BFS levels | Awkward |
| Recursion depth risk | None | Deep chains hit the limit |

## Complexity

- Time: `O(V + E)` for both.
- Space: `O(V + E)` for the graph, `O(V)` for in-degree / colors / queue.
- With a heap for lexicographic order: `O(V log V + E)`.

## Pitfalls

- Reversing the edge direction. Write down "u before v means edge u → v" before coding.
- Forgetting nodes with no edges. Initialize every node, including isolated ones.
- DFS version: appending in pre-order instead of post-order, or forgetting to reverse.
- Alien Dictionary: adding duplicate edges (inflates in-degree), and missing the prefix-invalid case.
- Assuming the order is unique. Many valid orders usually exist.

## Classic problems

- **Course Schedule**: can you finish? Kahn's, check count.
- **Course Schedule II**: return the order.
- **Alien Dictionary**: edges from first differing chars.
- **Parallel Courses**: BFS levels in Kahn's = minimum semesters.
- **Minimum Height Trees**: peel leaves layer by layer (Kahn's on an undirected tree).
- **Sequence Reconstruction**: unique order iff the queue never has more than one node.
- **Find All Possible Recipes from Given Supplies**: Kahn's starting from supplies.
- **Longest Increasing Path in a Matrix**: DAG by value; memo DFS or Kahn's by levels.
