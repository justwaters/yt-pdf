import re

import pytest

from yt_pdf import pdf

GUIDE = "[[TLDR]]\n- point\n[[/TLDR]]\n\n## Heading\n{{AT: 345}}\nBody text.\n\n{{SCREENSHOT: 5}}\n"
FRAME = "data:image/jpeg;base64,AAAA"


def render(guide=GUIDE, frames=None, **kw):
    return pdf.build_html(
        guide, frames or {}, title=kw.pop("title", "T"), video_id="jNQXAC9IVRw",
        source_url="https://youtu.be/jNQXAC9IVRw", theme=kw.pop("theme", "light"), **kw,
    )


def body_of(html):
    return html.split("<body>")[1]


# --- text size -----------------------------------------------------------------------------------


def test_default_text_is_smaller_than_large():
    assert "--scale:0.9;" in render()
    assert "--scale:1.0;" in render(text_size="large")
    assert pdf.TEXT_SCALES["normal"] < pdf.TEXT_SCALES["large"] == 1.0


def test_every_font_size_scales_with_the_text_size_setting():
    sizes = re.findall(r"font-size:\s*([^;}]+)", pdf.CSS)
    assert sizes
    for size in sizes:
        # Either scaled by --scale, or relative to a scaled parent. Never a fixed pt/px value.
        assert "var(--scale)" in size or size == "inherit" or size.endswith("em"), size


def test_unknown_text_size_is_rejected():
    with pytest.raises(KeyError):
        render(text_size="huge")


# --- markdown repair -----------------------------------------------------------------------------


def test_list_straight_after_a_sentence_gets_a_blank_line():
    fixed = pdf.normalize_markdown("Two files:\n- **Params** file\n- **Run** file")
    assert fixed == "Two files:\n\n- **Params** file\n- **Run** file"


def test_numbered_list_and_table_after_a_sentence_are_separated():
    assert pdf.normalize_markdown("Steps:\n1. one\n2. two") == "Steps:\n\n1. one\n2. two"
    assert pdf.normalize_markdown("Compare:\n| a | b |\n|---|---|\n| 1 | 2 |").startswith("Compare:\n\n| a |")


def test_already_correct_markdown_is_unchanged():
    text = "Intro\n\n- a\n- b\n  - nested\n\n1. x\n2. y\n\n| a |\n|---|\n| 1 |\n\n## H\n- item"
    assert pdf.normalize_markdown(text) == text


def test_code_fences_are_left_alone():
    text = "Run:\n\n```\nsentence\n- not a list\n| not a table |\n```"
    assert pdf.normalize_markdown(text) == text


def test_run_on_list_renders_as_a_real_list():
    body = body_of(render("## H\nTwo files:\n- Params\n- Run\n"))
    assert body.count("<li>") == 2 and "<ul>" in body


# --- timecodes and figures -----------------------------------------------------------------------


def test_heading_gets_a_timecode_that_links_to_the_video():
    body = body_of(render())
    assert '<h2><a class="tc" href="https://youtu.be/jNQXAC9IVRw?t=345">5:45</a>Heading</h2>' in body
    assert "{{" not in body and "@@" not in body


def test_hour_long_timecodes_include_hours():
    assert pdf.format_timestamp(3725) == "1:02:05" and pdf.format_timestamp(65) == "1:05"


def test_timecodes_are_clamped_to_the_video_length():
    body = body_of(render("## H\n{{AT: 9999}}\ntext", duration=60))
    assert "?t=59" in body


def test_a_misplaced_time_tag_is_dropped_not_printed():
    body = body_of(render("Some text\n\n{{AT: 30}}\n\nMore text"))
    assert "{{" not in body and "@@" not in body and "AT:" not in body


def test_screenshot_becomes_a_figure_with_a_timecode_only_when_a_frame_exists():
    with_frame = body_of(render(frames={5: FRAME}))
    assert 'class="shot"' in with_frame and '<a class="tc" href="https://youtu.be/jNQXAC9IVRw?t=5">0:05</a>' in with_frame
    without = body_of(render(frames={}))
    assert 'class="shot"' not in without and "SCREENSHOT" not in without


# --- safety --------------------------------------------------------------------------------------


def test_llm_html_is_sanitized():
    hostile = (
        "## H\n<script>alert(1)</script><img src=http://evil/x.png>"
        '<div style="position:fixed">FAKE</div> <a href="javascript:alert(1)">x</a> [ok](https://ok.example)'
    )
    body = body_of(render(hostile))
    for bad in ("<script", "evil", "position:fixed", "javascript:"):
        assert bad not in body
    assert "https://ok.example" in body


def test_a_hostile_video_title_cannot_break_out_of_the_stylesheet():
    html = render(title='</style><script>alert(1)</script>" } body { display:none')
    assert "</style><script>" not in html and "<script>alert(1)" not in html
    head = html.split("<body>")[0]
    assert head.count("</style>") == 1


def test_csp_allows_only_inline_images_and_fonts():
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', render()).group(1)
    assert "img-src data:" in csp and "font-src data:" in csp and "default-src 'none'" in csp


# --- fonts and page ------------------------------------------------------------------------------


def test_fonts_are_bundled_and_embedded():
    for _, _, _, filename in pdf.FONT_FACES:
        assert (pdf.FONT_DIR / filename).stat().st_size > 1000, filename
    html = render()
    for family in ("Source Serif 4", "IBM Plex Sans", "IBM Plex Mono"):
        assert f'font-family:"{family}"' in html
    assert (pdf.FONT_DIR / "OFL-IBM-Plex.txt").exists() and (pdf.FONT_DIR / "OFL-Source-Serif.md").exists()


def test_running_title_and_page_numbers_are_configured_for_both_themes():
    for theme in ("light", "dark"):
        css = render(theme=theme, title="My Talk").split("<body>")[0]
        assert '@top-left{content:"My Talk"' in css
        assert 'counter(page) " / " counter(pages)' in css
        assert "@page :first{@top-left{content:none;}}" in css
    assert "#0b0f19" in render(theme="dark") and "#ffffff" in render(theme="light")


def test_long_titles_are_shortened_in_the_running_header():
    assert pdf.format_timestamp(0) == "0:00"
    assert pdf._css_string("x" * 200).count("x") < 100


# --- guide parsing -------------------------------------------------------------------------------


def test_screenshot_times_are_clamped_and_deduplicated():
    body = "{{SCREENSHOT: 5}} {{SCREENSHOT: 5}} {{SCREENSHOT: 999}}"
    assert pdf.find_screenshot_times(body, duration=19, limit=20) == [5, 18]


def test_parse_guide_unwraps_a_markdown_fence():
    tldr, body = pdf.parse_guide("```markdown\n[[TLDR]]\n- a\n[[/TLDR]]\n\n## H\n```")
    assert tldr == "- a" and body == "## H"


def test_lead_in_paragraphs_stay_with_what_they_introduce():
    assert "p:has(+ table)" in pdf.CSS and "break-after: avoid" in pdf.CSS
