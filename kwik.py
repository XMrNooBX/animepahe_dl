"""
kwik.py  —  Stream URL extractor for kwik.cx

kwik.cx embeds the m3u8 stream URL inside a packed/obfuscated JavaScript eval.
This module fetches the embed page and unpacks the JS to extract the URL.

Flow:
  1. GET  kwik.cx/e/<id>    (with Referer: animepahe domain)
  2. Find the eval(function(p,a,c,k,e,d)...) packed JS
  3. Unpack it to reveal the m3u8 URL
  4. Return the URL directly — no POST needed anymore
"""

import re
import time
from colorama import Fore
import scraper as _scraper
from scraper import _session, REQUEST_DELAY


def _base_url():
    """Always read the current BASE_URL from the scraper module (may update at runtime)."""
    return _scraper.BASE_URL


def _get(url: str, **kwargs):
    time.sleep(REQUEST_DELAY)
    headers = {
        "User-Agent": _scraper._user_agent,  # always read fresh (may be updated by nodriver)
        "Referer": kwargs.pop("referer", f"{_base_url()}/"),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    return _session.get(url, headers=headers, timeout=30, **kwargs)


def _unpack_payloads(html: str) -> list[str]:
    """
    Find and unpack all Dean Edwards packed JS payloads in the HTML.

    Returns a list of unpacked JS strings.

    The packed format is:
      eval(function(p,a,c,k,e,d){...}('template',base,count,'w1|w2|...'.split('|'),0,{}))

    The template can contain escaped quotes (\') so we use a proper
    escaped-string-aware regex instead of trying to extract eval blocks.
    """
    results = []

    # Search the full HTML for packed payload arguments.
    # Pattern: }'TEMPLATE',base,count,'DICT'.split('|')
    # The template may contain \' (escaped quotes), so we match:
    #   (?:[^'\\]|\\.)* — any non-quote/non-backslash, or backslash + any char
    for m in re.finditer(
        r"\}\('((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\s*\.split\('\|'\)",
        html,
    ):
        template_raw = m.group(1)
        base = int(m.group(2))
        count = int(m.group(3))
        words = m.group(4).split("|")

        # Build lookup table: base-encoded index -> word
        def base_encode(val, b):
            chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if val < b:
                return chars[val]
            return base_encode(val // b, b) + chars[val % b]

        lookup = {}
        for i in range(count):
            key = base_encode(i, base)
            lookup[key] = words[i] if i < len(words) and words[i] else key

        # Unescape the template (\' -> ', \\ -> \)
        template = template_raw.replace("\\'", "'").replace("\\\\", "\\")

        # Replace all word-boundary tokens with their dictionary values
        unpacked = re.sub(r'\b\w+\b', lambda tok: lookup.get(tok.group(0), tok.group(0)), template)
        results.append(unpacked)

    return results


def get_stream_url(kwik_url: str) -> str | None:
    """
    Given a kwik embed URL, return the direct m3u8 stream URL or None.
    """
    # ── Step 1: load the embed page ──────────────────────────────────────────
    resp = _get(kwik_url, referer=f"{_base_url()}/")
    if not resp.ok:
        print(Fore.RED + f"[kwik] embed page failed: HTTP {resp.status_code}" + Fore.RESET)
        return None

    html = resp.text

    # ── Step 2: try direct m3u8 extraction first ─────────────────────────────
    m = re.search(r'(https://[^\s"\'<]+\.m3u8[^\s"\'<]*)', html)
    if m:
        return m.group(1)

    # ── Step 3: unpack all packed JS payloads and search for m3u8 ────────────
    for unpacked in _unpack_payloads(html):
        # Direct m3u8 URL
        m = re.search(r'(https://[^\s"\'<]+\.m3u8[^\s"\'<]*)', unpacked)
        if m:
            return m.group(1)

        # let/var url = '...'
        m = re.search(r"(?:let|var|const)\s+(?:url|source|stream)\s*=\s*[\"']([^\"']+)[\"']", unpacked)
        if m and '.m3u8' in m.group(1):
            return m.group(1)

    # ── Step 4: legacy form-POST flow (old kwik.si format) ───────────────────
    token_match = re.search(
        r'action=["\']([^"\']+)["\'].*?name=["\']_token["\'].*?value=["\']([^"\']+)["\']',
        html, re.DOTALL,
    )
    if token_match:
        form_action, token = token_match.group(1), token_match.group(2)
        print(Fore.CYAN + "[kwik] using legacy form-POST flow" + Fore.RESET)

        post_headers = {
            "User-Agent": _scraper._user_agent,
            "Referer": kwik_url,
            "Origin": re.match(r'https?://[^/]+', kwik_url).group(0),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        }
        time.sleep(REQUEST_DELAY)
        resp2 = _session.post(
            form_action,
            data={"_token": token},
            headers=post_headers,
            allow_redirects=True,
            timeout=30,
        )
        result_html = resp2.text

        m = re.search(r'(https://[^\s"\'<]+\.m3u8[^\s"\'<]*)', result_html)
        if m:
            return m.group(1)

    print(Fore.RED + "[kwik] m3u8 URL not found in response" + Fore.RESET)
    print(Fore.YELLOW +
          "[kwik] tip: kwik layout may have changed — inspect the page manually\n"
          "            and update the regex in kwik.py" + Fore.RESET)
    return None
