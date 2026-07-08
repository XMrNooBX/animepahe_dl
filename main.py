"""
Anime downloader — animepahe
By Ashi and SeD  (revamped 2026)

Requires either:
  • nodriver (pip install nodriver) for automatic Cloudflare solving
  • OR the ANIMEPAHE_CF_CLEARANCE environment variable set to your cf_clearance cookie

See README.md for setup instructions.

Episode input examples:
  5          -> single episode
  1 3 7      -> episodes 1, 3 and 7
  1-12       -> range
  1-5 10 15  -> range + individual picks
  5-         -> episode 5 to the last available

Navigation:
  Enter *    -> go back one step (or exit if at the start)

Usage:
  python main.py              # Interactive mode
  python main.py -o /path     # Custom output directory
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

# === Constants ===
MAX_PARALLEL = 6  # CDN rate-limits beyond this; more workers just fight each other

# Sentinel returned by interactive functions when the user types "*"
_GO_BACK = object()


# === Helpers ===================================================================================================─

def parse_episode_input(raw: str, available: set) -> list:
    """Parse episode selection string. Supports 'x-' to mean x through last available."""
    ep_in = []
    max_available = max(float(x) for x in available)

    for token in raw.strip().split():
        if "-" in token:
            parts = token.split("-")
            try:
                start = float(parts[0])
                # Open-ended range: "5-" means 5 through the last episode
                end_str = parts[-1].strip()
                if end_str == "":
                    end = max_available
                else:
                    end = float(end_str)
                # Generate sequence with step 1 for integer parts, or step for fractional
                if start == int(start) and end == int(end):
                    ep_in.extend(range(int(start), int(end) + 1))
                else:
                    # Handle fractional episodes with 0.5 step
                    current = start
                    while current <= end + 1e-9:  # small epsilon for float comparison
                        ep_in.append(current)
                        current += 1.0
            except ValueError:
                print(Fore.RED + f"[skip] bad range: {token}" + Fore.RESET)
        else:
            try:
                ep = float(token)
                # Normalize: if it's a whole number, store as int for cleaner display
                if ep == int(ep):
                    ep = int(ep)
                ep_in.append(ep)
            except ValueError:
                print(Fore.RED + f"[skip] bad token: {token}" + Fore.RESET)

    valid = []
    # Convert available to floats for comparison
    available_floats = set(float(x) for x in available)
    for ep in sorted(set(ep_in)):
        if ep in available_floats:
            valid.append(ep)
        else:
            print(Fore.YELLOW + f"[warn] episode {ep} not found, skipping" + Fore.RESET)
    return valid


def _check_cloudflare(resp):
    """Print a friendly error if we hit a Cloudflare block page."""
    if resp.status_code in (403, 503) or "Just a moment" in resp.text:
        print(Fore.RED + "\n[blocked] Cloudflare challenge detected.")
        print(Fore.YELLOW +
              "To fix this, choose one of:\n"
              "  A) Install nodriver:  pip install nodriver  (automatic)\n"
              "  B) Open animepahe.pw in your browser, wait for it to load,\n"
              "     then copy the 'cf_clearance' cookie value and set:\n"
              "     set ANIMEPAHE_CF_CLEARANCE=<your_cookie_value>" +
              Fore.RESET)
        sys.exit(1)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AnimePahe downloader — fetch & download anime episodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Episode format examples:
  5         -> single episode
  1 3 7     -> episodes 1, 3 and 7
  1-12      -> range 1 to 12
  5-        -> episode 5 to the last available
  1-5 10 15 -> range + individual picks

Navigation:
  *         -> go back one step (or exit if at the start)

Examples:
  %(prog)s                    # Fully interactive
  %(prog)s -o /path/to/dir    # Custom output directory
"""
    )
    p.add_argument("-o", "--output-dir", type=str, help="Custom output directory (default: current dir)")
    return p


# === Main flow ================================================================================================─

def _search_anime(name: str) -> dict:
    """Search for anime, with retries."""
    print(Fore.GREEN + "Searching..." + Fore.RESET)
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
                print(Fore.YELLOW + f"[retry] attempt {attempt} failed ({e}), retrying..." + Fore.RESET)
                time.sleep(2)

    if results is None:
        print(Fore.RED + f"Search failed after 3 attempts: {last_err}")
        sys.exit(1)

    if not results:
        print(Fore.RED + "No results found.")
        sys.exit(1)
    return results


def _step_enter_name():
    """Step 0: Enter anime name. Returns name string, or exits on '*'."""
    raw = input(Fore.CYAN + "\nEnter anime name: " + Fore.RESET).strip()
    if raw == "*":
        print(Fore.YELLOW + "Exiting." + Fore.RESET)
        sys.exit(0)
    if not raw:
        print(Fore.RED + "No name entered." + Fore.RESET)
        return None  # retry this step
    return raw


