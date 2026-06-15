"""
scraper.py

Cloudflare on animepahe uses IUAM/Turnstile (real browser fingerprint check).
This cannot be bypassed with plain requests or cloudscraper alone.

Two modes are supported:
  1. nodriver mode (recommended) - uses undetected Chrome to solve the
     Cloudflare challenge automatically and grab clearance cookies.

  2. Manual cookie mode - you visit animepahe in your browser, copy the
     cf_clearance cookie and set the ANIMEPAHE_CF_CLEARANCE env var.
     This is the quick-start option that requires no extra setup.

See README.md for setup instructions.
"""

import re
import asyncio
import time
import os
import json
import ssl as ssl_mod
import subprocess
import sys
import tempfile
import shutil
import warnings
import concurrent.futures
import threading
import json
import hashlib
import random
import time
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, quote
from colorama import Fore

# ── Config ────────────────────────────────────────────────────────────────

BASE_URL = "https://animepahe.pw"    # confirmed live domain (animepahe.org redirects here)

# ── Manual cookie fallback ────────────────────────────────────────────────────
# If nodriver doesn't work, set the ANIMEPAHE_CF_CLEARANCE environment variable
# with your cf_clearance cookie value.  It expires in ~30 minutes so you may
# need to refresh it.
#
# Example:   set ANIMEPAHE_CF_CLEARANCE=abc123xyz...   (Windows)
#            export ANIMEPAHE_CF_CLEARANCE=abc123xyz... (Linux/macOS)
MANUAL_CF_CLEARANCE = os.environ.get("ANIMEPAHE_CF_CLEARANCE", "")

# Polite delay between requests (seconds)
REQUEST_DELAY = 1.5

# Session cache file - stores cookies + user-agent between runs so you
# don't have to wait for Chrome to solve Cloudflare every time.
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session_cache.json")

# ── Download configuration ───────────────────────────────────────────────────
# Token bucket rate limiter for CDN connections (max concurrent segment downloads)
_DL_WORKERS = 8
_DL_RATE_LIMIT = 6          # max concurrent connections to CDN (token bucket)
_DL_MAX_RETRIES = 5         # per-segment max retries
_DL_BASE_BACKOFF = 1.0      # initial backoff seconds
_DL_MAX_BACKOFF = 30.0      # max backoff seconds
_DL_JITTER = 0.3            # jitter factor (0-1)
_DL_TIMEOUT = 60            # per-segment timeout (seconds)

# Resume/state file
_STATE_FILE_SUFFIX = ".download_state.json"

# Structured logging
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads.log.jsonl")


@dataclass
class TokenBucket:
    """Token bucket rate limiter for controlling CDN connection concurrency."""
    rate: int
    tokens: float = 0
    last_update: float = 0
    _lock: threading.Lock = None
    
    def __post_init__(self):
        self.tokens = float(self.rate)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()
    
    def take(self, tokens: int = 1) -> float:
        """Take tokens, blocking until available. Returns wait time."""
        with self._lock:
            now = time.monotonic()
            # Refill tokens based on elapsed time (assuming 1 token per second refill)
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed)
            self.last_update = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            
            # Need to wait for tokens
            wait_time = (tokens - self.tokens)  # ~1 token per second
            self.tokens = 0
            return wait_time


@dataclass
class SegmentState:
    """State for a single segment download."""
    index: int
    url: str
    filename: str
    downloaded: bool = False
    size: int = 0
    retries: int = 0
    last_error: str = ""
    checksum: str = ""


@dataclass
class DownloadState:
    """Complete download state for resume capability."""
    m3u8_url: str
    title: str
    ep: str
    total_segments: int
    segments: List[SegmentState]
    key_urls: List[str]
    key_map: Dict[str, str]  # remote_url -> local_filename
    started_at: float
    updated_at: float
    temp_dir: str
    local_m3u8_path: str
    output_file: str
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)
    
    @classmethod
    def from_json(cls, data: str) -> "DownloadState":
        d = json.loads(data)
        d["segments"] = [SegmentState(**s) for s in d["segments"]]
        return cls(**d)


def _log_event(event_type: str, **kwargs):
    """Write structured JSONL log entry."""
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **kwargs
    }
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Logging failures should never break the download


