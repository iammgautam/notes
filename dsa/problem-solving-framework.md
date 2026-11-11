---
title: Problem-solving framework
section: dsa
order: 1
summary: A repeatable six-step loop for unseen interview problems, plus a table mapping problem signals to the technique that usually fits.
---

Interviewers grade the process as much as the answer. Talk through every step out loud.

## The loop

| Step | What you do | Time (45 min round) |
|---|---|---|
| 1. Clarify | Restate the problem. Ask about input size, ranges, duplicates, negatives, empty input, sorted or not, return format. | 2–4 min |
| 2. Examples | Work one normal example by hand, then 2–3 edge cases. Confirm expected output with the interviewer. | 2–3 min |
| 3. Brute force | State the naive solution and its complexity. Do not code it unless asked. | 1–2 min |
| 4. Optimize | Find the bottleneck (repeated work, nested loop, re-sorting). Pick a pattern. State the new complexity. Get a nod before coding. | 5–10 min |
| 5. Code | Write clean code with good names. Small helpers are fine. Narrate decisions. | 15–20 min |
| 6. Test | Dry-run your example line by line. Then edge cases. Fix bugs calmly. | 5 min |

### Clarifying questions worth asking

- What is the size of `n`? (Decides the target complexity, see [Big-O cheat sheet]({{ '/dsa/complexity-cheatsheet/' | relative_url }}).)
- Can values be negative, zero, or duplicated?
- Is the input sorted? Can I modify it in place?
- What do I return if there is no answer: `-1`, `None`, empty list?
- For graphs: directed or undirected? Can there be cycles? Is it connected?
- For strings: ASCII only or Unicode? Case sensitive?

### How to optimize from brute force

- **Repeated lookup** inside a loop → hash map or set (`O(n)` → `O(1)` per lookup).
- **Repeated range sum** → prefix sums.
- **Nested loop over sorted data** → two pointers or binary search.
- **Recomputing overlapping subproblems** → memoize (DP).
- **Need the min/max repeatedly** → heap, or monotonic stack/deque.
- **Search space is monotonic** (if `x` works, `x+1` works) → binary search on the answer.
- Ask: "What information am I throwing away that I could keep?"

## Pattern recognition

| Signal in the problem | Technique to try |
|---|---|
| "Find pair/triplet with sum", sorted array | Two pointers |
| "Have we seen X before?", counts, anagrams | Hash map / set |
| "Longest/shortest contiguous subarray/substring with property" | Sliding window |
| "Subarray sum equals k", negatives allowed | Prefix sum + hash map |
| Sorted input, "find first/last position", `O(log n)` required | Binary search |
| "Minimize the maximum" / "smallest capacity such that" | Binary search on answer |
| "Next greater/smaller element", histogram | Monotonic stack |
| "Top k", "kth largest", "merge k sorted" | Heap |
| Overlapping ranges, meetings, calendars | Sort intervals + sweep |
| "All subsets/permutations/combinations", "generate all" | Backtracking |
| "Number of ways", "min cost", "can you reach", choices at each step | Dynamic programming |
| Tree problems | DFS (recursive) or BFS (level order) |
| "Shortest path", unweighted grid | BFS |
| Weighted shortest path, non-negative weights | Dijkstra |
| Dependencies, "order of courses/tasks" | Topological sort |
| "Connected components", "are these connected", dynamic merging | Union-find or DFS |
| Prefix matching, autocomplete, word search on many words | Trie |
| "Without extra space", XOR tricks, powers of two | Bit manipulation |
| Locally optimal choice is provably globally optimal | Greedy (prove with exchange argument) |
| Linked list cycle, middle node | Fast and slow pointers |

> **Interview tip:** If stuck, say the brute force first, then ask yourself which pattern from this table removes its bottleneck. Silence is worse than a slow but correct plan.

## Testing checklist

- Empty input, single element, two elements.
- All duplicates, all same value.
- Negative numbers, zero, very large values.
- Already sorted, reverse sorted.
- Answer at the very start or very end.
- For graphs: disconnected graph, self-loop, cycle.

## Pitfalls

- Coding before agreeing on the approach. You lose time when the interviewer redirects you.
- Silent thinking for more than ~30 seconds. Narrate.
- Skipping the dry run. Most bugs are off-by-one errors you find by tracing indices.
- Over-engineering: classes and abstractions the problem does not need.
- Forgetting to state time and space complexity at the end.

## Communication phrases

- "Let me make sure I understand: ..."
- "The brute force is `O(n^2)` because ... The bottleneck is ..."
- "I'll trade `O(n)` space for `O(1)` lookups using a hash map."
- "Before I code, does this approach sound reasonable?"
- "Let me trace through the example to check."
