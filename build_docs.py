"""Build self-contained HTML documentation from the markdown sources.

Converts README.md, docs/ARCHITECTURE.md, docs/APPROACH.md into a small static
site under docs/html/. Each page is standalone (open the .html file directly
in a browser — no server needed). Mermaid diagrams render via CDN. All other
styling is inlined.

Run:  python build_docs.py
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent
HTML_DIR = ROOT / "docs" / "html"
HTML_DIR.mkdir(parents=True, exist_ok=True)

# Source of truth for the favicon — single SVG file shared between the
# FastAPI dashboard (web/static/) and the standalone HTML docs.
FAVICON_SRC = ROOT / "web" / "static" / "favicon.svg"

# Pages: (source_md_path, output_html_filename, page_title, nav_label)
PAGES = [
    (ROOT / "README.md",            "index.html",        "Transcript Intelligence",          "Home"),
    (ROOT / "docs" / "ARCHITECTURE.md", "architecture.html", "Architecture · Transcript Intelligence", "Architecture"),
    (ROOT / "docs" / "APPROACH.md",  "approach.html",     "Approach · Transcript Intelligence",     "Approach"),
]

# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------
CSS = """
:root {
  --fg: #1f2328;
  --fg-muted: #59636e;
  --bg: #ffffff;
  --bg-soft: #f6f8fa;
  --bg-code: #f6f8fa;
  --border: #d0d7de;
  --link: #0969da;
  --accent: #2196f3;
  --warn: #fff8c5;
  --warn-border: #d4a72c;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 16px;
  line-height: 1.6;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
}
.nav {
  background: #0d1117;
  color: #f0f6fc;
  padding: 14px 32px;
  display: flex;
  align-items: center;
  gap: 28px;
  position: sticky;
  top: 0;
  z-index: 10;
  border-bottom: 1px solid #21262d;
}
.nav .brand {
  font-weight: 700;
  font-size: 16px;
  margin-right: auto;
  letter-spacing: 0.2px;
}
.nav .brand::before { content: "📞 "; }
.nav a {
  color: #adbac7;
  text-decoration: none;
  font-size: 14px;
  padding: 6px 10px;
  border-radius: 6px;
  transition: background 0.15s, color 0.15s;
}
.nav a:hover { background: #21262d; color: #f0f6fc; }
.nav a.active { color: #f0f6fc; background: #21262d; }
main {
  max-width: 920px;
  margin: 0 auto;
  padding: 40px 32px 80px;
}
h1, h2, h3, h4 { line-height: 1.25; margin-top: 28px; margin-bottom: 12px; }
h1 { font-size: 32px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
h2 { font-size: 24px; padding-bottom: 6px; border-bottom: 1px solid var(--border); margin-top: 36px; }
h3 { font-size: 20px; }
h4 { font-size: 17px; color: var(--fg-muted); }
p, ul, ol { margin: 12px 0; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
code {
  background: var(--bg-code);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.92em;
}
pre {
  background: var(--bg-code);
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid var(--border);
  font-size: 0.92em;
}
pre code { background: none; padding: 0; }
table {
  border-collapse: collapse;
  margin: 16px 0;
  width: 100%;
  font-size: 0.94em;
}
th, td {
  border: 1px solid var(--border);
  padding: 8px 12px;
  text-align: left;
}
th { background: var(--bg-soft); font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote {
  border-left: 4px solid var(--border);
  padding: 6px 16px;
  color: var(--fg-muted);
  margin: 16px 0;
  background: var(--bg-soft);
}
hr { border: 0; border-top: 1px solid var(--border); margin: 32px 0; }
img { max-width: 100%; }
.mermaid {
  background: var(--bg-soft);
  padding: 18px;
  border-radius: 8px;
  border: 1px solid var(--border);
  margin: 18px 0;
  text-align: center;
  overflow-x: auto;
}
.toc-card {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 20px;
  margin: 20px 0;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 0.85em;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  margin-right: 4px;
}
footer {
  margin-top: 48px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--fg-muted);
  text-align: center;
}
@media (max-width: 720px) {
  .nav { padding: 12px 16px; gap: 12px; }
  main { padding: 24px 16px 56px; }
  h1 { font-size: 26px; }
  h2 { font-size: 20px; }
}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<style>{css}</style>
</head>
<body>
<nav class="nav">
  <span class="brand">Transcript Intelligence</span>
  {nav_links}
</nav>
<main>
{body}
<footer>
  Generated from <code>{source}</code> · <a href="https://github.com/">view on GitHub</a>
</footer>
</main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'default',
    securityLevel: 'loose',
    flowchart: {{ htmlLabels: true, curve: 'basis' }}
  }});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# MD → HTML conversion
# ---------------------------------------------------------------------------
MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)


def render_markdown(md_text: str) -> str:
    """Convert markdown to HTML, preserving mermaid blocks as <div> elements."""
    placeholders: list[str] = []

    def stash(match: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(match.group(1))
        # use a placeholder unlikely to be touched by markdown parser
        return f"\n\n<!--MERMAID_{idx}-->\n\n"

    md_text = MERMAID_BLOCK.sub(stash, md_text)

    html = markdown.markdown(
        md_text,
        extensions=["extra", "tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"permalink": False}},
    )

    # restore mermaid blocks
    for i, code in enumerate(placeholders):
        html = html.replace(
            f"<p><!--MERMAID_{i}--></p>",
            f'<div class="mermaid">{code}</div>',
        )
        # also handle case where comment didn't get wrapped in <p>
        html = html.replace(
            f"<!--MERMAID_{i}-->",
            f'<div class="mermaid">{code}</div>',
        )

    return html


def fix_internal_links(html: str) -> str:
    """Rewrite cross-document markdown links to their HTML counterparts."""
    replacements = {
        'href="docs/ARCHITECTURE.md"': 'href="architecture.html"',
        'href="docs/APPROACH.md"': 'href="approach.html"',
        'href="../README.md"': 'href="index.html"',
        'href="ARCHITECTURE.md"': 'href="architecture.html"',
        'href="APPROACH.md"': 'href="approach.html"',
        'href="LICENSE"': 'href="https://github.com/"',  # repo-relative; keep generic
        'href="tests/"': 'href="https://github.com/"',
        'href="validate.py"': 'href="https://github.com/"',
        'href="requirements.txt"': 'href="https://github.com/"',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    return html


def build_nav(active: str) -> str:
    items = []
    for _, fname, _, label in PAGES:
        cls = ' class="active"' if fname == active else ""
        items.append(f'<a href="{fname}"{cls}>{label}</a>')
    return "\n  ".join(items)


def build() -> None:
    print(f"Building HTML docs → {HTML_DIR}/")

    # Copy favicon next to the HTML so the docs are self-contained
    # (works equally over file:// or any HTTP server).
    if FAVICON_SRC.exists():
        shutil.copy2(FAVICON_SRC, HTML_DIR / "favicon.svg")
        print(f"  ✓ favicon.svg  ←  {FAVICON_SRC.relative_to(ROOT)}")

    for src, out, title, _ in PAGES:
        md = src.read_text()
        html_body = render_markdown(md)
        html_body = fix_internal_links(html_body)
        page = TEMPLATE.format(
            title=title,
            css=CSS,
            nav_links=build_nav(out),
            body=html_body,
            source=src.relative_to(ROOT),
        )
        (HTML_DIR / out).write_text(page)
        print(f"  ✓ {out}  ←  {src.relative_to(ROOT)}")
    print(f"\nOpen: file://{HTML_DIR / 'index.html'}")


if __name__ == "__main__":
    build()
