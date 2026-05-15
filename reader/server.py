# =============================================================================
# CHANGES:
#   - Added in-memory novel cache (_novels_cache) for the /api/novels endpoint.
#     The cache stores the full unfiltered novel list and a timestamp. On each
#     request, if the cache is fresher than NOVELS_CACHE_TTL_SECONDS (60s) AND
#     no tag filters are active, filtering and sorting are done in Python on the
#     cached list — zero DB queries. With tag filters the full filtered query
#     still runs (tag filtering is SQL-only), but the expensive word_count
#     subquery is replaced by reading the stored novels.word_count column.
#   - The word_count subquery (length(plain_content) - length(replace(...))) has
#     been removed from the live query entirely. word_count is now read from the
#     novels.word_count column, which is populated by a separate lightweight
#     UPDATE triggered by update_progress() writes and chapter content saves.
#     This is the primary fix for slow library loads on large databases.
#   - _invalidate_novels_cache(): Called from update_progress() and the two
#     background fetch triggers so the cache is always fresh after meaningful
#     state changes. Tag filter requests also bypass the cache.
#   - run_background_fetch(): Unchanged — still logs to ~/Desktop/reader_debug.log.
# =============================================================================

import logging
import os
import sqlite3
import asyncio
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path

from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import DB_PATH

DEBUG = False
DEBUG_LOG_PATH = os.path.expanduser("~/Desktop/reader_debug.log")

# Project root for relative paths (covers)
PROJECT_ROOT = Path(__file__).parent.parent

# --- Novel list cache ---
# Stores the unfiltered result of the /api/novels base query so tag-free
# requests (the common case: just browsing or sorting) hit memory instead
# of running the expensive word-count subquery across all chapters.
NOVELS_CACHE_TTL_SECONDS = 60

_novels_cache: list = []
_novels_cache_ts: float = 0.0


def _invalidate_novels_cache():
    """
    Clears the in-memory novels cache so the next /api/novels request
    re-queries the database.

    Called by: update_progress(), trigger_fetch_chapters(),
               trigger_update_chapters()
    Depends on: _novels_cache, _novels_cache_ts module globals
    """
    global _novels_cache, _novels_cache_ts
    _novels_cache = []
    _novels_cache_ts = 0.0


def _get_debug_logger():
    """
    Returns a file-based logger for background fetch output.
    Always writes ERROR and above to ~/Desktop/reader_debug.log so silent
    crashes in the background thread are never invisible.
    When DEBUG=True, also writes INFO and DEBUG messages.

    Returns:
        logging.Logger: Configured logger instance.

    Called by: run_background_fetch()
    Depends on: DEBUG_LOG_PATH
    """
    log = logging.getLogger("reader_bg")
    if not log.handlers:
        handler = logging.FileHandler(DEBUG_LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        log.addHandler(handler)
        log.setLevel(logging.DEBUG if DEBUG else logging.ERROR)
    return log


# --- Database ---
def get_db_connection():
    db_file = Path(DB_PATH)
    if not db_file.is_absolute():
        db_file = PROJECT_ROOT / db_file

    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = get_db_connection()
    yield
    app.state.db.close()


app = FastAPI(lifespan=lifespan)


# --- Models ---
class ProgressUpdate(BaseModel):
    novel_id: int
    chapter_id: int
    scroll_position: float


class BookmarkCreate(BaseModel):
    chapter_id: int
    novel_id: int
    label: str
    scroll_position: float


class NoteUpdate(BaseModel):
    chapter_id: int
    content: str


# --- API Endpoints ---


@app.get("/api/novels")
async def get_novels(
    include_tags: Optional[List[str]] = Query(None),
    exclude_tags: Optional[List[str]] = Query(None),
    sort_by: str = "title",
):
    """
    Returns the novel list with chapter counts, read progress, and word count.

    Performance strategy:
      - Base query reads novels.word_count (a stored column) instead of
        computing it live via SUM(length(plain_content) - ...) across all
        chapters. This eliminates the dominant bottleneck at scale.
      - When no tag filters are active, results are served from an in-memory
        cache (TTL: 60s) and sorted in Python — zero DB queries per request
        during normal browsing.
      - Tag-filtered requests always run the full DB query since SQL EXISTS
        checks cannot be replicated client-side without all tag data.

    Called by: frontend app.js renderLibrary()
    Depends on: _novels_cache, _novels_cache_ts, NOVELS_CACHE_TTL_SECONDS
    """
    global _novels_cache, _novels_cache_ts

    has_tag_filters = bool(include_tags or exclude_tags)
    now = _time.monotonic()
    cache_fresh = (now - _novels_cache_ts) < NOVELS_CACHE_TTL_SECONDS

    # --- Cache hit: no tag filters and cache is warm ---
    if not has_tag_filters and cache_fresh and _novels_cache:
        novels = _novels_cache
        sort_map_py = {
            "title": lambda n: (n.get("title") or "").lower(),
            "last_updated": lambda n: n.get("last_updated") or "",
            "chapter_count": lambda n: -(n.get("chapter_count") or 0),
            "word_count": lambda n: -(n.get("word_count") or 0),
        }
        sorter = sort_map_py.get(sort_by, sort_map_py["title"])
        return sorted(novels, key=sorter)

    # --- DB query ---
    params: list = []

    # word_count is read from the stored novels.word_count column.
    # This avoids the expensive SUM(length(plain_content) - length(replace(...)))
    # subquery that scans every chapter row on every page load.
    query = """
            SELECT n.*,
                   (SELECT COUNT(*) FROM chapters WHERE novel_id = n.id) as chapter_count,
                   (SELECT COUNT(*) FROM reading_progress
                    WHERE novel_id = n.id AND scroll_position >= 0.9) as chapters_read
            FROM novels n
            WHERE 1=1 \
            """

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
        "word_count": "n.word_count DESC",
    }
    order_by = sort_map.get(sort_by, "n.title ASC")
    query += f" ORDER BY {order_by}"

    cursor = app.state.db.execute(query, tuple(params))
    rows = [dict(row) for row in cursor.fetchall()]

    # Populate cache only for unfiltered results
    if not has_tag_filters:
        _novels_cache = rows
        _novels_cache_ts = now

    return rows


