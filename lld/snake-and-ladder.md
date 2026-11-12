---
title: Design Snake and Ladder
section: lld
order: 29
phase: "Phase 5 · Intermediate"
summary: Snake and Ladder with a validated jumps map, dice as a strategy, a deque turn loop for many players, and configurable rules.
---

## Requirements

Clarifying questions to ask first:

- Board size? Who defines snakes and ladders? (Configurable, default 100 cells.)
- How many players and dice? (Two or more players; one die by default, pluggable.)
- What happens when a roll overshoots the last cell? (Configurable: stay, or bounce back.)
- Does a six give another turn? Does the game stop at the first winner? (Configurable; first winner ends it.)

**Functional**

- Board of `size` cells with snakes (move down) and ladders (move up).
- Players start off the board at 0 and take turns in a fixed order.
- A turn: roll, move, apply overshoot rule, follow a snake or ladder, check for a win.
- First player to land exactly on the last cell wins.
- Every move is logged.

**Non-functional**

- Reject broken boards at construction, not mid-game.
- Deterministic tests and replays: dice are injectable.

**Out of scope:** UI, networking, betting, persistence.

## Entities and relationships

```text
+------------------------------+ uses +--------------------------+
| Game                         |----->| Dice (ABC)               |
| positions: dict[name, int]   |      | roll() -> int            |
| turns: deque[name]           |      +--------------------------+
| winner, log: list[Move]      |        ^ is-a: RandomDice,
| take_turn() -> Move          |        |       FixedDice
| play(max_turns) -> name      |
+------------------------------+ has  +--------------------------+
     | has 1         | has 0..*  ---->| Rules (frozen)           |
     v               v                | overshoot: STAY|BOUNCE   |
+------------------+ +-------------+  | extra_turn_on: int|None  |
| Board            | | Move        |  +--------------------------+
| size             | | player,roll |
| jumps: {from:to} | | start, end  |
| land(pos) -> int | +-------------+
+------------------+
```

Snakes and ladders behave the same way: land on `from`, move to `to`. One dictionary holds both. Validation tells them apart by direction.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `Dice` | Fair die, two dice, scripted rolls for tests and replays; the game never knows which |
| Config object | `Rules` (frozen) | Variants without subclassing `Game`; frozen so rules can't change mid-game |
| Data over classes | `Board.jumps` | `O(1)` lookup, no `Snake`/`Ladder` hierarchy for behavior that is identical |
| Queue | `deque` of players | `rotate(-1)` moves to the next player in `O(1)`; easy to drop a finished player |

## Code

```python
import random
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import Enum


class Overshoot(Enum):
    STAY = "stay"      # roll past the end: don't move
    BOUNCE = "bounce"  # walk back by the excess


@dataclass(frozen=True)
class Rules:
    overshoot: Overshoot = Overshoot.STAY
    extra_turn_on: int | None = 6


class Board:
    def __init__(self, size: int = 100, snakes: dict[int, int] | None = None,
                 ladders: dict[int, int] | None = None):
        snakes, ladders = snakes or {}, ladders or {}
        if snakes.keys() & ladders.keys():
            raise ValueError("a snake and a ladder start on the same cell")
        self.size, self.jumps = size, {**snakes, **ladders}
        for start, end in self.jumps.items():
            if not (1 <= start < size and 1 <= end <= size):
                raise ValueError(f"jump {start}->{end} is off the board")
            if end in self.jumps:
                raise ValueError(f"jump {start}->{end} chains into another jump")
        if any(end >= start for start, end in snakes.items()):
            raise ValueError("snakes must go down")
        if any(end <= start for start, end in ladders.items()):
            raise ValueError("ladders must go up")

    def land(self, pos: int) -> int:
        return self.jumps.get(pos, pos)


class Dice(ABC):
    @abstractmethod
    def roll(self) -> int: ...


class RandomDice(Dice):
    def __init__(self, count: int = 1, faces: int = 6, rng: random.Random | None = None):
        self.count, self.faces = count, faces
        self.rng = rng or random.SystemRandom()

    def roll(self):
        return sum(self.rng.randint(1, self.faces) for _ in range(self.count))


class FixedDice(Dice):
    """Scripted rolls: tests, and replaying a stored game."""

    def __init__(self, rolls):
        self.rolls = iter(rolls)

    def roll(self):
        return next(self.rolls)


@dataclass(frozen=True)
class Move:
    player: str
    roll: int
    start: int
    end: int


class Game:
    def __init__(self, board: Board, players: list[str], dice: Dice, rules: Rules = Rules()):
        if len(players) < 2 or len(set(players)) != len(players):
            raise ValueError("need two or more distinct players")
        self.board, self.dice, self.rules = board, dice, rules
        self.positions = {p: 0 for p in players}
        self.turns = deque(players)
        self.winner: str | None = None
        self.log: list[Move] = []

    def take_turn(self) -> Move:
        if self.winner:
            raise RuntimeError("game over")
        player, size = self.turns[0], self.board.size
        roll = self.dice.roll()
        start = self.positions[player]
        target = start + roll
        if target > size:
            target = start if self.rules.overshoot is Overshoot.STAY else 2 * size - target
        end = self.board.land(target)
        self.positions[player] = end
        move = Move(player, roll, start, end)
        self.log.append(move)
        if end == size:
            self.winner = player
        elif roll != self.rules.extra_turn_on:
            self.turns.rotate(-1)
        return move

    def play(self, max_turns: int = 10_000) -> str:
        while not self.winner:
            if len(self.log) >= max_turns:
                raise RuntimeError("no winner: check the dice and rules")
            self.take_turn()
        return self.winner


SNAKES = {17: 7, 54: 34, 62: 19, 64: 60, 87: 36, 93: 73, 95: 75, 99: 78}
LADDERS = {4: 14, 9: 31, 20: 38, 28: 84, 40: 59, 51: 67, 63: 81, 71: 91}

if __name__ == "__main__":
    board = Board(30, snakes={27: 5, 17: 4}, ladders={3: 22, 11: 26})
    g = Game(board, ["A", "B"], FixedDice([3, 6, 5, 5, 6, 4]))
    assert g.take_turn() == Move("A", 3, 0, 22)   # ladder
    assert g.take_turn() == Move("B", 6, 0, 6)    # six: B again
    assert g.take_turn() == Move("B", 5, 6, 26)   # ladder
    assert g.take_turn() == Move("A", 5, 22, 5)   # snake at 27
    assert g.take_turn() == Move("B", 6, 26, 26)  # overshoot: stay, still a six
    assert g.play() == "B" and g.positions["B"] == 30

    g = Game(Board(30), ["A", "B"], FixedDice([5]), Rules(Overshoot.BOUNCE, None))
    g.positions["A"] = 28
    assert g.take_turn().end == 27 and g.turns[0] == "B"  # 28+5=33 -> 27

    for bad in [dict(snakes={30: 2}), dict(ladders={5: 3}),
                dict(snakes={20: 5}, ladders={5: 12}), dict(snakes={8: 2}, ladders={8: 20})]:
        try:
            Board(30, **bad)
            raise AssertionError(bad)
        except ValueError:
            pass

    # Simulation: every game ends; a seeded game replays exactly from its roll log.
    board = Board(100, SNAKES, LADDERS)
    for seed in range(1000):
        g = Game(board, ["A", "B", "C"], RandomDice(rng=random.Random(seed)))
        winner = g.play()
        assert g.positions[winner] == 100
        replay = Game(board, ["A", "B", "C"], FixedDice(m.roll for m in g.log))
        assert replay.play() == winner and replay.log == g.log
    print("snake and ladder ok")
```

