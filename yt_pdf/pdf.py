"""Markdown -> styled HTML -> PDF via Playwright/Chromium."""

from __future__ import annotations

import base64
import functools
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
AT_TAG_RE = re.compile(r"\{\{\s*AT\s*:\s*(\d+(?:\.\d+)?)\s*\}\}", re.IGNORECASE)
TLDR_RE = re.compile(r"\[\[TLDR\]\](.*?)\[\[/TLDR\]\]", re.DOTALL)
_SHOT_PLACEHOLDER_RE = re.compile(r"<p>\s*@@SHOT:(\d+)@@\s*</p>")
_AT_PLACEHOLDER_RE = re.compile(r"<h([23])>(.*?)</h\1>\s*<p>\s*@@AT:(\d+)@@\s*</p>", re.DOTALL)
_LEFTOVER_PLACEHOLDER_RE = re.compile(r"(?:<p>\s*)?@@(?:SHOT|AT):\d+@@(?:\s*</p>)?")

FONT_DIR = Path(__file__).parent / "fonts"
# (css family, weight, style, file)
FONT_FACES = [
    ("Source Serif 4", 400, "normal", "source-serif-4-400-normal.woff2"),
    ("Source Serif 4", 400, "italic", "source-serif-4-400-italic.woff2"),
    ("Source Serif 4", 600, "normal", "source-serif-4-600-normal.woff2"),
    ("IBM Plex Sans", 400, "normal", "ibm-plex-sans-400-normal.woff2"),
    ("IBM Plex Sans", 600, "normal", "ibm-plex-sans-600-normal.woff2"),
    ("IBM Plex Mono", 400, "normal", "ibm-plex-mono-400-normal.woff2"),
]

# Headings and chrome carry the text colour; the accent is reserved for video timecodes and links.
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#ffffff",
        "text": "#101828",
        "muted": "#5b6475",
        "border": "#dde1e8",
        "rule": "#aab1bf",
        "code_bg": "#f4f5f7",
        "accent": "#d92d20",
    },
    "dark": {
        "bg": "#0b0f19",
        "text": "#e2e8f0",
        "muted": "#94a3b8",
        "border": "#243044",
        "rule": "#475569",
        "code_bg": "#121a2b",
        "accent": "#ff6b5e",
    },
}

# "large" is the larger size; "normal" (the default) is 10% smaller across the board.
TEXT_SCALES = {"normal": 0.9, "large": 1.0}

