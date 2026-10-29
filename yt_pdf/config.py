"""Config loading/saving and the static provider catalogue."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    # Any server that speaks the OpenAI chat API: Ollama, LM Studio, vLLM, LiteLLM, Together, Groq, ...
    # It has no default model (that depends on the server) and its URL is stored with the key.
    "custom": {
        "label": "OpenAI-compatible (custom URL)",
        "default_model": None,
        "models": [],
    },
}

CUSTOM = "custom"
# Local servers usually need no key, but the OpenAI client insists on a non-empty one.
NO_KEY = "no-key"
EXAMPLE_URL = "http://localhost:11434/v1"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def normalize_base_url(url: str) -> str:
    """The API base URL, validated: http(s) with a host, no trailing slash, no `/chat/completions`."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise YtPdfError(f"{url!r} is not a valid URL. Use something like {EXAMPLE_URL} or https://api.example.com/v1")
    url = url.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def is_insecure_remote(url: str) -> bool:
    """Plain http to a host that is not this computer: the API key would cross the network unencrypted."""
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "") not in _LOCAL_HOSTS


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


def set_provider(
    cfg: dict[str, Any], provider: str, api_key: str, model: str, base_url: str | None = None
) -> None:
    entry = {"api_key": api_key, "active_model": model}
    if base_url:
        entry["base_url"] = base_url
    cfg["providers"][provider] = entry
    cfg["active_provider"] = provider


def active_base_url(cfg: dict[str, Any]) -> str | None:
    """The custom endpoint's URL when it is the active provider, else None."""
    if cfg.get("active_provider") != CUSTOM:
        return None
    return (cfg["providers"].get(CUSTOM) or {}).get("base_url")


def active_settings(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """Return (provider, api_key, model) for the active provider or raise a helpful error."""
    provider = cfg.get("active_provider")
    entry = cfg["providers"].get(provider) if provider else None
    if provider not in PROVIDERS or not entry or not entry.get("api_key"):
        raise YtPdfError("No active provider with an API key. Run `yt-pdf auth login` first.")
    if provider == CUSTOM and not entry.get("base_url"):
        raise YtPdfError("The custom endpoint has no URL saved. Run `yt-pdf auth login` again.")
    model = entry.get("active_model") or PROVIDERS[provider]["default_model"]
    if not model:
        raise YtPdfError("No model chosen for this provider. Run `yt-pdf model`.")
    return provider, entry["api_key"], model