## Key flows

```text
Game                   Dice         Board            turns (deque)
 | take_turn()          |             |                   |
 | player = turns[0]    |             |                   |
 | roll() ------------->|             |                   |
 |<-------- 6 ----------|             |                   |
 | target = pos + 6                   |                   |
 | target > size? STAY or BOUNCE      |                   |
 | land(target) --------------------->| jumps.get(t, t)   |
 |<----------------- end -------------|                   |
 | end == size? winner = player       |                   |
 | roll == extra_turn_on? keep player |                   |
 |   else rotate(-1) ---------------------------------->  |
 | log.append(Move)                   |                   |
```

## Edge cases

- Overshoot: `STAY` leaves the player in place; `BOUNCE` walks back and may land on a snake.
- A six while stuck near the end still grants another turn here. Confirm the house rule.
- A ladder ending on a snake head (a chain) is rejected at build time. Allowing chains needs a loop with a cycle check.
- A jump starting on the last cell, or a snake and ladder sharing a start cell: rejected.
- Two players on one cell: allowed; there is no capture.
- Fewer than two players or duplicate names: `ValueError`.
- A broken dice strategy (always rolling past the end with `STAY`) never finishes: `max_turns` turns that into an error instead of a hang.
- With `BOUNCE`, two dice and a tiny board, `2 * size - target` can go below zero. Validate that the maximum roll is smaller than the board.

## Production follow-ups

**Two players press "roll" at the same moment. What stops a double turn?** Serialize each game: a per-game lock, or one asyncio task per game reading a queue of commands (an actor). The server accepts a roll only from `turns[0]` and only for the expected turn number. Games share nothing, so there is no global lock.

**How does a game survive a restart?** Persist the roll log, not the board state. Replaying the rolls through `FixedDice` rebuilds positions, turn order and winner exactly, which the demo checks for 1,000 games. Store `(game_id, turn_no, player, roll)` rows and replay on load.

**What if the client retries "roll"?** The request carries `(game_id, turn_no)`. If that turn exists, return the stored `Move`; never roll again. Re-rolling on retry would let a client fish for a better number by dropping responses.

**How do you add new cell types without editing `Game`?** "Skip a turn" or "roll again" cells outgrow the jumps map. Replace it with `effects: dict[int, CellEffect]`, where each effect applies itself to the game. New dice are new `Dice` classes; new variants are new `Rules` fields with defaults.

**What changes at 10× or across machines?** Each game is tiny and turn-based, a few requests per minute. Shard by `game_id`, keep game state in Redis or PostgreSQL, and broadcast moves over WebSockets. Expire idle games with a TTL so abandoned ones don't pile up.

**What do you measure?** Turns per game, abandonment rate, time per turn, and the roll distribution per server. A chi-square test on rolls catches a broken or tampered RNG. Log each move with game id, turn number, player and roll.

**Is the dice fair, and can players trust it?** Roll on the server with `random.SystemRandom` (OS entropy), never on the client. For real-money games, use commit-reveal: publish a hash of the server seed before the game and reveal the seed after, so players can verify every roll.

## Interview follow-ups

- **Why no `Snake` and `Ladder` classes?** Same behavior, different direction. A dictionary gives `O(1)` lookup, and validation enforces direction. Classes pay off once cells behave differently.
- **Keep playing for 2nd and 3rd place?** On reaching the end, append the player to `finished`, remove them from the deque, and loop while more than one player remains.
- **Three sixes in a row forfeits the turn?** Track consecutive sixes for the current player; on the third, restore the position from the start of that turn and rotate.
- **How do you test randomness?** Inject the dice: `FixedDice` for scripted cases, a seeded `random.Random` for reproducible simulations.