def _step_select_anime(results: dict):
    """Step 1: Pick an anime from search results. Returns (anime_id, title) or _GO_BACK."""
    ids = scraper.show_results_get_id(results)

    raw = input(Fore.CYAN + "\nPick a number: " + Fore.RESET).strip()
    if raw == "*":
        return _GO_BACK

    try:
        pick = int(raw) - 1
        anime_id = ids[pick]
        title = list(results.keys())[pick]
        return anime_id, title
    except (ValueError, IndexError):
        print(Fore.RED + "Invalid selection." + Fore.RESET)
        return None  # retry this step


def _step_select_episodes(eps: dict, title: str):
    """Step 2: Select episodes. Returns episode list, or _GO_BACK."""
    available = set(eps.keys())
    ep_keys = sorted(available)
    first_ep = ep_keys[0]
    last_ep = ep_keys[-1]
    print(Fore.WHITE + "\nAvailable episodes: " +
          Fore.LIGHTMAGENTA_EX + f"{first_ep} - {last_ep}" + Fore.RESET)

    print(Fore.CYAN + "Episodes to download (e.g.  1-5  or  1 3 7  or  5-): " + Fore.RESET, end="")
    raw_in = input().strip()

    if raw_in == "*":
        return _GO_BACK

    # Check for open-ended range (e.g. "5-") and confirm with user
    has_open_range = False
    for token in raw_in.split():
        if token.endswith("-") and not token == "-":
            has_open_range = True
            break

    ep_list = parse_episode_input(raw_in, available)
    if not ep_list:
        print(Fore.RED + "No valid episodes selected." + Fore.RESET)
        return None  # retry this step

    # Confirmation prompt for open-ended ranges
    if has_open_range:
        print(Fore.LIGHTYELLOW_EX +
              f"You are about to download episodes {ep_list[0]}-{ep_list[-1]} "
              f"({len(ep_list)} eps) for {title}, proceed? (y/n): " +
              Fore.RESET, end="")
        confirm = input().strip().lower()
        if confirm != "y":
            print(Fore.YELLOW + "Cancelled." + Fore.RESET)
            return None  # retry this step

    return ep_list


def _step_resolve_streams(ep_links: dict):
    """Step 3: Quality selection + stream resolution. Returns dl_queue or _GO_BACK."""
    dl_queue = {}
    saved_quality = None

    for i, ep in enumerate(sorted(ep_links.keys())):
        links = ep_links[ep]
        remaining = len(ep_links) - i

        print(Fore.LIGHTGREEN_EX + f"\n=== Episode {ep} ===" + Fore.RESET)

        kwik_urls = scraper.show_dl_opts(links)

        # Use saved quality if available
        if saved_quality is not None and saved_quality < len(kwik_urls):
            choice = saved_quality
            quality_label = links[choice][1].strip() if len(links[choice]) > 1 else "?"
            print(Fore.WHITE + f"Using saved quality: {quality_label}" + Fore.RESET)
        else:
            # Interactive selection
            prompt = "Choose quality"
            if remaining > 1:
                prompt += " (add 'a' to apply to all remaining, e.g. '1a')"
            prompt += ": "

            raw_choice = input(Fore.CYAN + prompt + Fore.RESET).strip()

            # Back navigation — go back to episode selection
            if raw_choice == "*":
                return _GO_BACK

            apply_all = raw_choice.endswith("a") or raw_choice.endswith("A")
            raw_choice = raw_choice.rstrip("aA").strip()

            try:
                choice = int(raw_choice) - 1
                _ = kwik_urls[choice]  # validate index
            except (ValueError, IndexError):
                print(Fore.RED + "Invalid choice, skipping." + Fore.RESET)
                continue

            if apply_all:
                print(Fore.GREEN + "Quality choice saved for remaining episodes [OK]" + Fore.RESET)
                saved_quality = choice

        kwik_url = links[choice][0]
        print(Fore.WHITE + "Resolving stream URL..." + Fore.RESET)
        m3u8 = get_stream_url(kwik_url)
        if not m3u8:
            print(Fore.RED + f"[skip] could not resolve stream for ep {ep}" + Fore.RESET)
            continue

        dl_queue[ep] = m3u8
        print(Fore.GREEN + "Stream resolved [OK]" + Fore.RESET)

    return dl_queue


def _fetch_episodes(anime_id: str) -> dict:
    """Fetch all episodes for an anime."""
    print(Fore.GREEN + "Fetching episode list..." + Fore.RESET)
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