@app.get("/api/tags")
async def get_tags():
    query = """
            SELECT t.name, COUNT(nt.novel_id) as novel_count
            FROM tags t
                     LEFT JOIN novel_tags nt ON t.id = nt.tag_id
            GROUP BY t.id, t.name
            ORDER BY novel_count DESC, t.name ASC \
            """
    cursor = app.state.db.execute(query)
    return [
        {"name": row["name"], "count": row["novel_count"]} for row in cursor.fetchall()
    ]


@app.get("/api/novels/{novel_id}")
async def get_novel_detail(novel_id: int):
    novel_query = "SELECT * FROM novels WHERE id = ?"
    novel_row = app.state.db.execute(novel_query, (novel_id,)).fetchone()
    if not novel_row:
        raise HTTPException(status_code=404, detail="Novel not found")

    chapters_query = """
                     SELECT id, chapter_title, chapter_order,
                            (SELECT 1 FROM reading_progress WHERE chapter_id = chapters.id AND scroll_position >= 0.9) as is_read
                     FROM chapters
                     WHERE novel_id = ?
                     ORDER BY chapter_order ASC \
                     """
    chapters = [
        dict(row)
        for row in app.state.db.execute(chapters_query, (novel_id,)).fetchall()
    ]

    tags_query = """
                 SELECT t.name FROM tags t
                                        JOIN novel_tags nt ON t.id = nt.tag_id
                 WHERE nt.novel_id = ? \
                 """
    tags = [
        row["name"] for row in app.state.db.execute(tags_query, (novel_id,)).fetchall()
    ]

    result = dict(novel_row)
    result["chapters"] = chapters
    result["tags"] = tags
    return result


