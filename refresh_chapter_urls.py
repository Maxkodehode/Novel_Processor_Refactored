# =============================================================================
# refresh_chapter_urls.py
#
# PURPOSE:
#   Refreshes chapter URLs for ScribbleHub novels whose source URLs have
#   changed since they were first scraped.
#
#   ScribbleHub occasionally changes chapter URL slugs (e.g. after an author
#   edits a title). The old URL in the database then returns a 404, making
#   re-download impossible. This script re-scrapes each novel's landing page,
#   compares the fresh chapter list against the DB, and:
#
#     - Updates changed URLs in-place (preserving chapter content + metadata)
#     - Inserts any new chapters that appeared since the last sync
#     - Removes DB chapters that no longer exist on the source (optional)
#
#   Chapters are matched by chapter_order (not URL), so title/slug changes
#   are handled correctly.
#
# USAGE:
#   python refresh_chapter_urls.py                 # refresh all ScribbleHub novels
#   python refresh_chapter_urls.py --id 42         # refresh a single novel by DB id
#   python refresh_chapter_urls.py --dry-run       # show what would change, no writes
#   python refresh_chapter_urls.py --remove-stale  # also delete DB chapters that
#                                                  # no longer exist on the source
#
# SAFE TO RE-RUN:
#   Uses a single transaction per novel, so partial failures roll back.
#   Already-downloaded chapter content is preserved (we UPDATE in-place).
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


def get_scribblehub_novels(db_manager: DatabaseManager) -> list[tuple]:
    """
    Returns all non-ABANDONED novels whose source_url is a ScribbleHub URL.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.

    Returns:
        list[tuple]: (id, title, source_url).

    Called by: main()
    """
    query = """
        SELECT n.id, n.title, n.source_url
        FROM novels n
        WHERE n.source_url IS NOT NULL
          AND n.status != 'ABANDONED'
          AND n.source_url LIKE '%scribblehub.com%'
        ORDER BY n.id ASC
    """
    return db_manager.execute(query)


def get_single_novel(db_manager: DatabaseManager, novel_id: int) -> tuple | None:
    """
    Returns (id, title, source_url) for a single novel by ID.

    Parameters:
        db_manager (DatabaseManager): Active DB manager instance.
        novel_id (int): The DB id of the novel.

    Returns:
        tuple | None: (id, title, source_url) or None if not found.

    Called by: main()
    """
    rows = db_manager.execute(
        "SELECT id, title, source_url FROM novels WHERE id = ?", (novel_id,)
    )
    return rows[0] if rows else None


