# CHANGES:
#   - fix_cover(): Added pre-flight placeholder URL check (Step 0). When the
#     stored cover_url is a known placeholder (e.g. /dist/img/nocover-new-min.png),
#     the novel is now counted as 'skipped' rather than 'failed'. Previously
#     these went through download_and_save(), which correctly rejected them, but
#     then fix_cover() reported them as failures and printed misleading "✗ Failed"
#     lines. Now they are logged with a clear message and --re-scrape suggestion.
#   - fix_cover(): If --re-scrape is active and the stored URL is a placeholder,
#     we re-scrape the novel page first to check whether the author has since
#     added a real cover, before giving up with 'skipped'.
#   - fix_cover(): Delay logic extracted to _apply_delay() helper so all early-
#     return paths apply the inter-novel delay consistently — previously some
#     early returns skipped the delay entirely, bunching up requests.
#   - _is_placeholder_url(): New helper using the same fragment list as
#     CoverManager internally so placeholder detection is consistent.
#   - All audit, DB query, and rate-limiting logic otherwise unchanged.
# =============================================================================

import argparse
import logging
import os
import random
import sys
import time

from core import DatabaseManager, NetworkClient, NovelRepository
from core.database import NOVEL_STATUS_ABANDONED
from services import BrowserService, CoverManager, ScraperService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

DEBUG = False

MIN_VALID_COVER_BYTES = 1024
COVER_BACKFILL_DELAY_MIN = 5
COVER_BACKFILL_DELAY_MAX = 10

# Same fragments CoverManager uses — keep in sync if CoverManager changes
_PLACEHOLDER_FRAGMENTS = ["d_60_90.jpg", "nocover-new-min.png", "default-cover"]


def _is_placeholder_url(cover_url: str) -> bool:
    """
    Returns True if cover_url is a known generic placeholder that cannot be
    downloaded as a real cover image.

    Parameters:
        cover_url (str): The URL to test.

    Returns:
        bool

    Called by: fix_cover()
    Depends on: _PLACEHOLDER_FRAGMENTS
    """
    if not cover_url:
        return False
    lower = cover_url.lower()
    return any(frag in lower for frag in _PLACEHOLDER_FRAGMENTS)


def _apply_delay(delay_min: float, delay_max: float, title: str):
    """
    Applies a jittered inter-novel delay for CDN rate limiting.

    Parameters:
        delay_min (float): Minimum delay in seconds.
        delay_max (float): Maximum delay in seconds.
        title (str): Novel title (for DEBUG logging).

    Called by: fix_cover() (all return paths)
    Depends on: random.uniform, time.sleep
    """
    delay = random.uniform(delay_min, delay_max)
    if DEBUG:
        logger.debug(f"[_apply_delay] sleeping {delay:.1f}s after '{title}'")
    time.sleep(delay)


def get_all_novels(db_manager: DatabaseManager) -> list[tuple]:
    """
    Returns all non-ABANDONED novels with their cover metadata.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.

    Returns:
        list[tuple]: (id, title, slug, source_url, cover_path, cover_url).

    Called by: main()
    Depends on: DatabaseManager.execute()
    """
    query = """
            SELECT id, title, slug, source_url, cover_path, cover_url
            FROM novels
            WHERE status != ?
            ORDER BY id ASC \
            """
    return db_manager.execute(query, (NOVEL_STATUS_ABANDONED,))


def get_single_novel(db_manager: DatabaseManager, novel_id: int) -> tuple | None:
    """
    Returns cover metadata for a single novel by DB id.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.
        novel_id (int): The DB id of the novel.

    Returns:
        tuple | None

    Called by: main()
    Depends on: DatabaseManager.execute()
    """
    rows = db_manager.execute(
        "SELECT id, title, slug, source_url, cover_path, cover_url FROM novels WHERE id = ?",
        (novel_id,),
    )
    return rows[0] if rows else None


