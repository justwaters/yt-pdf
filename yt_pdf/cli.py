"""Command-line entry point: `yt-pdf <url>`, `yt-pdf auth [login]`, `yt-pdf model`."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click
import questionary
from rich.console import Console
from rich.table import Table

from . import __version__, config, formats, llm, transcribe, updater, video
from .errors import YtPdfError
from .pipeline import Consent, Options, default_output_path, is_interactive, make_guide

console = Console()
err = Console(stderr=True)

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
    yt-pdf "<url>" [--format pdf|html|md|epub] [--style full|summary|notes|tutorial]
                   [--dark] [--largeText] [-o out]      generate a guide
    yt-pdf "<playlist or channel url>" [--limit N]      one guide per video
    yt-pdf auth                            show provider status
    yt-pdf auth login                      add or switch an API key
    yt-pdf model                           choose the model
    yt-pdf update                          update to the latest release
    """


# --------------------------------------------------------------------------- run


def _read_url_file(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise YtPdfError(f"Could not read {path}: {exc}") from exc
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _collect(urls: list[str], limit: int) -> tuple[list[video.Entry], bool]:
    """Every distinct video behind the URLs, and whether any URL was a playlist or channel."""
    entries: list[video.Entry] = []
    from_collection = False
    for url in urls:
        if video.is_collection_url(url):
            from_collection = True
            with console.status(f"Reading {url}…"):
                found = video.expand_url(url, limit)
            console.print(f"[green]✓[/] {len(found)} videos in {url} (limit {limit})")
        else:
            found = video.expand_url(url, limit)
        entries += [e for e in found if all(e.video_id != x.video_id for x in entries)]
    return entries, from_collection


@cli.command(name="run", hidden=True)
@click.argument("urls", nargs=-1)
@click.option("--file", "url_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Read video, playlist or channel URLs from a file, one per line.")
@click.option("--format", "-f", "fmt", type=click.Choice(formats.FORMATS), default=None,
              help="Output format (default: pdf, or what the -o extension says).")
@click.option("--style", type=click.Choice(list(llm.STYLES)), default=llm.DEFAULT_STYLE,
              help="; ".join(f"{k}: {v.summary}" for k, v in llm.STYLES.items()))
@click.option("--dark", is_flag=True, help="Render the dark theme (default: light). PDF and HTML only.")
@click.option("--largeText", "--large-text", "large_text", is_flag=True, help="Use larger text (default: normal).")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              help="Output file, or a folder when making several guides.")
@click.option("--transcribe/--no-transcribe", "whisper", default=None,
              help="No captions? Transcribe locally with Whisper. Default: describe it and ask first.")
@click.option("--whisper-model", type=click.Choice(list(transcribe.MODEL_SIZES_MB)), default=transcribe.DEFAULT_MODEL,
              show_default=True, help="Whisper model size for local transcription.")
@click.option("--limit", type=click.IntRange(min=1), default=25, show_default=True,
              help="Most videos to take from a playlist or channel.")
@click.option("--skip-existing", is_flag=True, help="Skip videos whose output file already exists.")
@click.option("--yes", "-y", is_flag=True, help="Don't ask before processing a playlist or channel.")
def run(urls: tuple[str, ...], url_file: Path | None, fmt: str | None, style: str, dark: bool, large_text: bool,
        output: Path | None, whisper: bool | None, whisper_model: str, limit: int, skip_existing: bool,
        yes: bool) -> None:
    all_urls = list(urls) + (_read_url_file(url_file) if url_file else [])
    if not all_urls:
        raise click.UsageError("Give at least one YouTube URL (or --file).")
    cfg = config.load_config()
    provider, api_key, model = config.active_settings(cfg)
    entries, from_collection = _collect(all_urls, limit)
    batch = len(entries) > 1

    if fmt is None:
        fmt = formats.format_for_path(output) if output and not batch else None
        fmt = fmt or "pdf"
    opts = Options(
        provider=provider, api_key=api_key, model=model, theme="dark" if dark else "light",
        text_size="large" if large_text else "normal", style=style, fmt=fmt, whisper=whisper,
        whisper_model=whisper_model, base_url=config.active_base_url(cfg),
    )

    if batch and from_collection and not yes and is_interactive():
        label = config.PROVIDERS[provider]["label"]
        if not questionary.confirm(
            f"Make {len(entries)} guides ({fmt}, {style})? Each one is a separate {label} request.", default=True
        ).ask():
            raise click.Abort()

    consent = Consent()
    taken: set[Path] = set()
    results: list[tuple[video.Entry, Path | None, str | None]] = []
    for i, entry in enumerate(entries, 1):
        if batch:
            console.rule(f"[bold]{i}/{len(entries)}[/] {entry.title or entry.video_id}")
        # An explicit single-file -o is used as given; otherwise the name comes from the title.
        target = output if output and not batch and not output.is_dir() else None
        directory = output if batch or (output and output.is_dir()) else Path.cwd()
        try:
            written = make_guide(entry, target, directory, opts, consent, console, err, taken, skip_existing)
            if written is None:
                results.append((entry, Path(), "skipped"))
                continue
            console.print(f"[bold green]Saved[/] {written[0]}")
            results.append((entry, written[0], None))
        except YtPdfError as exc:
            if not batch:
                raise
            err.print(f"[bold red]Error:[/] {exc}")
            results.append((entry, None, str(exc)))

    if batch:
        _print_summary(results)
        if any(path is None for _, path, _ in results):
            sys.exit(1)


