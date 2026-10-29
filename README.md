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
yt-pdf "https://www.youtube.com/watch?v=VIDEO_ID" [--style S] [--format F] [--dark] [--largeText] [-o out]
yt-pdf auth               # show provider/model status
```

The PDF is titled with the video's title (page header, running title and PDF title), and unless you pass `-o` the file is named after it, e.g. `My-Talk_light.pdf`.

Each section heading and screenshot carries its video time in the left margin, linked to that moment on YouTube. Pages are numbered and carry a running title. The fonts (Source Serif 4 and IBM Plex, SIL Open Font License) ship with the package, so PDFs look the same on macOS and Linux.

Text is a compact size by default; `--largeText` gives the larger size. `--dark` switches to the dark theme.

### Styles

`--style` changes what the model writes:

| Style | Result |
| --- | --- |
| `full` (default) | The complete guide: every section, number and step. |
| `summary` | One to two pages: the TL;DR plus a few short sections, at most 3 screenshots. |
| `notes` | Study notes: the guide, then **Key terms** and **Review questions** with answers. |
| `tutorial` | How-to: **Before you start**, numbered steps with exact commands, common problems. |

### Formats

`--format pdf|html|md|epub` (or just give `-o` a matching extension, e.g. `-o guide.epub`).

- `pdf`: the styled document (default). `html`: the same page as one self-contained file.
- `md`: Markdown with front matter (good for Obsidian); screenshots are saved next to it in `<name>_images/`, and timecodes link to the video.
- `epub`: an e-book with a table of contents, for Kindle apps, Apple Books and e-readers.

`--dark` and `--largeText` apply to `pdf` and `html`. The default file name is the video title, plus the style when it isn't `full` (`My-Talk_summary_light.pdf`).

### Playlists, channels and several videos

```sh
yt-pdf "https://www.youtube.com/playlist?list=PL..." -o guides/ --limit 10
yt-pdf "https://www.youtube.com/@channel" -o guides/            # latest 25 uploads
yt-pdf URL1 URL2 URL3 -o guides/
yt-pdf --file urls.txt -o guides/ --skip-existing                  # one URL per line, # for comments
```

One guide is made per video, in order. A video that fails (no captions, private) is reported and the rest carry on; the exit code is 1 if any failed. `--limit` caps how many videos are taken from a playlist or channel (default 25), and for a playlist or channel you are asked to confirm first because each video is a separate model request (`-y` skips the question). `--skip-existing` resumes an interrupted batch. With several videos `-o` is a folder.

### Videos without captions

If a video has no captions, yt-pdf says what it would do and asks first: it can transcribe the audio on your computer with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). That downloads the audio track to a temporary folder, installs `faster-whisper` into yt-pdf's environment if it is missing (about 120 MB), downloads the Whisper model on first use (`base`, about 145 MB) and runs on the CPU. Nothing is sent to an API. In a batch you are asked once.

- `--transcribe`: agree in advance (the description is still printed). `--no-transcribe`: never.
- `--whisper-model tiny|base|small|medium|large-v3`: bigger is more accurate and slower.
- Without a terminal and without `--transcribe`, yt-pdf stops instead of asking.
- To install the package yourself: `pipx inject yt-pdf faster-whisper` or `pip install "yt-pdf[whisper]"`.

Providers: OpenAI, Anthropic, OpenRouter, Nvidia NIM, Google Gemini. Config lives in `~/.config/yt-pdf/config.json`.

Without `ffmpeg` (or if `yt-dlp` can't read the stream) the PDF is still produced, just without screenshots.
