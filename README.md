# AnimePahe Downloader

A fast, concurrent command-line downloader for [AnimePahe](https://animepahe.pw/).

It automatically solves Cloudflare challenges using an undetected Chrome instance (nodriver) or can fall back to a manually provided cookie. It extracts the direct stream URLs from embedded kwik.cx players and downloads HLS chunks concurrently (8 threads per episode, with global rate limiting) to maximize download speeds, followed by automatically muxing them into a final MP4 video using ffmpeg.

## Features

- **Concurrent Chunk Downloading**: Downloads video segments in parallel (up to 8x faster than sequential downloads).
- **Auto Cloudflare Bypass**: Uses `nodriver` to invisibly clear Cloudflare Turnstile challenges.
- **Batch Downloading**: Download single episodes, lists (`1 3 5`), or ranges (`1-12`).
- **Batch Quality Application**: Choose a resolution once and automatically apply it to all remaining episodes in the queue.
- **Parallel Episode Downloads**: Automatically downloads multiple episodes simultaneously (up to 6 concurrent).
- **Resume Support**: Automatically resumes interrupted downloads from where they left off.
- **Reliable Networking**: Includes automatic retries for transient API and download failures with exponential backoff. Rate-limited (429) responses get dedicated longer backoff with jitter.
- **Episode Metadata**: Embeds episode title, show name, episode ID, and auto-generated chapters (Start, Opening, Ending, End).
- **Cover Art**: Downloads and embeds episode thumbnails as attached pictures.
- **Custom Output Directory**: Specify download location with `-o`.

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

### With Custom Output Directory
```bash
python main.py -o /media/anime
```

### CLI Options

| Option | Description |
|--------|-------------|
| `-o`, `--output-dir PATH` | Custom output directory (default: current dir) |

### Episode Format Examples

| Input | Meaning |
|-------|---------|
| `5` | Single episode 5 |
| `1 3 7` | Episodes 1, 3, and 7 |
| `1-12` | Range 1 to 12 |
| `5-` | Episode 5 to the last available (confirms before proceeding) |
| `1-5 10 15` | Range 1-5 plus episodes 10 and 15 |

### Navigation

You can go back to the previous step at any prompt by entering `*`:

| Where you are | What `*` does |
|---|---|
| Enter anime name | Exits the script |
| Pick anime from results | Goes back to entering a name |
| Episode selection | Goes back to picking an anime |
| Quality selection | Goes back to episode selection |

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