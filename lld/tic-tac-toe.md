---
title: Design Tic-Tac-Toe
section: lld
order: 28
phase: "Phase 5 · Intermediate"
summary: N×N tic-tac-toe with an O(1) win check from line counters, human and bot players as strategies, and unlimited undo with the Command pattern.
---

## Requirements

Clarifying questions to ask first:

- Fixed 3×3 or N×N? Is a win a full line, or K in a row? (N×N, full line. K in a row is a follow-up.)
- Human vs human, human vs bot? (Both.)
- Undo? How far back? (Unlimited.)
- Any UI? (Console input is enough; the core must be testable without it.)

**Functional**

- Two players alternate X then O on an N×N board.
- Reject moves that are out of range, on an occupied cell, or after the game ended.
- Detect win and draw after each move.
- Players are pluggable: keyboard input or a bot.
- Undo the last move, repeatedly.

**Non-functional**

- Win check is `O(1)` per move, not a rescan of the board.
- Deterministic tests: no real input, no randomness in the core.

**Out of scope:** networking, persistence, graphics, a perfect AI.

## Entities and relationships

```text
+------------------------------+ uses +---------------------------+
| play(game, players)  # loop  |----->| Player (ABC)              |
+------------------------------+      | choose(game) -> (r, c)    |
        | drives                      +---------------------------+
        v                               ^ is-a: HumanPlayer,
+------------------------------+        |       SimpleBot
| Game                         |
| board, turn: Mark, status    | has  +---------------------------+
| history: list[MoveCommand]   |----->| MoveCommand               |
| play(r, c) -> Status         | 0..* | r, c, mark                |
| undo()                       |      | execute(game), undo(game) |
+------------------------------+      +---------------------------+
        | has 1
        v
+------------------------------+
| Board                        |
| n, cells[n][n]               |
| rows[n], cols[n], diag, anti |
| place, clear, wins_with      |
+------------------------------+
```

X adds +1 to its row, column and diagonals; O adds -1. A line belongs to one player exactly when its sum is `±n`. One integer per line replaces scanning `n` cells.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `Player` | Human, bot or remote player plug into the same loop; the loop never asks "is this a bot?" |
| Command | `MoveCommand` | Each move knows how to undo itself (subtract from the counters); the history doubles as a replay log |
| Running counters | `Board.rows/cols/diag/anti` | `O(1)` win check and `O(1)` "would this move win?" for the bot |

## Code

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Mark(Enum):
    X = 1
    O = -1

    def other(self) -> "Mark":
        return Mark.O if self is Mark.X else Mark.X


class Status(Enum):
    IN_PROGRESS = "in progress"
    X_WON = "X won"
    O_WON = "O won"
    DRAW = "draw"


class InvalidMove(Exception):
    pass


class Board:
    def __init__(self, n: int = 3):
        self.n = n
        self.cells: list[list[Mark | None]] = [[None] * n for _ in range(n)]
        self.rows, self.cols = [0] * n, [0] * n
        self.diag = self.anti = self.filled = 0

    def _add(self, r: int, c: int, delta: int) -> None:
        self.rows[r] += delta
        self.cols[c] += delta
        if r == c:
            self.diag += delta
        if r + c == self.n - 1:
            self.anti += delta

    def wins_with(self, r: int, c: int, mark: Mark) -> bool:
        """O(1): would mark at empty (r, c) complete a line?"""
        need = (self.n - 1) * mark.value
        return (self.rows[r] == need or self.cols[c] == need
                or (r == c and self.diag == need)
                or (r + c == self.n - 1 and self.anti == need))

    def place(self, r: int, c: int, mark: Mark) -> bool:
        won = self.wins_with(r, c, mark)
        self.cells[r][c] = mark
        self.filled += 1
        self._add(r, c, mark.value)
        return won

    def clear(self, r: int, c: int) -> None:
        self._add(r, c, -self.cells[r][c].value)
        self.cells[r][c] = None
        self.filled -= 1

    def empty(self) -> list[tuple[int, int]]:
        return [(r, c) for r in range(self.n) for c in range(self.n)
                if self.cells[r][c] is None]


@dataclass
class MoveCommand:
    r: int
    c: int
    mark: Mark

    def execute(self, game: "Game") -> None:
        if game.board.place(self.r, self.c, self.mark):
            game.status = Status.X_WON if self.mark is Mark.X else Status.O_WON
        elif game.board.filled == game.board.n ** 2:
            game.status = Status.DRAW
        game.turn = self.mark.other()

    def undo(self, game: "Game") -> None:
        game.board.clear(self.r, self.c)
        game.status = Status.IN_PROGRESS  # moves only happen while in progress
        game.turn = self.mark


class Game:
    def __init__(self, n: int = 3):
        self.board = Board(n)
        self.turn = Mark.X
        self.status = Status.IN_PROGRESS
        self.history: list[MoveCommand] = []

    def play(self, r: int, c: int) -> Status:
        n = self.board.n
        if self.status is not Status.IN_PROGRESS:
            raise InvalidMove("game is over")
        if not (0 <= r < n and 0 <= c < n) or self.board.cells[r][c]:
            raise InvalidMove(f"cell {(r, c)} is off the board or taken")
        cmd = MoveCommand(r, c, self.turn)
        cmd.execute(self)
        self.history.append(cmd)
        return self.status

    def undo(self) -> None:
        if not self.history:
            raise InvalidMove("nothing to undo")
        self.history.pop().undo(self)


class Player(ABC):
    @abstractmethod
    def choose(self, game: Game) -> tuple[int, int]: ...


