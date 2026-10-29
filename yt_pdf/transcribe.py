"""Local speech-to-text (faster-whisper) for videos that have no captions.

Nothing here runs without the user's say-so: `notice()` describes what will be installed and
downloaded, and the CLI asks before calling `install()` or `transcribe()`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from collections.abc import Callable

from . import video
from .errors import YtPdfError

DEFAULT_MODEL = "base"
# Approximate one-time model downloads.
MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1500, "large-v3": 3100}
PACKAGE = "faster-whisper"
# faster-whisper 1.2.1 passes `metadata_errors` to av.open, which av 19 removed.
PIP_SPECS = [PACKAGE, "av<19"]
PACKAGE_SIZE_MB = 120


def is_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def notice(model: str, duration: int | None) -> list[str]:
    """What transcribing locally involves, one line each, for the user to read before agreeing."""
    lines = [
        "This video has no captions. yt-pdf can transcribe its audio on this computer with Whisper:",
        "  - downloads the video's audio track (audio only, to a temporary folder that is deleted afterwards)",
    ]
    if not is_installed():
        lines.append(f"  - installs the `{PACKAGE}` package into yt-pdf's Python environment (about {PACKAGE_SIZE_MB} MB)")
    lines.append(
        f"  - downloads the Whisper `{model}` model on first use (about {MODEL_SIZES_MB.get(model, '?')} MB, "
        "cached in ~/.cache/huggingface)"
    )
    lines.append("  - runs on your CPU; that can take a good fraction of the video's length" + (
        f" ({video_minutes(duration)})" if duration else ""
    ))
    lines.append("Nothing is sent to an API for this step.")
    return lines


def video_minutes(duration: int) -> str:
    return f"this video is {max(1, round(duration / 60))} min"


def install(log: Callable[[str], None] = print) -> None:
    """pip-install faster-whisper into the running interpreter's environment."""
    log(f"Installing {PACKAGE}…")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *PIP_SPECS],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not is_installed():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise YtPdfError(
            f"Could not install {PACKAGE} automatically ({' '.join(tail)}). "
            f"Install it yourself (for pipx: `pipx inject yt-pdf {PACKAGE}`) and run again."
        )


def run_model(audio_path: str, model_name: str, log: Callable[[str], None] = print) -> list[tuple[float, str]]:
    from faster_whisper import WhisperModel

    log(f"Loading the Whisper {model_name} model (downloads on first use)…")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio_path, vad_filter=True)
    return [(float(seg.start), seg.text) for seg in segments]


def transcribe_video(url: str, model_name: str, log: Callable[[str], None] = print) -> list[tuple[float, str]]:
    with tempfile.TemporaryDirectory(prefix="yt-pdf-audio-") as tmp:
        log("Downloading the audio…")
        audio = video.download_audio(url, tmp)
        snippets = run_model(audio, model_name, log)
    if not snippets:
        raise YtPdfError("Whisper heard no speech in this video.")
    return snippets
