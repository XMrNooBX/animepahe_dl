# AnimePahe Downloader

A fast, concurrent command-line downloader for [AnimePahe](https://animepahe.pw/).

It automatically solves Cloudflare challenges using an undetected Chrome instance (nodriver) or can fall back to a manually provided cookie. It extracts the direct stream URLs from embedded kwik.cx players and downloads HLS chunks concurrently (8 threads per episode, with global rate limiting) to maximize download speeds, followed by automatically muxing them into a final MP4 video using ffmpeg.

## Features

- **Concurrent Chunk Downloading**: Downloads video segments in parallel (up to 8x faster than sequential downloads).
- **Auto Cloudflare Bypass**: Uses `nodriver` to invisibly clear Cloudflare Turnstile challenges.
- **Batch Downloading**: Download single episodes, lists (`1 3 5`), or ranges (`1-12`).
- **Batch Quality Application**: Choose a resolution once and automatically apply it to all remaining episodes in the queue.
- **Parallel Episode Downloads**: Download multiple episodes simultaneously with `-p N`.
- **Resume Support**: Skips already downloaded and complete episodes; can resume interrupted downloads.
- **Reliable Networking**: Includes automatic retries for transient API and download failures with exponential backoff.
- **Episode Metadata**: Embeds episode title, show name, episode ID, and auto-generated chapters (Start, Opening, Ending, End).
- **Cover Art**: Downloads and embeds episode thumbnails as attached pictures.
- **Custom Output Directory**: Specify download location with `-o`.
- **CLI & Interactive Modes**: Run fully non-interactive or with guided prompts.

## Prerequisites

1. **Python 3.10+**
2. **[ffmpeg](https://ffmpeg.org/download.html)** installed and available in your system's `PATH`.
3. *(Optional but recommended)* **Google Chrome** installed (required for the automatic Cloudflare bypass using `nodriver`).

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/XMrNooBX/animepahe_dl.git
   cd animepahe_dl
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Interactive Mode (Default)
```bash
python main.py
```
Prompts for anime name, episode selection, and quality for each episode.

### Batch / Non-Interactive Mode
```bash
# Download episodes 1-5 with 1080p Sub, no prompts
python main.py -a "One Piece" -e 1-5 -q 3 -b

# Download episodes 1-10 with 720p Dub, parallel (3 at a time), custom output dir
python main.py -a "One Piece" -e 1-10 -q 5 -b -p 3 -o /media/anime

# Download single episode with 1080p Dub
python main.py -a "One Piece" -e 100 -q 1080dub

# List available qualities then exit
python main.py -a "Witch Hat Atelier" --list
```

### CLI Options

| Option | Description |
|--------|-------------|
| `-a`, `--anime NAME` | Anime name to search (skips interactive search) |
| `-e`, `--episodes LIST` | Episodes to download (e.g., `1-5`, `1 3 7`, `1-5 10`) |
| `-q`, `--quality PRESET` | Quality preset: `1-6` or names like `1080p`, `720dub`, `360p` |
| `-o`, `--output-dir PATH` | Custom output directory (default: current dir) |
| `-b`, `--batch` | Batch mode: apply `--quality` to all episodes, skip prompts |
| `-p`, `--parallel N` | Number of episodes to download in parallel (default: 1) |
| `--list` | List episodes & qualities after anime selection, then exit |
| `--no-resume` | Disable resume (re-download even if partial state exists) |

### Quality Presets

| Index | Name | Resolution | Audio |
|-------|------|------------|-------|
| 1 | `360p` / `360sub` | 360p | Sub |
| 2 | `720p` / `720sub` | 720p | Sub |
| 3 | `1080p` / `1080sub` | 1080p | Sub |
| 4 | `360dub` | 360p | Dub |
| 5 | `720dub` | 720p | Dub |
| 6 | `1080dub` | 1080p | Dub |

### Episode Format Examples

| Input | Meaning |
|-------|---------|
| `5` | Single episode 5 |
| `1 3 7` | Episodes 1, 3, and 7 |
| `1-12` | Range 1 to 12 |
| `1-5 10 15` | Range 1-5 plus episodes 10 and 15 |

## Cloudflare Bypass Modes

animepahe uses strict Cloudflare protection. This script has two ways to deal with it:

### Option A: Automatic (nodriver) — Default & Recommended
If you have `nodriver` installed (it's in `requirements.txt`) and Google Chrome installed on your machine, the script will briefly launch an undetected Chrome window to solve the challenge and capture the required cookies. These cookies are saved to a `.session_cache.json` file for subsequent runs.

### Option B: Manual Cookie (Fallback)
If the automatic bypass fails, or you are running this on a headless server without Chrome, you can manually supply the clearance cookie.
1. Open https://animepahe.pw/ in your normal browser.
2. Wait for the Cloudflare challenge to pass.
3. Open Developer Tools (F12) → Application (or Storage) → Cookies.
4. Copy the value of the `cf_clearance` cookie.
5. Set it as an environment variable before running the script:
   - **Windows (Command Prompt):** `set ANIMEPAHE_CF_CLEARANCE=your_cookie_value`
   - **Windows (PowerShell):** `$env:ANIMEPAHE_CF_CLEARANCE="your_cookie_value"`
   - **Linux/macOS:** `export ANIMEPAHE_CF_CLEARANCE=your_cookie_value`

*(Note: This cookie typically expires after ~30 minutes or if your IP changes.)*

## Design Details
- **Architecture**: Separates the CLI flow (`main.py`), API and session management + downloader (`scraper.py`), and m3u8 stream extraction from obfuscated JavaScript payloads (`kwik.py`).
- **Parallelism**: Uses `concurrent.futures.ThreadPoolExecutor` to download HLS segments concurrently, significantly saturating network bandwidth compared to single-threaded ffmpeg downloads.
- **Decryption**: Downloads the AES-128 key natively and constructs a local `.m3u8` playlist for ffmpeg, ensuring the heavy lifting of downloading is done via parallel Python threads rather than sequentially by ffmpeg.
- **Metadata**: Embeds episode title, show name, episode ID, and auto-generated chapters (Start, Opening, Ending, End) into the MP4. Downloads and attaches episode thumbnails as cover art.

## Troubleshooting

- **`[blocked] Cloudflare challenge detected.`**  
  The automatic nodriver bypass failed. Try Option B (Manual Cookie).

- **`ffmpeg` is not recognized...**  
  You need to install ffmpeg and add it to your system's PATH.

- **`SyntaxError` or similar when running**  
  Ensure you are using Python 3.10 or newer.

- **`403 Forbidden` on API calls**  
  The cached session cookies have expired. Delete `.session_cache.json` and re-run, or ensure `ANIMEPAHE_CF_CLEARANCE` is set correctly.

## Disclaimer
This project is for educational purposes only. Always respect the terms of service of the websites you interact with.