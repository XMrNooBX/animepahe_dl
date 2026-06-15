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

Usage examples:
  python main.py                                    # Interactive mode
  python main.py -a "Witch Hat Atelier" -e 1-3      # Batch mode
  python main.py -a "One Piece" -e 1-10 -q 3        # Specific quality (1080p Sub)
  python main.py -a "One Piece" -e 5 -q 6 -b        # 1080p Dub, batch mode
  python main.py -a "One Piece" -e 1-5 -q 2 -o /custom/path  # Custom output dir
"""

import argparse
import asyncio
import concurrent.futures
import sys
import time

from colorama import Fore, init

import scraper
from kwik import get_stream_url
from scraper import EpisodeMeta  # for type hints

init(autoreset=True)

# === Quality mapping ===
QUALITY_MAP = {
    1: ("360", "Sub"),
    2: ("720", "Sub"),
    3: ("1080", "Sub"),
    4: ("360", "Dub"),
    5: ("720", "Dub"),
    6: ("1080", "Dub"),
}

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


def parse_quality_arg(qarg: str) -> int | None:
    """Parse quality argument. Accepts index (1-6) or resolution+type (e.g., '1080p', '720dub')."""
    if not qarg:
        return None
    qarg = qarg.lower().strip()
    
    # Try numeric index first
    if qarg.isdigit():
        idx = int(qarg)
        if 1 <= idx <= 6:
            return idx - 1
    
    # Try resolution+type patterns
    # 360p, 720p, 1080p, 360sub, 720dub, etc.
    for idx, (res, typ) in QUALITY_MAP.items():
        patterns = [
            f"{res}p", f"{res}sub", f"{res}dub",
            f"{res} sub", f"{res} dub",
            f"{res.lower()}p", f"{res.lower()}sub", f"{res.lower()}dub",
        ]
        if qarg in patterns:
            return idx - 1
    
    print(Fore.YELLOW + f"[warn] Unknown quality '{qarg}', will prompt interactively" + Fore.RESET)
    return None


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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AnimePahe downloader — fetch & download anime episodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quality options (index or name):
  1  360p Sub    2  720p Sub    3  1080p Sub
  4  360p Dub    5  720p Dub    6  1080p Dub

Episode format examples:
  5         → single episode
  1 3 7     → episodes 1, 3 and 7
  1-12      → range 1 to 12
  1-5 10 15 → range + individual picks

Examples:
  %(prog)s                           # Fully interactive
  %(prog)s -a "Witch Hat Atelier"    # Search & select anime interactively
  %(prog)s -a "One Piece" -e 1-5     # Batch: episodes 1-5, interactive quality
  %(prog)s -a "One Piece" -e 1-5 -q 3 -b  # Batch: 1-5, 1080p Sub, no prompts
  %(prog)s -a "One Piece" -e 10 -q 6     # Single ep, 1080p Dub
  %(prog)s -o /path/to/downloads       # Custom output directory
"""
    )
    p.add_argument("-a", "--anime", type=str, help="Anime name to search (skips interactive search)")
    p.add_argument("-e", "--episodes", type=str, help="Episodes to download (e.g., '1-5', '1 3 7')")
    p.add_argument("-q", "--quality", type=str, help="Quality preset (1-6 or '1080p', '720dub', etc.)")
    p.add_argument("-o", "--output-dir", type=str, help="Custom output directory (default: current dir)")
    p.add_argument("-b", "--batch", action="store_true", 
                   help="Batch mode: apply --quality to all episodes, skip interactive prompts")
    p.add_argument("--list", action="store_true",
                   help="List available episodes & qualities after anime selection, then exit")
    p.add_argument("--no-resume", action="store_true",
                   help="Disable resume (re-download even if partial state exists)")
    p.add_argument("-p", "--parallel", type=int, default=1, metavar="N",
                   help="Number of episodes to download in parallel (default: 1)")
    return p


# === Main flow ================================================================================================─

def _search_anime(name: str) -> dict:
    """Search for anime, with retries."""
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
    return results


def _select_anime(results: dict, anime_arg: str | None) -> tuple[str, str]:
    """Select anime from results. Returns (anime_id, title)."""
    ids = scraper.show_results_get_id(results)
    
    if anime_arg:
        # Try to match by name (case-insensitive partial)
        matches = [(i, t) for i, t in enumerate(results.keys()) if anime_arg.lower() in t.lower()]
        if len(matches) == 1:
            pick = matches[0][0]
            print(Fore.GREEN + f"\nAuto-selected: {matches[0][1]}" + Fore.RESET)
        else:
            # Ambiguous or no match - fall back to interactive
            print(Fore.YELLOW + f"[warn] '{anime_arg}' matched {len(matches)} results, showing all:" + Fore.RESET)
            for i, (idx, title) in enumerate(matches, 1):
                print(f"  {i}. {title}")
            try:
                pick = int(input(Fore.CYAN + "\nPick a number: " + Fore.RESET)) - 1
                pick = matches[pick][0]
            except (ValueError, IndexError):
                print(Fore.RED + "Invalid selection." + Fore.RESET)
                sys.exit(1)
    else:
        # Fully interactive
        try:
            pick = int(input(Fore.CYAN + "\nPick a number: " + Fore.RESET)) - 1
        except (ValueError, IndexError):
            print(Fore.RED + "Invalid selection." + Fore.RESET)
            sys.exit(1)
    
    anime_id = ids[pick]
    title = list(results.keys())[pick]
    return anime_id, title


