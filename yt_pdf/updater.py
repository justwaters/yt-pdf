"""`yt-pdf update`: find the newest GitHub release and upgrade the way yt-pdf was installed."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from importlib import metadata

from .errors import YtPdfError

REPO = "justwaters/yt-pdf"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
GIT_URL = f"git+https://github.com/{REPO}"
BREW_FORMULA = "justwaters/tap/yt-pdf"


def parse_version(text: str) -> tuple[int, ...]:
    """`v0.6.1` -> (0, 6, 1). Missing parts count as 0, so 0.7 == 0.7.0."""
    parts = [int(n) for n in re.findall(r"\d+", text.split("-")[0])]
    if not parts:
        raise YtPdfError(f"Unrecognised version: {text!r}")
    return tuple(parts + [0] * (3 - len(parts)))


def latest_version(timeout: float = 10) -> str:
    request = urllib.request.Request(LATEST_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "yt-pdf"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            tag = json.load(resp)["tag_name"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        raise YtPdfError(f"Could not check for updates ({exc}). Is GitHub reachable?") from exc
    return tag.lstrip("v")


def install_method() -> str:
    """brew, pipx, uv, dev (an editable checkout) or pip, judged from where yt-pdf is installed."""
    try:
        direct = metadata.distribution("yt-pdf").read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            return "dev"
    except (metadata.PackageNotFoundError, ValueError):
        pass
    prefix = sys.prefix.replace("\\", "/")
    if "/Cellar/yt-pdf/" in prefix or "/opt/yt-pdf/" in prefix:
        return "brew"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    return "pip"


def upgrade_command(method: str, version: str) -> list[str]:
    """The command that installs `version`; the non-brew ones pin the release tag, not the branch tip."""
    source = f"{GIT_URL}@v{version}"
    return {
        "brew": ["brew", "upgrade", BREW_FORMULA],
        "pipx": ["pipx", "install", "--force", source],
        "uv": ["uv", "tool", "install", "--force", source],
        "dev": ["git", "pull"],
    }.get(method, [sys.executable, "-m", "pip", "install", "--upgrade", source])