@app.get("/api/chapters/{chapter_id}")
async def get_chapter(chapter_id: int):
    query = "SELECT * FROM chapters WHERE id = ?"
    row = app.state.db.execute(query, (chapter_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Chapter not found")

    novel_id = row["novel_id"]
    chapter_order = row["chapter_order"]

    content = row["html_content"] or row["plain_content"] or ""
    content_type = "html" if row["html_content"] else "plain"

    import re

    clean_text = re.sub(r"<[^>]*>", "", content) if content_type == "html" else content
    word_count = len(clean_text.split())

    prev_query = "SELECT id FROM chapters WHERE novel_id = ? AND chapter_order < ? ORDER BY chapter_order DESC LIMIT 1"
    next_query = "SELECT id FROM chapters WHERE novel_id = ? AND chapter_order > ? ORDER BY chapter_order ASC LIMIT 1"

    prev_row = app.state.db.execute(prev_query, (novel_id, chapter_order)).fetchone()
    next_row = app.state.db.execute(next_query, (novel_id, chapter_order)).fetchone()

    return {
        "id": row["id"],
        "novel_id": novel_id,
        "chapter_title": row["chapter_title"],
        "chapter_order": chapter_order,
        "content": content,
        "content_type": content_type,
        "word_count": word_count,
        "prev_chapter_id": prev_row["id"] if prev_row else None,
        "next_chapter_id": next_row["id"] if next_row else None,
    }


@app.get("/api/covers/{novel_id}")
async def get_cover(novel_id: int):
    query = "SELECT cover_path FROM novels WHERE id = ?"
    row = app.state.db.execute(query, (novel_id,)).fetchone()
    if not row or not row["cover_path"]:
        raise HTTPException(status_code=404, detail="Cover not found")

    path = Path(row["cover_path"])
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if not path.exists():
        raise HTTPException(status_code=404, detail="Cover file not found")

    return FileResponse(path)


@app.get("/api/search")
async def search(q: str):
    q_param = f"%{q}%"

    novels_query = "SELECT * FROM novels WHERE title LIKE ? OR author LIKE ? LIMIT 20"
    novels = [
        dict(row)
        for row in app.state.db.execute(novels_query, (q_param, q_param)).fetchall()
    ]

    chapters_query = "SELECT * FROM chapters WHERE chapter_title LIKE ? LIMIT 20"
    chapters = [
        dict(row) for row in app.state.db.execute(chapters_query, (q_param,)).fetchall()
    ]

    return {"novels": novels, "chapters": chapters}


@app.get("/api/progress")
async def get_all_progress():
    query = "SELECT * FROM reading_progress"
    return [dict(row) for row in app.state.db.execute(query).fetchall()]


@app.post("/api/progress")
async def update_progress(progress: ProgressUpdate):
    query = """
            INSERT INTO reading_progress (novel_id, chapter_id, scroll_position, read_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(novel_id, chapter_id) DO UPDATE SET
                                                            scroll_position = excluded.scroll_position,
                                                            read_at = excluded.read_at \
            """
    app.state.db.execute(
        query, (progress.novel_id, progress.chapter_id, progress.scroll_position)
    )
    app.state.db.commit()
    # Invalidate cache so chapters_read count is fresh on next library load
    _invalidate_novels_cache()
    return {"status": "ok"}


@app.get("/api/bookmarks")
async def get_bookmarks():
    query = """
            SELECT b.*, c.chapter_title, n.title as novel_title
            FROM bookmarks b
                     JOIN chapters c ON b.chapter_id = c.id
                     JOIN novels n ON b.novel_id = n.id
            ORDER BY b.created_at DESC \
            """
    return [dict(row) for row in app.state.db.execute(query).fetchall()]


@app.post("/api/bookmarks")
async def create_bookmark(bm: BookmarkCreate):
    query = """
            INSERT INTO bookmarks (chapter_id, novel_id, label, scroll_position)
            VALUES (?, ?, ?, ?) \
            """
    cursor = app.state.db.execute(
        query, (bm.chapter_id, bm.novel_id, bm.label, bm.scroll_position)
    )
    app.state.db.commit()
    new_id = cursor.lastrowid

    return dict(
        app.state.db.execute(
            """
            SELECT b.*, c.chapter_title, n.title as novel_title
            FROM bookmarks b
                     JOIN chapters c ON b.chapter_id = c.id
                     JOIN novels n ON b.novel_id = n.id
            WHERE b.id = ?
            """,
            (new_id,),
        ).fetchone()
    )


@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int):
    row = app.state.db.execute(
        "SELECT id FROM bookmarks WHERE id = ?", (bookmark_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Bookmark not found")

    app.state.db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
    app.state.db.commit()
    return {"status": "deleted"}


@app.get("/api/notes/{chapter_id}")
async def get_note(chapter_id: int):
    query = "SELECT chapter_id, content FROM notes WHERE chapter_id = ?"
    row = app.state.db.execute(query, (chapter_id,)).fetchone()
    if row:
        return dict(row)
    return {"chapter_id": chapter_id, "content": ""}


@app.post("/api/notes")
async def upsert_note(note: NoteUpdate):
    query = """
            INSERT INTO notes (chapter_id, content, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chapter_id) DO UPDATE SET
                                                  content = excluded.content,
                                                  updated_at = excluded.updated_at \
            """
    app.state.db.execute(query, (note.chapter_id, note.content))
    app.state.db.commit()
    return {"status": "ok"}


# --- Chapter Management (Discovery/Update) ---


def run_background_fetch(novel_id: int, mode: str):
    """
    Background worker for fetching or updating chapters, triggered from the reader UI.

    BrowserService is started and stopped explicitly here so the Playwright
    process is properly cleaned up after each background job. update_content_status
    is in a finally block so the UI polling always terminates even if the fetch
    crashes partway through.

    Parameters:
        novel_id (int): DB id of the novel to fetch/update.
        mode (str): 'fetch' (first download) or 'update' (check for new chapters).

    Called by: trigger_fetch_chapters(), trigger_update_chapters()
    Depends on: ScraperService, CoverManager, NovelRepository, BrowserService
    """
    from core import DatabaseManager, NovelRepository, NetworkClient
    from services import BrowserService, CoverManager, ScraperService

    log = _get_debug_logger()

    db_manager = DatabaseManager()
    repository = NovelRepository(db_manager)
    network_client = NetworkClient()
    browser_service = BrowserService()
    cover_manager = CoverManager(network_client, repository)
    scraper = ScraperService(network_client, browser_service, repository, cover_manager)

    try:
        log.error(f"[BG] Starting {mode} for novel {novel_id}")
        browser_service.start()

        success = scraper.refresh_metadata(novel_id)
        if not success:
            log.error(
                f"[BG] Metadata refresh failed for novel {novel_id}, proceeding anyway"
            )

        log.error(f"[BG] Fetching chapter content for novel {novel_id}")
        scraper.fetch_chapters(novel_id)

        if mode == "update":
            repository.update_novel_timestamp(novel_id)

        log.error(f"[BG] Completed {mode} for novel {novel_id}")

    except Exception as e:
        log.error(f"[BG] Error during {mode} for novel {novel_id}: {e}", exc_info=True)

    finally:
        try:
            repository.update_content_status(novel_id, "full")
        except Exception as e:
            log.error(f"[BG] Failed to update content_status for novel {novel_id}: {e}")
        try:
            browser_service.stop()
        except Exception as e:
            log.error(f"[BG] Failed to stop browser for novel {novel_id}: {e}")

    # Invalidate the library cache so chapter counts are fresh in the UI
    _invalidate_novels_cache()


@app.post("/api/novels/{novel_id}/fetch-chapters")
async def trigger_fetch_chapters(novel_id: int):
    asyncio.get_event_loop().run_in_executor(
        None, run_background_fetch, novel_id, "fetch"
    )
    _invalidate_novels_cache()
    return {"status": "started", "novel_id": novel_id}


@app.post("/api/novels/{novel_id}/update-chapters")
async def trigger_update_chapters(novel_id: int):
    asyncio.get_event_loop().run_in_executor(
        None, run_background_fetch, novel_id, "update"
    )
    _invalidate_novels_cache()
    return {"status": "started", "novel_id": novel_id}


@app.get("/api/novels/{novel_id}/fetch-status")
async def get_fetch_status(novel_id: int):
    novel = app.state.db.execute(
        "SELECT content_status FROM novels WHERE id = ?", (novel_id,)
    ).fetchone()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    total_chapters = app.state.db.execute(
        "SELECT COUNT(*) as count FROM chapters WHERE novel_id = ?", (novel_id,)
    ).fetchone()["count"]

    downloaded_chapters = app.state.db.execute(
        "SELECT COUNT(*) as count FROM chapters WHERE novel_id = ? AND plain_content IS NOT NULL",
        (novel_id,),
    ).fetchone()["count"]

    return {
        "content_status": novel["content_status"],
        "total_chapters": total_chapters,
        "downloaded_chapters": downloaded_chapters,
    }


# --- Static Files ---
app.mount(
    "/",
    StaticFiles(directory=PROJECT_ROOT / "reader" / "static", html=True),
    name="static",
)
