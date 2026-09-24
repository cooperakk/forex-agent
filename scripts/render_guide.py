#!/usr/bin/env python3
"""Render a Persian Markdown guide to one self-contained, right-to-left HTML page.

    python scripts/render_guide.py docs/INSTALL-FA.md START-HERE-FA.html

Used by scripts/package.sh so the release carries a guide that opens with a
double-click on any machine: no Markdown viewer, no internet, no web font.
Code blocks, paths and commands stay left-to-right. Needs the `markdown`
package at build time only; the page itself has no dependencies.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

STYLE = """
:root { --bg:#f7f7f5; --fg:#1c1c1a; --muted:#5d5d58; --card:#fff; --line:#e3e3de;
        --accent:#1f5f8b; --code:#f1f1ee; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141413; --fg:#ececea; --muted:#a3a39e; --card:#1d1d1b; --line:#33332f;
          --accent:#7fb6de; --code:#262624; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.95 Tahoma, "Segoe UI", "Vazirmatn", "Noto Naskh Arabic", sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.6rem; line-height: 1.5; margin: 8px 0 16px; }
h2 { font-size: 1.25rem; margin: 28px 0 12px; }
h3 { font-size: 1.05rem; margin: 24px 0 8px; }
a { color: var(--accent); }
code, pre { font-family: Consolas, "Cascadia Mono", Menlo, monospace; direction: ltr;
            unicode-bidi: isolate; }
code { background: var(--code); padding: 1px 6px; border-radius: 6px; font-size: .9em; }
pre { background: var(--code); padding: 12px 14px; border-radius: 10px; overflow-x: auto;
      text-align: left; line-height: 1.6; }
pre code { background: none; padding: 0; }
:not(pre) > code { overflow-wrap: anywhere; }   /* long Windows paths on a phone */
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: .95rem;
        display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 8px 10px; vertical-align: top;
         text-align: right; }
th { background: var(--card); }
blockquote { margin: 16px 0; padding: 10px 16px; border-inline-start: 4px solid var(--accent);
             background: var(--card); border-radius: 8px; }
hr { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
"""


def render(source: Path) -> str:
    try:
        import markdown
    except ImportError:
        raise SystemExit("the 'markdown' package is needed to render the guide") from None
    text = source.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"], output_format="html")
    title = next((line.lstrip("# ").strip() for line in text.splitlines()
                  if line.startswith("# ")), "Sentinel-FX")
    return ("<!doctype html>\n<html lang=\"fa\" dir=\"rtl\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n"
            f"<body><main>\n{body}\n</main></body>\n</html>\n")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(__doc__)
        return 2
    Path(args[1]).write_text(render(Path(args[0])), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
