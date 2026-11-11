---
title: Shortest paths
section: dsa
order: 18
summary: Pick between BFS, 0-1 BFS, Dijkstra and Bellman-Ford based on edge weights and constraints, with templates for each.
---

## When to use which

| Graph | Algorithm | Time |
|---|---|---|
| Unweighted (all edges cost 1) | BFS | `O(V + E)` |
| Weights are only 0 or 1 | 0-1 BFS (deque) | `O(V + E)` |
| Non-negative weights | Dijkstra (heap) | `O((V + E) log V)` |
| Negative weights, or "at most k edges" | Bellman-Ford | `O(V * E)` or `O(k * E)` |
| All pairs, small `V` (≤ ~400) | Floyd-Warshall | `O(V^3)` |
| DAG, any weights | Topological order + relax | `O(V + E)` |

Signals: "minimum cost / time / effort", "cheapest flight", "network delay", "path with minimum maximum".

## Core idea

All of these repeatedly **relax** edges: if `dist[u] + w < dist[v]`, improve `dist[v]`. They differ in the order of relaxation. BFS works because all edges are equal. Dijkstra always expands the closest unfinalized node, which is safe only when weights are non-negative. Bellman-Ford relaxes every edge `V - 1` times, so it tolerates negative edges and can detect negative cycles.

## Templates

### BFS (unweighted)

```python
from collections import deque

def bfs_shortest(graph, src) -> dict:
    dist = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v in graph[u]:
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist
```

### Dijkstra (lazy deletion)

```python
import heapq

def dijkstra(n: int, graph, src: int) -> list[float]:
    """graph[u] = [(v, w), ...], w >= 0."""
    dist = [float("inf")] * n
    dist[src] = 0
    heap = [(0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue                    # stale entry, already improved
        for v, w in graph[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist
```

For a single target, return `d` the first time you pop it. For "minimize the maximum edge on the path" (Path With Minimum Effort, Swim in Rising Water), replace `d + w` with `max(d, w)`.

### 0-1 BFS

```python
def zero_one_bfs(n: int, graph, src: int) -> list[float]:
    """graph[u] = [(v, w), ...] with w in {0, 1}."""
    dist = [float("inf")] * n
    dist[src] = 0
    dq = deque([src])
    while dq:
        u = dq.popleft()
        for v, w in graph[u]:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                if w == 0:
                    dq.appendleft(v)    # same distance: front
                else:
                    dq.append(v)        # one more: back
    return dist
```

### Bellman-Ford

```python
def bellman_ford(n: int, edges: list[tuple[int, int, int]], src: int):
    dist = [float("inf")] * n
    dist[src] = 0
    for _ in range(n - 1):
        updated = False
        for u, v, w in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                updated = True
        if not updated:
            break                       # early exit
    for u, v, w in edges:               # one more pass: still improving?
        if dist[u] + w < dist[v]:
            return None                 # negative cycle reachable
    return dist
```

### At most k edges (Cheapest Flights Within K Stops)

Bellman-Ford with a **copy** per round, so each round extends paths by exactly one edge.

```python
def find_cheapest_price(n, flights, src, dst, k) -> int:
    dist = [float("inf")] * n
    dist[src] = 0
    for _ in range(k + 1):              # k stops = k + 1 edges
        nxt = dist[:]
        for u, v, w in flights:
            if dist[u] + w < nxt[v]:
                nxt[v] = dist[u] + w
        dist = nxt
    return -1 if dist[dst] == float("inf") else dist[dst]
```

### Floyd-Warshall

```python
def floyd_warshall(n: int, edges) -> list[list[float]]:
    d = [[float("inf")] * n for _ in range(n)]
    for i in range(n):
        d[i][i] = 0
    for u, v, w in edges:
        d[u][v] = min(d[u][v], w)
    for k in range(n):                  # k must be the outer loop
        for i in range(n):
            for j in range(n):
                if d[i][k] + d[k][j] < d[i][j]:
                    d[i][j] = d[i][k] + d[k][j]
    return d
```

## Pitfalls

- Dijkstra with negative edges gives wrong answers silently.
- Skipping the stale check (`d > dist[u]`) in lazy Dijkstra: still correct, but slower.
- Using a `visited` set in Dijkstra and marking on **push**. Mark (or finalize) on **pop**.
- K-stops with plain Bellman-Ford (no copy) lets a single round chain several edges.
- Dijkstra with a "k stops" constraint needs state `(node, stops)`; plain `dist[node]` pruning is wrong.
- Floyd-Warshall with `k` as an inner loop.

## Classic problems

- **Network Delay Time**: Dijkstra, answer is `max(dist)`.
- **Path With Minimum Effort**: Dijkstra with `max` instead of `+` (or binary search + BFS).
- **Swim in Rising Water**: same minimax Dijkstra.
- **Cheapest Flights Within K Stops**: Bellman-Ford with k + 1 rounds.
- **Shortest Path in Binary Matrix**: BFS, 8 directions.
- **Minimum Cost to Make at Least One Valid Path in a Grid**: 0-1 BFS.
- **Path with Maximum Probability**: Dijkstra with a max-heap of probabilities.
- **Find the City With the Smallest Number of Neighbors**: Floyd-Warshall.
- **Word Ladder**: BFS on implicit graph.
- **Shortest Path to Get All Keys**: BFS over `(r, c, key_mask)`.
