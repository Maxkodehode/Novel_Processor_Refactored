"""
Migration: Add unique index on novels.source_url for fast URL-based deduplication.

Run once: python migrate_source_url_unique.py
"""
import sqlite3
import logging

from core.config import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if index already exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_novels_source_url'")
    if cursor.fetchone():
        logger.info("Index idx_novels_source_url already exists — skipping.")
        conn.close()
        return

    # Check for duplicate source_url values that would violate uniqueness
    cursor.execute("""
        SELECT source_url, COUNT(*) as cnt
        FROM novels
        WHERE source_url IS NOT NULL
        GROUP BY source_url
        HAVING cnt > 1
    """)
    duplicates = cursor.fetchall()

    if duplicates:
        logger.warning(f"Found {len(duplicates)} duplicate source_url(s) — deduplicating before adding unique index.")
        for url, count in duplicates:
            # Keep the row with the lowest id, delete the rest
            cursor.execute("""
                DELETE FROM novels
                WHERE source_url = ? AND id NOT IN (
                    SELECT MIN(id) FROM novels WHERE source_url = ?
                )
            """, (url, url))
            logger.info(f"  Deduplicated '{url}': removed {count - 1} duplicate(s), kept lowest id.")

    # Create the unique index
    cursor.execute("CREATE UNIQUE INDEX idx_novels_source_url ON novels (source_url)")
    conn.commit()
    conn.close()
    logger.info("Migration complete: unique index on novels.source_url created.")


if __name__ == "__main__":
    migrate()
