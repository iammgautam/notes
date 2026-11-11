---
title: Linked lists
section: dsa
order: 9
summary: Dummy nodes, fast and slow pointers, and in-place reversal, the three tools that solve almost every linked list question.
---

## When to use

- Input is a `ListNode` head. You usually cannot index, so think in pointer moves.
- "Find the middle", "detect a cycle", "kth from the end" → fast and slow pointers.
- "Reverse", "reorder", "palindrome list" → in-place reversal.
- "Merge", "remove", "partition", anything that may change the head → dummy node.
- LRU cache → doubly linked list + hash map.

## Core idea

Linked list problems are pointer bookkeeping. Draw the boxes, save `next` before you overwrite it, and use a **dummy** node so the head is never a special case. Two pointers moving at different speeds give you middle and cycle information in one pass with `O(1)` space.

```python
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next
```

## Templates

### Dummy node (remove elements, merge)

```python
def merge_two_lists(a: ListNode | None, b: ListNode | None) -> ListNode | None:
    dummy = tail = ListNode()
    while a and b:
        if a.val <= b.val:
            tail.next, a = a, a.next
        else:
            tail.next, b = b, b.next
        tail = tail.next
    tail.next = a or b
    return dummy.next
```

### Reverse a list (iterative)

```python
def reverse_list(head: ListNode | None) -> ListNode | None:
    prev = None
    cur = head
    while cur:
        nxt = cur.next     # save before overwriting
        cur.next = prev
        prev = cur
        cur = nxt
    return prev            # new head
```

Recursive version (uses `O(n)` stack):

```python
def reverse_recursive(head: ListNode | None) -> ListNode | None:
    if not head or not head.next:
        return head
    new_head = reverse_recursive(head.next)
    head.next.next = head
    head.next = None
    return new_head
```

### Fast and slow: middle node

```python
def middle_node(head: ListNode) -> ListNode:
    slow = fast = head
    while fast and fast.next:
        slow = slow.next
        fast = fast.next.next
    return slow  # for even length, this is the second middle
```

To get the **first** middle (useful for splitting in merge sort), start with `fast = head.next`.

### Cycle detection and cycle start (Floyd)

```python
def detect_cycle(head: ListNode | None) -> ListNode | None:
    slow = fast = head
    while fast and fast.next:
        slow = slow.next
        fast = fast.next.next
        if slow is fast:
            # Distance head->start equals distance meet->start (mod cycle length).
            slow = head
            while slow is not fast:
                slow = slow.next
                fast = fast.next
            return slow
    return None
```

### Remove nth node from end (gap of n)

```python
def remove_nth_from_end(head: ListNode, n: int) -> ListNode | None:
    dummy = ListNode(0, head)
    fast = slow = dummy
    for _ in range(n + 1):    # gap of n + 1 so slow stops before the target
        fast = fast.next
    while fast:
        fast = fast.next
        slow = slow.next
    slow.next = slow.next.next
    return dummy.next
```

### Reorder list (combines all three)

```python
def reorder_list(head: ListNode) -> None:
    # 1. find the end of the first half
    slow, fast = head, head.next
    while fast and fast.next:
        slow, fast = slow.next, fast.next.next
    # 2. reverse the second half
    second = reverse_list(slow.next)
    slow.next = None
    # 3. interleave
    first = head
    while second:
        n1, n2 = first.next, second.next
        first.next = second
        second.next = n1
        first, second = n1, n2
```

## Complexity

| Operation | Time | Space |
|---|---|---|
| Traverse, reverse, find middle, detect cycle | `O(n)` | `O(1)` |
| Recursive reverse | `O(n)` | `O(n)` stack |
| Merge k sorted lists (heap) | `O(N log k)` | `O(k)` |
| Sort list (merge sort) | `O(n log n)` | `O(log n)` stack |

## Pitfalls

- Losing the rest of the list: always save `nxt = cur.next` before changing `cur.next`.
- `fast.next.next` on `None`: the loop condition must be `fast and fast.next`.
- Forgetting to cut the list (`slow.next = None`) when splitting, creating a cycle.
- Comparing nodes with `==` when you mean identity. Use `is`.
- Returning `head` instead of `dummy.next` after the head may have been removed.

## Classic problems

- **Reverse Linked List** / **Reverse Linked List II**: iterative reversal, reconnect around the sublist.
- **Merge Two Sorted Lists**: dummy + tail.
- **Linked List Cycle** / **Linked List Cycle II**: Floyd.
- **Middle of the Linked List**: fast and slow.
- **Remove Nth Node From End of List**: gap pointers + dummy.
- **Reorder List**: middle, reverse, interleave.
- **Palindrome Linked List**: reverse the second half and compare.
- **Add Two Numbers**: dummy + carry.
- **Copy List with Random Pointer**: old → new hash map, or interleave copies.
- **Intersection of Two Linked Lists**: switch heads when reaching the end; pointers meet at the intersection.
- **Merge k Sorted Lists**: heap of `(val, i, node)`.
- **Reverse Nodes in k-Group**: count k, reverse, reconnect.
- **LRU Cache**: `OrderedDict`, or doubly linked list + dict.
- **Find the Duplicate Number**: treat values as next pointers, Floyd.
