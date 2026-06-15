"""
Anime downloader — animepahe
By Ashi and SeD  (revamped 2026)

Requires either:
  • nodriver (pip install nodriver) for automatic Cloudflare solving
  • OR the ANIMEPAHE_CF_CLEARANCE environment variable set to your cf_clearance cookie

See README.md for setup instructions.

Episode input examples:
  5          → single episode
  1 3 7      → episodes 1, 3 and 7
  1-12       → range
  1-5 10 15  → range + individual picks
"""

import asyncio
import sys
import time

from colorama import Fore, init

import scraper
from kwik import get_stream_url

init(autoreset=True)


# === Helpers ===================================================================================================─

def parse_episode_input(raw: str, available: set) -> list:
    ep_in = []
    for token in raw.strip().split():
        if "-" in token:
            parts = token.split("-")
            try:
                start, end = int(parts[0]), int(parts[-1])
                ep_in.extend(range(start, end + 1))
            except ValueError:
                print(Fore.RED + f"[skip] bad range: {token}")
        else:
            try:
                ep_in.append(int(token))
            except ValueError:
                print(Fore.RED + f"[skip] bad token: {token}")

    valid = []
    for ep in sorted(set(ep_in)):
        if ep in available:
            valid.append(ep)
        else:
            print(Fore.YELLOW + f"[warn] episode {ep} not found, skipping")
    return valid


def _check_cloudflare(resp):
    """Print a friendly error if we hit a Cloudflare block page."""
    if resp.status_code in (403, 503) or "Just a moment" in resp.text:
        print(Fore.RED + "\n[blocked] Cloudflare challenge detected.")
        print(Fore.YELLOW +
              "To fix this, choose one of:\n"
              "  A) Install nodriver:  pip install nodriver  (automatic)\n"
              "  B) Open animepahe.org in your browser, wait for it to load,\n"
              "     then copy the 'cf_clearance' cookie value and set:\n"
              "     set ANIMEPAHE_CF_CLEARANCE=<your_cookie_value>" +
              Fore.RESET)
        sys.exit(1)


# === Main flow ================================================================================================─

def main():
    # 1. Init session (cached cookies or nodriver)
    print(Fore.CYAN + "Initialising session…" + Fore.RESET)
    scraper.init_session()

    # 2. Search
    name = input(Fore.CYAN + "\nEnter anime name: " + Fore.RESET).strip()
    if not name:
        print(Fore.RED + "No name entered.")
        sys.exit(1)

    print(Fore.GREEN + "Searching…" + Fore.RESET)
    results = None
    last_err = None
    for attempt in range(1, 4):
        try:
            results = scraper.get_query(name)
            break
        except SystemExit:
            raise
        except Exception as e:
            last_err = e
            if attempt < 3:
                print(Fore.YELLOW + f"[retry] attempt {attempt} failed ({e}), retrying…" + Fore.RESET)
                time.sleep(2)

    if results is None:
        print(Fore.RED + f"Search failed after 3 attempts: {last_err}")
        sys.exit(1)

    if not results:
        print(Fore.RED + "No results found.")
        sys.exit(1)

    ids = scraper.show_results_get_id(results)

    # 3. Pick anime
    try:
        pick = int(input(Fore.CYAN + "\nPick a number: " + Fore.RESET)) - 1
        anime_id = ids[pick]
        title = list(results.keys())[pick]
    except (ValueError, IndexError):
        print(Fore.RED + "Invalid selection.")
        sys.exit(1)

    print(Fore.GREEN + f"\nSelected: {title}" + Fore.RESET)
    print(Fore.GREEN + "Fetching episode list…" + Fore.RESET)

    # 4. Get last page (using the rate-limited _get helper)
    try:
        page_resp = scraper._get(
            f"{scraper.BASE_URL}/api?m=release&id={anime_id}&sort=episode_asc&page=1"
        )
        _check_cloudflare(page_resp)
        page_resp.raise_for_status()
        last_page = page_resp.json().get("last_page", 1)
    except SystemExit:
        raise
    except Exception as e:
        print(Fore.RED + f"Failed to fetch episode count: {e}")
        sys.exit(1)

    # 5. Get all episodes
    eps = asyncio.run(scraper.get_episode_list(anime_id, last_page))
    if not eps:
        print(Fore.RED + "No episodes found.")
        sys.exit(1)

    # 6. Episode selection
    raw_in = input(
        Fore.CYAN + "Episodes to download (e.g.  1-5  or  1 3 7  or  1-5 10): " + Fore.RESET
    )
    ep_list = parse_episode_input(raw_in, set(eps.keys()))
    if not ep_list:
        print(Fore.RED + "No valid episodes selected.")
        sys.exit(1)

    # 7. Per-episode: links → quality choice → resolve stream
    dl_queue = {}
    saved_quality = None  # "apply to all" quality choice

    for i, ep in enumerate(ep_list):
        ep_session = eps[ep]
        print(Fore.LIGHTGREEN_EX + f"\n=== Episode {ep} ===" + Fore.RESET)

        try:
            links = scraper.get_ep_links(anime_id, ep_session)
        except Exception as e:
            print(Fore.RED + f"[error] could not fetch links for ep {ep}: {e}")
            continue

        if not links:
            print(Fore.RED + f"[skip] no download links for ep {ep}")
            continue

        kwik_urls = scraper.show_dl_opts(links)

        # Use saved quality choice if available and valid
        if saved_quality is not None and saved_quality < len(kwik_urls):
            choice = saved_quality
            quality_label = links[choice][1].strip() if len(links[choice]) > 1 else "?"
            print(Fore.WHITE + f"Using saved quality: {quality_label}" + Fore.RESET)
        else:
            remaining = len(ep_list) - i
            prompt = "Choose quality"
            if remaining > 1:
                prompt += f" (add 'a' to apply to all remaining, e.g. '1a')"
            prompt += ": "

            raw_choice = input(Fore.CYAN + prompt + Fore.RESET).strip()
            apply_all = raw_choice.endswith("a") or raw_choice.endswith("A")
            raw_choice = raw_choice.rstrip("aA").strip()

            try:
                choice = int(raw_choice) - 1
                _ = kwik_urls[choice]  # validate index
            except (ValueError, IndexError):
                print(Fore.RED + "Invalid choice, skipping.")
                continue

            if apply_all:
                saved_quality = choice
                print(Fore.GREEN + "Quality choice saved for remaining episodes [OK]" + Fore.RESET)

        kwik_url = kwik_urls[choice]

        print(Fore.WHITE + "Resolving stream URL…" + Fore.RESET)
        m3u8 = get_stream_url(kwik_url)
        if not m3u8:
            print(Fore.RED + f"[skip] could not resolve stream for ep {ep}")
            continue

        dl_queue[ep] = m3u8
        print(Fore.GREEN + "Stream resolved [OK]" + Fore.RESET)

    if not dl_queue:
        print(Fore.RED + "\nNothing to download.")
        sys.exit(1)

    # 8. Download
    print(Fore.CYAN + f"\n{'='*40}" + Fore.RESET)
    print(Fore.CYAN + f"Downloading {len(dl_queue)} episode(s)..." + Fore.RESET)
    print(Fore.CYAN + f"{'='*40}" + Fore.RESET)

    for ep, m3u8 in dl_queue.items():
        scraper.download_vid(m3u8, title, ep)

    print(Fore.GREEN + "\nAll done!" + Fore.RESET)


if __name__ == "__main__":
    main()