class HumanPlayer(Player):
    def __init__(self, ask: Callable[[str], str] = input):
        self.ask = ask

    def choose(self, game):
        r, c = map(int, self.ask(f"{game.turn.name} row col: ").split())
        return r, c


class SimpleBot(Player):
    """Win if possible, else block, else center, else first free cell."""

    def choose(self, game):
        b, free = game.board, game.board.empty()
        for mark in (game.turn, game.turn.other()):
            for r, c in free:
                if b.wins_with(r, c, mark):
                    return r, c
        mid = (b.n // 2, b.n // 2)
        return mid if mid in free else free[0]


def play(game: Game, players: dict[Mark, Player]) -> Status:
    while game.status is Status.IN_PROGRESS:
        try:
            game.play(*players[game.turn].choose(game))
        except (InvalidMove, ValueError) as e:  # bad human input: ask again
            print("try again:", e)
    return game.status


if __name__ == "__main__":
    g = Game()
    for r, c in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        assert g.play(r, c) is Status.IN_PROGRESS
    assert g.play(0, 2) is Status.X_WON
    try:
        g.play(2, 2)
        raise AssertionError("move after win")
    except InvalidMove:
        pass
    g.undo()
    assert (g.status, g.turn, g.board.rows[0]) == (Status.IN_PROGRESS, Mark.X, 2)

    g = Game(4)  # anti-diagonal on 4x4
    for r, c in [(0, 3), (0, 0), (1, 2), (0, 1), (2, 1), (0, 2)]:
        g.play(r, c)
    assert g.play(3, 0) is Status.X_WON

    g = Game()
    for r, c in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        g.play(r, c)
    assert SimpleBot().choose(g) == (0, 2)  # X wins rather than blocks
    g.undo()
    assert SimpleBot().choose(g) == (0, 2)  # O blocks X's top row

    # Human (scripted input) vs bot, with bad input that is retried.
    answers = iter(["9 9", "oops", "1 1", "0 0", "0 2", "1 0", "2 1", "2 2"])
    human = HumanPlayer(ask=lambda _prompt: next(answers))
    g = Game()
    assert play(g, {Mark.X: human, Mark.O: SimpleBot()}) is Status.DRAW
    while g.history:
        g.undo()
    assert g.board.filled == 0 and not any(g.board.rows + g.board.cols)
    print("tic-tac-toe ok")
```

## Key flows

```text
Loop       Player       Game              MoveCommand          Board
 | choose(g) |            |                    |                  |
 |---------->|            |                    |                  |
 |<- (r, c) -|            |                    |                  |
 | play(r, c) ----------->| validate           |                  |
 |           |            | create, execute -->| place(r, c, X) ->|
 |           |            |                    |<--- won? --------|
 |           |            |                    | set status, turn |
 |           |            | history.append     |                  |
 |<------------ status ---|                    |                  |

undo():  Game -> history.pop().undo(game)
              -> board.clear(r, c)   # subtract from the four counters
              -> status = IN_PROGRESS, turn = command.mark
```

## Edge cases

- Out-of-range or occupied cell: `InvalidMove`, turn unchanged.
- Move after the game ends: rejected. Undo still works and reopens the game.
- Last cell both fills the board and wins: the win check runs first, so it is a win, not a draw.
- Undo on an empty history: `InvalidMove`.
- Garbage keyboard input: `ValueError` caught by the loop, which asks again.
- `n = 1`: the first move wins, and the counters handle it without a special case.

## Production follow-ups

**Two clients send moves for one game at the same moment. What happens?** Give each game its own lock, or go optimistic: every move carries the expected move number (`len(history)`), and the server applies it with `UPDATE game SET ... WHERE id = :id AND version = :n`. A stale client gets a conflict and refetches. Games are independent, so there is no global lock. See [Concurrency in LLD]({{ '/lld/concurrency-in-lld/' | relative_url }}).

**How does a game survive a restart?** Store the move list, not the board. Loading a game replays the moves through `MoveCommand`, which also rebuilds the counters. At most `n²` moves, so replay is instant.

**What if the client retries a move?** The request is `(game_id, move_no, r, c)`. If `move_no` is already applied with the same cell, return the current state; with a different cell, return a conflict. A retry never places a second mark.

**How do you add a minimax bot or a remote player?** Each is a new `Player`. The loop and `Game` stay untouched. K-in-a-row on a big board (Gomoku) breaks the full-line counters, so the win rule becomes its own strategy that scans four directions from the last move in `O(K)`.

**What changes at 10× players or across machines?** Each game is a few hundred bytes and independent, so shard by `game_id`. Keep game state in Redis or PostgreSQL so any app server can handle any move, and push the opponent's move over a WebSocket gateway. Matchmaking is a separate service.

**What do you measure?** Moves per second, invalid-move rate by client version (a spike means a client bug or cheating), game length, abandonment rate, bot response time. Log each move with game id, move number and player.

**Fairness and cheating?** The server is authoritative: it validates every move and decides the result, never trusting a client-reported win. A per-move timer on the server forfeits stalled games.

## Interview follow-ups

- **Why +1/-1 instead of two counter sets?** One integer per line captures both players, and `|sum| == n` only when the whole line is one mark.
- **Make the bot unbeatable?** Minimax with alpha-beta pruning; on 3×3, memoize on the board tuple and it solves the whole game instantly.
- **Redo?** Push undone commands onto a redo stack; clear it on any new move.
- **Why is `turn` on `Game`, not on `Player`?** One source of truth. Players are stateless strategies, so the same bot can play either side.
