# =============================================================================
# migrate_chapter_unique.py
#
# PURPOSE:
#   Migrates the chapters table to use (novel_id, chapter_order) as the
#   unique constraint instead of chapter_url.
#
#   This prevents duplicate rows when ScribbleHub changes a chapter's URL
#   slug — the existing row gets updated in-place instead of a second row
#   being inserted.
#
#   Safe to run multiple times (idempotent).
#
# USAGE:
#   python migrate_chapter_unique.py
# =============================================================================

import sqlite3
import sys

from core.config import DB_PATH

logger = print


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check current schema
    cursor.execute("PRAGMA table_info(chapters)")
    columns = {row[1]: row for row in cursor.fetchall()}
    logger(f"Current columns: {list(columns.keys())}")

    # Check if we already have the composite unique
    cursor.execute("PRAGMA index_list(chapters)")
    indexes = cursor.fetchall()
    logger(f"Current indexes: {[idx[1] for idx in indexes]}")

    has_composite_unique = any(
        idx[1] == "idx_chapters_novel_order" and idx[2] == 1
        for idx in indexes
    )
    has_url_unique = any(
        idx[1] == "sqlite_autoindex_chapters_1"  # auto-index from UNIQUE
        for idx in indexes
    )

    if has_composite_unique and not has_url_unique:
        logger("Schema already migrated. Nothing to do.")
        conn.close()
        return

    logger("Starting migration...")

    # SQLite doesn't support DROP CONSTRAINT. We need to recreate the table.
    # 1. Rename old table
    cursor.execute("ALTER TABLE chapters RENAME TO chapters_old")

    # 2. Create new table with correct constraints
    cursor.execute("""
        CREATE TABLE chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            novel_id INTEGER,
            chapter_title TEXT,
            chapter_url TEXT,
            chapter_hash TEXT NOT NULL,
            plain_content TEXT,
            html_content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
            chapter_order REAL,
            FOREIGN KEY (novel_id) REFERENCES novels (id),
            UNIQUE (novel_id, chapter_order)
        )
    """)

    # 3. Copy data, deduplicating by (novel_id, chapter_order) — keep lowest id
    cursor.execute("""
        INSERT INTO chapters
            (id, novel_id, chapter_title, chapter_url, chapter_hash,
             plain_content, html_content, created_at, last_updated, chapter_order)
        SELECT
            MIN(id), novel_id, chapter_title, chapter_url, chapter_hash,
            plain_content, html_content, created_at, last_updated, chapter_order
        FROM chapters_old
        GROUP BY novel_id, chapter_order
    """)

    # 4. Drop old table
    cursor.execute("DROP TABLE chapters_old")

    # 5. Recreate index
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_novel_order ON chapters (novel_id, chapter_order)"
    )

    conn.commit()

    # Verify
    cursor.execute("SELECT COUNT(*) FROM chapters")
    count = cursor.fetchone()[0]
    logger(f"Migration complete. {count} chapters in new table.")

    cursor.execute("PRAGMA index_list(chapters)")
    indexes = cursor.fetchall()
    logger(f"New indexes: {[idx[1] for idx in indexes]}")

    conn.close()


if __name__ == "__main__":
    migrate()
