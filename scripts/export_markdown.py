"""Write LLM-friendly Markdown next to every built note and blog post.

Run after `jekyll build`:  python3 scripts/export_markdown.py _site

For each note (<section>/<slug>.md) and post (_posts/YYYY-MM-DD-<slug>.md) it writes
_site/<url>/index.md, and it writes _site/llms.txt (index) and _site/llms-full.txt (all text).
"""
import re
import sys
from pathlib import Path

SITE_URL = "https://iammgautam.github.io/notes"
ROOT = Path(__file__).resolve().parent.parent

LINK = re.compile(r"\{\{\s*'([^']*)'\s*\|\s*relative_url\s*\}\}")
RAW_TAG = re.compile(r"\{%-?\s*(raw|endraw)\s*-?%\}\n?")


def split_front_matter(text):
    if not text.startswith("---\n"):
        return {}, text
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def clean(body):
    body = LINK.sub(lambda m: SITE_URL + m.group(1), body)
    return RAW_TAG.sub("", body).strip() + "\n"


def sections():
    """Section ids and titles in home-page order, read from _data/sections.yml."""
    out, current = [], None
    for line in (ROOT / "_data/sections.yml").read_text().splitlines():
        if line.startswith("- id:"):
            current = {"id": line.split(":", 1)[1].strip()}
            out.append(current)
        elif current is not None and line.strip().startswith("title:"):
            current["title"] = line.split(":", 1)[1].strip()
    return out


def collect():
    docs = []
    for sec in sections():
        for path in sorted((ROOT / sec["id"]).glob("*.md")):
            if path.name == "index.md":
                continue
            meta, body = split_front_matter(path.read_text())
            docs.append({
                "group": sec["title"], "url": f"/{sec['id']}/{path.stem}/",
                "order": int(meta.get("order", 999)), "meta": meta, "body": body,
            })
    for path in sorted((ROOT / "_posts").glob("*.md"), reverse=True):
        meta, body = split_front_matter(path.read_text())
        slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
        docs.append({"group": "Blog", "url": f"/blog/{slug}/", "order": 0,
                     "meta": meta, "body": body})
    return docs


def render(doc):
    m = doc["meta"]
    parts = [f"# {m.get('title', '')}\n"]
    if m.get("summary"):
        parts.append(f"> {m['summary']}\n")
    parts.append(f"Source: {SITE_URL}{doc['url']}\n")
    parts.append(clean(doc["body"]))
    return "\n".join(parts)


def main(site_dir):
    site = Path(site_dir)
    docs = collect()
    missing = [d["url"] for d in docs if not (site / d["url"].strip("/") / "index.html").exists()]
    if missing:
        sys.exit(f"built pages missing for: {missing}")

    full, index = [], ["# Notes · Mithilesh Gautam", "",
                       "> Working notes on DSA, system design, databases and LLM systems, "
                       "plus engineering blog posts. Every page is also available as Markdown "
                       "at <page-url>index.md.", ""]
    group = None
    ordered = sorted([d for d in docs if d["group"] != "Blog"],
                     key=lambda d: ([s["title"] for s in sections()].index(d["group"]), d["order"]))
    ordered += [d for d in docs if d["group"] == "Blog"]

    for doc in ordered:
        text = render(doc)
        (site / doc["url"].strip("/") / "index.md").write_text(text)
        full.append(text)
        if doc["group"] != group:
            group = doc["group"]
            index += ["", f"## {group}", ""]
        summary = doc["meta"].get("summary", "")
        index.append(f"- [{doc['meta'].get('title')}]({SITE_URL}{doc['url']}index.md)"
                     + (f": {summary}" if summary else ""))

    index += ["", "## Optional", "",
              f"- [All pages in one file]({SITE_URL}/llms-full.txt)"]
    (site / "llms.txt").write_text("\n".join(index) + "\n")
    (site / "llms-full.txt").write_text("\n\n---\n\n".join(full))
    print(f"wrote {len(ordered)} markdown pages, llms.txt, llms-full.txt")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_site")