def _checksum_file(path: str) -> str:
    """Compute SHA256 checksum of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _load_download_state(state_path: str) -> Optional[DownloadState]:
    """Load download state from sidecar file."""
    try:
        with open(state_path, "r") as f:
            return DownloadState.from_json(f.read())
    except Exception:
        return None


def _save_download_state(state: DownloadState):
    """Save download state to sidecar file."""
    state.updated_at = time.time()
    try:
        with open(state.temp_dir + _STATE_FILE_SUFFIX, "w") as f:
            f.write(state.to_json())
    except Exception as e:
        _log_event("state_save_failed", error=str(e), state_path=state.temp_dir + _STATE_FILE_SUFFIX)


def _verify_segment(path: str, expected_size: int = 0) -> bool:
    """Verify segment file exists and has expected size."""
    try:
        if not os.path.exists(path):
            return False
        actual_size = os.path.getsize(path)
        if expected_size and actual_size != expected_size:
            return False
        return actual_size > 0
    except Exception:
        return False

# ── Session state ─────────────────────────────────────────────────────────────
_session = requests.Session()

# Mount retry adapter - automatic retry on transient failures
_retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))

_cf_cookies: dict = {}      # populated by nodriver or manual mode
_user_agent: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_session_initialised = False


# ── Session cache ─────────────────────────────────────────────────────────────

def _save_session_cache():
    """Save current session cookies + user-agent to disk for reuse."""
    cache = {
        "user_agent": _user_agent,
        "cookies": dict(_session.cookies),
        "base_url": BASE_URL,
        "saved_at": time.time(),
    }
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        print(Fore.GREEN + f"[cache] session saved → reuse on next run" + Fore.RESET)
    except OSError as e:
        print(Fore.YELLOW + f"[cache] could not save: {e}" + Fore.RESET)


def _try_cached_session() -> bool:
    """
    Try to load a cached session from disk. If the cookies are still valid
    (tested with a quick API call), return True and skip all solvers.
    """
    global _user_agent, _cf_cookies

    if not os.path.exists(_CACHE_FILE):
        return False

    try:
        with open(_CACHE_FILE, "r") as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    # Check age - cf_clearance usually expires in 30 min, but can last longer.
    # Reject caches older than 3 hours to be safe.
    age_s = time.time() - cache.get("saved_at", 0)
    if age_s > 3 * 3600:
        print(Fore.YELLOW + f"[cache] expired ({age_s/3600:.1f}h old), re-solving…" + Fore.RESET)
        return False

    cookies = cache.get("cookies", {})
    if "cf_clearance" not in cookies:
        return False

    # Load cached values into the session
    _user_agent = cache.get("user_agent", _user_agent)
    _session.cookies.update(cookies)
    _cf_cookies = cookies

    # Quick validation - try a lightweight API call
    age_min = age_s / 60
    try:
        resp = _session.get(
            f"{BASE_URL}/api?m=search&q=test",
            headers=_build_headers(),
            timeout=30,
        )
        if resp.status_code == 200:
            print(Fore.GREEN + f"[cache] loaded saved session ({age_min:.0f}m old) [OK]" + Fore.RESET)
            return True
        elif resp.status_code == 403:
            print(Fore.YELLOW + f"[cache] cookies expired (403), re-solving…" + Fore.RESET)
            _session.cookies.clear()
            return False
        else:
            # Other errors (500, etc.) - assume cookies are fine, server is just being flaky
            print(Fore.YELLOW + f"[cache] server returned {resp.status_code}, using cached session anyway ({age_min:.0f}m old)" + Fore.RESET)
            return True
    except requests.exceptions.Timeout:
        # Server slow but not blocked - trust the cache if it's recent
        if age_s < 30 * 60:  # less than 30 min old
            print(Fore.YELLOW + f"[cache] server slow, using cached session ({age_min:.0f}m old)" + Fore.RESET)
            return True
        else:
            print(Fore.YELLOW + f"[cache] server timeout + old cache ({age_min:.0f}m), re-solving…" + Fore.RESET)
            _session.cookies.clear()
            return False
    except Exception:
        # Network error - trust recent cache
        if age_s < 30 * 60:
            print(Fore.YELLOW + f"[cache] network error, using cached session ({age_min:.0f}m old)" + Fore.RESET)
            return True
        _session.cookies.clear()
        return False


def _build_headers(referer: str = None) -> dict:
    headers = {
        "User-Agent": _user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": referer or f"{BASE_URL}/",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
    }
    return headers


# ── nodriver integration ──────────────────────────────────────────────────────

def _try_nodriver() -> bool:
    """
    Use nodriver (undetected Chrome) to solve the Cloudflare challenge locally.
    Opens a real Chrome window, waits for the challenge to clear, grabs cookies.
    Returns True on success.
    """
    global _cf_cookies, _user_agent, BASE_URL

    try:
        import nodriver as uc
    except ImportError:
        print(Fore.YELLOW + "[nodriver] not installed. Run: pip install nodriver" + Fore.RESET)
        return False

    print(Fore.CYAN + "[nodriver] launching Chrome to solve Cloudflare challenge…" + Fore.RESET)
    print(Fore.CYAN + "[nodriver] a browser window will open - please don't close it" + Fore.RESET)

    async def _solve():
        global _cf_cookies, _user_agent, BASE_URL

        browser = await uc.start()
        page = await browser.get(f"{BASE_URL}/")

        # Wait for Cloudflare to clear (up to 120 seconds)
        max_wait = 120
        poll_interval = 3
        elapsed = 0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Check page content - in nodriver v0.50+, page.title and page.url are often empty
            # So we rely on page content to detect challenge clearance
            try:
                page_content = await page.get_content()
            except Exception:
                page_content = ""

            # Cloudflare challenge pages have "Just a moment" in content
            if "Just a moment" in page_content or "Checking" in page_content:
                if elapsed % 15 == 0:
                    print(Fore.YELLOW + f"[nodriver] still solving challenge… ({elapsed}s)" + Fore.RESET)
                continue

            # If page has meaningful content (not just CF block), we're through
            if page_content and len(page_content) > 5000:
                print(Fore.GREEN + f"[nodriver] challenge cleared in {elapsed}s [OK]" + Fore.RESET)
                break

            if elapsed % 15 == 0:
                print(Fore.YELLOW + f"[nodriver] waiting for page load… ({elapsed}s, content len: {len(page_content)})" + Fore.RESET)
        else:
            print(Fore.RED + f"[nodriver] timed out after {max_wait}s" + Fore.RESET)
            try:
                browser.stop()
            except Exception:
                pass
            return False

        # Update BASE_URL if we got redirected
        try:
            final_url = page.url
            if final_url:
                parsed = urlparse(final_url)
                new_base = f"{parsed.scheme}://{parsed.netloc}"
                if new_base != BASE_URL:
                    BASE_URL = new_base
                    print(Fore.CYAN + f"[nodriver] updated base URL to {BASE_URL}" + Fore.RESET)
        except Exception:
            pass

        # Extract cookies
        try:
            all_cookies = await browser.cookies.get_all()
            cookie_dict = {}
            for c in all_cookies:
                cookie_dict[c.name] = c.value
            _cf_cookies = cookie_dict
        except Exception as e:
            print(Fore.YELLOW + f"[nodriver] cookie extraction error: {e}" + Fore.RESET)
            try:
                browser.stop()
            except Exception:
                pass
            return False

        # Extract user agent
        try:
            ua_result = await page.evaluate("navigator.userAgent")
            if ua_result:
                _user_agent = str(ua_result)
        except Exception:
            pass

        try:
            browser.stop()
        except Exception:
            pass

        if "cf_clearance" in _cf_cookies:
            _session.cookies.update(_cf_cookies)
            print(Fore.GREEN + f"[nodriver] {len(_cf_cookies)} cookies captured (cf_clearance [OK])" + Fore.RESET)
            return True
        else:
            print(Fore.YELLOW + f"[nodriver] no cf_clearance found in cookies: {list(_cf_cookies.keys())}" + Fore.RESET)
            # Still load whatever cookies we got
            _session.cookies.update(_cf_cookies)
            return True  # Try anyway - some pages work without cf_clearance

    try:
        return asyncio.run(_solve())
    except Exception as e:
        print(Fore.RED + f"[nodriver] error: {e}" + Fore.RESET)
        return False
    finally:
        # Suppress harmless asyncio cleanup warnings on Python 3.14+ / Windows
        warnings.filterwarnings("ignore", message="unclosed transport", category=ResourceWarning)


def _use_manual_cookies():
    """Load the manually-pasted cf_clearance into the session."""
    global _cf_cookies
    if not MANUAL_CF_CLEARANCE:
        return
    _cf_cookies = {
        "cf_clearance": MANUAL_CF_CLEARANCE,
    }
    _session.cookies.update(_cf_cookies)
    print(Fore.GREEN + "[auth] manual cf_clearance loaded (from ANIMEPAHE_CF_CLEARANCE env var)" + Fore.RESET)


def init_session():
    """
    Initialise the session. Called once at startup.
    Priority: cached session → manual cookies → nodriver → bare session.
    """
    global _session_initialised
    if _session_initialised:
        return

    # 0. Try cached session first (instant if cookies are still valid)
    if not MANUAL_CF_CLEARANCE and _try_cached_session():
        _session_initialised = True
        return

    if MANUAL_CF_CLEARANCE:
        _use_manual_cookies()
    else:
        ok = _try_nodriver()
        if ok:
            # Save session for reuse on next run
            _save_session_cache()
        else:
            # No solver available - bare session
            print(
                Fore.YELLOW +
                "[warn] nodriver failed and no manual cookie set.\n"
                "       Requests may be blocked by Cloudflare.\n"
                "       See README.md for setup instructions." +
                Fore.RESET
            )

    _session_initialised = True


def _get(url: str, **kwargs) -> requests.Response:
    """Rate-limited GET using the initialised session."""
    time.sleep(REQUEST_DELAY)
    init_session()
    headers = _build_headers(kwargs.pop("referer", None))
    return _session.get(url, headers=headers, timeout=30, **kwargs)


# ── Search ────────────────────────────────────────────────────────────────────

def get_query(query: str) -> dict:
    """
    Search animepahe. Returns { title: [status, episodes, score, session_id] }
    """
    url = f"{BASE_URL}/api?m=search&q={quote(query)}"
    resp = _get(url)
    resp.raise_for_status()
    data = resp.json()

    results = {}
    for item in data.get("data", []):
        results[item["title"]] = [
            item["status"],
            item["episodes"],
            item["score"],
            item["session"],
        ]
    return results


def show_results_get_id(results: dict) -> list:
    """Pretty-print search results, return list of session ids in order."""
    print("\nAvailable results:")
    ids = []
    for n, (title, info) in enumerate(results.items(), start=1):
        status, eps, score, session = info
        if status == "Currently Airing":
            print(
                Fore.MAGENTA + f"{n}. " +
                Fore.CYAN + f"{title}" +
                Fore.WHITE + " | Status -> " +
                Fore.RED + f"{status}" +
                Fore.WHITE + " | Rating -> " +
                Fore.LIGHTRED_EX + f"{score}" +
                Fore.RESET
            )
        else:
            print(
                Fore.MAGENTA + f"{n}. " +
                Fore.CYAN + f"{title}" +
                Fore.WHITE + " | Status -> " +
                Fore.GREEN + f"{status}" +
                Fore.WHITE + " | Eps -> " +
                Fore.LIGHTYELLOW_EX + f"{eps}" +
                Fore.WHITE + " | Rating -> " +
                Fore.LIGHTRED_EX + f"{score}" +
                Fore.RESET
            )
        ids.append(session)
    return ids


# ── Episode list ──────────────────────────────────────────────────────────────

async def _fetch_page_async(url: str, cookies: dict, ua: str) -> str | None:
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/",
    }
    ssl_ctx = ssl_mod.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl_mod.CERT_NONE
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, cookies=cookies, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                return await resp.text()
    except Exception as exc:
        print(Fore.RED + f"[warn] async fetch failed for {url}: {exc}" + Fore.RESET)
        return None


async def _fetch_all_pages_async(urls: list, cookies: dict, ua: str) -> list:
    tasks = [_fetch_page_async(u, cookies, ua) for u in urls]
    return await asyncio.gather(*tasks)


async def get_episode_list(anime_id: str, last_page: int) -> dict:
    """
    Fetches all release pages concurrently.
    Returns { episode_number(int|float): session_id(str) }
    """
    init_session()
    cookies = {c.name: c.value for c in _session.cookies}
    urls = [
        f"{BASE_URL}/api?m=release&id={anime_id}&sort=episode_asc&page={p}"
        for p in range(1, last_page + 1)
    ]

    if len(urls) > 1:
        print(
            Fore.WHITE +
            f"[warn] SSL verification disabled for async episode fetches "
            f"(aiohttp workaround)" + Fore.RESET
        )

    responses = await _fetch_all_pages_async(urls, cookies, _user_agent)

    eps: dict = {}
    for text in responses:
        if not text:
            continue
        try:
            data = json.loads(text)
            for item in data.get("data", []):
                eps[float(item["episode"])] = item["session"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(Fore.YELLOW + f"[warn] failed to parse episode page: {e}" + Fore.RESET)

    eps_clean = {}
    for k, v in eps.items():
        key = int(k) if k == int(k) else k
        eps_clean[key] = v

    ep_keys = sorted(eps_clean.keys())
    if ep_keys:
        print(
            Fore.WHITE + "\nAvailable episodes: " +
            Fore.LIGHTMAGENTA_EX + f"{ep_keys[0]} - {ep_keys[-1]}" +
            Fore.RESET
        )
    return eps_clean


# ── Episode page → kwik links ─────────────────────────────────────────────────

def get_ep_links(anime_id: str, ep_session: str) -> list:
    """
    Returns list of (kwik_url, quality_label, audio_type) tuples.
    """
    url = f"{BASE_URL}/play/{anime_id}/{ep_session}"
    resp = _get(url, referer=f"{BASE_URL}/")
    resp.raise_for_status()
    html = resp.text

    # 2026 format: <button data-src="https://kwik.cx/e/xxx" data-resolution="1080" data-audio="jpn">
    matches = re.findall(
        r'data-src="(https://kwik\.[a-z]+/e/[^"]+)"'
        r'[^>]*data-(?:fansub|resolution|audio)=[^>]*'
        r'data-resolution="([^"]*)"'
        r'[^>]*data-audio="([^"]*)"',
        html,
    )

    # Simpler fallback: just grab data-src + data-resolution + data-audio separately
    if not matches:
        matches = []
        for btn_match in re.finditer(
            r'<button[^>]*data-src="(https://kwik\.[a-z]+/e/[^"]+)"[^>]*>',
            html,
        ):
            btn_tag = btn_match.group(0)
            kwik_url = btn_match.group(1)
            res_m = re.search(r'data-resolution="([^"]*)"', btn_tag)
            aud_m = re.search(r'data-audio="([^"]*)"', btn_tag)
            quality = res_m.group(1) + "p" if res_m else "unknown"
            audio = aud_m.group(1) if aud_m else ""
            matches.append((kwik_url, quality, audio))

    # Legacy fallback: href-based links (old format)
    if not matches:
        matches = re.findall(
            r'href="(https://kwik\.[a-z]+/e/[^"]+)"[^>]*>\s*'
            r'<span[^>]*>([^<]+)</span>\s*<span[^>]*>([^<]*)</span>',
            html,
        )

    # Last resort: any kwik URL
    if not matches:
        raw = re.findall(r'(?:data-src|href|src)="(https://kwik\.[a-z]+/[^"]+)"', html)
        matches = [(m, "unknown", "") for m in raw]

    return matches


def show_dl_opts(links: list) -> list:
    """Print quality options, return ordered list of kwik URLs."""
    urls = []
    for n, item in enumerate(links, start=1):
        kwik_url = item[0]
        quality  = item[1].strip() if len(item) > 1 else "unknown"
        audio    = item[2].strip() if len(item) > 2 else ""

        if audio.lower() in ("dub", "eng"):
            tag = Fore.LIGHTGREEN_EX + f"{quality} Dub"
        elif audio.lower() in ("jpn", "sub", ""):
            tag = Fore.LIGHTBLUE_EX + f"{quality} Sub"
        else:
            tag = Fore.LIGHTYELLOW_EX + f"{quality} ({audio})"

        print(Fore.MAGENTA + f"{n}. " + tag + Fore.RESET)
        urls.append(kwik_url)
    return urls


# ── Filename sanitisation ─────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name).strip()


# ── Parallel HLS downloader with resume & rate limiting ───────────────────────

def download_vid(m3u8_url: str, title: str, ep, referer: str = "https://kwik.cx/"):
    """
    Download HLS stream to  <title>/<title> ep_XX.mp4.

    Segments are AES-128 encrypted, so the flow is:
      1. Fetch + parse the remote m3u8 playlist
      2. Download all segments in parallel (rate-limited, with resume)
      3. Download the encryption key
      4. Build a LOCAL m3u8 referencing local files + key
      5. Feed the local m3u8 to ffmpeg (decrypts + muxes to mp4)
    """
    safe_title = _sanitize(title)
    ep_str = f"{int(ep):02d}" if isinstance(ep, (int, float)) and ep == int(ep) else str(ep)
    os.makedirs(safe_title, exist_ok=True)
    out_file = os.path.join(safe_title, f"{safe_title} ep_{ep_str}.mp4")
    out_file_abs = os.path.abspath(out_file)

    # Skip if already downloaded
    if os.path.exists(out_file_abs) and os.path.getsize(out_file_abs) > 0:
        print(Fore.GREEN + f"\n[skip] Ep {ep_str} already downloaded → .{os.sep}{out_file}" + Fore.RESET)
        _log_event("skip_existing", episode=ep_str, output=out_file, size=os.path.getsize(out_file_abs))
        return

    print(Fore.LIGHTYELLOW_EX + f"\nDownloading Ep {ep_str} → .{os.sep}{out_file}" + Fore.RESET)
    _log_event("download_start", episode=ep_str, m3u8_url=m3u8_url, output=out_file)

    # ── Step 1: Fetch and parse m3u8 playlist ─────────────────────────────────
    dl_headers = {
        "User-Agent": _user_agent,
        "Referer": referer,
    }

    try:
        m3u8_resp = requests.get(m3u8_url, headers=dl_headers, timeout=30)
        m3u8_resp.raise_for_status()
    except Exception as e:
        print(Fore.RED + f"[error] failed to fetch m3u8 playlist: {e}" + Fore.RESET)
        _log_event("m3u8_fetch_failed", episode=ep_str, error=str(e))
        return

    m3u8_text = m3u8_resp.text
    m3u8_lines = m3u8_text.strip().split('\n')

    # Extract segment URLs
    segments = []
    for line in m3u8_lines:
        line = line.strip()
        if line and not line.startswith('#'):
            segments.append(line)

    if not segments:
        print(Fore.RED + "[error] no segments found in m3u8 playlist" + Fore.RESET)
        _log_event("no_segments", episode=ep_str)
        return

    # Extract encryption key URL(s)
    key_urls = re.findall(r'#EXT-X-KEY:.*?URI="([^"]+)"', m3u8_text)

    total_segments = len(segments)
    encrypted = "encrypted, " if key_urls else ""
    print(Fore.WHITE + f"  {total_segments} segments ({encrypted}{_DL_WORKERS} workers, {_DL_RATE_LIMIT} rate limit)…" + Fore.RESET)

    # ── Step 2: Check for resume state ────────────────────────────────────────
    # We'll use a temp dir named after the episode for easier resumption
    tmp_dir = os.path.join(tempfile.gettempdir(), f"animepahe_{safe_title}_ep{ep_str}")
    os.makedirs(tmp_dir, exist_ok=True)
    state_path = tmp_dir + _STATE_FILE_SUFFIX

    # Try to load existing state
    existing_state = _load_download_state(state_path)
    resume = False
    if existing_state and existing_state.m3u8_url == m3u8_url:
        # Verify the existing temp dir and segments
        resume = True
        print(Fore.CYAN + f"  [resume] Found existing download state, verifying…" + Fore.RESET)
        _log_event("resume_attempt", episode=ep_str, temp_dir=tmp_dir)

    # ── Step 3: Create temp dir, download key + segments in parallel ──────────
    if not resume:
        # Fresh download - clean temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)

    # Download encryption key(s) to temp dir
    key_map = {}  # remote_url → local_filename
    for i, key_url in enumerate(key_urls):
        key_filename = f"key_{i}.key"
        key_path = os.path.join(tmp_dir, key_filename)
        if not os.path.exists(key_path):
            try:
                key_resp = requests.get(key_url, headers=dl_headers, timeout=30)
                key_resp.raise_for_status()
                with open(key_path, "wb") as f:
                    f.write(key_resp.content)
                _log_event("key_downloaded", episode=ep_str, key_index=i, key_url=key_url, size=len(key_resp.content))
            except Exception as e:
                print(Fore.RED + f"[error] failed to download encryption key: {e}" + Fore.RESET)
                _log_event("key_download_failed", episode=ep_str, key_index=i, key_url=key_url, error=str(e))
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return
        key_map[key_url] = key_filename

    # Map remote segment URLs to local filenames
    seg_filenames = {}  # remote_url → local_filename
    for idx, url in enumerate(segments):
        seg_filenames[url] = f"seg_{idx:05d}.ts"

    # Build segment state list (for resume tracking)
    segment_states = []
    if resume and existing_state:
        # Use existing segment states
        segment_states = existing_state.segments
        # Ensure we have all segments
        if len(segment_states) != total_segments:
            print(Fore.YELLOW + f"  [resume] Segment count mismatch ({len(segment_states)} vs {total_segments}), starting fresh" + Fore.RESET)
            resume = False
            segment_states = []
    if not resume:
        segment_states = [
            SegmentState(index=idx, url=url, filename=seg_filenames[url])
            for idx, url in enumerate(segments)
        ]

    # Create download state object
    local_m3u8_path = os.path.join(tmp_dir, "local.m3u8")
    download_state = DownloadState(
        m3u8_url=m3u8_url,
        title=safe_title,
        ep=ep_str,
        total_segments=total_segments,
        segments=segment_states,
        key_urls=key_urls,
        key_map=key_map,
        started_at=time.time() if not resume else existing_state.started_at,
        updated_at=time.time(),
        temp_dir=tmp_dir,
        local_m3u8_path=local_m3u8_path,
        output_file=out_file_abs,
    )

    # Verify already-downloaded segments (on resume)
    completed = 0
    if resume:
        for seg_state in segment_states:
            seg_path = os.path.join(tmp_dir, seg_state.filename)
            if _verify_segment(seg_path):
                seg_state.downloaded = True
                seg_state.size = os.path.getsize(seg_path)
                seg_state.checksum = _checksum_file(seg_path)
                completed += 1
            else:
                seg_state.downloaded = False
        print(Fore.CYAN + f"  [resume] Verified {completed}/{total_segments} segments" + Fore.RESET)
        _log_event("resume_verified", episode=ep_str, completed=completed, total=total_segments)

    # Shared progress state
    lock = threading.Lock()
    progress = {"done": completed, "bytes": sum(s.size for s in segment_states if s.downloaded), "failed": 0}
    start_wall = time.time()

    # Token bucket rate limiter (shared across workers)
    rate_limiter = TokenBucket(_DL_RATE_LIMIT)

    def _download_segment(seg_state: SegmentState) -> bool:
        """Download a single segment with retries, backoff, and rate limiting."""
        idx = seg_state.index
        url = seg_state.url
        filename = seg_state.filename
        seg_path = os.path.join(tmp_dir, filename)

        # Skip if already downloaded and verified
        if seg_state.downloaded and _verify_segment(seg_path, seg_state.size):
            with lock:
                progress["done"] += 1
                progress["bytes"] += seg_state.size
            return True

        max_retries = _DL_MAX_RETRIES
        backoff = _DL_BASE_BACKOFF

        for attempt in range(max_retries):
            # Rate limit: wait for token
            wait_time = rate_limiter.take(1)
            if wait_time > 0:
                time.sleep(wait_time)

            try:
                resp = requests.get(url, headers=dl_headers, timeout=_DL_TIMEOUT, stream=True)
                
                # Handle HTTP errors with retry logic
                if resp.status_code in (429, 500, 502, 503, 504):
                    # Check for Retry-After header
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except ValueError:
                            wait = backoff
                    else:
                        wait = backoff
                    
                    # Add jitter
                    wait += random.uniform(0, wait * _DL_JITTER)
                    _log_event("segment_retry", episode=ep_str, segment=idx, attempt=attempt+1,
                              status=resp.status_code, wait=wait, url=url)
                    resp.close()
                    if attempt < max_retries - 1:
                        time.sleep(wait)
                        backoff = min(backoff * 2, _DL_MAX_BACKOFF)
                        seg_state.retries = attempt + 1
                        seg_state.last_error = f"HTTP {resp.status_code}"
                        _save_download_state(download_state)
                        continue
                    else:
                        raise requests.HTTPError(f"HTTP {resp.status_code} after {max_retries} retries")

                resp.raise_for_status()
                
                # Download with streaming to handle large segments
                with open(seg_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                # Verify download
                size = os.path.getsize(seg_path)
                if size == 0:
                    raise IOError("Downloaded segment is empty")

                seg_state.downloaded = True
                seg_state.size = size
                seg_state.checksum = _checksum_file(seg_path)
                seg_state.retries = attempt + 1
                seg_state.last_error = ""

                with lock:
                    progress["done"] += 1
                    progress["bytes"] += size
                    done = progress["done"]
                    total_bytes = progress["bytes"]

                pct = done / total_segments * 100
                size_mb = total_bytes / (1024 * 1024)
                elapsed = time.time() - start_wall
                speed = size_mb / elapsed if elapsed > 0.5 else 0.0
                status = (
                    f"\r  {Fore.WHITE}"
                    f"{done}/{total_segments} ({pct:4.1f}%)  "
                    f"size= {size_mb:7.1f} MB  "
                    f"speed= {speed:5.2f} MB/s"
                    f"{Fore.RESET}"
                )
                sys.stdout.write(status)
                sys.stdout.flush()

                _log_event("segment_downloaded", episode=ep_str, segment=idx, size=size,
                          attempt=attempt+1, speed_mbps=speed)
                _save_download_state(download_state)
                return True

            except Exception as e:
                seg_state.retries = attempt + 1
                seg_state.last_error = str(e)
                
                if attempt == max_retries - 1:
                    with lock:
                        progress["failed"] += 1
                        progress["done"] += 1
                    _log_event("segment_failed", episode=ep_str, segment=idx, 
                              error=str(e), retries=max_retries, url=url)
                    _save_download_state(download_state)
                    return False
                
                # Exponential backoff with jitter
                wait = backoff + random.uniform(0, backoff * _DL_JITTER)
                _log_event("segment_retry", episode=ep_str, segment=idx, attempt=attempt+1,
                          wait=wait, error=str(e), url=url)
                time.sleep(wait)
                backoff = min(backoff * 2, _DL_MAX_BACKOFF)
                _save_download_state(download_state)

        return False

    # Download segments in parallel
    remaining_segments = [s for s in segment_states if not s.downloaded]
    if remaining_segments:
        print(Fore.WHITE + f"  Downloading {len(remaining_segments)} remaining segments…" + Fore.RESET)
        with concurrent.futures.ThreadPoolExecutor(max_workers=_DL_WORKERS) as executor:
            executor.map(_download_segment, remaining_segments)

    print()  # newline after \r progress

    failed = progress["failed"]
    if failed > 0:
        print(Fore.YELLOW + f"  {failed} segment(s) failed to download" + Fore.RESET)

    # Allow up to 10% failure but log it
    if failed > total_segments * 0.1:
        print(Fore.RED + f"[error] too many failed segments ({failed}/{total_segments}), aborting" + Fore.RESET)
        _log_event("download_aborted", episode=ep_str, failed=failed, total=total_segments, reason="too_many_failures")
        # Don't delete temp dir on failure - keep for potential resume
        return

    # ── Step 4: Build local m3u8 with relative paths ──────────────────────────
    print(Fore.WHITE + "  Building local m3u8…" + Fore.RESET)
    with open(local_m3u8_path, "w") as f:
        for line in m3u8_lines:
            stripped = line.strip()
            if stripped in seg_filenames:
                f.write(seg_filenames[stripped] + "\n")
            elif '#EXT-X-KEY:' in stripped:
                new_line = stripped
                for remote_url, local_name in key_map.items():
                    new_line = new_line.replace(remote_url, local_name)
                f.write(new_line + "\n")
            else:
                f.write(stripped + "\n")

    # ── Step 5: ffmpeg decrypts + muxes to mp4 (local I/O, fast) ──────────────
    print(Fore.WHITE + "  Decrypting + muxing to mp4…" + Fore.RESET, end=" ", flush=True)
    cmd = [
        "ffmpeg",
        "-allowed_extensions", "ALL",
        "-i", local_m3u8_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-loglevel", "warning",
        "-y",
        out_file_abs,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_dir)

    # Cleanup temp files (but keep state file for a bit in case of issues)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if result.returncode != 0:
        print(Fore.RED + f"failed (code {result.returncode})" + Fore.RESET)
        if result.stderr.strip():
            print(Fore.RED + result.stderr.strip() + Fore.RESET)
        _log_event("ffmpeg_failed", episode=ep_str, code=result.returncode, stderr=result.stderr.strip())
    else:
        try:
            final_bytes = os.path.getsize(out_file_abs)
            final_mb = final_bytes / (1024 * 1024)
            wall_total = time.time() - start_wall
            avg_speed = final_mb / wall_total if wall_total > 0.5 else 0.0
            print(Fore.GREEN + "done" + Fore.RESET)
            print(
                Fore.GREEN + f"  Done → .{os.sep}{out_file}  "
                f"({final_mb:.1f} MB in {wall_total:.0f}s, avg {avg_speed:.2f} MB/s)" + Fore.RESET
            )
            _log_event("download_complete", episode=ep_str, output=out_file, 
                      size_mb=final_mb, duration_s=wall_total, avg_speed_mbps=avg_speed)
        except OSError:
            print(Fore.GREEN + f"  Done → .{os.sep}{out_file}" + Fore.RESET)
            _log_event("download_complete", episode=ep_str, output=out_file)
