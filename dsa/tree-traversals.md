---
title: Tree traversals
section: dsa
order: 10
summary: Recursive and iterative DFS (pre, in, post order) and BFS level order, plus how to pick between returning values and passing state down.
---

## When to use

- Any binary tree question. Decide first: **DFS** (depth, paths, subtree properties) or **BFS** (levels, nearest node, right side view).
- "Level by level", "minimum depth", "zigzag" → BFS.
- "Height", "diameter", "is balanced", "path sum", "LCA" → DFS post-order (compute from children).
- "Serialize", "copy", "root-to-leaf path" → DFS pre-order.
- BST in sorted order → in-order.

## Core idea

Two styles of DFS:

- **Bottom-up (return values):** each call returns something about its subtree (height, sum, is-valid). The parent combines children's results. Most tree problems fit this.
- **Top-down (pass state):** pass accumulated state into children (current path sum, depth, min/max bounds). Use for root-to-leaf problems.

When the answer is not what the function returns (e.g. diameter), keep a separate `best` variable updated along the way.

```python
class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right
```

## Templates

### Recursive DFS

```python
def preorder(root):   # node, left, right
    if not root:
        return []
    return [root.val] + preorder(root.left) + preorder(root.right)

def max_depth(root) -> int:  # bottom-up
    if not root:
        return 0
    return 1 + max(max_depth(root.left), max_depth(root.right))
```

### Bottom-up with a global answer (diameter)

```python
def diameter(root) -> int:
    best = 0

    def height(node) -> int:
        nonlocal best
        if not node:
            return 0
        lh, rh = height(node.left), height(node.right)
        best = max(best, lh + rh)  # path through this node, in edges
        return 1 + max(lh, rh)

    height(root)
    return best
```

### Iterative pre-order

```python
def preorder_iter(root) -> list[int]:
    res, stack = [], [root] if root else []
    while stack:
        node = stack.pop()
        res.append(node.val)
        if node.right:
            stack.append(node.right)  # push right first so left is processed first
        if node.left:
            stack.append(node.left)
    return res
```

### Iterative in-order

```python
def inorder_iter(root) -> list[int]:
    res, stack = [], []
    node = root
    while node or stack:
        while node:            # go as far left as possible
            stack.append(node)
            node = node.left
        node = stack.pop()
        res.append(node.val)
        node = node.right
    return res
```

### Iterative post-order

```python
def postorder_iter(root) -> list[int]:
    # Pre-order with children swapped (node, right, left), then reverse.
    res, stack = [], [root] if root else []
    while stack:
        node = stack.pop()
        res.append(node.val)
        if node.left:
            stack.append(node.left)
        if node.right:
            stack.append(node.right)
    return res[::-1]
```

### BFS level order

```python
from collections import deque

def level_order(root) -> list[list[int]]:
    if not root:
        return []
    res, q = [], deque([root])
    while q:
        level = []
        for _ in range(len(q)):  # exactly the nodes of the current level
            node = q.popleft()
            level.append(node.val)
            if node.left:
                q.append(node.left)
            if node.right:
                q.append(node.right)
        res.append(level)
    return res
```

Right side view: take `level[-1]`. Zigzag: reverse every other level. Minimum depth: return the depth at the first leaf.

### Lowest common ancestor (binary tree)

```python
def lca(root, p, q):
    if not root or root is p or root is q:
        return root
    left, right = lca(root.left, p, q), lca(root.right, p, q)
    if left and right:
        return root          # p and q are in different subtrees
    return left or right
```

## Complexity

| Traversal | Time | Space |
|---|---|---|
| DFS (any order) | `O(n)` | `O(h)`: `O(log n)` balanced, `O(n)` skewed |
| BFS | `O(n)` | `O(w)`, max width, up to `O(n)` |
| Morris in-order | `O(n)` | `O(1)` (modifies tree temporarily) |

## Pitfalls

- Confusing depth (edges vs nodes). Clarify what the problem counts.
- Recursion limit 1000 in Python on skewed trees with 10^4+ nodes. Go iterative or raise the limit.
- Leaf check is `not node.left and not node.right`, not `not node`. Path Sum fails otherwise on one-child nodes.
- In BFS, reading `len(q)` inside the inner loop instead of once before it.
- List concatenation in recursive traversals is `O(n^2)` worst case. Pass a result list for large inputs.

## Classic problems

- **Maximum Depth of Binary Tree**: bottom-up.
- **Invert Binary Tree**: swap children, recurse.
- **Same Tree** / **Subtree of Another Tree**: recursive equality.
- **Diameter of Binary Tree**: global best with heights.
- **Balanced Binary Tree**: return height or `-1` for unbalanced.
- **Binary Tree Level Order Traversal** / **Zigzag**: BFS by level.
- **Binary Tree Right Side View**: last node per level.
- **Path Sum** / **Path Sum II**: top-down with remaining sum, backtrack the path.
- **Binary Tree Maximum Path Sum**: return best single branch, `max(0, child)`; update global with both.
- **Lowest Common Ancestor of a Binary Tree**: template above.
- **Count Good Nodes**: pass max so far down.
- **Construct Binary Tree from Preorder and Inorder**: root from preorder, split by inorder index map.
- **Serialize and Deserialize Binary Tree**: pre-order with `"#"` for null.
