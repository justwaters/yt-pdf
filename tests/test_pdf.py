import re

import pytest

from yt_pdf import pdf

GUIDE = "[[TLDR]]\n- point\n[[/TLDR]]\n\n## Heading\nBody text.\n\n{{SCREENSHOT: 5}}\n"


def render(guide=GUIDE, frames=None, **kw):
    return pdf.build_html(
        guide, frames or {}, title="T", video_id="jNQXAC9IVRw", source_url="https://youtu.be/jNQXAC9IVRw",
        theme=kw.pop("theme", "light"), **kw,
    )


def test_default_text_is_smaller_than_large():
    assert "--scale:0.9;" in render()
    assert "--scale:1.0;" in render(text_size="large")
    assert pdf.TEXT_SCALES["normal"] < pdf.TEXT_SCALES["large"] == 1.0


def test_large_text_keeps_the_original_font_sizes():
    # Large is scale 1.0 over the original point sizes, so these must not drift.
    for size in ("10.5pt", "22pt", "15pt", "12pt", "9.5pt", "9pt", "8.5pt"):
        assert f"calc({size} * var(--scale))" in pdf.CSS


def test_every_font_size_is_scalable():
    sizes = re.findall(r"font-size:\s*([^;}]+)", pdf.CSS)
    assert sizes and all(s.startswith("calc(") or s == "inherit" for s in sizes)


def test_unknown_text_size_is_rejected():
    with pytest.raises(KeyError):
        render(text_size="huge")


def test_screenshot_tag_becomes_a_figure_only_when_a_frame_exists():
    assert 'class="shot"' in render(frames={5: "data:image/jpeg;base64,AAAA"})
    html = render(frames={})
    assert 'class="shot"' not in html and "SCREENSHOT" not in html


def test_llm_html_is_sanitized():
    hostile = (
        "## H\n<script>alert(1)</script><img src=http://evil/x.png>"
        '<div style="position:fixed">FAKE</div> <a href="javascript:alert(1)">x</a> [ok](https://ok.example)'
    )
    body = render(hostile).split("<body>")[1]
    for bad in ("<script", "evil", "position:fixed", "javascript:"):
        assert bad not in body
    assert "https://ok.example" in body


def test_screenshot_times_are_clamped_and_deduplicated():
    body = "{{SCREENSHOT: 5}} {{SCREENSHOT: 5}} {{SCREENSHOT: 999}}"
    assert pdf.find_screenshot_times(body, duration=19, limit=20) == [5, 18]


def test_parse_guide_unwraps_a_markdown_fence():
    tldr, body = pdf.parse_guide("```markdown\n[[TLDR]]\n- a\n[[/TLDR]]\n\n## H\n```")
    assert tldr == "- a" and body == "## H"