def refresh_novel(
    novel_id: int,
    title: str,
    source_url: str,
    scraper: ScraperService,
    repo: NovelRepository,
    db_manager: DatabaseManager,
    dry_run: bool = False,
    remove_stale: bool = False,
) -> dict:
    """
    Re-scrapes a single novel's landing page and reconciles chapter URLs.

    Algorithm:
      1. Call scraper.scrape_novel(source_url) to get the current chapter list.
      2. Fetch existing DB chapters for this novel, keyed by chapter_order.
      3. Compare source vs DB:
         - Same order, same URL  → no action
         - Same order, different URL → UPDATE the DB row's chapter_url in-place
         - Order in source but not DB → INSERT new chapter
         - Order in DB but not source → DELETE if --remove-stale, else skip
      4. Execute all changes inside a single transaction.

    Parameters:
        novel_id (int): DB id of the novel.
        title (str): Novel title (for logging).
        source_url (str): The novel's landing page URL.
        scraper (ScraperService): Initialised scraper service.
        repo (NovelRepository): Initialised repository.
        db_manager (DatabaseManager): Active DB manager.
        dry_run (bool): If True, compute and log changes but do not write.
        remove_stale (bool): If True, delete DB chapters not present in source.

    Returns:
        dict: Summary with keys 'updated', 'inserted', 'deleted', 'unchanged',
              'skipped', 'status' ('ok', 'failed', or 'no_source').

    Called by: main(), refresh_urls_for_novel()
    """
    result = {
        "updated": 0,
        "inserted": 0,
        "deleted": 0,
        "unchanged": 0,
        "skipped": 0,
        "status": "ok",
    }

    # --- Step 1: Scrape current chapter list from source ---
    logger.info(f"  Scraping: {source_url}")
    try:
        data = scraper.scrape_novel(source_url)
    except Exception as e:
        logger.error(f"  scrape_novel() raised: {e}")
        result["status"] = "failed"
        return result

    if not data:
        logger.warning(f"  scrape_novel() returned no data for '{title}'")
        result["status"] = "failed"
        return result

    source_chapters = data.get("chapters", [])
    if not source_chapters:
        logger.warning(f"  Source returned 0 chapters for '{title}' — skipping")
        result["status"] = "no_source"
        return result

    # Build source lookup: order -> {title, url}
    source_by_order: dict[int, dict] = {}
    for ch in source_chapters:
        source_by_order[ch["order"]] = {"title": ch["title"], "url": ch["url"]}

    # --- Step 2: Fetch existing DB chapters ---
    db_chapters = repo.get_novel_chapters(novel_id)
    # db_chapters: {order: {"url": str, "id": int}}

    # Build reverse lookup: url -> list of {id, order} for collision detection
    db_by_url: dict[str, list[dict]] = {}
    for order, row in db_chapters.items():
        db_by_url.setdefault(row["url"], []).append({"id": row["id"], "order": order})

    # --- Step 3: Compare ---
    updates = []   # (chapter_id, new_url, new_title)
    inserts = []   # (order, title, url)
    deletes = []   # (chapter_id, order)

    for order, src in source_by_order.items():
        if order in db_chapters:
            if db_chapters[order]["url"] != src["url"]:
                new_url = src["url"]
                ch_id = db_chapters[order]["id"]
                # If the new URL already exists as any row for this novel,
                # delete the stale old row — the row with the correct URL
                # already exists and may have downloaded content.
                if new_url in db_by_url:
                    deletes.append((ch_id, order))
                else:
                    updates.append((ch_id, new_url, src["title"]))
            else:
                result["unchanged"] += 1
        else:
            # Only insert if this URL doesn't already exist for the novel
            if src["url"] not in db_by_url:
                inserts.append((order, src["title"], src["url"]))

    for order, db_row in db_chapters.items():
        if order not in source_by_order:
            if remove_stale:
                deletes.append((db_row["id"], order))
            else:
                result["skipped"] += 1

    # --- Step 4: Log summary ---
    logger.info(
        f"  '{title}' (id={novel_id}): "
        f"{len(updates)} to update, "
        f"{len(inserts)} to insert, "
        f"{len(deletes)} to delete, "
        f"{result['unchanged']} unchanged, "
        f"{result['skipped']} stale (kept)"
    )

    if dry_run:
        for ch_id, new_url, new_title in updates:
            old_order = next(o for o, r in db_chapters.items() if r["id"] == ch_id)
            logger.info(f"    [DRY-RUN] UPDATE ch_id={ch_id} order={old_order}:")
            logger.info(f"               {db_chapters[old_order]['url']}")
            logger.info(f"            -> {new_url}")
        for order, t, url in inserts:
            logger.info(f"    [DRY-RUN] INSERT order={order}: '{t}' {url}")
        for ch_id, order in deletes:
            logger.info(f"    [DRY-RUN] DELETE ch_id={ch_id} order={order}")
        return result

    # --- Step 5: Execute in a single transaction ---
    operations = []

    for ch_id, new_url, new_title in updates:
        operations.append((
            """UPDATE chapters
               SET chapter_url = ?,
                   chapter_title = ?,
                   last_updated = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (new_url, new_title, ch_id),
        ))

    for order, t, url in inserts:
        operations.append((
            """INSERT INTO chapters (novel_id, chapter_title, chapter_hash, chapter_order, chapter_url)
               VALUES (?, ?, ?, ?, ?)""",
            (novel_id, t, "PENDING", order, url),
        ))

    for ch_id, _order in deletes:
        operations.append((
            "DELETE FROM chapters WHERE id = ?",
            (ch_id,),
        ))

    if operations:
        try:
            db_manager.execute_transaction(operations)
            result["updated"] = len(updates)
            result["inserted"] = len(inserts)
            result["deleted"] = len(deletes)
            logger.info(f"  Transaction committed for '{title}'")
        except Exception as e:
            logger.error(f"  Transaction failed for '{title}': {e}")
            result["status"] = "failed"
            return result
    else:
        logger.info(f"  No changes needed for '{title}'")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Refresh chapter URLs for ScribbleHub novels whose source slugs have changed."
    )
    parser.add_argument(
        "--id",
        type=int,
        default=None,
        metavar="NOVEL_ID",
        help="Refresh a single novel by its DB id",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the DB",
    )
    parser.add_argument(
        "--remove-stale",
        action="store_true",
        help="Delete DB chapters that no longer exist on the source",
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
        targets = get_scribblehub_novels(db_manager)

    if not targets:
        logger.info("No ScribbleHub novels found in the DB. Nothing to do.")
        sys.exit(0)

    logger.info(f"Found {len(targets)} ScribbleHub novel(s) to refresh:")
    for novel_id, title, source_url in targets:
        logger.info(f"  [{novel_id}] {title}  ({source_url})")

    if args.dry_run:
        logger.info("--dry-run: no changes will be written.")

    # --- Process each novel ---
    total_updated = 0
    total_inserted = 0
    total_deleted = 0
    total_unchanged = 0
    total_skipped = 0
    fail_count = 0
    no_source_count = 0

    for i, (novel_id, title, source_url) in enumerate(targets):
        logger.info(f"[{i + 1}/{len(targets)}] Refreshing: '{title}' (id={novel_id})")

        result = refresh_novel(
            novel_id, title, source_url,
            scraper, repo, db_manager,
            dry_run=args.dry_run,
            remove_stale=args.remove_stale,
        )

        if result["status"] == "failed":
            fail_count += 1
        elif result["status"] == "no_source":
            no_source_count += 1

        total_updated += result["updated"]
        total_inserted += result["inserted"]
        total_deleted += result["deleted"]
        total_unchanged += result["unchanged"]
        total_skipped += result["skipped"]

        if i < len(targets) - 1:
            delay = random.uniform(DISCOVERY_NOVEL_DELAY_MIN, DISCOVERY_NOVEL_DELAY_MAX)
            logger.info(f"  Waiting {delay:.1f}s before next novel...")
            time.sleep(delay)

    # --- Summary ---
    logger.info("=" * 60)
    logger.info("Refresh complete.")
    logger.info(f"  Novels processed : {len(targets)}")
    logger.info(f"  Chapters updated : {total_updated}")
    logger.info(f"  Chapters inserted: {total_inserted}")
    logger.info(f"  Chapters deleted : {total_deleted}")
    logger.info(f"  Chapters unchanged: {total_unchanged}")
    logger.info(f"  Stale (kept)     : {total_skipped}")
    logger.info(f"  Failed novels    : {fail_count}")
    logger.info(f"  No-source novels : {no_source_count}")

    if fail_count > 0:
        logger.info("Some novels failed. Re-run to retry.")
    if total_skipped > 0 and not args.remove_stale:
        logger.info(
            f"{total_skipped} stale chapters were kept. "
            "Re-run with --remove-stale to delete them."
        )


if __name__ == "__main__":
    main()