def cover_is_valid(cover_path: str | None, min_size: int) -> bool:
    """
    Returns True if cover_path points to an existing file >= min_size bytes.

    Parameters:
        cover_path (str | None): Path to check.
        min_size (int): Minimum file size in bytes.

    Returns:
        bool

    Called by: audit_novels()
    Depends on: os.path.exists(), os.path.getsize()
    """
    if not cover_path:
        return False
    abs_path = cover_path if os.path.isabs(cover_path) else os.path.abspath(cover_path)
    if not os.path.exists(abs_path):
        return False
    return os.path.getsize(abs_path) >= min_size


def fix_cover(
    novel_id: int,
    title: str,
    slug: str,
    source_url: str | None,
    cover_url: str | None,
    cover_manager: CoverManager,
    scraper,
    repo: NovelRepository,
    re_scrape: bool,
    delay_min: float,
    delay_max: float,
) -> str:
    """
    Attempts to download a valid cover for a single novel.

    Strategy:
      0. If cover_url is a placeholder image, skip (or re-scrape if enabled).
      1. If cover_url is NULL and re_scrape=True, re-scrape to find a URL.
      2. If cover_url is NULL and re_scrape=False, skip with a warning.
      3. Attempt download via CoverManager.
      4. If download fails and re_scrape=True, re-scrape for a fresh URL and retry.

    The inter-novel delay is always applied regardless of outcome.

    Parameters:
        novel_id (int): DB id of the novel.
        title (str): Novel title (for logging).
        slug (str): URL-safe slug for file naming.
        source_url (str | None): Landing page URL (for re-scraping).
        cover_url (str | None): Current DB cover_url.
        cover_manager (CoverManager): Initialized cover manager.
        scraper: ScraperService instance or None.
        repo (NovelRepository): Initialized repository.
        re_scrape (bool): Whether to re-scrape on missing/failed URLs.
        delay_min (float): Minimum inter-novel delay in seconds.
        delay_max (float): Maximum inter-novel delay in seconds.

    Returns:
        str: 'ok', 'skipped', or 'failed'.

    Called by: main()
    Depends on: CoverManager.download_and_save(), ScraperService.scrape_novel(),
                _is_placeholder_url(), _apply_delay()
    """
    if DEBUG:
        logger.debug(
            f"[fix_cover] novel_id={novel_id} cover_url={cover_url} re_scrape={re_scrape}"
        )

    active_cover_url = cover_url

    # --- Step 0: Placeholder URL pre-flight check ---
    # e.g. /dist/img/nocover-new-min.png — no point attempting a download.
    # Count as 'skipped', not 'failed', because the source site has no cover.
    if active_cover_url and _is_placeholder_url(active_cover_url):
        if re_scrape and source_url and scraper:
            logger.info(
                f"  '{title}' — stored URL is a placeholder; re-scraping for real cover..."
            )
            try:
                data = scraper.scrape_novel(source_url)
                fresh_url = data.get("cover_url") if data else None
                if fresh_url and not _is_placeholder_url(fresh_url):
                    active_cover_url = fresh_url
                    repo.db.execute(
                        "UPDATE novels SET cover_url = ? WHERE id = ?",
                        (active_cover_url, novel_id),
                        commit=True,
                    )
                    logger.info(
                        f"  Got real cover_url for '{title}': {active_cover_url}"
                    )
                else:
                    logger.info(
                        f"  Re-scrape for '{title}' also returned no real cover. Skipping."
                    )
                    _apply_delay(delay_min, delay_max, title)
                    return "skipped"
            except Exception as e:
                logger.error(f"  Re-scrape failed for '{title}': {e}")
                _apply_delay(delay_min, delay_max, title)
                return "failed"
        else:
            logger.info(
                f"  '{title}' — cover_url is a placeholder. "
                f"Re-run with --re-scrape to check for a real cover."
            )
            _apply_delay(delay_min, delay_max, title)
            return "skipped"

    # --- Step 1: No cover_url at all ---
    if not active_cover_url:
        if not re_scrape:
            logger.warning(
                f"  '{title}' — no cover_url in DB and --re-scrape not set. Skipping."
            )
            _apply_delay(delay_min, delay_max, title)
            return "skipped"

        if not source_url or not scraper:
            logger.warning(
                f"  '{title}' — cannot re-scrape (missing source_url or scraper). Skipping."
            )
            _apply_delay(delay_min, delay_max, title)
            return "skipped"

        logger.info(f"  '{title}' — cover_url missing, re-scraping: {source_url}")
        try:
            data = scraper.scrape_novel(source_url)
            fresh_url = data.get("cover_url") if data else None
            if fresh_url and not _is_placeholder_url(fresh_url):
                active_cover_url = fresh_url
                repo.db.execute(
                    "UPDATE novels SET cover_url = ? WHERE id = ?",
                    (active_cover_url, novel_id),
                    commit=True,
                )
                logger.info(f"  Updated cover_url for '{title}': {active_cover_url}")
            else:
                logger.warning(
                    f"  Re-scrape for '{title}' returned no real cover_url. Skipping."
                )
                _apply_delay(delay_min, delay_max, title)
                return "skipped"
        except Exception as e:
            logger.error(f"  Re-scrape failed for '{title}': {e}")
            _apply_delay(delay_min, delay_max, title)
            return "failed"

    # --- Step 2: Attempt download ---
    logger.info(f"  Downloading cover for '{title}' from: {active_cover_url}")
    result_path = None
    try:
        result_path = cover_manager.download_and_save(active_cover_url, novel_id, slug)
    except Exception as e:
        logger.error(f"  cover_manager.download_and_save() raised: {e}")

    # --- Step 3: Re-scrape for fresh URL if download failed ---
    if not result_path and re_scrape and source_url and scraper:
        logger.info(
            f"  Download failed for '{title}', re-scraping for fresh cover_url..."
        )
        try:
            data = scraper.scrape_novel(source_url)
            fresh_url = data.get("cover_url") if data else None
            if (
                fresh_url
                and fresh_url != active_cover_url
                and not _is_placeholder_url(fresh_url)
            ):
                repo.db.execute(
                    "UPDATE novels SET cover_url = ? WHERE id = ?",
                    (fresh_url, novel_id),
                    commit=True,
                )
                logger.info(
                    f"  Retrying with fresh cover_url for '{title}': {fresh_url}"
                )
                result_path = cover_manager.download_and_save(fresh_url, novel_id, slug)
            else:
                logger.warning(
                    f"  Re-scrape did not yield a new cover_url for '{title}'."
                )
        except Exception as e:
            logger.error(f"  Re-scrape retry failed for '{title}': {e}")

    _apply_delay(delay_min, delay_max, title)

    if result_path:
        logger.info(f"  ✓ Cover saved for '{title}': {result_path}")
        return "ok"
    else:
        logger.warning(f"  ✗ Failed to obtain cover for '{title}'.")
        return "failed"


