import pytest

from yt_pdf import video
from yt_pdf.errors import YtPdfError

INFO = video.VideoInfo(video_id="abcdefghijk", title="t", stream_url="https://example.invalid/v.mp4")


def test_stream_format_prefers_direct_https_over_hls():
    first_choice = video.STREAM_FORMAT.split("/")[0]
    assert "protocol=https" in first_choice
    # An HLS-only video must still resolve to something, so there is an unrestricted fallback.
    assert video.STREAM_FORMAT.endswith("/best")


def test_unreadable_stream_gives_up_after_the_probe_frames(monkeypatch):
    calls = []
    monkeypatch.setattr(video, "_grab_frame", lambda url, headers, sec: calls.append(sec))  # always None
    assert video.grab_frames(INFO, list(range(0, 200, 10))) == {}
    assert len(calls) == video.PROBE_FRAMES


def test_readable_stream_grabs_every_frame(monkeypatch):
    monkeypatch.setattr(video, "_grab_frame", lambda url, headers, sec: f"data:{sec}")
    got = video.grab_frames(INFO, [5, 50, 500])
    assert got == {5: "data:5", 50: "data:50", 500: "data:500"}


def test_probe_tolerates_one_bad_frame(monkeypatch):
    monkeypatch.setattr(video, "_grab_frame", lambda url, headers, sec: None if sec == 5 else f"data:{sec}")
    assert set(video.grab_frames(INFO, [5, 50, 500])) == {50, 500}


def test_progress_callback_fires_once_per_attempt(monkeypatch):
    monkeypatch.setattr(video, "_grab_frame", lambda url, headers, sec: "x")
    ticks = []
    video.grab_frames(INFO, [1, 2, 3, 4], on_done=lambda: ticks.append(1))
    assert len(ticks) == 4


def test_missing_stream_url_is_an_error():
    with pytest.raises(YtPdfError):
        video.grab_frames(video.VideoInfo(video_id="abcdefghijk", title="t"), [1])


@pytest.mark.parametrize(
    "url", ["https://youtu.be/dQw4w9WgXcQ?t=3", "youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ"]
)
def test_video_id_formats(url):
    assert video.extract_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["https://example.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=short"])
def test_bad_urls_are_rejected(url):
    with pytest.raises(YtPdfError):
        video.extract_video_id(url)


def test_filename_stem_uses_the_title_and_stays_safe():
    assert video.filename_stem("Let's build GPT: from scratch, in code", "abcdefghijk") == "Let-s-build-GPT-from-scratch-in-code"
    assert video.filename_stem("../../etc/passwd", "abcdefghijk") == "etc-passwd"
    assert video.filename_stem("???", "abcdefghijk") == "abcdefghijk"
    assert len(video.filename_stem("x" * 300, "abcdefghijk")) <= 80


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://www.youtube.com/@karpathy", True),
        ("https://www.youtube.com/@karpathy/videos", True),
        ("https://www.youtube.com/channel/UC123", True),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", False),
        ("https://youtu.be/dQw4w9WgXcQ", False),
    ],
)
def test_playlist_and_channel_urls_are_recognised(url, expected):
    assert video.is_collection_url(url) is expected


def test_missing_captions_is_a_distinct_error(monkeypatch):
    class TranscriptsDisabled(Exception):
        pass

    def boom(vid):
        raise TranscriptsDisabled()

    monkeypatch.setattr(video, "_snippets", boom)
    with pytest.raises(video.NoCaptionsError):
        video.fetch_snippets("abcdefghijk")


def test_format_transcript_groups_into_chunks():
    text = video.format_transcript([(0.0, "a"), (5.0, "b"), (25.0, "c")])
    assert text == "[0s] a b\n[25s] c"
