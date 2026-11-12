---
title: Design a chess game
section: lld
order: 30
phase: "Phase 5 · Intermediate"
summary: Two-player chess with polymorphic move generation, legality by try-and-undo, check, checkmate and stalemate, and undo through Command moves.
---

## Requirements

Clarifying questions to ask first:

- Two players in one process, or online? (One process; online is a follow-up.)
- Full rules? (Core: every piece's movement, check, checkmate, stalemate, auto-queen promotion. Castling, en passant, under-promotion and draw rules are extensions.)
- Move input? (Coordinate squares: `move("e2", "e4")`.)
- Undo? Clocks? (Undo yes, clocks no.)

**Functional**

- Standard start position; white moves first; turns alternate.
- Each piece moves by its own rules; sliding pieces stop at the first piece in the way.
- Reject moves that break movement rules or leave your own king in check (this covers pins).
- After each move, detect check, checkmate and stalemate.
- A pawn reaching the last rank becomes a queen.
- Undo any number of moves.

**Non-functional**

- A new piece type is one new class.
- Move generation is provably right: perft counts match the published numbers.

**Out of scope:** castling, en passant, under-promotion, draw by repetition or 50 moves, clocks, AI, PGN.

## Entities and relationships

```text
+----------------------------+ has  +------------------------------+
| Game                       |----->| Board                        |
| board, turn: Color, status |      | grid[8][8]: Piece | None     |
| history: list[Move]        |      | pieces(color), king(color)   |
| move(src, dst) -> Status   |      | attacked(pos, by) -> bool    |
| undo()                     |      +------------------------------+
| legal_moves(color)         |               | has 0..32
| in_check(color)            |               v
+----------------------------+      +------------------------------+
        | has 0..*                  | Piece (ABC)                  |
        v                           | color                        |
+----------------------------+ uses | moves(board, pos)  # pseudo  |
| Move  (Command)            |----->| attacks(board, pos)          |
| src, dst, piece, captured  |      +------------------------------+
| execute(board)             |        ^ is-a: Slider(DIRS): Rook,
| undo(board)                |        |         Bishop, Queen
+----------------------------+        |  Stepper(STEPS): King, Knight
                                      |  Pawn (overrides attacks)
```

Legality is two layers. Each piece yields **pseudo-legal** targets (movement rules only). `Game` keeps a move only if, after executing it, its own king is not attacked. That one rule handles pins, check evasion and "the king can't walk into check" without special cases.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Polymorphism + template | `Piece.moves`, `Slider`, `Stepper` | `Game` never switches on piece type; sliders share one loop and differ only in `DIRS` |
| Command | `Move.execute/undo` | Exact undo (it remembers the captured piece); reused to try a move and roll it back during legality checks |
| Generate then filter | `Game.legal_moves` | One king-safety rule instead of separate pin and check-evasion logic |
| Factory method | `Board.standard`, `Board.from_dict` | Start position for play, arbitrary positions for tests |

## Code

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

Pos = tuple[int, int]  # (row, col); row 0 is rank 1, col 0 is file a


class Color(Enum):
    WHITE = 1   # value doubles as the pawn's forward step
    BLACK = -1

    @property
    def other(self) -> "Color":
        return Color.BLACK if self is Color.WHITE else Color.WHITE


class Status(Enum):
    ACTIVE = "active"
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"


class IllegalMove(Exception):
    pass


def square(name: str) -> Pos:
    if len(name) != 2 or name[0] not in "abcdefgh" or name[1] not in "12345678":
        raise IllegalMove(f"no square {name}")
    return int(name[1]) - 1, ord(name[0]) - ord("a")


class Piece(ABC):
    def __init__(self, color: Color):
        self.color = color

    @abstractmethod
    def moves(self, board: "Board", pos: Pos) -> Iterator[Pos]:
        """Pseudo-legal targets: movement rules only, ignoring checks."""

    def attacks(self, board: "Board", pos: Pos) -> Iterator[Pos]:
        return self.moves(board, pos)


class Slider(Piece):
    DIRS: tuple[Pos, ...] = ()

    def moves(self, board, pos):
        for dr, dc in self.DIRS:
            t = (pos[0] + dr, pos[1] + dc)
            while board.inside(t):
                if board[t] is None or board[t].color is not self.color:
                    yield t
                if board[t] is not None:
                    break
                t = (t[0] + dr, t[1] + dc)


class Stepper(Piece):
    STEPS: tuple[Pos, ...] = ()

    def moves(self, board, pos):
        for dr, dc in self.STEPS:
            t = (pos[0] + dr, pos[1] + dc)
            if board.inside(t) and (board[t] is None or board[t].color is not self.color):
                yield t


STRAIGHT = ((1, 0), (-1, 0), (0, 1), (0, -1))
DIAGONAL = ((1, 1), (1, -1), (-1, 1), (-1, -1))


class Rook(Slider): DIRS = STRAIGHT
class Bishop(Slider): DIRS = DIAGONAL
class Queen(Slider): DIRS = STRAIGHT + DIAGONAL
class King(Stepper): STEPS = STRAIGHT + DIAGONAL
class Knight(Stepper):
    STEPS = ((1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1))


class Pawn(Piece):
    def moves(self, board, pos):
        step, start_row = self.color.value, 1 if self.color is Color.WHITE else 6
        one = (pos[0] + step, pos[1])
        if board.inside(one) and board[one] is None:
            yield one
            two = (pos[0] + 2 * step, pos[1])
            if pos[0] == start_row and board[two] is None:
                yield two
        for t in self.attacks(board, pos):
            if board[t] is not None and board[t].color is not self.color:
                yield t

    def attacks(self, board, pos):  # diagonals, whether or not occupied
        for dc in (-1, 1):
            t = (pos[0] + self.color.value, pos[1] + dc)
            if board.inside(t):
                yield t


class Board:
    def __init__(self):
        self.grid: list[list[Piece | None]] = [[None] * 8 for _ in range(8)]

    def __getitem__(self, pos: Pos) -> Piece | None:
        return self.grid[pos[0]][pos[1]]

    def __setitem__(self, pos: Pos, piece: Piece | None) -> None:
        self.grid[pos[0]][pos[1]] = piece

    @staticmethod
    def inside(pos: Pos) -> bool:
        return 0 <= pos[0] < 8 and 0 <= pos[1] < 8

    def pieces(self, color: Color) -> list[tuple[Pos, Piece]]:
        return [((r, c), p) for r, row in enumerate(self.grid)
                for c, p in enumerate(row) if p is not None and p.color is color]

    def king(self, color: Color) -> Pos:
        return next(pos for pos, p in self.pieces(color) if isinstance(p, King))

    def attacked(self, target: Pos, by: Color) -> bool:
        return any(target in p.attacks(self, pos) for pos, p in self.pieces(by))

    @classmethod
    def standard(cls) -> "Board":
        b = cls()
        for c, kind in enumerate([Rook, Knight, Bishop, Queen, King, Bishop, Knight, Rook]):
            b[0, c], b[7, c] = kind(Color.WHITE), kind(Color.BLACK)
            b[1, c], b[6, c] = Pawn(Color.WHITE), Pawn(Color.BLACK)
        return b

    @classmethod
    def from_dict(cls, layout: dict[str, Piece]) -> "Board":
        b = cls()
        for name, piece in layout.items():
            b[square(name)] = piece
        return b


@dataclass
class Move:
    src: Pos
    dst: Pos
    piece: Piece | None = None
    captured: Piece | None = None

    def execute(self, board: Board) -> None:
        self.piece, self.captured = board[self.src], board[self.dst]
        board[self.dst], board[self.src] = self.piece, None
        last_row = 7 if self.piece.color is Color.WHITE else 0
        if isinstance(self.piece, Pawn) and self.dst[0] == last_row:
            board[self.dst] = Queen(self.piece.color)  # auto-queen

    def undo(self, board: Board) -> None:
        board[self.src], board[self.dst] = self.piece, self.captured


class Game:
    def __init__(self, board: Board | None = None, turn: Color = Color.WHITE):
        self.board, self.turn = board or Board.standard(), turn
        for color in Color:
            if sum(isinstance(p, King) for _, p in self.board.pieces(color)) != 1:
                raise ValueError(f"{color.name} needs exactly one king")
        self.history: list[Move] = []
        self.status = self._status()

    def in_check(self, color: Color) -> bool:
        return self.board.attacked(self.board.king(color), color.other)

    def legal_moves(self, color: Color) -> Iterator[Move]:
        for pos, piece in self.board.pieces(color):
            for dst in list(piece.moves(self.board, pos)):
                m = Move(pos, dst)
                m.execute(self.board)
                safe = not self.in_check(color)
                m.undo(self.board)  # board is restored before we yield
                if safe:
                    yield m

    def move(self, src: str, dst: str) -> Status:
        if self.status is not Status.ACTIVE:
            raise IllegalMove("game is over")
        s, d = square(src), square(dst)
        piece = self.board[s]
        if piece is None or piece.color is not self.turn:
            raise IllegalMove(f"no {self.turn.name} piece on {src}")
        if d not in piece.moves(self.board, s):
            raise IllegalMove(f"{src}-{dst} breaks movement rules")
        m = Move(s, d)
        m.execute(self.board)
        if self.in_check(self.turn):
            m.undo(self.board)
            raise IllegalMove(f"{src}-{dst} leaves the king in check")
        self.history.append(m)
        self.turn = self.turn.other
        self.status = self._status()
        return self.status

    def undo(self) -> None:
        if not self.history:
            raise IllegalMove("nothing to undo")
        self.history.pop().undo(self.board)
        self.turn, self.status = self.turn.other, Status.ACTIVE

    def _status(self) -> Status:
        if next(self.legal_moves(self.turn), None):
            return Status.ACTIVE
        return Status.CHECKMATE if self.in_check(self.turn) else Status.STALEMATE


if __name__ == "__main__":
    W, B = Color.WHITE, Color.BLACK

    def perft(g: Game, color: Color, depth: int) -> int:
        if depth == 0:
            return 1
        total = 0
        for m in list(g.legal_moves(color)):
            m.execute(g.board)
            total += perft(g, color.other, depth - 1)
            m.undo(g.board)
        return total

    g = Game()
    assert [perft(g, W, d) for d in (1, 2, 3)] == [20, 400, 8902]

    for src, dst in [("e2", "e5"), ("b1", "b3"), ("a1", "a3"), ("e7", "e5"), ("e2", "e9")]:
        try:
            g.move(src, dst)
            raise AssertionError((src, dst))
        except IllegalMove:
            pass

    for src, dst in [("f2", "f3"), ("e7", "e5"), ("g2", "g4")]:
        assert g.move(src, dst) is Status.ACTIVE
    assert g.move("d8", "h4") is Status.CHECKMATE and g.in_check(W)  # fool's mate
    g.undo()
    assert g.status is Status.ACTIVE and g.turn is B and isinstance(g.board[7, 3], Queen)

    # Pin and king safety: the bishop shields e1 from the rook; d1 is attacked.
    g = Game(Board.from_dict({"e1": King(W), "e2": Bishop(W), "e8": Rook(B),
                              "d8": Rook(B), "a8": King(B)}))
    for src, dst in [("e2", "d3"), ("e1", "d1")]:
        try:
            g.move(src, dst)
            raise AssertionError((src, dst))
        except IllegalMove as e:
            assert "check" in str(e)
    assert g.move("e1", "f1") is Status.ACTIVE

    # Promotion gives check; undo puts the pawn back.
    g = Game(Board.from_dict({"h1": King(W), "a7": Pawn(W), "h8": King(B)}))
    assert g.move("a7", "a8") is Status.ACTIVE and g.in_check(B)
    assert isinstance(g.board[square("a8")], Queen)
    g.undo()
    assert isinstance(g.board[square("a7")], Pawn) and g.board[square("a8")] is None

    stalemate = {"c7": King(W), "b6": Queen(W), "a8": King(B)}
    assert Game(Board.from_dict(stalemate), turn=B).status is Status.STALEMATE
    print("chess ok")
```

## Key flows

```text
Player   Game                       Piece               Move / Board
  | move("e2", "e4")                  |                      |
  |------>| own piece on e2?          |                      |
  |       | e4 in moves(board, e2)? ->|                      |
  |       |<---------- yes -----------|                      |
  |       | Move(e2, e4).execute ----------------------------->|
  |       | in_check(WHITE)?  board.attacked(king, BLACK) --->|
  |       |   yes: undo, raise IllegalMove                    |
  |       | history.append, turn = BLACK                      |
  |       | any legal move for BLACK?                         |
  |       |   none and in check -> CHECKMATE                  |
  |       |   none, not in check -> STALEMATE                 |
  |<------| status                                            |

legal_moves(color): for each piece, each pseudo-legal dst:
   execute -> in_check(color)? -> undo -> yield if safe
```

## Edge cases

- Moving a pinned piece or walking into check: both fail the same execute, test, undo rule.
- King capturing a defended piece: after the move the defender attacks that square, so it is rejected.
- Pawns: forward moves need empty squares, the double step needs both squares empty and the start row, diagonals only capture.
- Checkmate and stalemate both mean no legal moves; whether the king is in check decides which.
- Move after the game ends: rejected; undo reopens the game.
- Bad square names (`e9`, `z1`) and positions without exactly one king per side: rejected up front.

## Production follow-ups

**Two clients send moves at once. What keeps the game consistent?** Serialize per game: a lock per game, or optimistic concurrency where each move carries the ply number and the write is `UPDATE game ... WHERE id = :id AND ply = :n`. The loser gets a conflict and refetches. The clock update belongs in the same transaction as the move.

**How does a game survive a restart?** Store the move list in UCI form (`e2e4`), plus a FEN snapshot every N moves for fast loading. Loading replays moves through `Move.execute`, which rebuilds the board exactly.

**What if a move request is retried?** It carries `(game_id, ply, move)`. If that ply already holds the same move, return the current state; a different move at that ply is a conflict. A retry never plays twice.

**How do castling, en passant and under-promotion fit?** All three stay inside `Move` and two piece classes. Castling needs a `has_moved` flag on king and rook, empty squares between, and no attacked square on the king's path; `Move` moves both pieces and undoes both. En passant needs the previous move from `history`. Under-promotion adds a `promote_to` field to `Move`. A fairy-chess piece is one new `Slider` or `Stepper` subclass.

**What breaks at 10× or across machines?** Games are independent, so shard by `game_id` and push moves over WebSockets. Validating a move costs about a millisecond. Engine analysis is the CPU hog; run it on a separate worker pool fed by a queue so it can't slow down live games.

**What do you log and measure?** Move-validation latency, illegal-move attempts per client (a client bug or cheating), disconnects, time forfeits, game length. Log each move with game id, ply, move and clock times.

**Who owns the clock?** The server: it timestamps each move, subtracts measured lag within a cap, and applies the increment. Clients only display it.

**How do you add draw rules?** Threefold repetition counts position hashes (Zobrist hashing updates the hash per move). The 50-move rule is a counter reset by any pawn move or capture.

## Interview follow-ups

- **Why generate pseudo-legal moves and then filter?** One king-safety check covers pins, check evasion and king moves. Special-casing each is where bugs hide.
- **How do you detect check faster?** Look outward from the king's square along the eight rays and the knight jumps for an enemy piece, instead of generating every enemy move.
- **How do you know move generation is right?** Perft: count leaf positions at each depth and compare with published values (20, 400, 8,902 from the start). The demo runs depth 3.
- **How would an engine represent the board?** Bitboards: one 64-bit integer per piece type and color, with precomputed attack tables. Mailbox arrays like this one are fine for validating human moves.
