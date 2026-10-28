"""Config loading/saving and the static provider catalogue."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import YtPdfError

# Display order matters: it is the order shown in prompts and tables.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "anthropic": {
        "label": "Anthropic",
        "default_model": "claude-sonnet-5-5",
        "models": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "default_model": "anthropic/claude-sonnet-5.5",
        "models": [
            "anthropic/claude-sonnet-5.5",
            "meta-llama/llama-3.3-70b-instruct",
            "google/gemini-2.5-flash",
        ],
    },
    "nvidia": {
        "label": "Nvidia NIM",
        "default_model": "meta/llama-3.3-70b-instruct",
        "models": ["meta/llama-3.3-70b-instruct", "nvidia/nemotron-4-340b-instruct"],
    },
    "gemini": {
        "label": "Google Gemini",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
    },
}


def config_dir() -> Path:
    override = os.environ.get("YT_PDF_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".config" / "yt-pdf"


def config_path() -> Path:
    return config_dir() / "config.json"


def _empty() -> dict[str, Any]:
    return {"active_provider": None, "providers": {}}


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise YtPdfError(
            f"Could not read {path} ({exc}). Fix or delete it, then run `yt-pdf auth login`."
        ) from exc
    if not isinstance(data, dict):
        raise YtPdfError(f"{path} is malformed. Delete it and run `yt-pdf auth login`.")
    data.setdefault("active_provider", None)
    if not isinstance(data.get("providers"), dict):
        data["providers"] = {}
    return data


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write the config with 0600 permissions (it holds API keys)."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    # mkstemp creates the file 0600, so the key is never briefly world-readable.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_path())
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise YtPdfError(f"Could not write {config_path()}: {exc}") from exc


def mask_key(key: str) -> str:
    """`sk-ant-api03-abcdef4a89` -> `sk-ant-...4a89`."""
    if len(key) <= 12:
        return "****"
    return f"{key[:7]}...{key[-4:]}"


def set_provider(cfg: dict[str, Any], provider: str, api_key: str, model: str) -> None:
    cfg["providers"][provider] = {"api_key": api_key, "active_model": model}
    cfg["active_provider"] = provider


def active_settings(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """Return (provider, api_key, model) for the active provider or raise a helpful error."""
    provider = cfg.get("active_provider")
    entry = cfg["providers"].get(provider) if provider else None
    if provider not in PROVIDERS or not entry or not entry.get("api_key"):
        raise YtPdfError("No active provider with an API key. Run `yt-pdf auth login` first.")
    model = entry.get("active_model") or PROVIDERS[provider]["default_model"]
    return provider, entry["api_key"], model
