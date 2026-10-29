"""Writers for the non-PDF output formats: self-contained HTML, Markdown (with image files), EPUB."""

from __future__ import annotations

import base64
import html
import json
import re
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from . import pdf
from .errors import YtPdfError

FORMATS = ("pdf", "html", "md", "epub")
EXTENSIONS = {"pdf": ".pdf", "html": ".html", "md": ".md", "epub": ".epub"}


def format_for_path(path: Path) -> str | None:
    """The format a file extension implies, or None."""
    return next((fmt for fmt, ext in EXTENSIONS.items() if path.suffix.lower() == ext), None)


def _jpeg_bytes(data_uri: str) -> bytes:
    return base64.b64decode(data_uri.split(",", 1)[1])


def _video_url(video_id: str, sec: int) -> str:
    return f"https://youtu.be/{video_id}?t={sec}"


# --------------------------------------------------------------------------- html


def write_html(doc: str, output: Path) -> None:
    output.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- markdown


_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _escape_line(line: str, spans: list[str]) -> str:
    """One prose line with every `<` escaped. Inline code spans are lifted out into `spans`.

    Follows CommonMark: a backslash escapes the next character (so an escaped backtick does not open
    a span) and a span closes on a backtick run of the same length. A span must close on the same
    line; if the viewer would see a longer one we merely over-escape, which is the safe direction.
    """
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            out.append(line[i : i + 2].replace("<", "&lt;"))
            i += 2
        elif ch == "`":
            run = re.match(r"`+", line[i:]).group(0)
            close = re.search(rf"(?<!`){'`' * len(run)}(?!`)", line[i + len(run) :])
            if close:
                end = i + len(run) + close.end()
                spans.append(line[i:end])
                out.append(_PLACEHOLDER.format(len(spans) - 1))
                i = end
            else:
                out.append(run)
                i += len(run)
        elif ch == "<":
            out.append("&lt;")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _neutralise_prose(chunk: str) -> str:
    # Runs over several lines at once so link text that wraps cannot dodge it. Breaking `](`, `]:` and
    # `][` stops inline links, reference definitions and reference links from forming at all, except
    # the http(s) links that are kept; `![` becomes `[` so the model cannot make a viewer fetch an image.
    chunk = chunk.replace("![", "[")
    chunk = re.sub(r"\]\((?!\s*<?https?://)", "] (", chunk, flags=re.IGNORECASE)
    return chunk.replace("]:", "] :").replace("][", "] [")


