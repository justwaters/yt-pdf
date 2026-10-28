# yt-pdf

Turn a YouTube video into a styled PDF reference guide: executive TL;DR, cleaned-up prose, headings, and inline screenshots.

See sample output and install options at **https://justwaters.github.io/yt-pdf/**.

## Install

Requires Python 3.10+ and `ffmpeg` on your PATH.

```sh
brew install justwaters/tap/yt-pdf     # or: pipx install . / pip install -e .
```

PDFs are printed by a browser. yt-pdf uses Playwright's headless Chromium if present, otherwise an installed Google Chrome or Microsoft Edge, and otherwise downloads the headless shell (about 100 MB, once) on first run.

## Use

```sh
yt-pdf auth login         # pick a provider, paste an API key (verified, stored 0600)
yt-pdf model              # optional: change the model
yt-pdf "https://www.youtube.com/watch?v=VIDEO_ID" [--dark] [--largeText] [-o out.pdf]
yt-pdf auth               # show provider/model status
```

Each section heading and screenshot carries its video time in the left margin, linked to that moment on YouTube. Pages are numbered and carry a running title. The fonts (Source Serif 4 and IBM Plex, SIL Open Font License) ship with the package, so PDFs look the same on macOS and Linux.

Text is a compact size by default; `--largeText` gives the larger size. `--dark` switches to the dark theme.

Providers: OpenAI, Anthropic, OpenRouter, Nvidia NIM, Google Gemini. Config lives in `~/.config/yt-pdf/config.json`.

Without `ffmpeg` (or if `yt-dlp` can't read the stream) the PDF is still produced, just without screenshots.
