---
layout: section
section: lld
title: Low-Level Design
---
Most LLD rounds test the same few things: can you split a problem into classes with clear jobs, pick a pattern for a reason, keep the design open to change, and stay correct when two threads hit it at once. These notes go in that order. All code is Python.

| Phase | Topic | Suggested time | What you should be able to do |
|---|---|---|---|
| 1 | OOP foundations | Week 1 | Give each class one job, hide state behind behaviour, draw a class diagram |
| 2 | SOLID | Week 2 | Name the problem each principle prevents, and spot the violation in code |
| 3 | Design patterns | Weeks 3–4 | Pick Strategy, Factory, State or Observer from the problem's shape |
| 4 | Concurrency | Week 5 | Make a design thread-safe and explain why it can't deadlock |
| 5 | Core problems | Weeks 6–8 | Design and code 25+ systems in 45 minutes, then answer the production follow-ups |

Every case study ends with **production follow-ups**: the questions a senior interviewer asks once the basic design works. Thread safety, restarts, retries, extension, scale and what you would measure.