def audit_novels(novels: list[tuple], min_size: int) -> tuple[list, list]:
    """
    Splits novels into those needing a cover and those already valid.

    Parameters:
        novels (list[tuple]): (id, title, slug, source_url, cover_path, cover_url).
        min_size (int): Minimum valid cover file size in bytes.

    Returns:
        tuple[list, list]: (needs_cover, already_valid).

    Called by: main()
    Depends on: cover_is_valid()
    """
    needs_cover = []
    already_valid = []

    for row in novels:
        novel_id, title, slug, source_url, cover_path, cover_url = row
        if cover_is_valid(cover_path, min_size):
            already_valid.append(row)
        else:
            reason = _invalid_reason(cover_path, min_size)
            logger.info(f"  Needs cover [{reason}]: '{title}' (id={novel_id})")
            needs_cover.append(row)

    return needs_cover, already_valid


def _invalid_reason(cover_path: str | None, min_size: int) -> str:
    """
    Returns a short human-readable reason a cover is invalid.

    Called by: audit_novels()
    """
    if not cover_path:
        return "no cover_path"
    abs_path = cover_path if os.path.isabs(cover_path) else os.path.abspath(cover_path)
    if not os.path.exists(abs_path):
        return "file missing"
    size = os.path.getsize(abs_path)
    if size < min_size:
        return f"file too small ({size} bytes)"
    return "unknown"


