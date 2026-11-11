---
title: Binary search trees
section: dsa
order: 11
summary: Use the BST ordering invariant for O(h) search, insert, delete, validation, kth smallest and LCA.
---

## When to use

- The input is a BST, or you need an ordered structure with fast insert and lookup.
- "Validate", "kth smallest", "closest value", "range sum", "successor".
- "Sorted array/list to balanced tree".
- In Python interviews, a sorted container is often simulated with `bisect` on a list, or a heap. `sortedcontainers.SortedList` is not in the standard library; ask before using it.

## Core idea

For every node, **all** values in the left subtree are smaller and **all** values in the right subtree are larger. This lets you discard half the tree at each step (`O(h)`), and an **in-order traversal yields sorted order**. Most BST tricks are one of these two facts.

## Templates

### Search and insert

```python
def search_bst(root, target):
    node = root
    while node and node.val != target:
        node = node.left if target < node.val else node.right
    return node

def insert_bst(root, val):
    if not root:
        return TreeNode(val)
    if val < root.val:
        root.left = insert_bst(root.left, val)
    else:
        root.right = insert_bst(root.right, val)
    return root
```

### Validate (pass bounds down)

```python
def is_valid_bst(root) -> bool:
    def valid(node, low, high) -> bool:
        if not node:
            return True
        if not (low < node.val < high):
            return False
        return valid(node.left, low, node.val) and valid(node.right, node.val, high)

    return valid(root, float("-inf"), float("inf"))
```

### Kth smallest (in-order, stop early)

```python
def kth_smallest(root, k: int) -> int:
    stack, node = [], root
    while True:
        while node:
            stack.append(node)
            node = node.left
        node = stack.pop()
        k -= 1
        if k == 0:
            return node.val
        node = node.right
```

### Delete

```python
def delete_node(root, key):
    if not root:
        return None
    if key < root.val:
        root.left = delete_node(root.left, key)
    elif key > root.val:
        root.right = delete_node(root.right, key)
    else:
        if not root.left:
            return root.right
        if not root.right:
            return root.left
        succ = root.right            # in-order successor: min of right subtree
        while succ.left:
            succ = succ.left
        root.val = succ.val
        root.right = delete_node(root.right, succ.val)
    return root
```

### Lowest common ancestor in a BST

```python
def lca_bst(root, p, q):
    node = root
    while node:
        if p.val < node.val and q.val < node.val:
            node = node.left
        elif p.val > node.val and q.val > node.val:
            node = node.right
        else:
            return node  # split point
```

### Sorted array to balanced BST

```python
def sorted_array_to_bst(nums: list[int]):
    def build(lo: int, hi: int):  # inclusive bounds
        if lo > hi:
            return None
        mid = (lo + hi) // 2
        return TreeNode(nums[mid], build(lo, mid - 1), build(mid + 1, hi))

    return build(0, len(nums) - 1)
```

## Complexity

| Operation | Balanced | Skewed (worst) |
|---|---|---|
| Search, insert, delete | `O(log n)` | `O(n)` |
| Validate, full in-order | `O(n)` | `O(n)` |
| Kth smallest | `O(h + k)` | `O(n)` |
| Space (recursion / stack) | `O(log n)` | `O(n)` |

Self-balancing variants (AVL, red-black) guarantee `O(log n)`. You rarely implement them in interviews; know that they exist and that Java `TreeMap` / C++ `std::map` are red-black trees.

## Pitfalls

- Validating by comparing only with direct children. `5 → left 3 → right 7` passes that check but is invalid. Pass bounds.
- Duplicates: clarify whether they go left, right, or are disallowed. Strict `<` in validation rejects duplicates.
- Using `0` or `-1` as bound sentinels when values can be any int. Use `float("inf")` or `None`.
- Forgetting the tree may be skewed: say `O(h)`, not `O(log n)`, unless it is balanced.

## Classic problems

- **Search in a BST** / **Insert into a BST**: walk left or right.
- **Validate Binary Search Tree**: bounds, or check in-order is strictly increasing.
- **Kth Smallest Element in a BST**: iterative in-order, stop at k.
- **Lowest Common Ancestor of a BST**: first node between `p` and `q`.
- **Delete Node in a BST**: replace with in-order successor.
- **Convert Sorted Array to BST**: middle as root.
- **Inorder Successor in BST**: last node where you went left.
- **Two Sum IV - Input is a BST**: in-order + two pointers, or a set.
- **Range Sum of BST**: prune subtrees outside `[low, high]`.
- **Trim a BST**: if `node.val < low`, return trimmed right subtree.
- **Recover Binary Search Tree**: find two inversions in in-order.
- **BST Iterator**: controlled iterative in-order with a stack.
