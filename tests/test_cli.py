import pytest
from click.testing import CliRunner

from yt_pdf import cli, config, llm, pdf, video

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Run the real `run` command with every network/browser step stubbed; capture the HTML."""
    seen: dict = {}
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "active_settings", lambda cfg: ("openai", "key", "gpt-4o"))
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(video, "get_transcript", lambda vid: "[0s] hello")
    monkeypatch.setattr(llm, "generate_guide", lambda *a: ("[[TLDR]]\n- a\n[[/TLDR]]\n\n## H\nbody", False))
    monkeypatch.setattr(pdf, "render_pdf", lambda doc, out, log=None: seen.update(html=doc, out=out))
    seen["tmp"] = tmp_path
    return seen


def invoke(captured, *flags):
    out = captured["tmp"] / "o.pdf"
    result = CliRunner().invoke(cli.cli, [URL, *flags, "-o", str(out)])
    assert result.exit_code == 0, result.output
    return captured["html"]


def test_default_is_normal_text(captured):
    assert "--scale:0.9;" in invoke(captured)


@pytest.mark.parametrize("flag", ["--largeText", "--large-text"])
def test_large_text_flag(captured, flag):
    assert "--scale:1.0;" in invoke(captured, flag)


def test_flag_combines_with_dark(captured):
    html = invoke(captured, "--dark", "--largeText")
    assert "--scale:1.0;" in html and "#0b0f19" in html


def test_flag_is_documented_in_help():
    assert "--largeText" in CliRunner().invoke(cli.cli, ["--help"]).output
