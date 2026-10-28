"""Transcript extraction and frame grabbing (yt-dlp + ffmpeg, no full download)."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from .errors import YtPdfError

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
PREFERRED_LANGS = ["en", "en-US", "en-GB"]
TRANSCRIPT_CHUNK_SECONDS = 20
FRAME_MAX_WIDTH = 1280
PROBE_FRAMES = 2
# Video-only is enough for stills. Prefer a direct https file at 720p (the frames are scaled to 1280
# wide anyway): ffmpeg seeks it with range requests in seconds, whereas the HLS playlists yt-dlp
# otherwise favours time out when seeking into a long video.
STREAM_FORMAT = (
    "bestvideo[height<=720][protocol=https]/bestvideo[height<=1080][protocol=https]"
    "/bestvideo[height<=1080]/best[height<=1080]/best"
)


@dataclass
class VideoInfo:
    video_id: str
    title: str
    duration: int | None = None
    channel: str | None = None
    stream_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def extract_video_id(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS:
        raise YtPdfError(f"Not a YouTube URL: {url!r}")
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
    if not candidate or not _ID_RE.match(candidate):
        raise YtPdfError(f"Could not find a video ID in {url!r}")
    return candidate


# --------------------------------------------------------------------------- transcript


def _snippets(video_id: str) -> list[tuple[float, str]]:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=PREFERRED_LANGS)
    except Exception as exc:
        if type(exc).__name__ != "NoTranscriptFound":
            raise
        # No English track: take whatever exists, translated to English when YouTube allows it.
        listing = api.list(video_id)
        track = next(iter(listing), None)
        if track is None:
            raise
        if track.language_code not in PREFERRED_LANGS and track.is_translatable:
            track = track.translate("en")
        fetched = track.fetch()
    return [(float(s.start), s.text) for s in fetched]


def get_transcript(video_id: str) -> str:
    """Transcript as `[123s] text` lines, grouped into ~20 second chunks to save tokens."""
    try:
        snippets = _snippets(video_id)
    except Exception as exc:
        name = type(exc).__name__
        if name == "TranscriptsDisabled":
            raise YtPdfError("Subtitles are disabled for this video, so there is no transcript.") from exc
        if name == "NoTranscriptFound":
            raise YtPdfError("This video has no usable transcript or captions.") from exc
        if name in {"VideoUnavailable", "VideoUnplayable", "InvalidVideoId"}:
            raise YtPdfError("The video is unavailable (private, removed or region-locked).") from exc
        if name in {"IpBlocked", "RequestBlocked"}:
            raise YtPdfError(
                "YouTube is blocking transcript requests from this IP. Try again later or from another network."
            ) from exc
        if name in {"AgeRestricted"}:
            raise YtPdfError("The video is age-restricted; its transcript cannot be fetched.") from exc
        raise YtPdfError(f"Could not fetch the transcript: {exc}") from exc

    lines: list[str] = []
    chunk_start: float | None = None
    buf: list[str] = []
    for start, text in snippets:
        text = " ".join(text.split())
        if not text:
            continue
        if chunk_start is None:
            chunk_start = start
        elif start - chunk_start >= TRANSCRIPT_CHUNK_SECONDS:
            lines.append(f"[{int(chunk_start)}s] {' '.join(buf)}")
            chunk_start, buf = start, []
        buf.append(text)
    if buf and chunk_start is not None:
        lines.append(f"[{int(chunk_start)}s] {' '.join(buf)}")
    if not lines:
        raise YtPdfError("The transcript was empty.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- yt-dlp


def get_video_info(url: str, video_id: str) -> VideoInfo:
    """Title, duration and a direct video stream URL via yt-dlp (nothing is downloaded)."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": STREAM_FORMAT,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise YtPdfError(f"yt-dlp could not read the video: {exc}") from exc

    fmt = (info.get("requested_formats") or [info])[0]
    headers = {**(info.get("http_headers") or {}), **(fmt.get("http_headers") or {})}
    return VideoInfo(
        video_id=video_id,
        title=info.get("title") or video_id,
        duration=int(info["duration"]) if info.get("duration") else None,
        channel=info.get("channel") or info.get("uploader"),
        stream_url=fmt.get("url"),
        headers=headers,
    )


# --------------------------------------------------------------------------- ffmpeg


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _grab_frame(stream_url: str, headers: dict[str, str], seconds: int) -> str | None:
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += [
        "-ss", str(seconds),
        "-i", stream_url,
        "-frames:v", "1",
        "-vf", f"scale='min({FRAME_MAX_WIDTH},iw)':-2",
        "-q:v", "3",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")


def grab_frames(
    info: VideoInfo, timestamps: list[int], on_done: Callable[[], None] | None = None, workers: int = 4
) -> dict[int, str]:
    """Return {seconds: data-URI}. Timestamps whose frame could not be read are omitted."""
    if not info.stream_url:
        raise YtPdfError("No direct stream URL was available for frame extraction.")

    def one(sec: int) -> tuple[int, str | None]:
        result = _grab_frame(info.stream_url, info.headers, sec)
        if on_done:
            on_done()
        return sec, result

    # Probe the first couple of frames on their own. If the stream can't be read at all, give up now
    # instead of waiting out a timeout for every remaining frame (20 frames took ~8 minutes before).
    frames: dict[int, str] = {}
    probed: set[int] = set()
    for sec in timestamps[:PROBE_FRAMES]:
        probed.add(sec)
        _, uri = one(sec)
        if uri:
            frames[sec] = uri
            break
    if not frames:
        return {}

    rest = [sec for sec in timestamps if sec not in probed]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        frames.update({sec: uri for sec, uri in pool.map(one, rest) if uri})
    return frames