def _fetch_episode_links(anime_id: str, eps: dict, ep_list: list) -> dict:
    """Fetch download links for each episode. Returns {ep: links}.

    Uses progressive delays between requests to avoid triggering the
    server's rate limiter, and retries any episodes that fail with a
    longer cooldown between retry rounds.
    """
    _LINK_FETCH_MAX_RETRIES = 2   # extra retry rounds for failed episodes
    _LINK_FETCH_COOLDOWN = 10     # seconds to wait before retrying failed eps

    ep_links: dict = {}
    remaining = list(ep_list)

    for attempt in range(1 + _LINK_FETCH_MAX_RETRIES):
        failed = []

        for i, ep in enumerate(remaining):
            ep_session = eps[ep]

            # Progressive delay: ramp up from 0→2s as we fetch more links
            # in one burst, to keep us under the server's rate window
            if i > 0:
                extra_delay = min(i * 0.3, 2.0)
                time.sleep(extra_delay)

            try:
                links = scraper.get_ep_links(anime_id, ep_session)
            except Exception as e:
                print(Fore.RED + f"[error] could not fetch links for ep {ep}: {e}" + Fore.RESET)
                failed.append(ep)
                continue
            if not links:
                print(Fore.RED + f"[skip] no download links for ep {ep}" + Fore.RESET)
                continue
            ep_links[ep] = links

        if not failed:
            break

        if attempt < _LINK_FETCH_MAX_RETRIES:
            print(Fore.YELLOW +
                  f"[retry] {len(failed)} episode(s) hit rate limit — "
                  f"waiting {_LINK_FETCH_COOLDOWN}s before retry "
                  f"(round {attempt + 2}/{1 + _LINK_FETCH_MAX_RETRIES})..."
                  + Fore.RESET)
            time.sleep(_LINK_FETCH_COOLDOWN)
            remaining = failed
        else:
            print(Fore.RED +
                  f"[error] gave up on {len(failed)} episode(s) after "
                  f"{1 + _LINK_FETCH_MAX_RETRIES} attempts: {failed}"
                  + Fore.RESET)

    return ep_links


