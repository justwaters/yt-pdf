import pytest
from click.testing import CliRunner

from yt_pdf import cli, config, llm, pdf, pipeline, transcribe, video
from yt_pdf.errors import NoCaptionsError

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture
def env(monkeypatch, tmp_path):
    ev: list[str] = []
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "active_settings", lambda cfg: ("openai", "key", "gpt-4o"))
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(
        video, "get_video_info", lambda url, vid: video.VideoInfo(video_id=vid, title="T", duration=600)
    )

    def no_captions(vid):
        ev.append("fetch")
        raise NoCaptionsError("This video has no usable transcript or captions.")

    monkeypatch.setattr(video, "fetch_snippets", no_captions)
    monkeypatch.setattr(transcribe, "is_installed", lambda: True)
    monkeypatch.setattr(transcribe, "install", lambda log=print: ev.append("install"))
    monkeypatch.setattr(
        transcribe, "transcribe_video", lambda url, model, log=print: ev.append(f"transcribe:{model}") or [(0.0, "hi")]
    )
    monkeypatch.setattr(llm, "generate_guide", lambda *a, **k: ("[[TLDR]]\n- a\n[[/TLDR]]\n\n## H\nbody", False))
    monkeypatch.setattr(pdf, "render_pdf", lambda doc, out, log=None: ev.append("render"))
    return ev, tmp_path / "o.pdf"


def run(env, *flags):
    ev, out = env
    return CliRunner().invoke(cli.cli, [URL, *flags, "-o", str(out)])


def test_without_consent_nothing_is_downloaded_or_installed(env):
    result = run(env)  # not a terminal, no flag
    assert result.exit_code == 1
    assert "--transcribe" in str(result.exception)
    assert "downloads the video's audio" in result.output  # the notice was shown
    assert env[0] == ["fetch"]


def test_no_transcribe_never_describes_or_runs_whisper(env):
    result = run(env, "--no-transcribe")
    assert result.exit_code == 1
    assert "downloads the video's audio" not in result.output
    assert env[0] == ["fetch"]


def test_flag_still_prints_the_notice_before_doing_anything(env, monkeypatch):
    ev = env[0]
    real_notice = transcribe.notice
    monkeypatch.setattr(transcribe, "notice", lambda *a: ev.append("notice") or real_notice(*a))
    result = run(env, "--transcribe", "--whisper-model", "small")
    assert result.exit_code == 0, result.output
    assert ev == ["fetch", "notice", "transcribe:small", "render"]


def test_missing_package_is_installed_only_after_consent(env, monkeypatch):
    monkeypatch.setattr(transcribe, "is_installed", lambda: False)
    result = run(env, "--transcribe")
    assert result.exit_code == 0, result.output
    assert env[0] == ["fetch", "install", "transcribe:base", "render"]
    assert "installs the `faster-whisper` package" in result.output


def test_interactive_no_answer_stops(env, monkeypatch):
    class Q:
        def ask(self):
            return False

    monkeypatch.setattr(pipeline, "is_interactive", lambda: True)
    monkeypatch.setattr(pipeline.questionary, "confirm", lambda *a, **k: Q())
    result = run(env)
    assert result.exit_code == 1 and "Skipped local transcription" in str(result.exception)
    assert env[0] == ["fetch"]


def test_notice_lists_what_will_happen():
    text = "\n".join(transcribe.notice("small", 1800))
    assert "audio" in text and "485 MB" in text and "30 min" in text and "Nothing is sent to an API" in text


def test_install_pins_av_below_19():
    # faster-whisper 1.2.1 passes `metadata_errors` to av.open, which av 19 removed; found in a real run.
    assert "av<19" in transcribe.PIP_SPECS and transcribe.PACKAGE in transcribe.PIP_SPECS
    assert "av<19" in open("pyproject.toml").read()
