---
title: Design an in-memory file system
section: lld
order: 20
phase: "Phase 5 · Beginner"
summary: A Composite tree of files and directories with path resolution, mkdir -p, ls, cat, write and append, rm and mv, plus locking and persistence follow-ups.
---

## Requirements

Clarifying questions to ask first:

- Absolute paths only, or also a current directory and relative paths?
- Which commands: `mkdir`, `ls`, `cat`, write, append, `rm`, `mv`? Search (`find`)?
- Does `mkdir` create missing parents (`mkdir -p`)? Does `mv` overwrite?
- Text content only, or bytes? Any size limits?
- Permissions and owners? Symlinks?
- Multiple threads (or users) at once?

Functional:

- `mkdir(path)` creates the directory and any missing parents; repeating it is a no-op.
- `ls(path)` returns sorted child names; on a file it returns `[name]` (Unix behavior).
- `write(path, data)` creates or overwrites a file; `append` adds to it; `cat` reads it.
- `rm(path, recursive=False)` refuses a non-empty directory unless recursive.
- `mv(src, dst)` moves or renames; a directory `dst` means "move into it"; never overwrites.
- `du(path)` returns total bytes under a path.
- Paths are normalized: `/a/./b/../c/` is `/a/c`.

Non-functional: operations are atomic with respect to each other (thread-safe); lookups cost `O(depth)`.

Out of scope: relative paths and `cd`, symlinks, permissions (see follow-ups), persistence.

## Entities and relationships

```text
              +-------------------------+
              | Node (ABC)              |
              | name: str               |
              | size() -> int           |
              +-------------------------+
                 ^ is-a             ^ is-a
   +-------------+-------+   +------+------------------------+
   | File                |   | Directory                     |
   | content: str        |   | children: dict[str, Node] ----+--+
   | size(): len(content)|   | size(): sum(child.size())     |  |
   +---------------------+   +-------------------------------+  |
                                    ^          has 0..* Node    |
                                    +---------------------------+

   +----------------------------------------------+
   | FileSystem (facade)                          |
   | root: Directory, _lock                       |
   | mkdir, ls, cat, write, append, rm, mv, du    |
   | _walk(parts, create), _parent(path)          |
   +----------------------------------------------+
        | uses
        v
   split_path("/a/./b/../c") -> ["a", "c"]
```

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---------|-------|------------------------|
| Composite | `Node` ← `File`, `Directory` | `size()`, `rm` and traversal treat a file and a whole subtree the same way, with no `isinstance` chains in callers |
| Facade | `FileSystem` | One entry point owns path parsing, errors and locking; nodes stay dumb data |
| Monitor (one lock) | `FileSystem._lock` | Multi-step operations like `mv` (detach + attach) are never seen half-done |

The directory tree is a trie keyed on path components: each lookup walks one dict per level.

## Code

