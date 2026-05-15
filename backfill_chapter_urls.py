# =============================================================================
# backfill_chapter_urls.py
#
# PURPOSE:
#   Finds all novels in the DB that have no chapter rows at all (i.e. the
#   discovery pipeline ran before the chapter-URL fix was applied) and re-scrapes
#   each novel's landing page to populate chapter titles and URLs.
#
#   This does NOT download chapter content — it only fills in the chapter list
#   (titles + URLs) that should have been saved during discovery. After running
#   this script, you can use the reader UI's "Download Chapters" button or
#   run sync_novels.py --fetch-content to get the actual text.
#
# USAGE:
#   python backfill_chapter_urls.py            # backfill all novels with 0 chapters
#   python backfill_chapter_urls.py --dry-run  # just show which novels would be fixed
#   python backfill_chapter_urls.py --id 42    # backfill a single novel by DB id
#   python backfill_chapter_urls.py --abandon  # mark 0-chapter novels as ABANDONED
#                                              # after a failed scrape instead of
#                                              # counting them as failures
#
# SAFE TO RE-RUN:
#   upsert_chapters() uses ON CONFLICT(chapter_url) DO UPDATE, so running this
#   multiple times will not create duplicate chapters.
#
# ABANDONED NOVELS:
#   Novels marked ABANDONED are excluded from this script's query — they will
#   never be retried. To un-abandon a novel, set its status back to ACTIVE
#   directly in the database.
# =============================================================================

import argparse
import logging
import random
import sys
import time

from core import DatabaseManager, NetworkClient, NovelRepository
from core.config import DISCOVERY_NOVEL_DELAY_MIN, DISCOVERY_NOVEL_DELAY_MAX
from core.database import NOVEL_STATUS_ABANDONED
from services import BrowserService, CoverManager, ScraperService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

DEBUG = False


def get_novels_missing_chapters(db_manager: DatabaseManager) -> list[tuple]:
    """
    Returns novels that have zero chapter rows and are not already ABANDONED.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.

    Returns:
        list[tuple]: List of (id, title, source_url).

    Called by: main()
    Depends on: DatabaseManager.execute()
    """
    query = """
            SELECT n.id, n.title, n.source_url, n.status
            FROM novels n
            WHERE n.source_url IS NOT NULL
              AND n.status != 'ABANDONED'
              AND NOT EXISTS (
                SELECT 1 FROM chapters c WHERE c.novel_id = n.id
            )
            ORDER BY n.id ASC
            """
    return db_manager.execute(query)


def get_single_novel(db_manager: DatabaseManager, novel_id: int) -> tuple | None:
    """
    Returns (id, title, source_url) for a single novel by ID.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.
        novel_id (int): The DB id of the novel to look up.

    Returns:
        tuple | None: (id, title, source_url) or None if not found.

    Called by: main()
    Depends on: DatabaseManager.execute()
    """
    rows = db_manager.execute(
        "SELECT id, title, source_url FROM novels WHERE id = ?", (novel_id,)
    )
    return rows[0] if rows else None


def count_local_chapters(db_manager: DatabaseManager, novel_id: int) -> int:
    """
    Returns the number of chapter rows in the DB for a given novel.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.
        novel_id (int): The DB id of the novel.

    Returns:
        int: Chapter count (0 if none).

    Called by: backfill_novel()
    Depends on: DatabaseManager.execute()
    """
    rows = db_manager.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,)
    )
    return rows[0][0] if rows else 0


