import sqlite3
import logging
from .config import DB_PATH

logger = logging.getLogger(__name__)

# Valid values for novels.status
# ACTIVE      — normal novel, included in all queries
# ABANDONED   — chapters were removed (sold to publisher, etc). Kept in DB for
#               deduplication but excluded from sync, reader library, and backfill.
# COMPLETED   — story is finished, still readable
# HIATUS      — author paused, still readable
NOVEL_STATUS_ACTIVE = "ACTIVE"
NOVEL_STATUS_ABANDONED = "ABANDONED"
NOVEL_STATUS_COMPLETED = "COMPLETED"
NOVEL_STATUS_HIATUS = "HIATUS"

# Statuses that should appear in the reader library and be synced
READABLE_STATUSES = (NOVEL_STATUS_ACTIVE, NOVEL_STATUS_COMPLETED, NOVEL_STATUS_HIATUS)


class DatabaseManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def execute(self, query, params=(), commit=False, row_factory=None):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                if row_factory:
                    conn.row_factory = row_factory
                cursor = conn.cursor()
                cursor.execute(query, params)
                results = cursor.fetchall()
                if commit:
                    conn.commit()
                return results
        except sqlite3.Error as e:
            logger.error(f"Database error: {e}")
            raise

    def execute_transaction(self, operations):
        """
        Executes a list of (query, params) in a single transaction.

        Parameters:
            operations (list[tuple]): List of (query, params) pairs.

        Called by: NovelRepository.upsert_chapters()
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()
                for query, params in operations:
                    cursor.execute(query, params)
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Transaction error: {e}")
            conn.rollback()
            raise


class NovelRepository:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def upsert_novel(self, data: dict, slug: str) -> int | None:
        """
        Inserts or updates a novel row. Does NOT overwrite status — so an
        ABANDONED novel that gets re-scraped stays ABANDONED.

        Parameters:
            data (dict): Parsed novel data.
            slug (str): URL-safe slug for the novel.

        Returns:
            int | None: The novel's DB id, or None on failure.

        Called by: ScraperService.populate_novel()
        Depends on: DatabaseManager.execute()
        """
        query = """
                INSERT INTO novels (title, author, synopsis, source_url, slug, language, cover_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(title) DO UPDATE SET
                                                 author       = excluded.author,
                                                 synopsis     = excluded.synopsis,
                                                 source_url   = excluded.source_url,
                                                 slug         = excluded.slug,
                                                 language     = excluded.language,
                                                 last_updated = CURRENT_TIMESTAMP,
                                                 cover_url    = excluded.cover_url
                RETURNING id \
                """
        params = (
            data["title"],
            data.get("author"),
            data.get("synopsis"),
            data.get("url"),
            slug,
            data.get("language", "en"),
            data.get("cover_url"),
        )
        try:
            rows = self.db.execute(query, params, commit=True)
            if rows:
                return rows[0][0]
        except sqlite3.Error as e:
            logger.warning(f"RETURNING clause failed, trying fallback: {e}")

        try:
            rows = self.db.execute(
                "SELECT id FROM novels WHERE title = ?", (data["title"],)
            )
            return rows[0][0] if rows else None
        except Exception as e:
            logger.error(f"Failed to upsert novel (fallback): {e}")
            return None

    def upsert_chapters(self, novel_id: int, chapters: list[dict]):
        """
        Inserts or updates chapter rows for a novel.

        Parameters:
            novel_id (int): DB id of the novel.
            chapters (list[dict]): Chapter dicts with title, order, url keys.

        Called by: ScraperService.populate_novel(), backfill scripts
        Depends on: DatabaseManager.execute_transaction()
        """
        operations = []
        for ch in chapters:
            query = """
                    INSERT INTO chapters (novel_id, chapter_title, chapter_hash, chapter_order, chapter_url)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chapter_url) DO UPDATE SET
                                                           chapter_title = excluded.chapter_title,
                                                           chapter_order = excluded.chapter_order \
                    """
            params = (novel_id, ch["title"], "PENDING", ch["order"], ch["url"])
            operations.append((query, params))

        if operations:
            self.db.execute_transaction(operations)

    def link_tags(self, novel_id: int, tags: list[str]):
        """
        Creates tag rows and links them to a novel.

        Parameters:
            novel_id (int): DB id of the novel.
            tags (list[str]): Tag name strings.

        Called by: ScraperService.populate_novel()
        Depends on: DatabaseManager.execute()
        """
        for tag_name in tags:
            self.db.execute(
                "INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                (tag_name,),
                commit=True,
            )
            rows = self.db.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
            if rows:
                tag_id = rows[0][0]
                self.db.execute(
                    "INSERT INTO novel_tags (novel_id, tag_id) VALUES (?, ?) ON CONFLICT(novel_id, tag_id) DO NOTHING",
                    (novel_id, tag_id),
                    commit=True,
                )

    def update_cover_path(self, novel_id: int, cover_path: str):
        """
        Persists the local cover image path for a novel.

        Parameters:
            novel_id (int): DB id of the novel.
            cover_path (str): Relative path to the saved cover file.

        Called by: CoverManager.download_and_save()
        Depends on: DatabaseManager.execute()
        """
        query = "UPDATE novels SET cover_path = ? WHERE id = ?"
        self.db.execute(query, (cover_path, novel_id), commit=True)

    def get_pending_chapters(self, novel_id: int = None):
        """
        Returns chapters with no downloaded content, excluding ABANDONED novels.

        Parameters:
            novel_id (int | None): If set, restrict to this novel only.

        Returns:
            list[tuple]: List of (id, chapter_title, chapter_url).

        Called by: ScraperService.fetch_chapters()
        Depends on: DatabaseManager.execute()
        """
        if novel_id is not None:
            query = """
                    SELECT c.id, c.chapter_title, c.chapter_url
                    FROM chapters c
                             JOIN novels n ON c.novel_id = n.id
                    WHERE c.plain_content IS NULL
                      AND c.novel_id = ?
                      AND n.status != 'ABANDONED' \
                    """
            return self.db.execute(query, (novel_id,))
        query = """
                SELECT c.id, c.chapter_title, c.chapter_url
                FROM chapters c
                         JOIN novels n ON c.novel_id = n.id
                WHERE c.plain_content IS NULL
                  AND n.status != 'ABANDONED' \
                """
        return self.db.execute(query)

    def update_chapter_content(
        self, ch_id: int, content_text: str, raw_html: str, chapter_hash: str
    ):
        """
        Saves downloaded plain text and HTML content for a chapter.

        Parameters:
            ch_id (int): DB id of the chapter.
            content_text (str): Plain text content.
            raw_html (str): Raw HTML content.
            chapter_hash (str): SHA-256 hash of the plain text.

        Called by: ScraperService.fetch_chapters()
        Depends on: DatabaseManager.execute()
        """
        query = """
                UPDATE chapters
                SET plain_content = ?,
                    html_content = ?,
                    chapter_hash = ?,
                    last_updated = CURRENT_TIMESTAMP
                WHERE id = ? \
                """
        self.db.execute(
            query, (content_text, raw_html, chapter_hash, ch_id), commit=True
        )

    def get_novel_by_id(self, novel_id: int):
        """
        Retrieves a novel's full row by its DB id.

        Parameters:
            novel_id (int): DB id of the novel.

        Returns:
            sqlite3.Row | None

        Called by: various
        Depends on: DatabaseManager.execute()
        """
        query = "SELECT * FROM novels WHERE id = ?"
        rows = self.db.execute(query, (novel_id,), row_factory=sqlite3.Row)
        if rows:
            return rows[0]
        return None

    def get_active_novels(self):
        """
        Returns novels eligible for sync — excludes ABANDONED.

        Returns:
            list[tuple]: (id, title, source_url, last_updated)

        Called by: NovelUpdateService.sync_all()
        Depends on: DatabaseManager.execute()
        """
        placeholders = ",".join("?" * len(READABLE_STATUSES))
        query = f"""
            SELECT id, title, source_url, last_updated
            FROM novels
            WHERE status IN ({placeholders})
        """
        return self.db.execute(query, READABLE_STATUSES)

    def get_novel_chapters(self, novel_id: int):
        """
        Returns a dict of chapter_order → {url, id} for a novel.

        Parameters:
            novel_id (int): DB id of the novel.

        Returns:
            dict[float, dict]

        Called by: NovelUpdateService.sync_novel()
        Depends on: DatabaseManager.execute()
        """
        query = "SELECT chapter_order, chapter_url, id FROM chapters WHERE novel_id = ? ORDER BY chapter_order"
        rows = self.db.execute(query, (novel_id,))
        return {row[0]: {"url": row[1], "id": row[2]} for row in rows}

    def is_url_known(self, url: str) -> bool:
        """
        Checks if a URL exists in novels or novel_sources.
        Returns True even for ABANDONED novels so they are never re-inserted.

        Parameters:
            url (str): Source URL to check.

        Returns:
            bool

        Called by: DiscoveryService.discover()
        Depends on: DatabaseManager.execute()
        """
        q1 = "SELECT 1 FROM novels WHERE source_url = ?"
        if self.db.execute(q1, (url,)):
            return True
        q2 = "SELECT 1 FROM novel_sources WHERE source_url = ?"
        if self.db.execute(q2, (url,)):
            return True
        return False

    def get_all_novels_for_fuzzy(self) -> list[tuple[int, str]]:
        """
        Returns all novel IDs and titles for fuzzy deduplication.
        Includes ABANDONED novels so they don't get re-inserted under a
        slightly different title.

        Returns:
            list[tuple[int, str]]

        Called by: DiscoveryService.discover()
        Depends on: DatabaseManager.execute()
        """
        return self.db.execute("SELECT id, title FROM novels")

    def add_novel_source(self, novel_id: int, site: str, url: str):
        """
        Records an additional source URL for an existing novel.

        Parameters:
            novel_id (int): DB id of the novel.
            site (str): Site key (e.g. 'royalroad').
            url (str): Source URL on that site.

        Called by: DiscoveryService.discover()
        Depends on: DatabaseManager.execute()
        """
        query = "INSERT INTO novel_sources (novel_id, source_site, source_url) VALUES (?, ?, ?)"
        self.db.execute(query, (novel_id, site, url), commit=True)

    def insert_discovered_novel(self, title: str, url: str, slug: str) -> int:
        """
        Inserts a new novel with 'discovered' content_status.

        Parameters:
            title (str): Novel title.
            url (str): Source URL.
            slug (str): URL-safe slug.

        Returns:
            int: New novel's DB id.

        Called by: DiscoveryService.discover()
        Depends on: DatabaseManager.execute()
        """
        query = """
                INSERT INTO novels (title, source_url, slug, language, content_status)
                VALUES (?, ?, ?, 'en', 'discovered')
                RETURNING id \
                """
        rows = self.db.execute(query, (title, url, slug), commit=True)
        if rows:
            return rows[0][0]
        row = self.db.execute("SELECT id FROM novels WHERE title = ?", (title,))
        return row[0][0]

    def set_novel_status(self, novel_id: int, status: str):
        """
        Sets the status column for a novel.
        Use NOVEL_STATUS_* constants from this module.

        Parameters:
            novel_id (int): DB id of the novel.
            status (str): New status value.

        Called by: backfill_chapter_urls.py, server.py abandon endpoint
        Depends on: DatabaseManager.execute()
        """
        query = "UPDATE novels SET status = ? WHERE id = ?"
        self.db.execute(query, (status, novel_id), commit=True)

    def update_content_status(self, novel_id: int, status: str):
        """
        Updates the content_status column (not the same as status).
        Valid values: 'discovered', 'metadata', 'full'.

        Parameters:
            novel_id (int): DB id of the novel.
            status (str): New content_status value.

        Called by: ScraperService, backfill scripts, server.py
        Depends on: DatabaseManager.execute()
        """
        query = "UPDATE novels SET content_status = ? WHERE id = ?"
        self.db.execute(query, (status, novel_id), commit=True)

    def update_novel_timestamp(self, novel_id: int):
        """
        Bumps last_updated to now for a novel.

        Parameters:
            novel_id (int): DB id of the novel.

        Called by: NovelUpdateService.sync_novel(), server.py
        Depends on: DatabaseManager.execute()
        """
        query = "UPDATE novels SET last_updated = CURRENT_TIMESTAMP WHERE id = ?"
        self.db.execute(query, (novel_id,), commit=True)

    def get_tags(self):
        """
        Returns all tag names sorted alphabetically.

        Returns:
            list[str]

        Called by: server.py
        Depends on: DatabaseManager.execute()
        """
        query = "SELECT name FROM tags ORDER BY name ASC"
        rows = self.db.execute(query)
        return [row[0] for row in rows]

    def get_filtered_novels(
        self, include_tags=None, exclude_tags=None, sort_by="title"
    ):
        """
        Retrieves readable novels with tri-state tag filtering and sorting.
        Excludes ABANDONED novels.

        Parameters:
            include_tags (list[str] | None): Must have all these tags.
            exclude_tags (list[str] | None): Must have none of these tags.
            sort_by (str): 'title', 'last_updated', 'chapter_count', 'word_count'

        Returns:
            list[tuple]

        Called by: server.py (via direct SQL, not this method currently)
        Depends on: DatabaseManager.execute()
        """
        params = []

        placeholders = ",".join("?" * len(READABLE_STATUSES))
        query = f"""
            SELECT n.*,
                   (SELECT COUNT(*) FROM chapters WHERE novel_id = n.id) as chapter_count,
                   (SELECT COUNT(*) FROM reading_progress WHERE novel_id = n.id AND scroll_position >= 0.9) as chapters_read,
                   (SELECT SUM(length(plain_content) - length(replace(plain_content, ' ', '')) + 1)
                    FROM chapters WHERE novel_id = n.id AND plain_content IS NOT NULL) as word_count
            FROM novels n
            WHERE n.status IN ({placeholders})
        """
        params.extend(READABLE_STATUSES)

        if include_tags:
            for tag in include_tags:
                query += """
                    AND EXISTS (
                        SELECT 1 FROM novel_tags nt
                        JOIN tags t ON nt.tag_id = t.id
                        WHERE nt.novel_id = n.id AND t.name = ?
                    )
                """
                params.append(tag)

        if exclude_tags:
            for tag in exclude_tags:
                query += """
                    AND NOT EXISTS (
                        SELECT 1 FROM novel_tags nt
                        JOIN tags t ON nt.tag_id = t.id
                        WHERE nt.novel_id = n.id AND t.name = ?
                    )
                """
                params.append(tag)

        sort_map = {
            "title": "n.title ASC",
            "last_updated": "n.last_updated DESC",
            "chapter_count": "chapter_count DESC",
            "word_count": "word_count DESC",
        }
        order_by = sort_map.get(sort_by, "n.title ASC")
        query += f" ORDER BY {order_by}"

        return self.db.execute(query, tuple(params))
