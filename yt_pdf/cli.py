"""Command-line entry point: `yt-pdf <url>`, `yt-pdf auth [login]`, `yt-pdf model`."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import questionary
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, config, llm, pdf, video
from .errors import YtPdfError

console = Console()
err = Console(stderr=True)

MAX_SHOTS = 20
CUSTOM_MODEL = "Custom model name…"


class DefaultToRun(click.Group):
    """Lets `yt-pdf <url>` work alongside the `auth` and `model` subcommands."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ctx.help_option_names + ["--version"]:
            args = ["run", *args]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultToRun, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="yt-pdf")
def cli() -> None:
    """Turn a YouTube video into a styled PDF reference guide.

    \b
    yt-pdf "<url>" [--dark] [-o out.pdf]   generate a PDF
    yt-pdf auth                            show provider status
    yt-pdf auth login                      add or switch an API key
    yt-pdf model                           choose the model
    """


# --------------------------------------------------------------------------- run


@cli.command(name="run", hidden=True)
@click.argument("url")
@click.option("--dark", is_flag=True, help="Render the dark theme (default: light).")
@click.option("--output", "-o", type=click.Path(dir_okay=False, path_type=Path), help="Output PDF path.")
def run(url: str, dark: bool, output: Path | None) -> None:
    theme = "dark" if dark else "light"
    provider, api_key, model = config.active_settings(config.load_config())
    video_id = video.extract_video_id(url)
    output = output or Path.cwd() / f"{video_id}_{theme}.pdf"

    with console.status("Fetching video details…"):
        info = None
        if video.ffmpeg_available():
            try:
                info = video.get_video_info(url, video_id)
            except YtPdfError as exc:
                err.print(f"[yellow]Warning:[/] {exc}\nContinuing without screenshots.")
        else:
            err.print(
                "[yellow]Warning:[/] ffmpeg not found (macOS: `brew install ffmpeg`, "
                "Debian/Ubuntu: `apt install ffmpeg`). Continuing without screenshots."
            )
    title = info.title if info else video_id
    duration = info.duration if info else None

    with console.status("Fetching transcript…"):
        transcript = video.get_transcript(video_id)
    console.print(f"[green]✓[/] Transcript ready ({len(transcript.splitlines())} segments)")

    max_shots = MAX_SHOTS if info and info.stream_url else 0
    with console.status(f"Writing the guide with {config.PROVIDERS[provider]['label']} / {model}…"):
        guide = llm.generate_guide(provider, api_key, model, title, transcript, max_shots)
    console.print("[green]✓[/] Guide written")

    frames: dict[int, str] = {}
    _, body = pdf.parse_guide(guide)
    times = pdf.find_screenshot_times(body, duration, MAX_SHOTS) if max_shots else []
    if times:
        with Progress(
            TextColumn("Capturing frames"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(), console=console, transient=True,
        ) as progress:
            task = progress.add_task("frames", total=len(times))
            frames = video.grab_frames(info, times, on_done=lambda: progress.advance(task))
        console.print(f"[green]✓[/] Captured {len(frames)}/{len(times)} frames")
        if len(frames) < len(times):
            err.print("[yellow]Warning:[/] some frames could not be read and were skipped.")

    with console.status("Rendering PDF…"):
        doc = pdf.build_html(
            guide, frames, title=title, video_id=video_id, source_url=url, theme=theme,
            channel=info.channel if info else None, duration=duration,
        )
        pdf.render_pdf(doc, output, log=console.print)
    console.print(f"[bold green]Saved[/] {output}")


# --------------------------------------------------------------------------- auth


@cli.group(invoke_without_command=True)
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Show provider status, or manage keys with `auth login`."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = config.load_config()
    active = cfg.get("active_provider")
    if active in config.PROVIDERS:
        entry = cfg["providers"].get(active, {})
        model = entry.get("active_model") or config.PROVIDERS[active]["default_model"]
        console.print(f"Active provider: [bold]{config.PROVIDERS[active]['label']}[/]")
        console.print(f"Active model:    [bold]{model}[/]\n")
    else:
        console.print("[yellow]No active provider.[/] Run `yt-pdf auth login`.\n")

    table = Table(show_header=True, header_style="bold")
    for col in ("Provider", "Status", "API key", "Model"):
        table.add_column(col)
    for name, meta in config.PROVIDERS.items():
        entry = cfg["providers"].get(name) or {}
        key = entry.get("api_key")
        label = f"{meta['label']} *" if name == active else meta["label"]
        if key:
            table.add_row(label, "[green]Configured[/]", config.mask_key(key), entry.get("active_model", "-"))
        else:
            table.add_row(label, "[dim]Not Configured[/]", "-", "-")
    console.print(table)


@auth.command()
def login() -> None:
    """Add an API key and make that provider active."""
    labels = {meta["label"]: name for name, meta in config.PROVIDERS.items()}
    choice = questionary.select("Select a provider:", choices=list(labels)).ask()
    if choice is None:
        raise click.Abort()
    provider = labels[choice]
    key = questionary.password(f"{choice} API key:").ask()
    if key is None:
        raise click.Abort()
    key = key.strip()
    if not key:
        raise YtPdfError("No API key entered.")

    model = config.PROVIDERS[provider]["default_model"]
    with console.status(f"Verifying key with {model}…"):
        llm.verify_key(provider, key, model)

    cfg = config.load_config()
    config.set_provider(cfg, provider, key, model)
    config.save_config(cfg)
    console.print(f"[green]✓[/] {choice} configured ({config.mask_key(key)}), model [bold]{model}[/].")
    console.print(f"Saved to {config.config_path()}")


# --------------------------------------------------------------------------- model


@cli.command()
def model() -> None:
    """Choose the model for the active provider."""
    cfg = config.load_config()
    provider, _, current = config.active_settings(cfg)
    meta = config.PROVIDERS[provider]
    choices = [questionary.Choice(m + ("  (current)" if m == current else ""), value=m) for m in meta["models"]]
    choices.append(questionary.Choice(CUSTOM_MODEL, value=CUSTOM_MODEL))
    picked = questionary.select(f"Model for {meta['label']}:", choices=choices).ask()
    if picked is None:
        raise click.Abort()
    if picked == CUSTOM_MODEL:
        picked = questionary.text("Model name:").ask()
        if picked is None:
            raise click.Abort()
        picked = picked.strip()
        if not picked:
            raise YtPdfError("No model name entered.")
    cfg["providers"][provider]["active_model"] = picked
    config.save_config(cfg)
    console.print(f"[green]✓[/] {meta['label']} model set to [bold]{picked}[/]")


# --------------------------------------------------------------------------- main


def main() -> None:
    try:
        cli(standalone_mode=False)
    except YtPdfError as exc:
        err.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)
    except (click.Abort, KeyboardInterrupt):
        err.print("Aborted.")
        sys.exit(130)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)


if __name__ == "__main__":
    main()
