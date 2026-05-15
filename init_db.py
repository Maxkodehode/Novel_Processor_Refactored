import sqlite3


from core.config import DB_PATH


def create_pure_schema():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. The Novels Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS novels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL UNIQUE,
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
        synopsis TEXT,
        author TEXT,
        source_url TEXT,
        cover_path TEXT,
        cover_url TEXT,
        slug TEXT NOT NULL UNIQUE,
        language TEXT NOT NULL,
        status TEXT DEFAULT 'ACTIVE',
        content_status TEXT NOT NULL DEFAULT 'metadata'
    )
    """)

    # New novel_sources table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS novel_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER,
        source_site TEXT,
        source_url TEXT UNIQUE,
        discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (novel_id) REFERENCES novels (id)
    )
    """)

    # 2. The Chapters Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chapters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER,
        chapter_title TEXT,
        chapter_url TEXT UNIQUE,
        chapter_hash TEXT NOT NULL,
        plain_content TEXT,
        html_content TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
        chapter_order REAL,
        FOREIGN KEY (novel_id) REFERENCES novels (id)
    )
    """)

    # Indexing for performance
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_novel_order ON chapters (novel_id, chapter_order)"
    )

    # 3. The Tags Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """)

    # 4. The Link Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS novel_tags (
        novel_id INTEGER,
        tag_id INTEGER,
        PRIMARY KEY (novel_id, tag_id),
        FOREIGN KEY (novel_id) REFERENCES novels (id),
        FOREIGN KEY (tag_id) REFERENCES tags (id)
    )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_novel_tags_composite ON novel_tags (novel_id, tag_id)"
    )

    conn.commit()
    conn.close()
    print("Empty Database Schema Created Successfully!")


if __name__ == "__main__":
    create_pure_schema()