```python
import sys
import threading
from abc import ABC, abstractmethod


class Node(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def size(self) -> int: ...


class File(Node):
    def __init__(self, name: str, content: str = ""):
        super().__init__(name)
        self.content = content

    def size(self) -> int:
        return len(self.content)


class Directory(Node):
    def __init__(self, name: str):
        super().__init__(name)
        self.children: dict[str, Node] = {}

    def size(self) -> int:                  # composite: recurse, no type checks
        return sum(child.size() for child in self.children.values())


def split_path(path: str) -> list[str]:
    """'/a/./b/../c/' -> ['a', 'c']. Absolute paths only."""
    if not path.startswith("/"):
        raise ValueError(f"absolute path required: {path!r}")
    parts: list[str] = []
    for p in path.split("/"):
        if p == "..":
            if parts:
                parts.pop()                 # '..' at root stays at root
        elif p not in ("", "."):
            parts.append(p)
    return parts


class FileSystem:
    def __init__(self):
        self.root = Directory("")
        # Simplification: one lock for the whole tree; per-directory locks if writes contend
        self._lock = threading.Lock()

    # ---- path resolution (caller holds the lock) ----
    def _walk(self, parts: list[str], create: bool = False) -> Directory:
        node = self.root
        for i, name in enumerate(parts):
            child = node.children.get(name)
            if child is None and create:
                child = node.children[name] = Directory(name)
            where = "/" + "/".join(parts[: i + 1])
            if child is None:
                raise FileNotFoundError(where)
            if not isinstance(child, Directory):
                raise NotADirectoryError(where)
            node = child
        return node

    def _parent(self, path: str) -> tuple[Directory, str]:
        parts = split_path(path)
        if not parts:
            raise PermissionError("not allowed on /")
        return self._walk(parts[:-1]), parts[-1]

    def _get(self, path: str) -> Node:
        if not split_path(path):
            return self.root
        parent, name = self._parent(path)
        if name not in parent.children:
            raise FileNotFoundError(path)
        return parent.children[name]

    # ---- commands ----
    def mkdir(self, path: str) -> None:     # mkdir -p: parents created, idempotent
        with self._lock:
            self._walk(split_path(path), create=True)

    def ls(self, path: str = "/") -> list[str]:
        with self._lock:
            node = self._get(path)
            if isinstance(node, File):
                return [node.name]
            return sorted(node.children)

    def cat(self, path: str) -> str:
        with self._lock:
            node = self._get(path)
            if isinstance(node, Directory):
                raise IsADirectoryError(path)
            return node.content

    def write(self, path: str, data: str, append: bool = False) -> None:
        with self._lock:
            parent, name = self._parent(path)
            node = parent.children.setdefault(name, File(name))
            if isinstance(node, Directory):
                raise IsADirectoryError(path)
            node.content = node.content + data if append else data

    def append(self, path: str, data: str) -> None:
        self.write(path, data, append=True)

    def rm(self, path: str, recursive: bool = False) -> None:
        with self._lock:
            parent, name = self._parent(path)
            node = parent.children.get(name)
            if node is None:
                raise FileNotFoundError(path)
            if isinstance(node, Directory) and node.children and not recursive:
                raise OSError(f"directory not empty: {path}")
            del parent.children[name]       # the whole subtree goes with it

    def mv(self, src: str, dst: str) -> None:   # never overwrites, like mv -n
        with self._lock:
            src_dir, name = self._parent(src)
            node = src_dir.children.get(name)
            if node is None:
                raise FileNotFoundError(src)
            s, d = split_path(src), split_path(dst)
            if d[: len(s)] == s:
                raise ValueError(f"cannot move {src} into itself")
            try:
                dest = self._get(dst)
            except FileNotFoundError:
                dest = None
            if isinstance(dest, Directory):  # mv a.txt /docs -> /docs/a.txt
                dst_dir, new_name = dest, name
            else:
                dst_dir, new_name = self._parent(dst)
            if new_name in dst_dir.children:
                raise FileExistsError(dst)
            del src_dir.children[name]      # every check passed: now mutate
            node.name = new_name
            dst_dir.children[new_name] = node

    def du(self, path: str = "/") -> int:
        with self._lock:
            return self._get(path).size()


if __name__ == "__main__":
    fs = FileSystem()
    fs.mkdir("/home/ana/docs")
    fs.mkdir("/home/ana/docs")                       # idempotent
    fs.write("/home/ana/docs/todo.txt", "buy milk")
    fs.append("/home/ana/docs/todo.txt", "\nfix bug")
    assert fs.cat("/home/ana/./docs/../docs/todo.txt") == "buy milk\nfix bug"
    assert fs.ls("/home/ana") == ["docs"]
    assert fs.ls("/home/ana/docs/todo.txt") == ["todo.txt"]
    fs.write("/home/ana/notes.md", "hello")
    assert fs.du("/home") == len("buy milk\nfix bug") + len("hello")

    fs.mkdir("/archive")
    fs.mv("/home/ana/docs", "/archive")              # into an existing directory
    assert fs.ls("/archive") == ["docs"] and fs.ls("/home/ana") == ["notes.md"]
    fs.mv("/home/ana/notes.md", "/archive/readme.md")   # move and rename
    assert fs.cat("/archive/readme.md") == "hello"

    def raises(fn, err) -> bool:
        try:
            fn()
        except err:
            return True
        return False

    assert raises(lambda: fs.cat("/archive"), IsADirectoryError)
    assert raises(lambda: fs.ls("/nope"), FileNotFoundError)
    assert raises(lambda: fs.mkdir("/archive/readme.md/x"), NotADirectoryError)
    assert raises(lambda: fs.rm("/archive"), OSError)          # not empty
    assert raises(lambda: fs.mv("/archive", "/archive/docs"), ValueError)
    assert raises(lambda: fs.write("/missing/f.txt", "x"), FileNotFoundError)
    fs.rm("/archive", recursive=True)
    assert fs.ls("/") == ["home"]

    sys.setswitchinterval(1e-6)                  # switch threads often so races show up
    # stress: 8 threads mkdir and append concurrently; no lost appends
    def worker(i: int) -> None:
        for j in range(200):
            fs.mkdir(f"/t/{i % 3}/{j % 5}")
            fs.append("/shared.log", "x")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fs.cat("/shared.log")) == 8 * 200
    assert fs.ls("/t") == ["0", "1", "2"] and fs.ls("/t/0") == list("01234")
    print("ok")
```

Errors reuse the stdlib `OSError` family (`FileNotFoundError`, `IsADirectoryError`, `NotADirectoryError`, `FileExistsError`), so callers catch what they already know.