def backfill_novel(
    novel_id: int,
    title: str,
    source_url: str,
    scraper: ScraperService,
    repo: NovelRepository,
    db_manager: DatabaseManager,
    mark_abandoned_on_empty: bool = False,
) -> str:
    """
    Re-scrapes a single novel's landing page and saves chapter titles + URLs.

    Does not download chapter text content. Sets content_status to 'metadata'
    after saving chapters. If the scrape returns 0 chapters:
      - DB already has chapters → novel was stubbed/sold. Local chapters are
        preserved. Novel status left unchanged (not ABANDONED).
      - DB also has 0 chapters and mark_abandoned_on_empty is True → mark
        ABANDONED so it is excluded from all future processing.
      - DB also has 0 chapters and mark_abandoned_on_empty is False → counted
        as a failure, can be retried.

    Parameters:
        novel_id (int): DB id of the novel.
        title (str): Novel title (for logging only).
        source_url (str): The novel's landing page URL to re-scrape.
        scraper (ScraperService): Initialised scraper service.
        repo (NovelRepository): Initialised repository.
        db_manager (DatabaseManager): Active DB manager (for chapter count check).
        mark_abandoned_on_empty (bool): If True, mark as ABANDONED when scrape
                                        returns 0 chapters and DB also has 0.

    Returns:
        str: 'ok', 'stubbed', 'abandoned', or 'failed'.

    Called by: main()
    Depends on: ScraperService.scrape_novel(), NovelRepository.upsert_chapters(),
                NovelRepository.update_content_status(), NovelRepository.set_novel_status(),
                count_local_chapters()
    """
    logger.info(f"  Scraping: {source_url}")
    try:
        data = scraper.scrape_novel(source_url)
    except Exception as e:
        logger.error(f"  scrape_novel() raised: {e}")
        return "failed"

    if not data:
        logger.warning(f"  scrape_novel() returned no data for '{title}'")
        return "failed"

    chapters = data.get("chapters", [])

    if not chapters:
        local_count = count_local_chapters(db_manager, novel_id)

        if local_count > 0:
            # We already scraped chapters before the author stubbed the novel.
            # Leave everything alone — this novel is still readable locally.
            logger.info(
                f"  Source has 0 chapters for '{title}' but DB has {local_count}. "
                f"Novel was likely stubbed/sold — preserving local chapters."
            )
            return "stubbed"

        if mark_abandoned_on_empty:
            repo.set_novel_status(novel_id, NOVEL_STATUS_ABANDONED)
            logger.info(
                f"  No chapters found for '{title}' and DB also has 0 — "
                f"marked ABANDONED. It will be excluded from all future processing."
            )
            return "abandoned"
        else:
            logger.warning(
                f"  Scrape succeeded but found 0 chapters for '{title}'. "
                f"Re-run with --abandon to mark it as ABANDONED."
            )
            return "failed"

    if DEBUG:
        logger.debug(f"  [backfill_novel] {len(chapters)} chapters scraped")
        for ch in chapters[:3]:
            logger.debug(
                f"    order={ch['order']} title='{ch['title']}' url={ch['url']}"
            )

    try:
        repo.upsert_chapters(novel_id, chapters)
        repo.update_content_status(novel_id, "metadata")
        logger.info(f"  Saved {len(chapters)} chapters for '{title}'")
        return "ok"
    except Exception as e:
        logger.error(f"  upsert_chapters() raised: {e}")
        return "failed"


def main():
    parser = argparse.ArgumentParser(
        description="Backfill chapter titles and URLs for novels that have none."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List affected novels without making any changes",
    )
    parser.add_argument(
        "--id",
        type=int,
        default=None,
        metavar="NOVEL_ID",
        help="Backfill a single novel by its DB id instead of all missing ones",
    )
    parser.add_argument(
        "--abandon",
        action="store_true",
        help=(
            "Mark novels as ABANDONED when scrape returns 0 chapters "
            "and the DB also has 0 chapters (chapters removed by publisher). "
            "ABANDONED novels are excluded from all future syncs and the reader library."
        ),
    )
    args = parser.parse_args()

    # --- Initialise infrastructure ---
    db_manager = DatabaseManager()
    repo = NovelRepository(db_manager)
    network = NetworkClient()
    browser = BrowserService()
    cover_manager = CoverManager(network, repo)
    scraper = ScraperService(network, browser, repo, cover_manager)

    # --- Find target novels ---
    if args.id is not None:
        row = get_single_novel(db_manager, args.id)
        if not row:
            logger.error(f"No novel found with id={args.id}")
            sys.exit(1)
        targets = [row]
    else:
        targets = get_novels_missing_chapters(db_manager)

    if not targets:
        logger.info("No novels found that are missing chapters. Nothing to do.")
        sys.exit(0)

    logger.info(f"Found {len(targets)} novel(s) with no chapter rows:")
    for novel_id, title, source_url, _status in targets:
        logger.info(f"  [{novel_id}] {title}  ({source_url})")

    if args.dry_run:
        logger.info("--dry-run: no changes made.")
        sys.exit(0)

    if args.abandon:
        logger.info(
            "Note: novels with 0 chapters in both source and DB will be "
            "marked ABANDONED."
        )

    # --- Backfill each novel ---
    ok_count = 0
    fail_count = 0
    abandoned_count = 0
    stubbed_count = 0

    for i, (novel_id, title, source_url) in enumerate(targets):
        logger.info(f"[{i + 1}/{len(targets)}] Processing: '{title}' (id={novel_id})")

        result = backfill_novel(
            novel_id,
            title,
            source_url,
            scraper,
            repo,
            db_manager,
            mark_abandoned_on_empty=args.abandon,
        )

        if result == "ok":
            ok_count += 1
        elif result == "abandoned":
            abandoned_count += 1
        elif result == "stubbed":
            stubbed_count += 1
        else:
            fail_count += 1

        # Rate-limited delay between novels (skip after the last one)
        if i < len(targets) - 1:
            delay = random.uniform(DISCOVERY_NOVEL_DELAY_MIN, DISCOVERY_NOVEL_DELAY_MAX)
            logger.info(f"  Waiting {delay:.1f}s before next novel...")
            time.sleep(delay)

    logger.info("=" * 50)
    logger.info(
        f"Backfill complete — "
        f"OK: {ok_count}  "
        f"Stubbed (preserved): {stubbed_count}  "
        f"Abandoned: {abandoned_count}  "
        f"Failed: {fail_count}"
    )
    if fail_count > 0:
        logger.info(
            "Some novels still have 0 chapters. Re-run to retry, "
            "or use --abandon to mark them as ABANDONED."
        )


if __name__ == "__main__":
    main()