def _download_episodes(dl_queue: dict, title: str, output_dir: str | None,
                        metadata: dict | None = None):
    """Download all resolved episodes in batched parallel waves.

    Episodes are split into batches of up to MAX_PARALLEL (6).
    Each batch runs concurrently, and the next batch starts only after the
    current one finishes.  This avoids CDN rate-limiting while still giving
    full parallelism within each wave.

    Resume is always enabled — partially downloaded episodes are continued
    from where they left off.
    """
    if not dl_queue:
        print(Fore.RED + "\nNothing to download." + Fore.RESET)
        sys.exit(1)

    total = len(dl_queue)
    effective_parallel = min(total, MAX_PARALLEL)

    print(Fore.CYAN + f"\n{'='*40}" + Fore.RESET)
    print(Fore.CYAN + f"Downloading {total} episode(s)" +
          (f" ({effective_parallel} parallel per batch)" if total > 1 else "") +
          "..." + Fore.RESET)
    print(Fore.CYAN + f"{'='*40}" + Fore.RESET)

    # Avoid os.chdir in parallel mode - use absolute paths instead
    if output_dir and total > 1:
        import os
        os.makedirs(output_dir, exist_ok=True)
        chdir_needed = False
    elif output_dir:
        import os
        orig_cwd = os.getcwd()
        os.makedirs(output_dir, exist_ok=True)
        os.chdir(output_dir)
        chdir_needed = True
    else:
        chdir_needed = False

    def _download_one(ep_m3u8_meta: tuple) -> tuple[int, bool]:
        ep, m3u8, ep_meta = ep_m3u8_meta
        try:
            scraper.download_vid(m3u8, title, ep, meta=ep_meta, no_resume=False)
            return ep, True
        except Exception as e:
            print(Fore.RED + f"[error] Episode {ep} failed: {e}" + Fore.RESET)
            return ep, False

    episodes_to_download = [(ep, m3u8, metadata.get(ep) if metadata else None)
                            for ep, m3u8 in dl_queue.items()]

    failed_episodes = []

    if total > 1:
        # Split into batches of effective_parallel
        batches = [episodes_to_download[i:i + effective_parallel]
                   for i in range(0, len(episodes_to_download), effective_parallel)]
        num_batches = len(batches)

        for batch_idx, batch in enumerate(batches, 1):
            ep_nums = [item[0] for item in batch]
            if num_batches > 1:
                print(Fore.CYAN + f"\n>> Batch {batch_idx}/{num_batches}  "
                      f"episodes {ep_nums}  ({len(batch)} parallel)" + Fore.RESET)

            with concurrent.futures.ThreadPoolExecutor(max_workers=effective_parallel) as executor:
                future_to_ep = {executor.submit(_download_one, item): item[0]
                                for item in batch}
                for future in concurrent.futures.as_completed(future_to_ep):
                    ep, success = future.result()
                    if not success:
                        failed_episodes.append(ep)

            if num_batches > 1:
                done_so_far = sum(len(b) for b in batches[:batch_idx])
                print(Fore.GREEN + f"[done] Batch {batch_idx}/{num_batches} complete  "
                      f"({done_so_far}/{total} processed)" + Fore.RESET)
    else:
        # Single episode — no threading overhead
        for ep, m3u8 in dl_queue.items():
            ep_meta = metadata.get(ep) if metadata else None
            try:
                scraper.download_vid(m3u8, title, ep, meta=ep_meta, no_resume=False)
            except Exception as e:
                print(Fore.RED + f"[error] Episode {ep} failed: {e}" + Fore.RESET)
                failed_episodes.append(ep)

    if chdir_needed:
        os.chdir(orig_cwd)

    # Final summary
    succeeded = total - len(failed_episodes)
    if failed_episodes:
        print(Fore.YELLOW + f"\n[!] {len(failed_episodes)} episode(s) failed: "
              f"{sorted(failed_episodes)}" + Fore.RESET)
    print(Fore.GREEN + f"\nAll done!  ({succeeded}/{total} succeeded)" + Fore.RESET)


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # 1. Init session (cached cookies or nodriver)
    print(Fore.CYAN + "Initialising session..." + Fore.RESET)
    scraper.init_session()

    # ── State machine with back-navigation ────────────────────────────────
    #
    # Steps:
    #   0 — Enter anime name            (* → exit)
    #   1 — Pick anime from results     (* → step 0)
    #   2 — Select episodes             (* → step 1)
    #   3 — Quality + resolve streams   (* → step 2)
    #   4 — Download (no going back)
    #
    # Each step can return _GO_BACK to go back, None to retry itself,
    # or a value to proceed to the next step.

    step = 0
    name = None
    results = None
    anime_id = None
    title = None
    eps = None
    ep_list = None
    ep_links = None
    dl_queue = None

    while True:
        # ── Step 0: Enter anime name ──────────────────────────────────────
        if step == 0:
            result = _step_enter_name()
            if result is None:
                continue  # retry (empty input)
            name = result
            results = _search_anime(name)
            step = 1

        # ── Step 1: Pick anime from results ───────────────────────────────
        elif step == 1:
            result = _step_select_anime(results)
            if result is _GO_BACK:
                print(Fore.YELLOW + "← Back to search" + Fore.RESET)
                step = 0
                continue
            if result is None:
                continue  # retry (invalid pick)
            anime_id, title = result
            print(Fore.GREEN + f"\nSelected: {title}" + Fore.RESET)

            # Fetch episode list (network step — only when anime changes)
            eps = _fetch_episodes(anime_id)
            step = 2

        # ── Step 2: Select episodes ───────────────────────────────────────
        elif step == 2:
            result = _step_select_episodes(eps, title)
            if result is _GO_BACK:
                print(Fore.YELLOW + "← Back to anime selection" + Fore.RESET)
                step = 1
                continue
            if result is None:
                continue  # retry (bad input or cancelled confirmation)
            ep_list = result

            # Fetch links for selected episodes
            ep_links = _fetch_episode_links(anime_id, eps, ep_list)
            if not ep_links:
                print(Fore.RED + "\nNo download links available for selected episodes." + Fore.RESET)
                continue  # retry episode selection
            step = 3

        # ── Step 3: Quality selection + stream resolution ─────────────────
        elif step == 3:
            result = _step_resolve_streams(ep_links)
            if result is _GO_BACK:
                print(Fore.YELLOW + "← Back to episode selection" + Fore.RESET)
                step = 2
                continue
            dl_queue = result
            if not dl_queue:
                print(Fore.RED + "\nNo streams resolved." + Fore.RESET)
                continue  # retry quality selection
            step = 4

        # ── Step 4: Download ──────────────────────────────────────────────
        elif step == 4:
            # Fetch episode metadata for chapters & cover art
            metadata = {}
            if dl_queue:
                print(Fore.WHITE + "\nFetching episode metadata..." + Fore.RESET)
                metadata = scraper.fetch_all_episode_meta(anime_id, eps, ep_list)

            _download_episodes(dl_queue, title, args.output_dir, metadata)
            break  # all done


if __name__ == "__main__":
    main()
