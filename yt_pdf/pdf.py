"""Markdown -> styled HTML -> PDF via Playwright/Chromium."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import markdown
import nh3

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

# "large" is the original sizing; "normal" (the default) is 10% smaller across the board.
TEXT_SCALES = {"normal": 0.9, "large": 1.0}

CSS = """
@page { size: A4; margin: 15mm 15mm; }
* { box-sizing: border-box; }
html { background: var(--bg); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: calc(10.5pt * var(--scale)); line-height: 1.6;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4 { color: var(--heading); line-height: 1.25; break-after: avoid; }
h1 { font-size: calc(22pt * var(--scale)); margin: 0 0 4pt; }
h2 { font-size: calc(15pt * var(--scale)); margin: 22pt 0 8pt; padding-bottom: 4pt; border-bottom: 1px solid var(--border); }
h3 { font-size: calc(12pt * var(--scale)); margin: 16pt 0 6pt; }
p { margin: 0 0 8pt; orphans: 3; widows: 3; }
a { color: var(--accent); text-decoration: none; }
ul, ol { margin: 0 0 8pt; padding-left: 18pt; }
li { margin-bottom: 3pt; }
strong { color: var(--heading); }
blockquote { margin: 8pt 0; padding: 2pt 12pt; border-left: 3px solid var(--accent); color: var(--muted); break-inside: avoid; }
code, pre { font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }
code { background: var(--code_bg); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; font-size: calc(9.5pt * var(--scale)); }
pre {
  background: var(--code_bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 10pt 12pt; margin: 8pt 0 12pt; font-size: calc(9pt * var(--scale)); line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; break-inside: avoid;
}
pre code { background: none; border: 0; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; break-inside: avoid; font-size: calc(9.5pt * var(--scale)); }
th, td { border: 1px solid var(--border); padding: 5pt 8pt; text-align: left; vertical-align: top; }
th { background: var(--card); color: var(--heading); }
.doc-header { margin-bottom: 14pt; padding-bottom: 10pt; border-bottom: 2px solid var(--border); }
.meta { color: var(--muted); font-size: calc(9pt * var(--scale)); }
.tldr {
  background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--accent);
  border-radius: 8px; padding: 10pt 14pt; margin: 0 0 16pt; break-inside: avoid;
}
.tldr h2 { margin: 0 0 6pt; padding: 0; border: 0; font-size: calc(12pt * var(--scale)); text-transform: uppercase; letter-spacing: .06em; }
.tldr ul { margin-bottom: 0; }
figure.shot {
  margin: 12pt 0 14pt; padding: 6pt; background: var(--card);
  border: 1px solid var(--border); border-radius: 10px; break-inside: avoid;
}
figure.shot img { display: block; width: 100%; height: auto; border-radius: 6px; }
figure.shot figcaption { margin-top: 5pt; font-size: calc(8.5pt * var(--scale)); color: var(--muted); text-align: right; }
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


_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "del",
    "code", "pre", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "a",
}


def _md(text: str) -> str:
    """Markdown -> HTML, with anything the LLM (or a poisoned transcript) smuggled in stripped
    down to a small allowlist: no styles, no images, only http(s) links."""
    raw = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes={"a": {"href"}, "code": {"class"}},
        url_schemes={"http", "https"},
    )


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
    text_size: str = "normal",
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

    variables = "".join(f"--{k}:{v};" for k, v in THEMES[theme].items()) + f"--scale:{TEXT_SCALES[text_size]};"
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


def _is_missing_browser(exc: Exception) -> bool:
    text = str(exc)
    return "Executable doesn't exist" in text or "is not found at" in text or "playwright install" in text


def _install_headless_shell(log: Callable[[str], None]) -> None:
    """Download just the headless shell (~100 MB), not the full Chromium (~370 MB more)."""
    log("Downloading Chromium headless shell (~100 MB, one time)…")
    cmd = [sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YtPdfError(f"Could not download Chromium ({exc}). Run: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise YtPdfError("Chromium download failed:\n" + "\n".join(tail) + f"\nRetry with: {' '.join(cmd)}")


def _print_with(pw, html_doc: str, output: Path, channel: str | None) -> None:
    browser = pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
    try:
        context = browser.new_context(java_script_enabled=False)
        # Everything is inline; refuse any network fetch the document attempts.
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content(html_doc, wait_until="load")
        page.pdf(path=str(output), format="A4", prefer_css_page_size=True, print_background=True)
    finally:
        browser.close()


def render_pdf(html_doc: str, output: Path, log: Callable[[str], None] | None = None) -> None:
    """Print the HTML to PDF with the first browser that works.

    Order: Playwright's headless shell if present, then an installed Chrome or Edge (no download),
    and only then a one-time download of the headless shell.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    log = log or (lambda msg: print(msg, file=sys.stderr))
    output.parent.mkdir(parents=True, exist_ok=True)

    def attempt(channels: tuple[str | None, ...]) -> bool:
        with sync_playwright() as pw:
            for channel in channels:
                try:
                    _print_with(pw, html_doc, output, channel)
                    return True
                except PlaywrightError as exc:
                    if not _is_missing_browser(exc):
                        raise
        return False

    try:
        if attempt((None, "chrome", "msedge")):
            return
        _install_headless_shell(log)
        if attempt((None,)):
            return
        raise YtPdfError("No usable browser found even after installing Chromium.")
    except PlaywrightError as exc:
        raise YtPdfError(f"PDF rendering failed: {exc}") from exc
    except OSError as exc:
        raise YtPdfError(f"Could not write {output}: {exc}") from exc
