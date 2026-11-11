# Notes

Working notes on DSA, LLD, HLD, PostgreSQL, Python & Django, LLM systems and more, plus a blog.
Published with GitHub Pages at **https://iammgautam.github.io/notes/**.

The design system (tokens, header, buttons) is shared with the portfolio: pages load
`https://iammgautam.github.io/style.css`, and `assets/notes.css` adds notes-only patterns.

## Write a note

1. Create `<section>/<slug>.md`, e.g. `postgresql/vacuum-tuning.md`.
2. Add front matter:

   ```yaml
   ---
   title: Vacuum tuning
   section: postgresql
   order: 18
   summary: One sentence on what you'll know after reading.
   ---
   ```

3. Write Markdown. Full conventions live in [STYLE.md](STYLE.md). The important one: wrap any
   code that contains `{{` or `{%` in `{% raw %}` / `{% endraw %}`.

Sections are listed in `_data/sections.yml`. Adding a section = one entry there plus
`<id>/index.md` with `layout: section` and `section: <id>`.

## Blog

- Drafts live in `_drafts/` and are **not published**.
- To publish, move a draft to `_posts/` and prefix the date: `_posts/2026-11-20-my-post.md`.

## Preview locally

```bash
gem install --user-install jekyll -v 3.9.5   # once
jekyll serve --drafts --baseurl /notes
```

Local preview has no portfolio `style.css`; pages render unstyled unless you serve both sites
together (copy the portfolio's `style.css` next to the built `notes/` folder).
