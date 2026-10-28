"""Markdown -> styled HTML -> PDF via Playwright/Chromium."""

from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

import markdown

from .errors import YtPdfError

SHOT_TAG_RE = re.compile(r"\{\{\s*SCREENSHOT\s*:\s*(\d+(?:\.\d+)?)\s*\}\}", re.IGNORECASE)
TLDR_RE = re.compile(r"\[\[TLDR\]\](.*?)\[\[/TLDR\]\]", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<p>\s*@@SHOT:(\d+)@@\s*</p>")

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#ffffff",
        "text": "#1e293b",
        "heading": "#0f2a5c",
        "muted": "#64748b",
        "border": "#e2e8f0",
        "card": "#f8fafc",
        "code_bg": "#f1f5f9",
        "accent": "#1d4ed8",
    },
    "dark": {
        "bg": "#0b0f19",
        "text": "#e2e8f0",
        "heading": "#38bdf8",
        "muted": "#94a3b8",
        "border": "#1e293b",
        "card": "#111827",
        "code_bg": "#111827",
        "accent": "#38bdf8",
    },
}

CSS = """
@page { size: A4; margin: 15mm 15mm; }
* { box-sizing: border-box; }
html { background: var(--bg); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.6;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4 { color: var(--heading); line-height: 1.25; break-after: avoid; }
h1 { font-size: 22pt; margin: 0 0 4pt; }
h2 { font-size: 15pt; margin: 22pt 0 8pt; padding-bottom: 4pt; border-bottom: 1px solid var(--border); }
h3 { font-size: 12pt; margin: 16pt 0 6pt; }
p { margin: 0 0 8pt; orphans: 3; widows: 3; }
a { color: var(--accent); text-decoration: none; }
ul, ol { margin: 0 0 8pt; padding-left: 18pt; }
li { margin-bottom: 3pt; }
strong { color: var(--heading); }
blockquote { margin: 8pt 0; padding: 2pt 12pt; border-left: 3px solid var(--accent); color: var(--muted); break-inside: avoid; }
code, pre { font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }
code { background: var(--code_bg); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; font-size: 9.5pt; }
pre {
  background: var(--code_bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 10pt 12pt; margin: 8pt 0 12pt; font-size: 9pt; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; break-inside: avoid;
}
pre code { background: none; border: 0; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; break-inside: avoid; font-size: 9.5pt; }
th, td { border: 1px solid var(--border); padding: 5pt 8pt; text-align: left; vertical-align: top; }
th { background: var(--card); color: var(--heading); }
.doc-header { margin-bottom: 14pt; padding-bottom: 10pt; border-bottom: 2px solid var(--border); }
.meta { color: var(--muted); font-size: 9pt; }
.tldr {
  background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--accent);
  border-radius: 8px; padding: 10pt 14pt; margin: 0 0 16pt; break-inside: avoid;
}
.tldr h2 { margin: 0 0 6pt; padding: 0; border: 0; font-size: 12pt; text-transform: uppercase; letter-spacing: .06em; }
.tldr ul { margin-bottom: 0; }
figure.shot {
  margin: 12pt 0 14pt; padding: 6pt; background: var(--card);
  border: 1px solid var(--border); border-radius: 10px; break-inside: avoid;
}
figure.shot img { display: block; width: 100%; height: auto; border-radius: 6px; }
figure.shot figcaption { margin-top: 5pt; font-size: 8.5pt; color: var(--muted); text-align: right; }
"""


def format_timestamp(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_guide(text: str) -> tuple[str, str]:
    """Split the LLM output into (tldr_markdown, body_markdown)."""
    text = text.strip()
    # Some models wrap everything in a ```markdown fence despite instructions.
    fenced = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    match = TLDR_RE.search(text)
    if not match:
        return "", text
    return match.group(1).strip(), (text[: match.start()] + text[match.end() :]).strip()


def find_screenshot_times(body: str, duration: int | None, limit: int) -> list[int]:
    """Valid, de-duplicated, ordered timestamps requested in the guide."""
    seen: list[int] = []
    for raw in SHOT_TAG_RE.findall(body):
        sec = int(float(raw))
        if duration is not None and sec >= duration:
            sec = max(duration - 1, 0)
        if sec not in seen:
            seen.append(sec)
    return seen[:limit]


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])


def build_html(
    guide: str,
    frames: dict[int, str],
    *,
    title: str,
    video_id: str,
    source_url: str,
    theme: str,
    channel: str | None = None,
    duration: int | None = None,
) -> str:
    tldr_md, body_md = parse_guide(guide)
    ordered = sorted(frames)
    index_of = {sec: i for i, sec in enumerate(ordered)}

    def swap(match: re.Match[str]) -> str:
        sec = int(float(match.group(1)))
        if duration is not None and sec >= duration:
            sec = max(duration - 1, 0)
        return f"\n\n@@SHOT:{sec}@@\n\n" if sec in index_of else "\n\n"

    body_html = _md(SHOT_TAG_RE.sub(swap, body_md))

    def figure(match: re.Match[str]) -> str:
        sec = int(match.group(1))
        link = f"https://youtu.be/{video_id}?t={sec}"
        return (
            f'<figure class="shot"><img src="{frames[sec]}" alt="Video frame at {format_timestamp(sec)}">'
            f'<figcaption><a href="{link}">{format_timestamp(sec)}</a></figcaption></figure>'
        )

    body_html = _PLACEHOLDER_RE.sub(figure, body_html)

    tldr_html = f'<section class="tldr"><h2>TL;DR</h2>{_md(tldr_md)}</section>' if tldr_md else ""

    meta = [f'<a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a>']
    if channel:
        meta.insert(0, html.escape(channel))
    if duration:
        meta.append(format_timestamp(duration))
    meta.append(date.today().isoformat())

    variables = "".join(f"--{k}:{v};" for k, v in THEMES[theme].items())
    # The CSP is a backstop: transcript-derived text must never load remote content or run scripts.
    csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        f"<title>{html.escape(title)}</title>"
        # @page can't read custom properties, so the margin-area background gets the literal colour.
        f"<style>:root{{{variables}}}{CSS}@page{{background:{THEMES[theme]['bg']};}}</style></head><body>"
        f'<header class="doc-header"><h1>{html.escape(title)}</h1>'
        f'<div class="meta">{" · ".join(meta)}</div></header>'
        f"{tldr_html}{body_html}</body></html>"
    )


def render_pdf(html_doc: str, output: Path) -> None:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(java_script_enabled=False)
                page = context.new_page()
                page.set_content(html_doc, wait_until="load")
                page.pdf(
                    path=str(output),
                    format="A4",
                    prefer_css_page_size=True,
                    print_background=True,
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise YtPdfError(
                "Chromium is not installed for Playwright. Run: playwright install chromium"
            ) from exc
        raise YtPdfError(f"PDF rendering failed: {exc}") from exc
    except OSError as exc:
        raise YtPdfError(f"Could not write {output}: {exc}") from exc
