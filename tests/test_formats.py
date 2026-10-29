import base64

import pytest
import xml.etree.ElementTree as ET
import zipfile

from yt_pdf import formats

GUIDE = (
    "[[TLDR]]\n- one\n- two\n[[/TLDR]]\n\n"
    "## First part\n{{AT: 12}}\nText with a <script>alert(1)</script> & more.<br>\n\n"
    "{{SCREENSHOT: 30}}\n\n{{SCREENSHOT: 99}}\n\n"
    "## Second\n{{AT: 70}}\n\n```py\nprint(1)\n```\n"
)
JPEG = b"\xff\xd8fake-jpeg"
FRAMES = {30: "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}
KW = dict(title='Talk: "A & B"', video_id="abcdefghijk", source_url="https://youtu.be/abcdefghijk", channel="Chan", duration=600)


def test_format_from_extension(tmp_path):
    from pathlib import Path

    assert formats.format_for_path(Path("x.EPUB")) == "epub"
    assert formats.format_for_path(Path("x.md")) == "md"
    assert formats.format_for_path(Path("x.txt")) is None


def test_markdown_has_front_matter_links_and_image_files(tmp_path):
    out = tmp_path / "guide.md"
    written = formats.write_markdown(GUIDE, FRAMES, output=out, **KW)
    text = out.read_text()
    assert text.startswith('---\ntitle: "Talk: \\"A & B\\""\n')
    assert "\n# Talk" in text and "## TL;DR\n\n- one" in text
    assert "*[0:12](https://youtu.be/abcdefghijk?t=12)*" in text
    assert "![Video frame at 0:30](guide_images/shot-30.jpg)" in text
    assert "{{" not in text  # tags are all resolved, including the screenshot with no frame
    assert (tmp_path / "guide_images" / "shot-30.jpg").read_bytes() == JPEG
    assert written == [out, tmp_path / "guide_images" / "shot-30.jpg"]


def test_markdown_without_frames_makes_no_image_folder(tmp_path):
    out = tmp_path / "g.md"
    assert formats.write_markdown(GUIDE, {}, output=out, **KW) == [out]
    assert not (tmp_path / "g_images").exists()


def test_epub_is_a_valid_package_with_well_formed_xhtml(tmp_path):
    out = tmp_path / "g.epub"
    formats.write_epub(GUIDE, FRAMES, output=out, **KW)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        for part in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml", "OEBPS/guide.xhtml"):
            ET.fromstring(z.read(part))  # raises if not well-formed XML
        guide = z.read("OEBPS/guide.xhtml").decode()
        assert z.read("OEBPS/images/shot-30.jpg") == JPEG
        nav = z.read("OEBPS/nav.xhtml").decode()
    assert '<img src="images/shot-30.jpg"' in guide and "/>" in guide
    assert "<script" not in guide  # sanitised like the PDF
    assert 'id="s1"' in guide and 'id="s2"' in guide
    assert "guide.xhtml#s1" in nav and ">First part<" in nav and ">Second<" in nav
    assert "Talk: &quot;A &amp; B&quot;" in guide or "Talk: &#x27;" in guide or "A &amp; B" in guide


def test_html_is_written_as_is(tmp_path):
    out = tmp_path / "g.html"
    formats.write_html("<p>hi</p>", out)
    assert out.read_text() == "<p>hi</p>"


def _outside_code(text: str) -> str:
    """What a CommonMark viewer treats as prose: no fenced blocks (an unclosed one runs to the end), no code spans."""
    import re

    keep, fence = [], None
    for line in text.splitlines():
        if fence:
            if re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$", line):
                fence = None
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            fence = opener.group(1)
            continue
        keep.append(line)
    return re.sub(r"`[^`\n]*`", "", "\n".join(keep))


def _write(tmp_path, body, title="Plain"):
    out = tmp_path / "e.md"
    formats.write_markdown(f"[[TLDR]]\n- x\n[[/TLDR]]\n\n{body}", {}, output=out, **{**KW, "title": title})
    return out.read_text()


def test_markdown_is_fail_closed_for_html_images_and_links(tmp_path):
    text = _write(
        tmp_path,
        "## Part\n{{AT: 5}}\n<script>alert(1)</script>Hello <!-- hidden --> world <javascript:alert(2)>.\n\n"
        "![track](https://evil.example/pixel.gif) and [click](javascript:alert(1)) and [ok](https://example.com/a)\n\n"
        "[ref]: javascript:alert(3)\n\n[use][ref]\n\nInline `<b>kept</b>` code.\n\n```html\n<div>kept</div>\n```\n",
        title="<b>Hi</b> there",
    )
    prose = _outside_code(text)
    assert "<" not in prose.replace("](", "")  # no raw tag, comment or autolink survives in prose
    assert "![" not in text and "](javascript" not in text and "]: javascript" not in text and "][ref]" not in text
    assert "[ok](https://example.com/a)" in text
    assert "`<b>kept</b>`" in text and "<div>kept</div>" in text
    assert "# <b>Hi</b>" not in text and "&lt;b>Hi" in text


@pytest.mark.parametrize(
    "body",
    [
        # an escaped backtick does not open a code span
        "text \\`<script>alert(1)</script>`",
        # link text that wraps onto the next line
        "[wrapped\nlink](javascript:alert(1))",
        # a comment that hides a fence line from the viewer
        "<!--\n```\n-->\n<script>alert(1)</script>",
        # a code span that would cross list items
        "- a `x\n- b` <script>alert(1)</script>",
        # four-space indent is not a fence
        "    ```\n<script>alert(1)</script>",
        # backtick fence whose info string has a backtick is not a fence
        "``` a`b\n<script>alert(1)</script>",
        # reference definition inside a quote, used later
        "> [r]: javascript:alert(1)\n\n[x][r]",
    ],
)
def test_markdown_known_viewer_differentials_stay_inert(tmp_path, body):
    text = _write(tmp_path, f"## P\n{{{{AT: 1}}}}\n{body}\n")
    prose = _outside_code(text)
    assert "<script" not in prose and "<!--" not in prose
    assert "](javascript" not in text and "]: javascript" not in text and "][r]" not in text


def _viewer_sees_unsafe(text: str, allowed_images: tuple[str, ...] = ()) -> bool:
    """Independent check with a CommonMark parser: raw HTML, images or non-http(s) links?"""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": True})
    md.validateLink = lambda url: True

    def walk(tokens):
        for t in tokens:
            if t.type in {"html_block", "html_inline"} or (t.type == "image" and t.attrGet("src") not in allowed_images):
                return True
            if t.type == "link_open" and not (t.attrGet("href") or "").lower().startswith(("http://", "https://")):
                return True
            if t.children and walk(t.children):
                return True
        return False

    return walk(md.parse(text))


@pytest.mark.parametrize(
    "body",
    [
        "- item\n\n  ```\n  x\n  ```\n<script>alert(1)</script>",
        "1. a\n   ```js\n   code\n<script>alert(1)</script>\n   ```",
        "> ```\n<script>alert(1)</script>\n> ```",
        "- a `x\n  y` <script>alert(1)</script>",
        "a ``x ` y`` <script>alert(1)</script> ``z",
        "- ```\n<img src=x onerror=alert(1)>",
        "<div>\n```\n</div>\n<script>alert(1)</script>",
        "[a]\n\n[a]: javascript:alert(1)",
        "<https://ok.example> and <mailto:x@y.z>",
    ],
)
def test_markdown_survives_a_commonmark_parser_check(tmp_path, body):
    text = _write(tmp_path, f"## P\n{{{{AT: 1}}}}\n{body}\n")
    assert not _viewer_sees_unsafe(text)


def test_stricter_escape_kicks_in_if_the_scanner_misses_something(monkeypatch):
    monkeypatch.setattr(formats, "_rewrite", lambda text: text)  # a scanner that lets everything through
    out = formats.clean_markdown("hi <script>alert(1)</script> [x](javascript:alert(1)) ![i](https://e/x.png)")
    assert not _viewer_sees_unsafe(out) and "&lt;script>" in out


def test_ordinary_guides_keep_code_and_http_links():
    src = "Use [docs](https://example.com) with:\n\n```html\n<div id=\"a\"></div>\n```\n\nand `a<b`."
    assert formats.clean_markdown(src) == src


@pytest.mark.parametrize(
    "body",
    [
        # tags inside an indented fence in a list: the inserted blocks end the fence for the viewer
        "- item\n\n  ```\n  {{AT: 5}}\n<script>alert(1)</script>\n  ```",
        "1. step\n   ```\n   {{SCREENSHOT: 30}}\n<img src=x onerror=alert(1)>\n   ```",
        "> ```\n> {{SCREENSHOT: 30}}\n<script>alert(1)</script>",
        "- a\n  {{AT: 7}}\n  <script>alert(1)</script>",
    ],
)
def test_tag_substitution_cannot_reopen_html(tmp_path, body):
    out = tmp_path / "t.md"
    formats.write_markdown(
        f"[[TLDR]]\n- x\n[[/TLDR]]\n\n## P\n{{{{AT: 1}}}}\n{body}\n", FRAMES, output=out, **KW
    )
    text = out.read_text()
    assert not _viewer_sees_unsafe(text, ("t_images/shot-30.jpg",))


def test_screenshot_names_come_from_the_writer_not_the_model_text(tmp_path):
    out = tmp_path / "t.md"
    # Prose that merely mentions a screenshot file name must not make the writer look for that frame.
    formats.write_markdown("## P\n{{AT: 1}}\nSee shot-9999.jpg for details.\n", FRAMES, output=out, **KW)
    assert out.exists() and not (tmp_path / "t_images").exists()


def test_epub_ignores_screenshot_names_mentioned_in_the_text(tmp_path):
    out = tmp_path / "t.epub"
    formats.write_epub("## P\n{{AT: 1}}\nSee images/shot-9999.jpg and shot-5.jpg.\n\n{{SCREENSHOT: 30}}\n", FRAMES, output=out, **KW)
    with zipfile.ZipFile(out) as z:
        assert sorted(n for n in z.namelist() if n.endswith(".jpg")) == ["OEBPS/images/shot-30.jpg"]