def _fetch_episodes(anime_id: str) -> dict:
    """Fetch all episodes for an anime."""
    print(Fore.GREEN + "Fetching episode list…" + Fore.RESET)
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
        print(Fore.RED + f"Failed to fetch episode count: {e}" + Fore.RESET)
        sys.exit(1)

    eps = asyncio.run(scraper.get_episode_list(anime_id, last_page))
    if not eps:
        print(Fore.RED + "No episodes found." + Fore.RESET)
        sys.exit(1)
    return eps


def _select_episodes(eps: dict, ep_arg: str | None) -> list:
    """Select episodes from available list."""
    available = set(eps.keys())
    ep_keys = sorted(available)
    print(Fore.WHITE + "\nAvailable episodes: " +
          Fore.LIGHTMAGENTA_EX + f"{ep_keys[0]} - {ep_keys[-1]}" + Fore.RESET)

    if ep_arg:
        ep_list = parse_episode_input(ep_arg, available)
        if not ep_list:
            print(Fore.RED + "No valid episodes selected." + Fore.RESET)
            sys.exit(1)
        print(Fore.GREEN + f"Selected episodes: {ep_list}" + Fore.RESET)
    else:
        print(Fore.CYAN + "Episodes to download (e.g.  1-5  or  1 3 7  or  1-5 10): " + Fore.RESET, end="")
        raw_in = input().strip()
        ep_list = parse_episode_input(raw_in, available)
        if not ep_list:
            print(Fore.RED + "No valid episodes selected." + Fore.RESET)
            sys.exit(1)
    return ep_list


def _fetch_episode_links(anime_id: str, eps: dict, ep_list: list) -> dict[int, list]:
    """Fetch download links for each episode. Returns {ep: links}."""
    ep_links = {}
    for ep in ep_list:
        ep_session = eps[ep]
        try:
            links = scraper.get_ep_links(anime_id, ep_session)
        except Exception as e:
            print(Fore.RED + f"[error] could not fetch links for ep {ep}: {e}" + Fore.RESET)
            continue
        if not links:
            print(Fore.RED + f"[skip] no download links for ep {ep}" + Fore.RESET)
            continue
        ep_links[ep] = links
    return ep_links


def _select_quality(links: list, quality_arg: str | None, batch_mode: bool, 
                     saved_quality: int | None, remaining_eps: int) -> tuple[int, int | None]:
    """Interactive or auto quality selection. Returns (choice, new_saved_quality)."""
    kwik_urls = scraper.show_dl_opts(links)

    # Auto-select if quality arg provided and valid
    if quality_arg is not None:
        q_idx = parse_quality_arg(quality_arg)
        if q_idx is not None and q_idx < len(kwik_urls):
            choice = q_idx
            quality_label = links[choice][1].strip() if len(links[choice]) > 1 else "?"
            print(Fore.WHITE + f"Using CLI quality: {quality_label}" + Fore.RESET)
            if batch_mode:
                return choice, choice  # Save for all remaining
            return choice, None

    # Use saved quality if available
    if saved_quality is not None and saved_quality < len(kwik_urls):
        choice = saved_quality
        quality_label = links[choice][1].strip() if len(links[choice]) > 1 else "?"
        print(Fore.WHITE + f"Using saved quality: {quality_label}" + Fore.RESET)
        return choice, saved_quality

    # Interactive selection
    remaining = remaining_eps
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
        print(Fore.RED + "Invalid choice, skipping." + Fore.RESET)
        return -1, saved_quality

    if apply_all:
        print(Fore.GREEN + "Quality choice saved for remaining episodes [OK]" + Fore.RESET)
        return choice, choice

    return choice, saved_quality


def _resolve_streams(anime_id: str, ep_links: dict[int, list], quality_arg: str | None, 
                      batch_mode: bool) -> dict[int, str]:
    """Resolve stream URLs for all episodes."""
    dl_queue = {}
    saved_quality = None
    
    for i, ep in enumerate(sorted(ep_links.keys())):
        links = ep_links[ep]
        remaining = len(ep_links) - i
        
        print(Fore.LIGHTGREEN_EX + f"\n=== Episode {ep} ===" + Fore.RESET)
        
        choice, saved_quality = _select_quality(
            links, quality_arg if i == 0 else None, 
            batch_mode, saved_quality, remaining
        )
        
        if choice < 0:
            continue
            
        kwik_url = links[choice][0]
        print(Fore.WHITE + "Resolving stream URL…" + Fore.RESET)
        m3u8 = get_stream_url(kwik_url)
        if not m3u8:
            print(Fore.RED + f"[skip] could not resolve stream for ep {ep}" + Fore.RESET)
            continue

        dl_queue[ep] = m3u8
        print(Fore.GREEN + "Stream resolved [OK]" + Fore.RESET)
    
    return dl_queue


