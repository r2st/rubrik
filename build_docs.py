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
    (ROOT / "README.md",                "index.html",        "Transcript Intelligence",                  "Overview"),
    (ROOT / "docs" / "APPROACH.md",     "approach.html",     "Approach · Transcript Intelligence",       "Approach"),
    (ROOT / "docs" / "ARCHITECTURE.md", "architecture.html", "Architecture · Transcript Intelligence",   "Architecture"),
]

# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------
CSS = """
:root {
  --fg: #1f2328;
  --fg-muted: #59636e;
  --fg-faint: #848d97;
  --bg: #ffffff;
  --bg-soft: #f6f8fa;
  --bg-code: #f6f8fa;
  --border: #d8dee4;
  --border-soft: #eaeef2;
  --link: #0969da;
  --link-hover: #0550ae;
  --accent: #2196f3;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
  --radius: 6px;
  --content-width: 920px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
  font-size: 16px;
  line-height: 1.65;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

/* --------------------------------------------------------------------- nav */
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
  font-size: 15px;
  letter-spacing: -0.01em;
  margin-right: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
.nav .brand-icon {
  width: 22px;
  height: 22px;
  border-radius: 5px;
}
.nav a {
  color: #adbac7;
  text-decoration: none;
  font-size: 13px;
  padding: 6px 12px;
  border-radius: 6px;
  transition: background 0.12s, color 0.12s;
  font-weight: 500;
}
.nav a:hover { background: #21262d; color: #f0f6fc; }
.nav a.active { color: #f0f6fc; background: #21262d; }

/* ----------------------------------------------------------------- layout */
.layout {
  max-width: 1280px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px;
  gap: 56px;
  padding: 48px 32px 96px;
}
main {
  min-width: 0;
}
.toc {
  position: sticky;
  top: 88px;
  align-self: start;
  font-size: 13px;
  line-height: 1.55;
  max-height: calc(100vh - 120px);
  overflow-y: auto;
  padding-right: 4px;
}
.toc-title {
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 11px;
  font-weight: 700;
  color: var(--fg-faint);
  margin: 0 0 8px;
}
.toc ul {
  list-style: none;
  padding: 0;
  margin: 0;
  border-left: 1px solid var(--border-soft);
}
.toc li { margin: 0; }
.toc a {
  display: block;
  padding: 4px 0 4px 12px;
  margin-left: -1px;
  border-left: 2px solid transparent;
  color: var(--fg-muted);
  text-decoration: none;
  transition: color 0.1s, border-color 0.1s;
}
.toc a:hover { color: var(--fg); }
.toc a.h3 { padding-left: 24px; font-size: 12.5px; }
.toc a.active {
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 600;
}

/* ---------------------------------------------------------------- content */
main h1, main h2, main h3, main h4 {
  line-height: 1.25;
  letter-spacing: -0.01em;
  margin: 32px 0 12px;
  scroll-margin-top: 80px;
  position: relative;
}
main h1 {
  font-size: 32px;
  font-weight: 700;
  margin-top: 0;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-soft);
}
main h2 {
  font-size: 22px;
  font-weight: 600;
  margin-top: 44px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-soft);
}
main h3 {
  font-size: 18px;
  font-weight: 600;
  margin-top: 28px;
}
main h4 {
  font-size: 15px;
  font-weight: 600;
  color: var(--fg-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* Hover anchor link (GitHub-style). H1 has no anchor — it's the page title. */
main h1 .anchor { display: none; }
main h2 .anchor, main h3 .anchor, main h4 .anchor {
  position: absolute;
  left: -24px;
  text-decoration: none;
  color: var(--fg-faint);
  opacity: 0;
  transition: opacity 0.15s;
  font-weight: 400;
  font-size: 0.85em;
}
main h2:hover .anchor,
main h3:hover .anchor,
main h4:hover .anchor { opacity: 1; }

main p, main ul, main ol { margin: 12px 0; }
main ul, main ol { padding-left: 26px; }
main li { margin: 4px 0; }
main li > p { margin: 4px 0; }

main a { color: var(--link); text-decoration: none; }
main a:hover { color: var(--link-hover); text-decoration: underline; }
main a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }

/* Strong / em */
main strong { font-weight: 600; color: var(--fg); }

/* ------------------------------------------------------------------ code */
main code {
  background: var(--bg-soft);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 0.88em;
  color: #24292f;
  border: 1px solid var(--border-soft);
}
main pre {
  background: var(--bg-soft);
  padding: 16px 18px;
  border-radius: var(--radius);
  overflow-x: auto;
  border: 1px solid var(--border);
  font-size: 13.5px;
  line-height: 1.5;
  margin: 16px 0;
}
main pre code {
  background: none;
  padding: 0;
  border: none;
  font-size: 1em;
}

/* ----------------------------------------------------------------- tables */
main table {
  border-collapse: collapse;
  margin: 16px 0;
  width: 100%;
  font-size: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}
main th, main td {
  padding: 10px 14px;
  text-align: left;
  border-bottom: 1px solid var(--border-soft);
}
main th {
  background: var(--bg-soft);
  font-weight: 600;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--fg-muted);
  border-bottom: 1px solid var(--border);
}
main td:first-child { font-weight: 500; color: var(--fg); }
main tr:last-child td { border-bottom: none; }
main tr:hover td { background: #fafbfc; }

/* --------------------------------------------------------- blockquotes */
main blockquote {
  border-left: 3px solid var(--accent);
  padding: 8px 18px;
  margin: 16px 0;
  background: var(--bg-soft);
  color: var(--fg-muted);
  border-radius: 0 var(--radius) var(--radius) 0;
}
main blockquote p:first-child { margin-top: 0; }
main blockquote p:last-child { margin-bottom: 0; }

/* --------------------------------------------------------------- mermaid */
main .mermaid {
  background: var(--bg-soft);
  padding: 20px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 20px 0;
  text-align: center;
  overflow-x: auto;
}
main .mermaid svg { max-width: 100%; height: auto; }

/* ---------------------------------------------------------- horizontal rule */
main hr {
  border: 0;
  border-top: 1px solid var(--border-soft);
  margin: 36px 0;
}

main img { max-width: 100%; }

/* ------------------------------------------------------------------ footer */
footer {
  margin-top: 56px;
  padding-top: 18px;
  border-top: 1px solid var(--border-soft);
  font-size: 13px;
  color: var(--fg-faint);
  text-align: center;
}
footer code {
  font-size: 0.95em;
  background: var(--bg-soft);
  padding: 1px 5px;
  border-radius: 3px;
}

/* --------------------------------------------------------------- responsive */
@media (max-width: 1080px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }
  .toc { display: none; }
}
@media (max-width: 720px) {
  .nav { padding: 12px 18px; gap: 12px; }
  .nav a { padding: 5px 8px; font-size: 12.5px; }
  .layout { padding: 28px 18px 64px; }
  main h1 { font-size: 26px; }
  main h2 { font-size: 19px; }
  main table { display: block; overflow-x: auto; }
}

/* --------------------------------------------------------------- print */
@media print {
  .nav, .toc, footer { display: none; }
  .layout { display: block; padding: 0; max-width: 100%; }
  main h2 { page-break-after: avoid; }
  main pre, main table, main .mermaid { page-break-inside: avoid; }
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
  <span class="brand">
    <img src="favicon.svg" alt="" class="brand-icon">
    Transcript Intelligence
  </span>
  {nav_links}
</nav>
<div class="layout">
<main>
{body}
<footer>
  Generated from <code>{source}</code>
</footer>
</main>
{toc_html}
</div>
<script
  src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
  integrity="sha384-WmdflGW9aGfoBdHc4rRyWzYuAjEmDwMdGdiPNacbwfGKxBW/SO6guzuQ76qjnSlr"
  crossorigin="anonymous"
  referrerpolicy="no-referrer"></script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'default',
    securityLevel: 'loose',
    flowchart: {{ htmlLabels: true, curve: 'basis' }}
  }});

  // Active section highlighting in the TOC
  (function() {{
    var tocLinks = Array.from(document.querySelectorAll('.toc a'));
    if (!tocLinks.length) return;
    var headings = tocLinks
      .map(function(a) {{ return document.getElementById(a.getAttribute('href').slice(1)); }})
      .filter(Boolean);
    function update() {{
      var pos = window.scrollY + 120;
      var current = headings[0];
      for (var i = 0; i < headings.length; i++) {{
        if (headings[i].offsetTop <= pos) current = headings[i];
      }}
      tocLinks.forEach(function(a) {{ a.classList.remove('active'); }});
      var active = tocLinks.find(function(a) {{
        return a.getAttribute('href') === '#' + current.id;
      }});
      if (active) active.classList.add('active');
    }}
    window.addEventListener('scroll', update, {{ passive: true }});
    update();
  }})();
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
        return f"\n\n<!--MERMAID_{idx}-->\n\n"

    md_text = MERMAID_BLOCK.sub(stash, md_text)

    html = markdown.markdown(
        md_text,
        extensions=["extra", "tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"permalink": "¶", "permalink_class": "anchor"}},
    )

    # restore mermaid blocks
    for i, code in enumerate(placeholders):
        html = html.replace(
            f"<p><!--MERMAID_{i}--></p>",
            f'<div class="mermaid">{code}</div>',
        )
        html = html.replace(
            f"<!--MERMAID_{i}-->",
            f'<div class="mermaid">{code}</div>',
        )

    return html


HEADING_RE = re.compile(r'<h([23])\s+id="([^"]+)">(.*?)</h\1>', re.DOTALL)


def extract_toc(html: str) -> str:
    """Build a sidebar TOC from h2/h3 headings."""
    items = []
    for m in HEADING_RE.finditer(html):
        level, anchor, text = m.group(1), m.group(2), m.group(3)
        # strip the trailing permalink anchor that toc extension adds
        text = re.sub(r'<a class="anchor"[^>]*>.*?</a>\s*$', "", text)
        # drop other tags inside the heading (e.g., <code>)
        text = re.sub(r"<[^>]+>", "", text).strip()
        cls = "h3" if level == "3" else "h2"
        items.append(f'<li><a href="#{anchor}" class="{cls}">{text}</a></li>')

    if not items:
        return ""
    return (
        '<aside class="toc">'
        '<p class="toc-title">On this page</p>'
        f'<ul>{"".join(items)}</ul>'
        "</aside>"
    )


def fix_internal_links(html: str) -> str:
    """Rewrite cross-document markdown links to their HTML counterparts."""
    replacements = {
        'href="docs/ARCHITECTURE.md"': 'href="architecture.html"',
        'href="docs/APPROACH.md"': 'href="approach.html"',
        'href="../README.md"': 'href="index.html"',
        'href="ARCHITECTURE.md"': 'href="architecture.html"',
        'href="APPROACH.md"': 'href="approach.html"',
        'href="LICENSE"': 'href="https://github.com/"',
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
    if FAVICON_SRC.exists():
        shutil.copy2(FAVICON_SRC, HTML_DIR / "favicon.svg")
        print(f"  ✓ favicon.svg  ←  {FAVICON_SRC.relative_to(ROOT)}")

    for src, out, title, _ in PAGES:
        md = src.read_text()
        html_body = render_markdown(md)
        html_body = fix_internal_links(html_body)
        toc_html = extract_toc(html_body)
        page = TEMPLATE.format(
            title=title,
            css=CSS,
            nav_links=build_nav(out),
            body=html_body,
            toc_html=toc_html,
            source=src.relative_to(ROOT),
        )
        (HTML_DIR / out).write_text(page)
        print(f"  ✓ {out}  ←  {src.relative_to(ROOT)}")
    print(f"\nOpen: file://{HTML_DIR / 'index.html'}")


if __name__ == "__main__":
    build()
