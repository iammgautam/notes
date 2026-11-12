# Notes style guide

This repo is a Jekyll site (GitHub Pages, Jekyll 3.9, kramdown GFM, Rouge).
Every note is one Markdown file. The layout, sidebar, prev/next and section
index are generated. You only write front matter + Markdown body.

## File location and naming

```
<section>/<slug>.md        e.g. dsa/binary-search.md, postgresql/explain-analyze.md
```

- `slug`: lowercase, hyphenated, no numbers or dates.
- Section ids: `dsa`, `lld`, `hld`, `postgresql`, `python-django`, `llm`,
  `networking`, `os-concurrency`, `production`, `behavioral`.
- Do not touch `<section>/index.md`, `_layouts/`, `_includes/`, `_data/`, `assets/`.

## Front matter (required, exactly these keys)

```yaml
---
title: Binary search
section: dsa
order: 3
summary: Find a boundary in a monotonic space in O(log n). Templates and the off-by-one rules.
---
```

- `title`: sentence case, 2–6 words. No "Notes on…", no colon subtitles.
- `order`: integer, controls position in the section (1 = first). Read order should go basics → advanced → case studies.
- `summary`: one sentence, ≤ 160 characters, says what you will know after reading. Shown on the section index.
- Quote the value if it contains `: ` or starts with a special character.
- Optional `phase`: groups notes under a heading in the section index and sidebar (used by `lld`). Notes with the same phase must have consecutive `order` values.

## Body structure

- Do **not** repeat the title as `# H1`. The layout renders the title. Start with a short intro paragraph or go straight to the first `##`.
- Use `##` for main sections, `###` for subsections. Never skip levels.
- Headings are sentence case.

## Two writing modes

### Mode A: Interview core (`dsa`, `lld`, `hld`, `behavioral`), short and direct

Direct to the point. Something to re-read in 5 minutes before an interview.

- Lead with **When to use / how to recognise it** (the signal in the problem statement).
- Then the **core idea** in 2–4 sentences, then a **template** (code), then **complexity**, then **pitfalls**, then **classic problems** (names only, a bullet list, optional one-line hint each).
- Bullets and tables over paragraphs. No history, no fluff, no motivational intros.
- DSA code in **Python**. Clean, idiomatic, commented only where non-obvious.
- LLD: show class design in Python (dataclasses / ABCs) or a compact class list, plus which patterns apply and why. Case studies: requirements → entities → class sketch → key flows → extensions.
- HLD case studies: requirements (functional / non-functional) → estimates → API → data model → high-level design (ASCII diagram) → deep dives → bottlenecks and trade-offs. Building-block notes: what it is → when to use → trade-offs table → gotchas.
- Typical length: 300–900 words.

### Mode B: Engineering deep dives (`postgresql`, `python-django`, `llm`, `networking`, `os-concurrency`, `production`), engineer's POV

Write like a senior backend engineer explaining what actually happens in production: the **what**, the **how it works underneath**, the **how to use it**, and **what goes wrong**.

Recommended shape (adapt, don't force):
1. One-paragraph framing: why this matters in a real system.
2. `## How it works`: the mechanism, at the level that changes decisions.
3. `## In practice`: concrete code / SQL / commands / config, with real-looking examples (multi-tenant SaaS, background jobs, APIs, RAG pipelines).
4. `## Pitfalls`: what bites people, how you notice it (symptoms, metrics, logs), how you fix it.
5. `## Checklist` or `## Rules of thumb`: short bullets to apply tomorrow.
6. Optional `## Interview angle`: 2–4 likely questions with crisp answers.

- Be specific: name the setting, the function, the flag, the metric. Prefer numbers with units and orders of magnitude.
- The author's context (use it to choose examples, don't invent personal anecdotes): Python/Django/DRF, PostgreSQL (JSONB, full-text search, pgvector, heavy indexing, rollups), multi-tenant SaaS microservices, Postgres-native task queues, Celery, Vue.js, Sentry, Docker, RAG/agents with multi-provider LLM routing.
- Never claim personal experiences ("At my company we…"). Write in second person or neutral voice.
- Typical length: 700–1600 words.

## Markdown conventions

- Fenced code blocks **always** with a language: `python`, `sql`, `bash`, `text`, `yaml`, `json`, `http`, `javascript`.
- ASCII diagrams go in ```` ```text ```` blocks. Keep under ~76 characters wide.
- Callouts are blockquotes starting with a bold label, one of:
  `> **Rule of thumb:** …`, `> **Gotcha:** …`, `> **Interview tip:** …`, `> **In production:** …`
- Tables: GFM pipe tables. Keep cells short.
- Complexity: write `O(n log n)` in inline code.
- Links between notes: `[Binary search]({{ '/dsa/binary-search/' | relative_url }})` (this is the only Liquid you may use).

## Liquid safety (IMPORTANT, breaks the build otherwise)

Jekyll runs Liquid over every file **including code blocks**. Any `{{`, `}}`, `{%` or `%}`
in content (Django/Jinja templates, GitHub Actions `${{ }}`, Go templates, some JS) must be wrapped:

```text
{% raw %}
...code containing {{ or {% ...
{% endraw %}
```

Put `{% raw %}` on the line **before** the opening fence and `{% endraw %}` on the line **after** the closing fence.
Python f-strings with doubled braces (`f"{{x}}"`) and dict/set literals like `{{'a': 1}}` also need this.

## Voice

- Plain, direct, confident. Active voice. Second person ("you") where natural.
- No filler: no "In this article", "Let's dive in", "It's important to note that".
- Sentence case everywhere except proper nouns (PostgreSQL, Django, Redis, vLLM).
- American spelling.
