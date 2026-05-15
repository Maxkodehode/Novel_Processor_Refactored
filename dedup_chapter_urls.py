# =============================================================================
# dedup_chapter_urls.py
#
# PURPOSE:
#   Cleans up duplicate chapter rows caused by ScribbleHub URL slug changes.
#
#   When ScribbleHub changes a chapter URL, upsert_chapters() inserts a new
#   row (new URL = no conflict on chapter_url UNIQUE) while the old row with
#   the stale URL remains. This leaves two rows per chapter_order.
#
#   This script, for each group of duplicates:
#     1. Picks the "best" row to keep — the one with plain_content, highest id
#     2. If the kept row has a stale URL but the duplicate has content + a
#        different URL, the kept row gets the duplicate's URL (it's newer)
#     3. Deletes all other rows in the group
#
#   This preserves all downloaded content while keeping the most current URL.
#
# USAGE:
#   python dedup_chapter_urls.py            # clean up all duplicates
#   python dedup_chapter_urls.py --dry-run  # show what would happen
#   python dedup_chapter_urls.py --id 42    # clean up a single novel
# =============================================================================

import argparse
import logging
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def dedup(db_path: str, dry_run: bool = False, novel_id_filter: int = None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find all duplicate groups
    cursor.execute("""
        SELECT novel_id, chapter_order, COUNT(*) as cnt
        FROM chapters
        GROUP BY novel_id, chapter_order
        HAVING cnt > 1
        ORDER BY novel_id, chapter_order
    """)
    groups = cursor.fetchall()

    if novel_id_filter is not None:
        groups = [g for g in groups if g["novel_id"] == novel_id_filter]

    if not groups:
        logger.info("No duplicate chapter rows found. DB is clean.")
        return

    total_deleted = 0
    total_url_updates = 0
    novels_affected = set()

    for group in groups:
        nid = group["novel_id"]
        order = group["chapter_order"]
        novels_affected.add(nid)

        # Get all rows for this group, ordered by id
        cursor.execute("""
            SELECT id, chapter_url, chapter_title, plain_content, chapter_hash
            FROM chapters
            WHERE novel_id = ? AND chapter_order = ?
            ORDER BY id ASC
        """, (nid, order))
        rows = cursor.fetchall()

        # Pick the best row to keep:
        # Priority 1: has plain_content (downloaded content is precious)
        # Priority 2: highest id (most recent = most current URL)
        keep = None
        for row in rows:
            if row["plain_content"] is not None:
                keep = row
                break
        if keep is None:
            keep = rows[-1]  # highest id as fallback

        # Check if any duplicate has a URL we should adopt
        # (kept row has stale URL, duplicate has newer URL + content)
        url_updated = False
        for row in rows:
            if row["id"] == keep["id"]:
                continue
            if row["chapter_url"] != keep["chapter_url"]:
                # The duplicate has a different URL — it's likely the newer one
                # since ScribbleHub changes slugs over time. Update the kept row.
                if not dry_run:
                    cursor.execute("""
                        UPDATE chapters
                        SET chapter_url = ?, chapter_title = ?, last_updated = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (row["chapter_url"], row["chapter_title"], keep["id"]))
                logger.info(
                    f"  Novel {nid} ch {order}: UPDATE kept row {keep['id']} URL to {row['chapter_url']}"
                )
                url_updated = True
                total_url_updates += 1
                break  # only need one URL update

        # Delete all other rows
        delete_ids = [row["id"] for row in rows if row["id"] != keep["id"]]
        if not dry_run and delete_ids:
            cursor.executemany(
                "DELETE FROM chapters WHERE id = ?",
                [(i,) for i in delete_ids]
            )
        total_deleted += len(delete_ids)

        content_rows_deleted = sum(
            1 for row in rows
            if row["id"] != keep["id"] and row["plain_content"] is not None
        )
        if content_rows_deleted > 0:
            logger.warning(
                f"  Novel {nid} ch {order}: WARNING — deleting {content_rows_deleted} "
                f"row(s) with downloaded content (content in kept row {keep['id']} preserved)"
            )

    if not dry_run:
        conn.commit()

    conn.close()

    logger.info("=" * 60)
    logger.info(f"Dedup complete:")
    logger.info(f"  Duplicate groups processed: {len(groups)}")
    logger.info(f"  Novels affected           : {len(novels_affected)}")
    logger.info(f"  URL updates (stale->new)  : {total_url_updates}")
    logger.info(f"  Rows deleted              : {total_deleted}")
    if dry_run:
        logger.info("  --dry-run: no changes were written")


def main():
    parser = argparse.ArgumentParser(
        description="Remove duplicate chapter rows caused by ScribbleHub URL changes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    parser.add_argument(
        "--id",
        type=int,
        default=None,
        metavar="NOVEL_ID",
        help="Only clean up a single novel by DB id",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to novels.db (default: auto-detect from config)",
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path is None:
        from core.config import DB_PATH
        db_path = DB_PATH

    dedup(db_path, dry_run=args.dry_run, novel_id_filter=args.id)


if __name__ == "__main__":
    main()