## Key flows

`write("/home/ana/docs/todo.txt", "buy milk")`:

```text
client       FileSystem          split_path       Directory tree
  | write()       |                   |                  |
  |-------------->| lock              |                  |
  |               |--split(path)----->|                  |
  |               |<-[home,ana,docs,todo.txt]            |
  |               |--_walk([home,ana,docs])------------->| root->home->
  |               |<---------- Directory docs -----------| ana->docs
  |               | setdefault("todo.txt", File)         |
  |               | content = data                       |
  |<--------------| unlock                               |
```

`mv("/home/ana/docs", "/archive")`:

```text
client       FileSystem                          tree
  | mv()          |                                |
  |-------------->| lock                           |
  |               | resolve src parent, find node  |
  |               | dst inside src? -> ValueError  |
  |               | dst is a Directory: move into  |
  |               | name taken? -> FileExistsError |
  |               |--del src_dir["docs"]---------->|
  |               |--archive["docs"] = node------->|
  |<--------------| unlock                         |
```

All checks run before the first mutation, so a failed `mv` leaves the tree unchanged.

## Edge cases

- `rm("/")`, `mv("/", ...)`, `write("/", ...)`: rejected with `PermissionError`.
- `mv("/a", "/a/b")`: moving a directory into its own subtree would detach it from the tree; rejected.
- A file in the middle of a path (`/a/file.txt/x`) raises `NotADirectoryError`, not "not found".
- `write` to a path whose parent is missing fails; only `mkdir` creates parents.
- `ls` output is sorted so tests and users get a stable order.
- `..` is resolved lexically. That is correct only because there are no symlinks; with symlinks you must resolve component by component.
- `content + data` copies the whole string per append (`O(n)` each). Store a list of chunks if files grow large.

## Production follow-ups

**Is it thread-safe, and how would you scale the locking?** One lock makes every command atomic and is easy to prove correct; the GIL alone would not, since `mv` and append are multi-step. For more concurrency, give each `Directory` a read-write lock and lock top-down along the path (hand-over-hand). `mv` needs two directories: lock them in a fixed global order (for example, by path string) to avoid deadlock; see [Deadlock, livelock and starvation]({{ '/lld/deadlock-livelock-starvation/' | relative_url }}).

**What about persistence and restart?** Append every mutating command (`mkdir`, `write`, `rm`, `mv`) to a write-ahead log and fsync before applying it. On restart, load the latest snapshot and replay the log after it; take snapshots periodically so replay stays short. This is how journaling file systems and databases recover.

**How do you make retries safe?** `mkdir -p`, overwrite `write` and `rm` of a missing path (if treated as success) are naturally idempotent. `append` and `mv` are not: a retried append duplicates data. Give each mutating request an operation id and keep a recent-ops table so a replay returns the earlier result, or make appends offset-based ("write at byte 120").

**How do you add a node type without editing old code?** A `Symlink(Node)` stores a target path; path resolution follows it with a hop limit to catch loops. New tree-wide operations (`find`, export, checksum) fit a Visitor or a generator `walk()`, so `Node` subclasses do not grow a method per feature.

**What breaks at 10× or across machines?** Deep trees make every lookup walk many dicts; add a path → node index if hot paths repeat. `du` on `/` is `O(total nodes)`; cache sizes per directory and update them up the parent chain on writes (needs parent pointers). Across machines, split metadata from data: a metadata service owns the tree and chunk servers hold content, as in GFS and HDFS.

**What would you log and measure?** Count and time each command by type, lock wait time, number of nodes and total bytes, and errors by type. Keep an audit log of mutations with the caller and operation id; it doubles as the replay log.

**How would you add permissions?** Store `owner` and `mode` on `Node`. During `_walk`, require execute permission on each directory; check read or write on the final node, and raise `PermissionError`. Keep the checks in the facade so nodes stay plain data.

**How would you enforce quotas?** Keep a running size per directory (or per user) and reject a `write` that would push it over the limit, before mutating anything.

## Interview follow-ups

- **Why Composite instead of one `Node` class with an `is_dir` flag?** The flag spreads `if is_dir` checks everywhere and lets a file accidentally hold children. With Composite, each type owns its behavior and `size()` is one polymorphic call.
- **How would you implement `find /path -name "*.py" -size +1k`?** A DFS generator over the subtree, filtered by small predicate objects combined with AND/OR (Specification pattern). New filters are new classes.
- **How do you support `cd` and relative paths?** Keep a `cwd` per session, join a relative path onto it, then call the same `split_path`.
- **Why does `mv` never overwrite?** It keeps the operation reversible and the demo simple; `mv -f` semantics would delete the existing target first, inside the same lock.