def main():
    parser = argparse.ArgumentParser(
        description="Audit and backfill missing or invalid novel cover images."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List novels that need covers without downloading anything",
    )
    parser.add_argument(
        "--id",
        type=int,
        default=None,
        metavar="NOVEL_ID",
        help="Fix a single novel by its DB id",
    )
    parser.add_argument(
        "--re-scrape",
        action="store_true",
        help=(
            "Re-scrape the novel landing page to get a fresh cover_url when "
            "cover_url is NULL, is a placeholder image, or when a download fails."
        ),
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=MIN_VALID_COVER_BYTES,
        metavar="BYTES",
        help=f"Minimum valid cover file size in bytes (default: {MIN_VALID_COVER_BYTES})",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=COVER_BACKFILL_DELAY_MIN,
        metavar="SECONDS",
        help=f"Minimum inter-novel delay in seconds (default: {COVER_BACKFILL_DELAY_MIN})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=COVER_BACKFILL_DELAY_MAX,
        metavar="SECONDS",
        help=f"Maximum inter-novel delay in seconds (default: {COVER_BACKFILL_DELAY_MAX})",
    )
    args = parser.parse_args()

    db_manager = DatabaseManager()
    repo = NovelRepository(db_manager)
    network = NetworkClient()
    browser = BrowserService()
    cover_manager = CoverManager(network, repo)

    scraper = None
    if args.re_scrape:
        scraper = ScraperService(network, browser, repo, cover_manager)
        logger.info("Re-scrape mode enabled.")

    if args.id is not None:
        row = get_single_novel(db_manager, args.id)
        if not row:
            logger.error(f"No novel found with id={args.id}")
            sys.exit(1)
        all_novels = [row]
    else:
        all_novels = get_all_novels(db_manager)

    logger.info(f"Auditing {len(all_novels)} novel(s) for valid covers...")
    logger.info(f"Minimum valid cover size: {args.min_size} bytes")
    logger.info(f"Inter-novel delay: {args.delay_min}–{args.delay_max}s")

    needs_cover, already_valid = audit_novels(all_novels, args.min_size)
    logger.info(
        f"\nAudit complete: {len(already_valid)} valid, {len(needs_cover)} need covers."
    )

    if not needs_cover:
        logger.info("All covers are valid. Nothing to do.")
        sys.exit(0)

    if args.dry_run:
        logger.info("--dry-run: no downloads will be performed.")
        for novel_id, title, slug, source_url, cover_path, cover_url in needs_cover:
            if cover_url and _is_placeholder_url(cover_url):
                url_status = "placeholder URL — needs --re-scrape"
            elif cover_url:
                url_status = "has cover_url"
            else:
                url_status = "NO cover_url"
            logger.info(f"  [{novel_id}] {title} ({url_status})")
        sys.exit(0)

    ok_count = 0
    skipped_count = 0
    fail_count = 0

    total = len(needs_cover)
    for i, (novel_id, title, slug, source_url, cover_path, cover_url) in enumerate(
        needs_cover, start=1
    ):
        logger.info(f"[{i}/{total}] Processing: '{title}' (id={novel_id})")

        result = fix_cover(
            novel_id=novel_id,
            title=title,
            slug=slug,
            source_url=source_url,
            cover_url=cover_url,
            cover_manager=cover_manager,
            scraper=scraper,
            repo=repo,
            re_scrape=args.re_scrape,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
        )

        if result == "ok":
            ok_count += 1
        elif result == "skipped":
            skipped_count += 1
        else:
            fail_count += 1

    logger.info("=" * 60)
    logger.info(
        f"Cover backfill complete — "
        f"Fixed: {ok_count}  Skipped: {skipped_count}  Failed: {fail_count}"
    )
    if skipped_count > 0:
        logger.info(
            f"  {skipped_count} novels had no real cover URL. "
            f"Re-run with --re-scrape to check for newly added covers."
        )
    if fail_count > 0:
        logger.info(
            f"  {fail_count} downloads failed. "
            f"Re-run to retry, or use --re-scrape if the stored URL may be stale."
        )

    if args.re_scrape:
        try:
            browser.stop()
        except Exception as e:
            logger.warning(f"Browser stop failed: {e}")


if __name__ == "__main__":
    main()
