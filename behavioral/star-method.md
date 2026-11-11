---
title: The STAR method
section: behavioral
order: 1
summary: Structure behavioral answers as Situation, Task, Action, Result, with timing, the "I vs we" rule and how to quantify outcomes.
---

## When to use it

Any question that starts with:

- "Tell me about a time…"
- "Give me an example of…"
- "Describe a situation where…"
- "How did you handle…"

The interviewer wants evidence from the past, not a hypothetical. STAR keeps you from rambling and makes sure you land on a result.

## The structure

| Part | What to say | Share of answer |
|---|---|---|
| **S**ituation | Context: team, product, constraint. One or two sentences. | ~10% |
| **T**ask | What *you* were responsible for, and why it mattered. | ~10% |
| **A**ction | What you did, step by step. Decisions, trade-offs, who you talked to. | ~60% |
| **R**esult | Outcome with numbers, plus what you learned. | ~20% |

Most people spend half the answer on Situation. The interviewer scores the Action.

## Timing

- Target **2 to 3 minutes** per story. Under 1 minute sounds shallow, over 4 loses the room.
- Situation + Task: **under 30 seconds** combined.
- Stop after the Result and let them ask follow-ups. Follow-ups are where they probe depth; leave room for them.

## The "I vs we" rule

- Use **"I"** for your actions: "I proposed", "I wrote the migration", "I pushed back".
- Use **"we"** only for genuinely shared outcomes, and say what your part was.
- If the interviewer can't tell what you personally did, the story scores zero, no matter how good the project was.

> **Interview tip:** If you catch yourself saying "we" three times in a row, add one sentence starting with "My part was…".

## Quantifying results

Numbers make a result real. Use what you actually measured:

- **Latency / time:** "239s → 0.9s", "15–25s → 1–2s".
- **Volume / counts:** "500+ queries → 5–10 per request".
- **Scope:** "3–4 modules built from scratch", "~10-year-old codebase".
- **Business effect:** fewer support tickets, client unblocked, release date met.

If you don't have a number, use a concrete before/after: "the page used to time out for large clients; after the change it loaded normally for all of them." Never invent precision you can't defend in a follow-up.

## Add a learning

End with one line on what you'd keep or change. It signals reflection and gives the interviewer a natural follow-up.

- "Since then I measure query counts in tests before shipping list endpoints."
- "Next time I'd raise the concern in the design review, not after the first build."

## Template

```text
S: At <company>, <product/team context>, <the problem or constraint>.
T: I was responsible for <your specific goal>.
A: First I <investigated/measured>. I found <insight>.
   I considered <option A> vs <option B> and chose <B> because <reason>.
   I <built/changed/communicated> <specific things>.
   When <obstacle>, I <how you handled it>.
R: <Metric before -> after>. <Business effect>.
   I learned <one line>.
```

## Pitfalls

- Long setup, thin action.
- No result, or a result with no number or concrete change.
- Hypotheticals ("I would…") instead of what you did.
- Blaming others in conflict or failure stories.
- Reusing the same story for every question. Prepare 6–8 (see [Story bank]({{ '/behavioral/story-bank/' | relative_url }})).
- Memorizing word-for-word. Memorize the bullet points and numbers, not sentences.

## Quick checklist

- Could someone repeat your action back in three bullets?
- Is there at least one number or concrete before/after?
- Did you say "I" for your decisions?
- Did you stop at 2–3 minutes?
