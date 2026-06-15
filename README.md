# AnimePahe Downloader

A fast, concurrent command-line downloader for [AnimePahe](https://animepahe.pw/).

It automatically solves Cloudflare challenges using an undetected Chrome instance (nodriver) or can fall back to a manually provided cookie. It extracts the direct stream URLs from embedded kwik.cx players and downloads HLS chunks concurrently (8 threads per episode) to maximize download speeds, followed by automatically muxing them into a final MP4 video using ffmpeg.

## Features
- **Concurrent Chunk Downloading**: Downloads video segments in parallel (up to 8x faster than sequential downloads).
- **Auto Cloudflare Bypass**: Uses `nodriver` to invisibly clear Cloudflare Turnstile challenges.
- **Batch Downloading**: Download single episodes, lists (`1 3 5`), or ranges (`1-12`).
- **Batch Quality Application**: Choose a resolution once and automatically apply it to all remaining episodes.
- **Resume Support**: Skips already downloaded and complete episodes.
- **Reliable Networking**: Includes automatic retries for transient API and download failures.

## Prerequisites

1. **Python 3.10+**
2. **[ffmpeg](https://ffmpeg.org/download.html)** installed and available in your system's `PATH`.
3. *(Optional but recommended)* **Google Chrome** installed (required for the automatic Cloudflare bypass using `nodriver`).

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/animepahe-downloader.git
   cd animepahe-downloader
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Simply run the script:
```bash
python main.py
```

### Flow Example:
1. **Search**: Enter the anime name (e.g., `one piece`).
2. **Select**: Choose from the available search results.
3. **Episodes**: Enter the episodes you want to download (e.g., `1-5` or `1 3 5-10`).
4. **Quality**: Select the video quality. You can enter `1a` (the number of the quality option followed by `a`) to apply that choice to all remaining episodes in the queue.

### Cloudflare Bypass Modes

animepahe uses strict Cloudflare protection. This script has two ways to deal with it:

**Option A: Automatic (nodriver)** - *Default & Recommended*
If you have `nodriver` installed (it's in `requirements.txt`) and Google Chrome installed on your machine, the script will briefly launch an undetected Chrome window to solve the challenge and capture the required cookies. These cookies are saved to a `.session_cache.json` file for subsequent runs.

**Option B: Manual Cookie (Fallback)**
If the automatic bypass fails, or you are running this on a headless server without Chrome, you can manually supply the clearance cookie.
1. Open https://animepahe.pw/ in your normal browser.
2. Wait for the Cloudflare challenge to pass.
3. Open Developer Tools (F12) -> Application (or Storage) -> Cookies.
4. Copy the value of the `cf_clearance` cookie.
5. Set it as an environment variable before running the script:
   - **Windows (Command Prompt):** `set ANIMEPAHE_CF_CLEARANCE=your_cookie_value`
   - **Windows (PowerShell):** `$env:ANIMEPAHE_CF_CLEARANCE="your_cookie_value"`
   - **Linux/macOS:** `export ANIMEPAHE_CF_CLEARANCE=your_cookie_value`

*(Note: This cookie typically expires after ~30 minutes or if your IP changes).*

## Design Details
- **Architecture**: Separates the CLI flow (`main.py`), API and session management (`scraper.py`), and m3u8 stream extraction from obfuscated JavaScript payloads (`kwik.py`).
- **Parallelism**: Uses `concurrent.futures.ThreadPoolExecutor` to download HLS segments concurrently, significantly saturating network bandwidth compared to single-threaded ffmpeg downloads.
- **Decryption**: Downloads the AES-128 key natively and constructs a local `.m3u8` playlist for ffmpeg, ensuring the heavy lifting of downloading is done via parallel Python threads rather than sequentially by ffmpeg.

## Troubleshooting

- **`[blocked] Cloudflare challenge detected.`**
  The automatic nodriver bypass failed. Try Option B (Manual Cookie).
- **`ffmpeg` is not recognized...**
  You need to install ffmpeg and add it to your system's PATH.
- **`SyntaxError` or similar when running**
  Ensure you are using Python 3.10 or newer.

## Disclaimer
This project is for educational purposes only. Always respect the terms of service of the websites you interact with.
