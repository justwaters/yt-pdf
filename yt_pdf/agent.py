"""Building blocks for AI agents: video facts, a timestamped transcript and still frames, with no model key.

The commands built on these print data to stdout and everything else (progress, notices) to stderr,
so an agent can read the output directly or parse it as JSON.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from . import video
from .errors import YtPdfError

_CLOCK_RE = re.compile(r"^\d+(?::[0-5]?\d){1,2}$")
MAX_FRAMES = 40


def parse_time(text: str) -> int:
    """Seconds from `90`, `90s`, `1:30` or `1:02:03`."""
    raw = text.strip().lower().removesuffix("s")
    if raw.isdigit():
        return int(raw)
    if _CLOCK_RE.match(raw):
        seconds = 0
        for part in raw.split(":"):
            seconds = seconds * 60 + int(part)
        return seconds
    raise YtPdfError(f"Not a time: {text!r}. Use seconds (90), minutes:seconds (1:30) or h:mm:ss (1:02:03).")


def parse_times(spec: str) -> list[int]:
    """Comma-separated times, in the order given, without repeats."""
    seen: list[int] = []
    for part in spec.split(","):
        if part.strip():
            sec = parse_time(part)
            if sec not in seen:
                seen.append(sec)
    if not seen:
        raise YtPdfError("No times given.")
    return seen


def clock(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def video_facts(info: video.VideoInfo, url: str) -> dict:
    return {
        "video_id": info.video_id,
        "url": url,
        "title": info.title,
        "channel": info.channel,
        "duration_seconds": info.duration,
        "duration": clock(info.duration) if info.duration else None,
        "chapters": [{"start_seconds": start, "start": clock(start), "title": title} for start, title in info.chapters],
    }


def select_range(chunks: list[tuple[int, str]], start: int | None, end: int | None) -> list[tuple[int, str]]:
    """Chunks that begin at or after `start` and before `end` (either may be None)."""
    lo = start or 0
    return [(t, text) for t, text in chunks if t >= lo and (end is None or t < end)]


def frame_times(times: list[int], around: int, duration: int | None) -> list[int]:
    """The requested seconds, plus one candidate `around` seconds either side of each, kept inside the video."""
    top = max(duration - 1, 0) if duration else None
    out: list[int] = []
    for sec in times:
        for t in (sec - around, sec, sec + around) if around else (sec,):
            t = max(t, 0)
            if top is not None:
                t = min(t, top)
            if t not in out:
                out.append(t)
    if len(out) > MAX_FRAMES:
        raise YtPdfError(f"That is {len(out)} frames; ask for at most {MAX_FRAMES} at a time.")
    return out


def save_frames(frames: dict[int, str], directory: Path, video_id: str) -> list[tuple[int, Path]]:
    """Write each data-URI still to `directory` as <video_id>_<seconds>s.jpg, in time order."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[tuple[int, Path]] = []
    for sec in sorted(frames):
        path = directory / f"{video_id}_{sec}s.jpg"
        path.write_bytes(base64.b64decode(frames[sec].split(",", 1)[1]))
        written.append((sec, path))
    return written