def _download_episodes(dl_queue: dict[int, str], title: str, output_dir: str | None, 
                        no_resume: bool, metadata: dict[int, EpisodeMeta] | None = None,
                        parallel: int = 1):
    """Download all resolved episodes, optionally in parallel."""
    if not dl_queue:
        print(Fore.RED + "\nNothing to download." + Fore.RESET)
        sys.exit(1)

    print(Fore.CYAN + f"\n{'='*40}" + Fore.RESET)
    print(Fore.CYAN + f"Downloading {len(dl_queue)} episode(s)" + 
          (f" with {parallel} parallel" if parallel > 1 else "") + "..." + Fore.RESET)
    print(Fore.CYAN + f"{'='*40}" + Fore.RESET)

    if output_dir:
        import os
        orig_cwd = os.getcwd()
        os.makedirs(output_dir, exist_ok=True)
        os.chdir(output_dir)

    def _download_one(ep_m3u8_meta: tuple) -> tuple[int, bool]:
        ep, m3u8, ep_meta = ep_m3u8_meta
        try:
            scraper.download_vid(m3u8, title, ep, meta=ep_meta)
            return ep, True
        except Exception as e:
            print(Fore.RED + f"[error] Episode {ep} failed: {e}" + Fore.RESET)
            return ep, False

    episodes_to_download = [(ep, m3u8, metadata.get(ep) if metadata else None) 
                            for ep, m3u8 in dl_queue.items()]

    if parallel > 1 and len(episodes_to_download) > 1:
        # Parallel download with progress tracking
        print(Fore.WHITE + f"  Starting {parallel} parallel downloads…" + Fore.RESET)
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
            future_to_ep = {executor.submit(_download_one, item): item[0] for item in episodes_to_download}
            for future in concurrent.futures.as_completed(future_to_ep):
                ep, success = future.result()
                if success:
                    pass  # Progress shown by individual download
                else:
                    print(Fore.YELLOW + f"  Episode {ep} had errors" + Fore.RESET)
    else:
        # Sequential download (original behavior)
        for ep, m3u8 in dl_queue.items():
            ep_meta = metadata.get(ep) if metadata else None
            scraper.download_vid(m3u8, title, ep, meta=ep_meta)

    if output_dir:
        os.chdir(orig_cwd)

    print(Fore.GREEN + "\nAll done!" + Fore.RESET)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # 1. Init session (cached cookies or nodriver)
    print(Fore.CYAN + "Initialising session…" + Fore.RESET)
    scraper.init_session()

    # 2. Search
    if args.anime:
        name = args.anime.strip()
        print(Fore.CYAN + f"\nSearching for: {name}" + Fore.RESET)
    else:
        name = input(Fore.CYAN + "\nEnter anime name: " + Fore.RESET).strip()
        if not name:
            print(Fore.RED + "No name entered." + Fore.RESET)
            sys.exit(1)

    results = _search_anime(name)

    # 3. Select anime
    anime_id, title = _select_anime(results, args.anime)
    print(Fore.GREEN + f"\nSelected: {title}" + Fore.RESET)

    # 4. Fetch episodes
    eps = _fetch_episodes(anime_id)

    # 5. Select episodes
    ep_list = _select_episodes(eps, args.episodes)

    # 6. Handle --list flag
    if args.list:
        print(Fore.CYAN + "\nAvailable qualities for each episode:" + Fore.RESET)
        for ep in sorted(ep_list):
            links = scraper.get_ep_links(anime_id, eps[ep])
            if links:
                print(Fore.LIGHTGREEN_EX + f"\n  Episode {ep}:" + Fore.RESET)
                scraper.show_dl_opts(links)
        sys.exit(0)

    # 7. Fetch links for selected episodes
    ep_links = _fetch_episode_links(anime_id, eps, ep_list)
    if not ep_links:
        print(Fore.RED + "\nNo download links available for selected episodes." + Fore.RESET)
        sys.exit(1)

    # 8. Resolve streams
    dl_queue = _resolve_streams(anime_id, ep_links, args.quality, args.batch)

    # 9. Fetch episode metadata for chapters & cover art
    metadata = {}
    if dl_queue:
        print(Fore.WHITE + "\nFetching episode metadata…" + Fore.RESET)
        metadata = scraper.fetch_all_episode_meta(anime_id, eps)

    # 10. Download
    _download_episodes(dl_queue, title, args.output_dir, args.no_resume, metadata, args.parallel)


if __name__ == "__main__":
    main()