CSS = """
@page { size: A4; margin: 15mm 30mm; }
:root {
  --gutter: 24mm;
  --serif: "Source Serif 4", "Iowan Old Style", Charter, Georgia, serif;
  --sans: "IBM Plex Sans", -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "IBM Plex Mono", "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html { background: var(--bg); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; padding-left: var(--gutter); background: var(--bg); color: var(--text);
  font-family: var(--serif); font-size: calc(12pt * var(--scale)); line-height: 1.55;
  font-kerning: normal; -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4 {
  position: relative; font-family: var(--sans); font-weight: 600; color: var(--text);
  line-height: 1.2; break-after: avoid;
}
h1 { font-size: calc(26pt * var(--scale)); line-height: 1.12; letter-spacing: -0.01em; margin: 0 0 0.4em; }
h2 { font-size: calc(17pt * var(--scale)); margin: 1.9em 0 0.55em; padding-top: 0.7em; border-top: 0.5pt solid var(--border); }
h3 { font-size: calc(13pt * var(--scale)); margin: 1.5em 0 0.4em; }
h4 { font-size: calc(11pt * var(--scale)); margin: 1.2em 0 0.3em; }
p { margin: 0 0 0.75em; orphans: 3; widows: 3; }
/* Keep a lead-in sentence on the same page as the list, table or code it introduces. */
p:has(+ ul), p:has(+ ol), p:has(+ table), p:has(+ pre) { break-after: avoid; }
a { color: var(--accent); text-decoration: none; }
strong { font-weight: 600; }
ul, ol { margin: 0 0 0.85em; padding-left: 1.3em; }
li { margin-bottom: 0.3em; }
li::marker { color: var(--muted); }
blockquote {
  margin: 0.9em 0; padding: 0.1em 0 0.1em 1em; border-left: 2pt solid var(--rule);
  color: var(--muted); font-style: italic; break-inside: avoid;
}
code { font-family: var(--mono); font-size: calc(10pt * var(--scale)); background: var(--code_bg); padding: 0.05em 0.3em; border-radius: 2px; }
pre {
  font-family: var(--mono); font-size: calc(9.5pt * var(--scale)); line-height: 1.5;
  background: var(--code_bg); border-left: 2pt solid var(--rule); padding: 0.7em 1em;
  margin: 0.8em 0 1.1em; white-space: pre-wrap; word-break: break-word; break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: inherit; }
table {
  width: 100%; border-collapse: collapse; margin: 0.9em 0 1.2em; break-inside: avoid;
  font-family: var(--sans); font-size: calc(9.5pt * var(--scale)); line-height: 1.4;
}
th, td { padding: 0.45em 0.8em 0.45em 0; text-align: left; vertical-align: top; border-bottom: 0.5pt solid var(--border); }
th { font-weight: 600; border-bottom: 1pt solid var(--text); }
th:last-child, td:last-child { padding-right: 0; }

/* Header: starts at the gutter edge so the title lines up with the timecodes below it. */
.doc-header { margin: 0 0 1.5em calc(-1 * var(--gutter)); }
.meta {
  display: flex; flex-wrap: wrap; row-gap: 0.2em; color: var(--muted);
  font-family: var(--sans); font-size: calc(9pt * var(--scale)); line-height: 1.3;
}
.meta span + span { margin-left: 0.8em; padding-left: 0.8em; border-left: 0.5pt solid var(--rule); }
.meta a { font-family: var(--mono); font-size: calc(8.5pt * var(--scale)); }

.tldr { margin: 0 0 1.6em; padding: 0.8em 0 0.5em; border-top: 2pt solid var(--text); border-bottom: 0.5pt solid var(--border); break-inside: avoid; }
.tldr h2 { margin: 0 0 0.5em; padding: 0; border: 0; font-size: calc(11pt * var(--scale)); }
.tldr ul { margin-bottom: 0.4em; font-size: 1.04em; }
.tldr + h2 { margin-top: 1.2em; padding-top: 0; border-top: 0; }

/* Timecodes hang in the left gutter and link to that moment in the video. */
.tc {
  position: absolute; right: 100%; margin-right: 4mm; white-space: nowrap;
  font-family: var(--mono); font-size: calc(9pt * var(--scale)); font-weight: 400; line-height: 1;
  font-variant-numeric: tabular-nums;
}
/* Offsets put the timecode baseline on the heading's first-line baseline (heading padding + half-leading + ascent). */
h2 .tc { top: calc(20.6pt * var(--scale)); }
.tldr + h2 .tc { top: calc(8.7pt * var(--scale)); }
h3 .tc { top: calc(4.8pt * var(--scale)); }

figure.shot { position: relative; margin: 1.1em 0 1.3em; break-inside: avoid; }
figure.shot img {
  display: block; width: auto; height: auto; max-width: 100%; max-height: 80mm;
  border: 0.5pt solid var(--border);
}
figure.shot .tc { top: 0; }
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


def _clamp_time(seconds: float, duration: int | None) -> int:
    sec = int(seconds)
    if duration is not None and sec >= duration:
        sec = max(duration - 1, 0)
    return sec


def find_screenshot_times(body: str, duration: int | None, limit: int) -> list[int]:
    """Valid, de-duplicated, ordered timestamps requested in the guide."""
    seen: list[int] = []
    for raw in SHOT_TAG_RE.findall(body):
        sec = _clamp_time(float(raw), duration)
        if sec not in seen:
            seen.append(sec)
    return seen[:limit]


_LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,3}[.)])\s+\S")


def normalize_markdown(text: str) -> str:
    """Add the blank lines Markdown needs before lists and tables.

    Models often write a sentence and put the list or table on the very next line. Python-Markdown
    then folds the whole thing into one paragraph. Code fences are left untouched.
    """
    out: list[str] = []
    in_fence = False
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            in_list = False
            continue
        if in_fence:
            out.append(line)
            continue
        if not stripped:
            out.append(line)
            in_list = False
            continue
        prev = out[-1].strip() if out else ""
        is_item = bool(_LIST_ITEM_RE.match(line))
        is_row = stripped.startswith("|")
        prev_is_row = prev.startswith("|")
        starts_block = (is_item and not in_list) or (is_row and not prev_is_row)
        if starts_block and prev and not prev.startswith("#"):
            out.append("")
        if is_item:
            in_list = True
        elif not line.startswith((" ", "\t")):
            in_list = False
        out.append(line)
    return "\n".join(out)


_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "del",
    "code", "pre", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "a",
}


def _md(text: str) -> str:
    """Markdown -> HTML, with anything the LLM (or a poisoned transcript) smuggled in stripped
    down to a small allowlist: no styles, no images, only http(s) links."""
    raw = markdown.markdown(normalize_markdown(text), extensions=["fenced_code", "tables", "sane_lists"])
    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes={"a": {"href"}, "code": {"class"}},
        url_schemes={"http", "https"},
    )


@functools.lru_cache(maxsize=1)
def _font_face_css() -> str:
    rules = []
    for family, weight, style, filename in FONT_FACES:
        data = base64.b64encode((FONT_DIR / filename).read_bytes()).decode("ascii")
        rules.append(
            f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:{style};'
            f"font-display:block;src:url(data:font/woff2;base64,{data}) format('woff2');}}"
        )
    return "".join(rules)


def _css_string(text: str, limit: int = 80) -> str:
    """A quoted CSS string that is safe inside a <style> element (the title is untrusted)."""
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3C ")
    return f'"{text}"'


def _page_css(theme: str, title: str) -> str:
    t = THEMES[theme]
    box = f'font-family:"IBM Plex Sans",sans-serif;font-size:8pt;color:{t["muted"]};'
    return (
        f"@page{{background:{t['bg']};"
        f"@top-left{{content:{_css_string(title)};{box}}}"
        f'@bottom-right{{content:counter(page) " / " counter(pages);{box}}}}}'
        "@page :first{@top-left{content:none;}}"
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

    def swap_shot(match: re.Match[str]) -> str:
        sec = _clamp_time(float(match.group(1)), duration)
        return f"\n\n@@SHOT:{sec}@@\n\n" if sec in frames else "\n\n"

    def swap_at(match: re.Match[str]) -> str:
        return f"\n\n@@AT:{_clamp_time(float(match.group(1)), duration)}@@\n\n"

    body_md = AT_TAG_RE.sub(swap_at, SHOT_TAG_RE.sub(swap_shot, normalize_markdown(body_md)))
    body_html = _md(body_md)

    def timecode(sec: int) -> str:
        return f'<a class="tc" href="https://youtu.be/{video_id}?t={sec}">{format_timestamp(sec)}</a>'

    def timed_heading(match: re.Match[str]) -> str:
        level, text, sec = match.group(1), match.group(2), int(match.group(3))
        return f"<h{level}>{timecode(sec)}{text}</h{level}>"

    def figure(match: re.Match[str]) -> str:
        sec = int(match.group(1))
        return (
            f'<figure class="shot">{timecode(sec)}'
            f'<img src="{frames[sec]}" alt="Video frame at {format_timestamp(sec)}"></figure>'
        )

    body_html = _AT_PLACEHOLDER_RE.sub(timed_heading, body_html)
    body_html = _SHOT_PLACEHOLDER_RE.sub(figure, body_html)
    body_html = _LEFTOVER_PLACEHOLDER_RE.sub("", body_html)  # tags the model misplaced

    tldr_html = f'<section class="tldr"><h2>TL;DR</h2>{_md(tldr_md)}</section>' if tldr_md else ""

    meta = []
    if channel:
        meta.append(f"<span>{html.escape(channel)}</span>")
    if duration:
        meta.append(f"<span>{format_timestamp(duration)}</span>")
    meta.append(f"<span>{date.today().isoformat()}</span>")
    meta.append(f'<span><a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a></span>')

    variables = "".join(f"--{k}:{v};" for k, v in THEMES[theme].items()) + f"--scale:{TEXT_SCALES[text_size]};"
    # The CSP is a backstop: transcript-derived text must never load remote content or run scripts.
    csp = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'"
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_font_face_css()}:root{{{variables}}}{CSS}{_page_css(theme, title)}</style></head><body>"
        f'<header class="doc-header"><h1>{html.escape(title)}</h1><div class="meta">{"".join(meta)}</div></header>'
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
