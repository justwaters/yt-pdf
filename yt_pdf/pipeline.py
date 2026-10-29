"""One video in, one guide file out. Shared by the single-video and batch paths of the CLI."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import questionary
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from . import config, formats, llm, pdf, transcribe, video
from .errors import NoCaptionsError, YtPdfError

MAX_SHOTS = 20


@dataclass
class Options:
    provider: str
    api_key: str
    model: str
    theme: str = "light"
    text_size: str = "normal"
    style: str = llm.DEFAULT_STYLE
    fmt: str = "pdf"
    whisper: bool | None = None  # True: agreed up front, False: never, None: ask
    whisper_model: str = transcribe.DEFAULT_MODEL


@dataclass
class Consent:
    """The answer to "transcribe locally?", kept so a batch asks once, not once per video."""

    granted: bool | None = None


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def default_output_path(
    directory: Path, title: str, video_id: str, opts: Options, taken: set[Path] | None = None
) -> Path:
    stem = video.filename_stem(title, video_id)
    if opts.style != llm.DEFAULT_STYLE:
        stem += f"_{opts.style}"
    if opts.fmt in ("pdf", "html"):
        stem += f"_{opts.theme}"
    path = directory / f"{stem}{formats.EXTENSIONS[opts.fmt]}"
    if taken is not None:
        if path in taken:  # two videos with the same title in one batch
            path = directory / f"{stem}_{video_id}{formats.EXTENSIONS[opts.fmt]}"
        taken.add(path)
    return path


def _transcript(
    entry: video.Entry, info: video.VideoInfo | None, opts: Options, consent: Consent, console: Console, err: Console
) -> str:
    try:
        with console.status("Fetching transcript…"):
            snippets = video.fetch_snippets(entry.video_id)
    except NoCaptionsError as exc:
        snippets = _whisper_fallback(exc, entry, info, opts, consent, console, err)
    return video.format_transcript(snippets)


def _whisper_fallback(
    exc: NoCaptionsError, entry: video.Entry, info: video.VideoInfo | None, opts: Options,
    consent: Consent, console: Console, err: Console,
) -> list[tuple[float, str]]:
    hint = "Pass --transcribe to transcribe the audio locally with Whisper."
    if opts.whisper is False or consent.granted is False:
        raise YtPdfError(f"{exc} ({hint})") from exc

    if consent.granted is None:
        # Say what is about to happen before doing any of it.
        for line in transcribe.notice(opts.whisper_model, info.duration if info else None):
            err.print(f"[yellow]{line}[/]" if line.startswith("This video") else line, highlight=False)
        if opts.whisper is True:
            consent.granted = True
        elif is_interactive():
            consent.granted = bool(questionary.confirm("Transcribe locally with Whisper?", default=False).ask())
        else:
            raise YtPdfError(f"{exc} ({hint})") from exc
        if not consent.granted:
            raise YtPdfError(f"{exc} (Skipped local transcription.)") from exc
    else:
        err.print(f"No captions; transcribing locally with Whisper {opts.whisper_model}.")

    if not transcribe.is_installed():
        with console.status(f"Installing {transcribe.PACKAGE}…"):
            transcribe.install()
    with console.status("Transcribing locally (this can take a while)…"):
        snippets = transcribe.transcribe_video(entry.url, opts.whisper_model, log=lambda m: None)
    console.print("[green]✓[/] Transcribed locally")
    return snippets


def make_guide(
    entry: video.Entry,
    output: Path | None,
    directory: Path,
    opts: Options,
    consent: Consent,
    console: Console,
    err: Console,
    taken: set[Path] | None = None,
    skip_existing: bool = False,
) -> list[Path] | None:
    """Build one guide. Returns the files written (the guide first), or None if it was skipped.

    `output` is the exact file to write; when None the name comes from the video's title inside
    `directory`.
    """
    video_id, url = entry.video_id, entry.url
    if skip_existing and output is None and entry.title:
        early = default_output_path(directory, entry.title, video_id, opts)
        if early.exists():
            console.print(f"[dim]Skipped, already exists: {early}[/]")
            return None

    ffmpeg = video.ffmpeg_available()
    if not ffmpeg:
        err.print(
            "[yellow]Warning:[/] ffmpeg not found (macOS: `brew install ffmpeg`, "
            "Debian/Ubuntu: `apt install ffmpeg`). Continuing without screenshots."
        )
    with console.status("Fetching video details…"):
        info = None
        try:
            info = video.get_video_info(url, video_id)
        except YtPdfError as exc:
            err.print(f"[yellow]Warning:[/] {exc}\nContinuing without screenshots; the title falls back to the video ID.")
    title = info.title if info else (entry.title or video_id)
    duration = info.duration if info else None
    output = output or default_output_path(directory, title, video_id, opts, taken)
    if skip_existing and output.exists():
        console.print(f"[dim]Skipped, already exists: {output}[/]")
        return None

    transcript = _transcript(entry, info, opts, consent, console, err)
    console.print(f"[green]✓[/] Transcript ready ({len(transcript.splitlines())} segments)")

    style = llm.STYLES[opts.style]
    max_shots = min(MAX_SHOTS, style.max_shots) if ffmpeg and info and info.stream_url else 0
    with console.status(f"Writing the guide with {config.PROVIDERS[opts.provider]['label']} / {opts.model}…"):
        guide, truncated = llm.generate_guide(
            opts.provider, opts.api_key, opts.model, title, transcript, max_shots, style=opts.style
        )
    console.print("[green]✓[/] Guide written")
    if truncated:
        err.print("[yellow]Warning:[/] the model hit its output limit, so the guide may end early. Try another model.")

    frames: dict[int, str] = {}
    _, body = pdf.parse_guide(guide)
    times = pdf.find_screenshot_times(body, duration, max_shots) if max_shots else []
    if times:
        with Progress(
            TextColumn("Capturing frames"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(), console=console, transient=True,
        ) as progress:
            task = progress.add_task("frames", total=len(times))
            frames = video.grab_frames(info, times, on_done=lambda: progress.advance(task))
        console.print(f"[green]✓[/] Captured {len(frames)}/{len(times)} frames")
        if not frames:
            err.print("[yellow]Warning:[/] the video stream could not be read, so the guide has no screenshots.")
        elif len(frames) < len(times):
            err.print("[yellow]Warning:[/] some frames could not be read and were skipped.")

    output.parent.mkdir(parents=True, exist_ok=True)
    channel = info.channel if info else None
    common = dict(title=title, video_id=video_id)
    with console.status(f"Writing {opts.fmt}…"):
        if opts.fmt in ("pdf", "html"):
            doc = pdf.build_html(
                guide, frames, source_url=url, theme=opts.theme, channel=channel, duration=duration,
                text_size=opts.text_size, **common,
            )
            if opts.fmt == "pdf":
                pdf.render_pdf(doc, output, log=console.print)
            else:
                formats.write_html(doc, output)
            return [output]
        kwargs = dict(source_url=url, channel=channel, duration=duration, output=output, **common)
        if opts.fmt == "md":
            return formats.write_markdown(guide, frames, **kwargs)
        formats.write_epub(guide, frames, **kwargs)
        return [output]