def _is_inert(text: str, allowed_images: frozenset[str] = frozenset()) -> bool:
    """True if a CommonMark parser finds no raw HTML, no image (except `allowed_images`, the writer's
    own screenshot paths) and no non-http(s) link in `text`."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": True})
    md.validateLink = lambda url: True  # report javascript: links too instead of silently dropping them

    def walk(tokens) -> bool:
        for tok in tokens:
            if tok.type == "html_block" or tok.type == "html_inline":
                return False
            if tok.type == "image" and tok.attrGet("src") not in allowed_images:
                return False
            if tok.type == "link_open" and not re.match(r"https?://", tok.attrGet("href") or "", re.IGNORECASE):
                return False
            if tok.children and not walk(tok.children):
                return False
        return True

    return walk(md.parse(text))


def _escape_structure(text: str) -> str:
    """Last resort: no `<` or bracket anywhere (code included) is left to be read as markup."""
    return text.replace("<", "&lt;").replace("[", "&#91;").replace("]", "&#93;")


def clean_markdown(text: str) -> str:
    """Model output made safe to hand to any Markdown viewer.

    Fail-closed in two steps. First a conservative rewrite: outside fenced code and inline code every
    `<` is escaped, images become plain links and only http(s) link destinations survive. Because a
    hand-written scanner can disagree with a viewer about what is code, the result is then re-parsed
    with a real CommonMark parser; if that finds any HTML, image or unsafe link, `<` and brackets are
    escaped everywhere, code included, which costs fidelity but not safety.
    """
    text = text.replace("\x00", "")
    cleaned = _rewrite(text)
    if _is_inert(cleaned):
        return cleaned
    return _escape_structure(cleaned)


def _rewrite(text: str) -> str:
    out: list[str] = []
    prose: list[str] = []
    spans: list[str] = []
    fence: tuple[str, int] | None = None

    def flush() -> None:
        if prose:
            chunk = _neutralise_prose("\n".join(prose))
            out.append(_PLACEHOLDER_RE.sub(lambda m: spans[int(m.group(1))], chunk))
            prose.clear()
            spans.clear()

    for line in text.splitlines():
        if fence:
            out.append(line)
            close = re.match(rf"^ {{0,3}}({re.escape(fence[0])}{{{fence[1]},}})\s*$", line)
            if close:
                fence = None
            continue
        opener = _FENCE_OPEN_RE.match(line)
        if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            flush()
            fence = (opener.group(1)[0], len(opener.group(1)))
            out.append(line)
        else:
            prose.append(_escape_line(line, spans))
    flush()
    return "\n".join(out)


def write_markdown(
    guide: str,
    frames: dict[int, str],
    *,
    title: str,
    video_id: str,
    source_url: str,
    channel: str | None,
    duration: int | None,
    output: Path,
) -> list[Path]:
    """Write `output` (.md) plus `<name>_images/` when there are screenshots. Returns every file written."""
    raw_tldr, raw_body = pdf.parse_guide(guide)
    title = " ".join(clean_markdown(title).split()) or video_id
    images_dir = output.with_name(f"{output.stem}_images")

    front = ["---", f"title: {json.dumps(title, ensure_ascii=False)}", f"source: {json.dumps(source_url)}"]
    if channel:
        front.append(f"channel: {json.dumps(channel, ensure_ascii=False)}")
    if duration:
        front.append(f"duration: {pdf.format_timestamp(duration)}")
    front += [f"date: {date.today().isoformat()}", "---", ""]

    def assemble(clean) -> tuple[str, set[int]]:
        tldr, body = clean(raw_tldr), clean(raw_body)
        used: set[int] = set()

        def shot(match: re.Match[str]) -> str:
            sec = pdf._clamp_time(float(match.group(1)), duration)
            if sec not in frames:
                return ""
            used.add(sec)
            stamp = pdf.format_timestamp(sec)
            return (
                f"\n\n![Video frame at {stamp}]({images_dir.name}/shot-{sec}.jpg)\n\n"
                f"*[{stamp}]({_video_url(video_id, sec)})*\n\n"
            )

        def at(match: re.Match[str]) -> str:
            sec = pdf._clamp_time(float(match.group(1)), duration)
            return f"\n\n*[{pdf.format_timestamp(sec)}]({_video_url(video_id, sec)})*\n\n"

        body = pdf.AT_TAG_RE.sub(at, pdf.SHOT_TAG_RE.sub(shot, body))
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        parts = ["\n".join(front), f"# {title}\n"]
        if tldr:
            parts.append(f"## TL;DR\n\n{tldr}\n")
        parts.append(body + "\n")
        return "\n".join(parts), used

    # The tag substitutions add blank lines and links, which can change how the text around them parses
    # (for example ending a fenced block inside a list). So the check runs on the finished document,
    # not on the pieces; if it fails the pieces are escaped strictly and put together again.
    for clean in (clean_markdown, lambda t: _escape_structure(_rewrite(t.replace("\x00", "")))):
        text, used = assemble(clean)
        allowed = frozenset(f"{images_dir.name}/shot-{sec}.jpg" for sec in used)
        if _is_inert(text, allowed):
            break
    else:
        raise YtPdfError("Could not make the Markdown output safe; try --format pdf or epub.")

    output.write_text(text, encoding="utf-8")
    written = [output]
    if used:
        images_dir.mkdir(exist_ok=True)
        for sec in sorted(used):
            path = images_dir / f"shot-{sec}.jpg"
            path.write_bytes(_jpeg_bytes(frames[sec]))
            written.append(path)
    return written


# --------------------------------------------------------------------------- epub

_EPUB_CSS = """\
body{font-family:serif;line-height:1.55;margin:0 4%}
h1{font-size:1.6em;line-height:1.2;margin:1em 0 .2em}
h2{font-size:1.25em;margin:1.6em 0 .3em}
h3{font-size:1.05em;margin:1.2em 0 .2em}
.meta{color:#555;font-size:.85em;margin:0 0 1.4em}
.tc{font-family:monospace;font-size:.75em;color:#b42318;text-decoration:none;margin-right:.6em}
.tldr{border-left:3px solid #b42318;padding-left:.9em;margin:1em 0}
pre{white-space:pre-wrap;word-wrap:break-word;background:#f4f4f4;padding:.6em;font-size:.8em}
code{font-family:monospace;font-size:.9em}
figure{margin:1.2em 0}
img{max-width:100%;height:auto}
table{border-collapse:collapse}
td,th{border:1px solid #bbb;padding:.2em .5em}
"""

_VOID_RE = re.compile(r"<(br|hr|img)\b([^>]*?)\s*/?>")
_H2_RE = re.compile(r"<h2>(.*?)</h2>", re.DOTALL)
_TC_RE = re.compile(r'<a class="tc"[^>]*>.*?</a>')
_TAG_RE = re.compile(r"<[^>]+>")


def _xhtml(fragment: str) -> str:
    """Make sanitised HTML5 well-formed XML, which EPUB content documents must be."""
    return _VOID_RE.sub(r"<\1\2 />", fragment).replace("&nbsp;", "&#160;")


def write_epub(
    guide: str,
    frames: dict[int, str],
    *,
    title: str,
    video_id: str,
    source_url: str,
    channel: str | None,
    duration: int | None,
    output: Path,
) -> None:
    inserted: set[int] = set()

    def image_src(sec: int) -> str:
        inserted.add(sec)  # only frames the renderer really placed; never names found in the text
        return f"images/shot-{sec}.jpg"

    content = pdf.render_body(guide, frames, video_id=video_id, duration=duration, image_src=image_src)
    used = sorted(inserted & frames.keys())

    toc: list[tuple[str, str]] = []

    def add_id(match: re.Match[str]) -> str:
        inner = match.group(1)
        label = html.unescape(_TAG_RE.sub("", _TC_RE.sub("", inner))).strip()
        anchor = f"s{len(toc) + 1}"
        toc.append((anchor, label))
        return f'<h2 id="{anchor}">{inner}</h2>'

    content = _xhtml(_H2_RE.sub(add_id, content))

    meta = [html.escape(m) for m in (channel, pdf.format_timestamp(duration) if duration else None, date.today().isoformat()) if m]
    meta.append(f'<a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a>')
    esc_title = html.escape(title)
    ns = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
    guide_doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html {ns} lang="en" xml:lang="en"><head><meta charset="utf-8" /><title>{esc_title}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css" /></head><body>'
        f'<h1>{esc_title}</h1><p class="meta">{" · ".join(meta)}</p>{content}</body></html>'
    )
    entries = "".join(f'<li><a href="guide.xhtml#{a}">{html.escape(t)}</a></li>' for a, t in toc)
    nav_doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html {ns} lang="en" xml:lang="en"><head><meta charset="utf-8" /><title>{esc_title}</title></head><body>'
        f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol><li><a href="guide.xhtml">{esc_title}</a></li>{entries}</ol></nav>'
        "</body></html>"
    )
    images = "".join(f'<item id="img{n}" href="images/shot-{n}.jpg" media-type="image/jpeg" />' for n in used)
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="uid">urn:uuid:{uuid.uuid4()}</dc:identifier>'
        f"<dc:title>{esc_title}</dc:title><dc:language>en</dc:language>"
        + (f"<dc:creator>{html.escape(channel)}</dc:creator>" if channel else "")
        + f"<dc:source>{html.escape(source_url)}</dc:source>"
        f'<meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>'
        "</metadata><manifest>"
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />'
        '<item id="guide" href="guide.xhtml" media-type="application/xhtml+xml" />'
        '<item id="css" href="style.css" media-type="text/css" />'
        f'{images}</manifest><spine><itemref idref="guide" /></spine></package>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" />'
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(output, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav_doc, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/guide.xhtml", guide_doc, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", _EPUB_CSS, compress_type=zipfile.ZIP_DEFLATED)
        for sec in used:
            z.writestr(f"OEBPS/images/shot-{sec}.jpg", _jpeg_bytes(frames[sec]), compress_type=zipfile.ZIP_STORED)
