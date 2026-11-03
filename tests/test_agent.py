import base64
import json

import pytest
from click.testing import CliRunner

from yt_pdf import agent, cli, video
from yt_pdf.errors import NoCaptionsError, YtPdfError

URL = "https://www.youtube.com/watch?v=abcdefghijk"
SNIPPETS = [(0.0, "hello"), (10.0, "world"), (25.0, "second chunk"), (70.0, "late")]
INFO = video.VideoInfo(
    video_id="abcdefghijk", title="A Talk", duration=100, channel="Chan", stream_url="https://x.invalid/v",
    chapters=[(0, "Intro"), (65, "Part two")],
)


@pytest.mark.parametrize("text,seconds", [("90", 90), ("90s", 90), ("1:30", 90), ("1:02:03", 3723), (" 0:05 ", 5)])
def test_parse_time(text, seconds):
    assert agent.parse_time(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "1:99", "1:2:3:4", "-5"])
def test_parse_time_rejects_nonsense(bad):
    with pytest.raises(YtPdfError):
        agent.parse_time(bad)


def test_parse_times_keeps_order_and_drops_repeats():
    assert agent.parse_times("1:00, 30,60") == [60, 30]


def test_frame_times_adds_neighbours_inside_the_video():
    assert agent.frame_times([50], 3, 100) == [47, 50, 53]
    assert agent.frame_times([1, 99], 3, 100) == [0, 1, 4, 96, 99]
    assert agent.frame_times([50], 0, 100) == [50]


def test_frame_times_refuses_huge_requests():
    with pytest.raises(YtPdfError):
        agent.frame_times(list(range(0, 500, 10)), 0, None)


def test_select_range_is_half_open():
    chunks = [(0, "a"), (20, "b"), (40, "c")]
    assert agent.select_range(chunks, 20, 40) == [(20, "b")]
    assert agent.select_range(chunks, None, None) == chunks


def test_transcript_command_prints_timestamped_lines(monkeypatch):
    monkeypatch.setattr(video, "fetch_snippets", lambda vid: SNIPPETS)
    result = CliRunner().invoke(cli.cli, ["transcript", URL, "--from", "0:20", "--to", "1:00"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "[25s] second chunk"


def test_transcript_command_json(monkeypatch):
    monkeypatch.setattr(video, "fetch_snippets", lambda vid: SNIPPETS)
    result = CliRunner().invoke(cli.cli, ["transcript", URL, "--json"])
    data = json.loads(result.stdout)
    assert data["source"] == "captions" and data["video_id"] == "abcdefghijk"
    assert data["segments"][0] == {"start_seconds": 0, "start": "0:00", "text": "hello world"}


def test_transcript_without_captions_never_prompts_or_downloads(monkeypatch):
    def none(vid):
        raise NoCaptionsError("no captions")

    monkeypatch.setattr(video, "fetch_snippets", none)
    monkeypatch.setattr(video, "download_audio", lambda *a, **k: pytest.fail("must not download"))
    result = CliRunner().invoke(cli.cli, ["transcript", URL], standalone_mode=False)
    assert isinstance(result.exception, YtPdfError)
    assert "--transcribe" in str(result.exception)


def test_info_command_lists_chapters(monkeypatch):
    monkeypatch.setattr(video, "get_video_info", lambda url, vid: INFO)
    out = CliRunner().invoke(cli.cli, ["info", URL]).stdout
    assert "title: A Talk" in out and "1:05  Part two" in out
    data = json.loads(CliRunner().invoke(cli.cli, ["info", URL, "--json"]).stdout)
    assert data["duration_seconds"] == 100 and data["chapters"][1]["start_seconds"] == 65


def test_frames_command_writes_jpegs_and_prints_paths(monkeypatch, tmp_path):
    uri = "data:image/jpeg;base64," + base64.b64encode(b"jpegbytes").decode()
    monkeypatch.setattr(video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(video, "get_video_info", lambda url, vid: INFO)
    asked = []
    monkeypatch.setattr(video, "grab_frames", lambda info, times, **k: asked.append(times) or {t: uri for t in times})
    out_dir = tmp_path / "shots"
    result = CliRunner().invoke(cli.cli, ["frames", URL, "--at", "0:50", "--around", "3", "-o", str(out_dir), "--json"])
    assert result.exit_code == 0, result.output
    assert asked == [[47, 50, 53]]
    paths = [item["path"] for item in json.loads(result.stdout)]
    assert [p.rsplit("_", 1)[1] for p in paths] == ["47s.jpg", "50s.jpg", "53s.jpg"]
    assert (out_dir / "abcdefghijk_50s.jpg").read_bytes() == b"jpegbytes"


def test_frames_without_ffmpeg_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    result = CliRunner().invoke(cli.cli, ["frames", URL, "--at", "5"], standalone_mode=False)
    assert isinstance(result.exception, YtPdfError) and "ffmpeg" in str(result.exception)