def _print_summary(results: list[tuple[video.Entry, Path | None, str | None]]) -> None:
    done = sum(1 for _, p, note in results if p is not None and note is None)
    skipped = sum(1 for _, _, note in results if note == "skipped")
    failed = [(e, note) for e, p, note in results if p is None]
    console.rule("[bold]Batch finished")
    console.print(f"[green]{done} saved[/], {skipped} skipped, [red]{len(failed)} failed[/]")
    for entry, note in failed:
        console.print(f"  [red]✗[/] {entry.title or entry.video_id}: {note}")


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
        if entry.get("base_url"):
            console.print(f"Endpoint:        [bold]{entry['base_url']}[/]")
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
            model_cell = entry.get("active_model", "-")
            if entry.get("base_url"):
                model_cell += f"  ({entry['base_url']})"
            table.add_row(label, "[green]Configured[/]", config.mask_key(key), model_cell)
        else:
            table.add_row(label, "[dim]Not Configured[/]", "-", "-")
    console.print(table)


def _pick_model(models: list[str], current: str | None = None) -> str:
    """Choose from `models`, or type a name. With no list, just ask for the name."""
    if models:
        choices = [questionary.Choice(m + ("  (current)" if m == current else ""), value=m) for m in models]
        choices.append(questionary.Choice(CUSTOM_MODEL, value=CUSTOM_MODEL))
        picked = questionary.select("Model:", choices=choices).ask()
        if picked is None:
            raise click.Abort()
        if picked != CUSTOM_MODEL:
            return picked
    picked = questionary.text("Model name:", default=current or "").ask()
    if picked is None:
        raise click.Abort()
    picked = picked.strip()
    if not picked:
        raise YtPdfError("No model name entered.")
    return picked


@auth.command()
def login() -> None:
    """Add an API key and make that provider active."""
    labels = {meta["label"]: name for name, meta in config.PROVIDERS.items()}
    choice = questionary.select("Select a provider:", choices=list(labels)).ask()
    if choice is None:
        raise click.Abort()
    provider = labels[choice]
    cfg = config.load_config()
    base_url = None
    if provider == config.CUSTOM:
        saved = (cfg["providers"].get(provider) or {}).get("base_url") or ""
        raw = questionary.text(
            f"API base URL (e.g. {config.EXAMPLE_URL} for Ollama):", default=saved
        ).ask()
        if raw is None:
            raise click.Abort()
        base_url = config.normalize_base_url(raw)
        if config.is_insecure_remote(base_url):
            err.print(
                f"[yellow]Warning:[/] {base_url} is plain http to another machine, so the API key and the "
                "video transcript will cross the network unencrypted."
            )
        key = questionary.password("API key (leave empty if the server needs none):").ask()
    else:
        key = questionary.password(f"{choice} API key:").ask()
    if key is None:
        raise click.Abort()
    key = key.strip()
    if not key and provider != config.CUSTOM:
        raise YtPdfError("No API key entered.")
    key = key or config.NO_KEY

    if provider == config.CUSTOM:
        with console.status("Asking the server for its models…"):
            available = llm.list_models(key, base_url)
        model = _pick_model(available)
    else:
        model = config.PROVIDERS[provider]["default_model"]
    with console.status(f"Verifying with {model}…"):
        llm.verify_key(provider, key, model, base_url)

    config.set_provider(cfg, provider, key, model, base_url)
    config.save_config(cfg)
    shown = "no key" if key == config.NO_KEY else config.mask_key(key)
    console.print(f"[green]✓[/] {choice} configured ({shown}), model [bold]{model}[/].")
    if base_url:
        console.print(f"Endpoint: {base_url}")
    console.print(f"Saved to {config.config_path()}")


# --------------------------------------------------------------------------- model


@cli.command()
def model() -> None:
    """Choose the model for the active provider."""
    cfg = config.load_config()
    provider, key, current = config.active_settings(cfg)
    meta = config.PROVIDERS[provider]
    if provider == config.CUSTOM:
        with console.status("Asking the server for its models…"):
            available = llm.list_models(key, config.active_base_url(cfg))
        picked = _pick_model(available, current)
    else:
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


# --------------------------------------------------------------------------- update


@cli.command()
@click.option("--check", is_flag=True, help="Only say whether a newer version exists.")
def update(check: bool) -> None:
    """Update yt-pdf to the latest release."""
    latest = updater.latest_version()
    if updater.parse_version(latest) <= updater.parse_version(__version__):
        console.print(f"[green]✓[/] yt-pdf {__version__} is up to date.")
        return
    console.print(f"yt-pdf {__version__} → [bold]{latest}[/] is available.")
    method = updater.install_method()
    command = updater.upgrade_command(method, latest)
    if method == "dev":
        console.print("This is a development checkout: `git pull` and `pip install -e .` to update it.")
        return
    if check:
        console.print(f"Run `yt-pdf update` to install it ([dim]{' '.join(command)}[/dim]).")
        return
    if shutil.which(command[0]) is None:
        raise YtPdfError(f"`{command[0]}` was not found. Update it yourself with: {' '.join(command)}")
    console.print(f"Updating with [bold]{method}[/]: [dim]{' '.join(command)}[/]")
    code = subprocess.run(command).returncode
    if code != 0:
        raise YtPdfError(f"The update command failed (exit {code}). Run it yourself: {' '.join(command)}")
    console.print(f"[green]✓[/] Updated. Run `yt-pdf --version` to confirm.")


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
