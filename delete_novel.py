# =============================================================================
# delete_novel.py
#
# PURPOSE:
#   Safely removes a duplicate novel and all its related data from the DB.
#
#   Deletes:
#     - The novel row itself
#     - All chapters belonging to it
#     - All novel_tags links (does NOT delete shared tags)
#     - All novel_sources links
#     - All reading_progress rows for this novel
#     - All bookmarks rows for this novel
#     - All notes linked to this novel's chapters
#     - Cover file on disk ONLY if no other novel references the same path
#
#   Use this when a novel was duplicated (e.g. RoyalRoad title/slug change
#   caused a second entry for the same story) and you want to remove the
#   duplicate while keeping the original untouched.
#
# USAGE:
#   python delete_novel.py 3087              # delete novel 3087
#   python delete_novel.py 3087 --dry-run    # preview what would be deleted
#   python delete_novel.py 3087 --force      # skip confirmation prompt
# =============================================================================

import argparse
import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def delete_novel(db_path: str, novel_id: int, dry_run: bool = False, force: bool = False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # --- Verify the novel exists ---
    cursor.execute("SELECT * FROM novels WHERE id = ?", (nid := novel_id,))
    novel = cursor.fetchone()
    if novel is None:
        logger.error(f"Novel {novel_id} not found in database.")
        conn.close()
        return False

    title = novel["title"]
    cover_path = novel["cover_path"]
    source_url = novel["source_url"]

    # --- Gather counts for report ---
    cursor.execute("SELECT COUNT(*) as cnt FROM chapters WHERE novel_id = ?", (nid,))
    chapter_count = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM novel_tags WHERE novel_id = ?", (nid,))
    tag_link_count = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM novel_sources WHERE novel_id = ?", (nid,))
    source_link_count = cursor.fetchone()["cnt"]

    # --- Reader tables ---
    cursor.execute("SELECT COUNT(*) as cnt FROM reading_progress WHERE novel_id = ?", (nid,))
    reading_progress_count = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM bookmarks WHERE novel_id = ?", (nid,))
    bookmark_count = cursor.fetchone()["cnt"]

    cursor.execute("""
        SELECT COUNT(*) as cnt FROM notes n
        JOIN chapters c ON n.chapter_id = c.id
        WHERE c.novel_id = ?
    """, (nid,))
    notes_count = cursor.fetchone()["cnt"]

    # --- Check if any other novel shares the same cover_path ---
    cover_shared = False
    cover_file_exists = False
    full_cover_path = None
    if cover_path:
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM novels WHERE cover_path = ? AND id != ?",
            (cover_path, nid),
        )
        cover_shared = cursor.fetchone()["cnt"] > 0
        # Resolve full path for disk check
        full_cover_path = os.path.join(os.path.dirname(db_path), cover_path)
        cover_file_exists = os.path.isfile(full_cover_path)

    # --- Print summary ---
    print()
    print("=" * 60)
    print(f"  DELETE NOVEL {novel_id}")
    print("=" * 60)
    print(f"  Title       : {title}")
    print(f"  Source URL  : {source_url}")
    print(f"  Cover path  : {cover_path}")
    print(f"  Cover file  : {'exists' if cover_file_exists else 'not found'}")
    print("-" * 60)
    print(f"  Chapters to delete       : {chapter_count}")
    print(f"  Tag links to delete      : {tag_link_count}")
    print(f"  Source links to delete   : {source_link_count}")
    print(f"  Reading progress to delete : {reading_progress_count}")
    print(f"  Bookmarks to delete      : {bookmark_count}")
    print(f"  Notes to delete          : {notes_count}")
    if cover_path:
        if cover_shared:
            print(f"  Cover file             : SKIPPED (shared with other novel(s))")
        elif cover_file_exists:
            print(f"  Cover file             : WILL DELETE from disk")
        else:
            print(f"  Cover file             : no file on disk, nothing to remove")
    print("=" * 60)

    if dry_run:
        logger.info("--dry-run: no changes were written")
        conn.close()
        return True

    # --- Confirmation ---
    if not force:
        answer = input(f"\n  Really delete novel {novel_id} and all its data? [y/N]: ").strip().lower()
        if answer != "y":
            logger.info("Aborted by user.")
            conn.close()
            return False

    # --- Execute deletions ---
    # Order matters: delete child tables before parent tables.
    # notes -> chapters (notes.chapter_id references chapters.id)
    # reading_progress, bookmarks -> novels (reference novel_id directly)

    # 0. Delete notes linked to this novel's chapters
    cursor.execute("""
        DELETE FROM notes WHERE chapter_id IN (
            SELECT id FROM chapters WHERE novel_id = ?
        )
    """, (nid,))
    deleted_notes = cursor.rowcount
    logger.info(f"  Deleted {deleted_notes} note rows")

    # 1. Delete chapters
    cursor.execute("DELETE FROM chapters WHERE novel_id = ?", (nid,))
    deleted_chapters = cursor.rowcount
    logger.info(f"  Deleted {deleted_chapters} chapter rows")

    # 2. Delete novel_tags links (tags table is NOT touched)
    cursor.execute("DELETE FROM novel_tags WHERE novel_id = ?", (nid,))
    deleted_tag_links = cursor.rowcount
    logger.info(f"  Deleted {deleted_tag_links} novel_tags links")

    # 3. Delete novel_sources links
    cursor.execute("DELETE FROM novel_sources WHERE novel_id = ?", (nid,))
    deleted_source_links = cursor.rowcount
    logger.info(f"  Deleted {deleted_source_links} novel_sources links")

    # 4. Delete reading_progress
    cursor.execute("DELETE FROM reading_progress WHERE novel_id = ?", (nid,))
    deleted_reading = cursor.rowcount
    logger.info(f"  Deleted {deleted_reading} reading_progress rows")

    # 5. Delete bookmarks
    cursor.execute("DELETE FROM bookmarks WHERE novel_id = ?", (nid,))
    deleted_bookmarks = cursor.rowcount
    logger.info(f"  Deleted {deleted_bookmarks} bookmark rows")

    # 6. Delete the novel row itself
    cursor.execute("DELETE FROM novels WHERE id = ?", (nid,))
    deleted_novel = cursor.rowcount
    logger.info(f"  Deleted novel row: {deleted_novel} row(s)")

    # 5. Handle cover file — only delete if not shared and file exists
    cover_deleted = False
    if cover_path and full_cover_path and cover_file_exists and not cover_shared:
        try:
            os.remove(full_cover_path)
            cover_deleted = True
            logger.info(f"  Deleted cover file: {full_cover_path}")
        except OSError as e:
            logger.warning(f"  Could not delete cover file: {e}")
    elif cover_path and cover_shared:
        logger.info(f"  Cover file kept (shared): {cover_path}")
    elif cover_path and not cover_file_exists:
        logger.info(f"  Cover file already gone: {cover_path}")

    conn.commit()
    conn.close()

    # --- Final summary ---
    print()
    logger.info("Deletion complete.")
    logger.info(f"  Novel rows deleted        : {deleted_novel}")
    logger.info(f"  Chapter rows deleted      : {deleted_chapters}")
    logger.info(f"  Tag links deleted         : {deleted_tag_links}")
    logger.info(f"  Source links deleted      : {deleted_source_links}")
    logger.info(f"  Reading progress deleted  : {deleted_reading}")
    logger.info(f"  Bookmarks deleted         : {deleted_bookmarks}")
    logger.info(f"  Notes deleted             : {deleted_notes}")
    logger.info(f"  Cover file deleted        : {cover_deleted}")
    print()

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Safely delete a duplicate novel and all its related data."
    )
    parser.add_argument(
        "novel_id",
        type=int,
        help="Database ID of the novel to delete",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without making changes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
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

    success = delete_novel(
        db_path, args.novel_id, dry_run=args.dry_run, force=args.force
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
