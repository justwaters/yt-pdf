import pytest
from click.testing import CliRunner

from yt_pdf import cli, config, llm, pdf, pipeline, video
from yt_pdf.errors import YtPdfError

IDS = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
LIST_URL = "https://www.youtube.com/playlist?list=PLxyz"


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "active_settings", lambda cfg: ("openai", "key", "gpt-4o"))
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(
        video, "expand_url",
        lambda url, limit: [video.Entry(i, f"https://www.youtube.com/watch?v={i}", f"Talk {n}") for n, i in enumerate(IDS)][:limit]
        if video.is_collection_url(url) else [video.Entry(video.extract_video_id(url), url)],
    )
    monkeypatch.setattr(
        video, "get_video_info", lambda url, vid: video.VideoInfo(video_id=vid, title=f"Talk {IDS.index(vid)}")
    )

    def snippets(vid):
        calls.append(vid)
        if vid == IDS[1]:
            raise YtPdfError("Subtitles are disabled for this video.")
        return [(0.0, "hello")]

    monkeypatch.setattr(video, "fetch_snippets", snippets)
    monkeypatch.setattr(llm, "generate_guide", lambda *a, **k: ("[[TLDR]]\n- a\n[[/TLDR]]\n\n## H\nbody", False))
    monkeypatch.setattr(pdf, "render_pdf", lambda doc, out, log=None: out.write_text("pdf"))
    return calls, tmp_path


def test_playlist_makes_one_file_per_video_and_survives_a_failure(env):
    calls, tmp = env
    result = CliRunner().invoke(cli.cli, [LIST_URL, "-o", str(tmp / "out"), "-y"])
    assert calls == IDS
    assert sorted(p.name for p in (tmp / "out").iterdir()) == ["Talk-0_light.pdf", "Talk-2_light.pdf"]
    assert "2 saved" in result.output and "1 failed" in result.output and "Talk 1" in result.output
    assert result.exit_code == 1  # a failure in the batch is visible to scripts


def test_limit_caps_a_playlist(env):
    calls, tmp = env
    CliRunner().invoke(cli.cli, [LIST_URL, "-o", str(tmp), "-y", "--limit", "1"])
    assert calls == IDS[:1]


def test_skip_existing_resumes_a_batch(env):
    calls, tmp = env
    (tmp / "Talk-0_light.pdf").write_text("done")
    CliRunner().invoke(cli.cli, [LIST_URL, "-o", str(tmp), "-y", "--skip-existing"])
    assert calls == IDS[1:]
    assert (tmp / "Talk-0_light.pdf").read_text() == "done"


def test_several_urls_and_a_url_file_are_deduplicated(env):
    calls, tmp = env
    listing = tmp / "urls.txt"
    listing.write_text(f"# my list\nhttps://youtu.be/{IDS[0]}\n\nhttps://youtu.be/{IDS[2]}\n")
    CliRunner().invoke(cli.cli, [f"https://youtu.be/{IDS[0]}", "--file", str(listing), "-o", str(tmp / "d")])
    assert calls == [IDS[0], IDS[2]]


def test_same_title_twice_does_not_overwrite(env):
    opts = pipeline.Options(provider="p", api_key="k", model="m")
    taken: set = set()
    from pathlib import Path

    a = pipeline.default_output_path(Path("d"), "Same", "aaaaaaaaaaa", opts, taken)
    b = pipeline.default_output_path(Path("d"), "Same", "bbbbbbbbbbb", opts, taken)
    assert a != b and "bbbbbbbbbbb" in b.name


def test_single_video_failure_keeps_the_plain_error_path(env):
    result = CliRunner().invoke(cli.cli, [f"https://youtu.be/{IDS[1]}", "-o", str(env[1] / "x.pdf")])
    assert result.exit_code == 1 and "Subtitles are disabled" in str(result.exception)


@pytest.mark.parametrize("fmt,ext", [("md", ".md"), ("epub", ".epub"), ("html", ".html")])
def test_format_flag_and_extension_pick_the_writer(env, fmt, ext):
    _, tmp = env
    out = tmp / f"o{ext}"
    r = CliRunner().invoke(cli.cli, [f"https://youtu.be/{IDS[0]}", "-o", str(out)])  # format inferred from -o
    assert r.exit_code == 0, r.output
    assert out.exists() and out.read_bytes()[:2] != b"%P"


def test_style_is_in_the_default_file_name(env):
    _, tmp = env
    CliRunner().invoke(cli.cli, [f"https://youtu.be/{IDS[0]}", "-o", str(tmp), "--style", "summary", "--format", "md"])
    assert (tmp / "Talk-0_summary.md").exists()
