# yt-pdf

Turn a YouTube video into a styled PDF reference guide: executive TL;DR, cleaned-up prose, headings, and inline screenshots.

## Install

Requires Python 3.10+ and `ffmpeg` on your PATH.

```sh
pipx install .            # or: pip install -e .
playwright install chromium
```

## Use

```sh
yt-pdf auth login         # pick a provider, paste an API key (verified, stored 0600)
yt-pdf model              # optional: change the model
yt-pdf "https://www.youtube.com/watch?v=VIDEO_ID" [--dark] [-o out.pdf]
yt-pdf auth               # show provider/model status
```

Providers: OpenAI, Anthropic, OpenRouter, Nvidia NIM, Google Gemini. Config lives in `~/.config/yt-pdf/config.json`.

Without `ffmpeg` (or if `yt-dlp` can't read the stream) the PDF is still produced, just without screenshots.
