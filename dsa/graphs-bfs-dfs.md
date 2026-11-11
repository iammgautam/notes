---
title: "Graphs: BFS and DFS"
section: dsa
order: 16
summary: Build adjacency lists, traverse graphs and grids with BFS and DFS, count components, and detect cycles in directed and undirected graphs.
---

## When to use

- Input is edges, a list of neighbors, a grid, or "X is connected to Y".
- "Number of islands / components", "can you reach", "clone graph".
- "Shortest number of steps" in an unweighted graph or grid → **BFS**.
- "Explore everything", "detect a cycle", "all paths" → **DFS**.
- Hidden graphs: word ladders, lock combinations, state spaces.

## Core idea

A graph is nodes plus edges. Store it as an **adjacency list** (`dict` or list of lists). Both BFS and DFS visit each node once using a `visited` set; they differ only in order. BFS uses a queue and explores in rings of increasing distance, so the first time it reaches a node is the shortest path (unweighted). DFS uses recursion or a stack and goes deep first.

## Templates

### Build an adjacency list

```python
from collections import defaultdict

def build_graph(n: int, edges: list[list[int]], directed: bool = False):
    graph = defaultdict(list)          # or [[] for _ in range(n)]
    for u, v in edges:
        graph[u].append(v)
        if not directed:
            graph[v].append(u)
    return graph
```

### DFS (recursive and iterative)

```python
def dfs(graph, start) -> set:
    visited = set()

    def visit(u):
        visited.add(u)
        for v in graph[u]:
            if v not in visited:
                visit(v)

    visit(start)
    return visited

def dfs_iter(graph, start) -> set:
    visited, stack = {start}, [start]
    while stack:
        u = stack.pop()
        for v in graph[u]:
            if v not in visited:
                visited.add(v)
                stack.append(v)
    return visited
```

### BFS with distance

```python
from collections import deque

def bfs(graph, start) -> dict:
    dist = {start: 0}
    q = deque([start])
    while q:
        u = q.popleft()
        for v in graph[u]:
            if v not in dist:          # mark when enqueued, not when popped
                dist[v] = dist[u] + 1
                q.append(v)
    return dist
```

### Grid traversal (Number of Islands)

```python
def num_islands(grid: list[list[str]]) -> int:
    rows, cols = len(grid), len(grid[0])
    DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

    def sink(r: int, c: int) -> None:
        stack = [(r, c)]
        grid[r][c] = "0"
        while stack:
            r, c = stack.pop()
            for dr, dc in DIRS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == "1":
                    grid[nr][nc] = "0"  # mark visited by mutating the grid
                    stack.append((nr, nc))

    count = 0
    for r in range(rows):
        for c in range(cols):
            if grid[r][c] == "1":
                count += 1
                sink(r, c)
    return count
```

### Multi-source BFS (Rotting Oranges)

Seed the queue with **all** sources at distance 0, then BFS by levels.

```python
def oranges_rotting(grid: list[list[int]]) -> int:
    rows, cols = len(grid), len(grid[0])
    q = deque()
    fresh = 0
    for r in range(rows):
        for c in range(cols):
            if grid[r][c] == 2:
                q.append((r, c))
            elif grid[r][c] == 1:
                fresh += 1
    minutes = 0
    while q and fresh:
        for _ in range(len(q)):
            r, c = q.popleft()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == 1:
                    grid[nr][nc] = 2
                    fresh -= 1
                    q.append((nr, nc))
        minutes += 1
    return minutes if fresh == 0 else -1
```

### Cycle detection

**Undirected:** a visited neighbor that is not your parent means a cycle. (Or use [Union-find]({{ '/dsa/union-find/' | relative_url }}).)

```python
def has_cycle_undirected(n: int, graph) -> bool:
    visited = set()

    def dfs(u: int, parent: int) -> bool:
        visited.add(u)
        for v in graph[u]:
            if v == parent:
                continue
            if v in visited or dfs(v, u):
                return True
        return False

    return any(u not in visited and dfs(u, -1) for u in range(n))
```

**Directed:** three colors. A back edge to a node still on the current path (gray) is a cycle.

```python
def has_cycle_directed(n: int, graph) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n

    def dfs(u: int) -> bool:
        color[u] = GRAY
        for v in graph[u]:
            if color[v] == GRAY:
                return True             # back edge
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(color[u] == WHITE and dfs(u) for u in range(n))
```

## Complexity

| Representation / algorithm | Time | Space |
|---|---|---|
| BFS / DFS, adjacency list | `O(V + E)` | `O(V)` |
| Grid BFS / DFS | `O(R * C)` | `O(R * C)` |
| Adjacency matrix traversal | `O(V^2)` | `O(V^2)` |

## Pitfalls

- Marking visited when **popping** in BFS instead of when **pushing**. Nodes get enqueued many times.
- Using `list.pop(0)` as a queue (`O(n)` per pop).
- Recursive DFS on a 1000x1000 grid blows Python's recursion limit. Use an explicit stack.
- Undirected parent check fails with parallel edges between the same pair. Mention or use union-find.
- Forgetting disconnected components: loop over every node as a potential start.
- Directed cycle detection with a plain `visited` set gives false positives on DAG diamonds. Use colors.

## Classic problems

- **Number of Islands**: count DFS/BFS launches.
- **Max Area of Island**: DFS returns area.
- **Clone Graph**: DFS with old → new map.
- **Pacific Atlantic Water Flow**: reverse BFS from each ocean, intersect.
- **Surrounded Regions**: mark border-connected `O`s first.
- **Rotting Oranges** / **Walls and Gates** / **01 Matrix**: multi-source BFS.
- **Word Ladder**: BFS over words, wildcard buckets like `h*t`.
- **Graph Valid Tree**: `n - 1` edges and connected.
- **Number of Connected Components in an Undirected Graph**: DFS or union-find.
- **Course Schedule**: directed cycle detection, see [Topological sort]({{ '/dsa/topological-sort/' | relative_url }}).
- **Shortest Path in Binary Matrix**: BFS with 8 directions.
