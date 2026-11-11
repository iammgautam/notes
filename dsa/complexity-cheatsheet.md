---
title: Big-O cheat sheet
section: dsa
order: 2
summary: Costs of common Python list, dict, set, deque and heapq operations, sorting, and which complexity fits a given input size.
---

## Input size to target complexity

Assume roughly 10^7–10^8 simple operations per second in C++, and about 10x fewer in Python.

| `n` up to | Feasible complexity | Typical technique |
|---|---|---|
| 10–12 | `O(n!)`, `O(n * 2^n)` | Permutations, brute force backtracking |
| 20–25 | `O(2^n)` | Subsets, bitmask DP |
| 100–500 | `O(n^3)` | Floyd-Warshall, interval DP |
| 1,000–5,000 | `O(n^2)` | 2D DP, all pairs |
| 10^5–10^6 | `O(n log n)` or `O(n)` | Sorting, heap, binary search, two pointers |
| 10^8+ | `O(log n)` or `O(1)` | Binary search on answer, math |

> **Rule of thumb:** `n ≤ 10^5` almost always means the interviewer wants `O(n log n)` or better.

## Python list

| Operation | Cost |
|---|---|
| `a[i]`, `a[i] = x`, `len(a)` | `O(1)` |
| `a.append(x)`, `a.pop()` | `O(1)` amortized |
| `a.pop(0)`, `a.insert(0, x)` | `O(n)` |
| `a.insert(i, x)`, `a.pop(i)`, `del a[i]` | `O(n)` |
| `x in a`, `a.index(x)`, `a.count(x)`, `a.remove(x)` | `O(n)` |
| `a[i:j]` (slice copy) | `O(j - i)` |
| `a + b`, `a.extend(b)` | `O(len(a) + len(b))`, `O(len(b))` |
| `a.reverse()`, `a[::-1]` | `O(n)` |
| `min(a)`, `max(a)`, `sum(a)` | `O(n)` |
| `a.sort()`, `sorted(a)` | `O(n log n)` |

## Python dict and set

| Operation | Average | Worst |
|---|---|---|
| `d[k]`, `d[k] = v`, `k in d`, `del d[k]` | `O(1)` | `O(n)` |
| `s.add(x)`, `x in s`, `s.discard(x)` | `O(1)` | `O(n)` |
| Iterate | `O(n)` | `O(n)` |
| `s.union(t)` | `O(len(s) + len(t))` | |
| `s.intersection(t)` | `O(min(len(s), len(t)))` | |
| `s.difference(t)` | `O(len(s))` | |

- `collections.Counter(a)` is `O(n)`; `c.most_common(k)` is `O(n log k)`.
- Keys must be hashable: use `tuple`, not `list`, for composite keys.

## collections.deque

| Operation | Cost |
|---|---|
| `append`, `appendleft`, `pop`, `popleft` | `O(1)` |
| `d[0]`, `d[-1]` | `O(1)` |
| `d[i]` in the middle | `O(n)` |
| `rotate(k)` | `O(k)` |

> **Gotcha:** Using `list.pop(0)` as a queue makes BFS `O(n^2)`. Always use `deque.popleft()`.

## heapq (min-heap on a list)

| Operation | Cost |
|---|---|
| `heapify(a)` | `O(n)` |
| `heappush`, `heappop`, `heappushpop`, `heapreplace` | `O(log n)` |
| `a[0]` (peek min) | `O(1)` |
| `nlargest(k, a)`, `nsmallest(k, a)` | `O(n log k)` |
| Remove arbitrary element | `O(n)` (use lazy deletion instead) |

- Max-heap: push `-x`.
- Ties: push tuples `(priority, counter, item)` so items are never compared.

## Sorting and searching

| Operation | Cost |
|---|---|
| `sorted`, `list.sort` (Timsort, stable) | `O(n log n)` time, `O(n)` space |
| `bisect.bisect_left/right` | `O(log n)` |
| `bisect.insort` | `O(n)` (insertion shifts elements) |
| Counting sort / bucket sort | `O(n + k)` |

## Strings

| Operation | Cost |
|---|---|
| `s[i]`, `len(s)` | `O(1)` |
| `s + t` | `O(len(s) + len(t))`, new string |
| `"".join(parts)` | `O(total length)` |
| `sub in s`, `s.find(sub)` | `O(n * m)` worst, fast in practice |
| `s[::-1]`, `s.split()`, `s.lower()` | `O(n)` |

> **Gotcha:** Strings are immutable. `res += ch` in a loop can be `O(n^2)`. Collect into a list and `"".join` once.

## Common algorithm costs

| Algorithm | Time | Space |
|---|---|---|
| BFS / DFS on graph | `O(V + E)` | `O(V)` |
| Dijkstra with heap | `O((V + E) log V)` | `O(V)` |
| Bellman-Ford | `O(V * E)` | `O(V)` |
| Topological sort | `O(V + E)` | `O(V)` |
| Union-find (path compression + rank) | ~`O(α(n))` per op | `O(n)` |
| Trie insert/search | `O(L)` per word | `O(total chars)` |
| Recursion depth | | `O(depth)` stack |

## Space reminders

- Recursion uses stack space: a DFS on a skewed tree is `O(n)` space.
- Python's default recursion limit is 1000. Use `sys.setrecursionlimit` or go iterative for deep inputs.
- Output size counts if you must build it (e.g. all subsets is `O(n * 2^n)`).
