# =============================================================================
# CHANGES:
#   - scrape_novel(): ScribbleHub now uses fast network fetch (curl_cffi GET)
#     instead of forcing Playwright. The adapter fetches all chapters via a
#     direct POST to admin-ajax.php, so Playwright is not needed for chapter
#     listing. Playwright fallback is kept for sites that need it.
#   - All other logic unchanged.
# =============================================================================

import hashlib
import logging
import os
import random
import time

from bs4 import BeautifulSoup

from adapters import get_adapter
from core.config import FETCH_DELAY, FETCH_DELAY_JITTER, FETCH_MAX_RETRIES, TIMEOUT
from core.database import NovelRepository, NOVEL_STATUS_ABANDONED
from core.network import NetworkClient
from core.run_logger import RunLogger
from services import BrowserService, CoverManager
from utils import slugify

logger = logging.getLogger(__name__)

DEBUG = False


class ScraperService:
    def __init__(
        self,
        network_client: NetworkClient,
        browser_service: BrowserService,
        repository: NovelRepository,
        cover_manager: CoverManager,
    ):
        self.network = network_client
        self.browser = browser_service
        self.repository = repository
        self.cover_manager = cover_manager

    def scrape_novel(
        self, url: str, use_local: str = None, save_html: str = None
    ) -> dict | None:
        """
        Fetches and parses a novel landing page.

        For ScribbleHub, always uses Playwright with keep_page_open=True,
        wait_until="load", and block_resources=False. The full JS bundle must
        execute and remain active so that pagination click handlers can fire
        AJAX requests that the adapter intercepts via page.route(). Blocking
        resources would install a competing "**/*" route handler that consumes
        route events before the adapter's handler runs.

        For all other sites, tries fast network fetch first with Playwright as
        fallback. Playwright fallback uses keep_page_open=False so the page is
        closed inside get_page_content() immediately after HTML capture.

        Parameters:
            url (str): The novel's landing page URL.
            use_local (str): Path to a local HTML file to use instead of fetching.
            save_html (str): If set, saves the raw fetched HTML to this path.

        Returns:
            dict | None: Parsed novel data, or None on failure.

        Called by: main.py, discovery_service.py, refresh_metadata()
        Depends on: get_adapter(), NetworkClient.get(), BrowserService.get_page_content()
        """
        from adapters.scribblehub import ScribbleHubAdapter

        adapter = get_adapter(url)
        logger.info(f"Using adapter: {type(adapter).__name__}")
        if DEBUG:
            logger.debug(f"[scrape_novel] url={url} use_local={use_local}")

        # --- Local file mode (dev/debug) ---
        if use_local and os.path.exists(use_local):
            with open(use_local, "r", encoding="utf-8") as f:
                html = f.read()
            soup = BeautifulSoup(html, "html.parser")
            return adapter.parse(soup, url)

        # --- ScribbleHub: fast network fetch + AJAX ---
        # The adapter now uses a direct POST to admin-ajax.php to get all
        # chapters, so Playwright is NOT needed for chapter listing.
        # We use the same fast network fetch path as other sites.
        if isinstance(adapter, ScribbleHubAdapter):
            logger.info(f"[SH] Using fast network fetch for ScribbleHub: {url}")
            try:
                response = self.network.get(url)
                if response.status_code == 200:
                    html = response.text
                else:
                    logger.warning(
                        f"[SH] Fast fetch returned HTTP {response.status_code} "
                        f"for {url}, trying browser..."
                    )
                    html = None
            except Exception as e:
                logger.warning(
                    f"[SH] Fast fetch failed for {url}: {e}. Trying browser..."
                )
                html = None

            if not html:
                try:
                    html, _ = self.browser.get_page_content(
                        url, keep_page_open=False
                    )
                except Exception as e:
                    logger.error(
                        f"[scrape_novel] Browser fetch also failed for {url}: {e}"
                    )
                    return None

            if not html:
                logger.error(f"[scrape_novel] Failed to get any content for {url}")
                return None

            if save_html:
                with open(save_html, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info(f"[scrape_novel] Saved raw HTML to: {save_html}")

            try:
                soup = BeautifulSoup(html, "html.parser")
                return adapter.parse(soup, url, network_client=self.network)
            except Exception as e:
                logger.error(
                    f"[scrape_novel] Failed to parse ScribbleHub novel {url}: {e}"
                )
                return None

        # --- All other sites: try fast network fetch first ---
        html = None

        logger.info(f"[scrape_novel] Attempting fast fetch: {url}")
        try:
            response = self.network.get(url)
            if response.status_code == 200:
                html = response.text
            else:
                logger.warning(
                    f"[scrape_novel] Fast fetch returned HTTP {response.status_code} "
                    f"for {url}, trying browser..."
                )
        except Exception as e:
            logger.warning(
                f"[scrape_novel] Fast fetch failed for {url}: {e}. Trying browser..."
            )

        # --- Playwright fallback for non-ScribbleHub sites ---
        if not html:
            try:
                # keep_page_open=False — we only need the HTML, page closes inside
                html, _ = self.browser.get_page_content(url, keep_page_open=False)
            except Exception as e:
                logger.error(f"[scrape_novel] Browser fetch also failed for {url}: {e}")
                return None

        if not html:
            logger.error(f"[scrape_novel] Failed to get any content for {url}")
            return None

        if save_html:
            with open(save_html, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"[scrape_novel] Saved raw HTML to: {save_html}")

        try:
            soup = BeautifulSoup(html, "html.parser")
            return adapter.parse(soup, url)
        except Exception as e:
            logger.error(f"[scrape_novel] Failed to parse novel {url}: {e}")
            return None

    def populate_novel(self, data: dict, metadata_only: bool = False) -> int | None:
        """
        Inserts or updates a novel and its chapters/tags in the database.

        Parameters:
            data (dict): Parsed novel data from scrape_novel().
            metadata_only (bool): If True, skips chapter upsert and marks
                                  content_status as 'metadata'. Used by
                                  discovery runs where chapter content is not
                                  yet downloaded.

        Returns:
            int | None: The novel's database ID, or None on failure.

        Called by: main.py, discovery_service.py
        Depends on: NovelRepository, CoverManager
        """
        slug = data.get("slug") or slugify(data["title"])
        novel_id = self.repository.upsert_novel(data, slug)

        if novel_id:
            if not metadata_only:
                self.repository.upsert_chapters(novel_id, data.get("chapters", []))

            self.repository.link_tags(novel_id, data.get("tags", []))

            cover_url = data.get("cover_url")
            if cover_url:
                self.cover_manager.download_and_save(cover_url, novel_id, slug)

        if novel_id and metadata_only:
            self.repository.update_content_status(novel_id, "metadata")

        return novel_id

    def refresh_metadata(self, novel_id: int) -> bool:
        """
        Re-scrapes and updates metadata for a novel already in the database.

        Parameters:
            novel_id (int): The database ID of the novel to refresh.

        Returns:
            bool: True if refresh succeeded, False otherwise.

        Called by: reader API, server.run_background_fetch()
        Depends on: scrape_novel(), populate_novel()
        """
        rows = self.repository.db.execute(
            "SELECT source_url FROM novels WHERE id = ?", (novel_id,)
        )
        if not rows:
            logger.warning(f"[refresh_metadata] Novel {novel_id} not found in DB")
            return False

        url = rows[0][0]
        if not url:
            logger.warning(f"[refresh_metadata] No source_url for novel {novel_id}")
            return False

        logger.info(
            f"[refresh_metadata] Refreshing metadata for novel {novel_id}: {url}"
        )
        data = self.scrape_novel(url)
        if not data:
            logger.warning(f"[refresh_metadata] Failed to scrape {url}")
            return False

        self.populate_novel(data, metadata_only=True)
        return True

    def refresh_chapter_urls_for_novel(self, novel_id: int) -> bool:
        """
        Re-scrapes a ScribbleHub novel's landing page and updates any changed
        chapter URLs in the database before chapter content is fetched.

        This should be called before fetch_chapters(novel_id=...) to ensure the
        DB has current URLs. Without this, stale URLs (from ScribbleHub slug
        changes) cause 404 errors during content download.

        Algorithm:
          1. Look up the novel's source_url from the DB.
          2. Call scrape_novel(source_url) to get the current chapter list.
          3. Compare source chapters (by order) against DB chapters.
          4. For any order where the URL changed: UPDATE the row in-place
             (preserving content, hash, and metadata).
          5. For any new orders: INSERT as PENDING.
          6. Execute all changes in a single transaction.

        Parameters:
            novel_id (int): DB id of the novel to refresh.

        Returns:
            bool: True if refresh succeeded (or no changes needed), False on error.

        Called by: fetch_chapters(), sync_novels.py, backfill_chapters.py
        Depends on: scrape_novel(), NovelRepository.get_novel_chapters(),
                    DatabaseManager.execute_transaction()
        """
        # --- Look up the novel ---
        novel_row = self.repository.get_novel_by_id(novel_id)
        if not novel_row:
            logger.warning(
                f"[refresh_urls] Novel {novel_id} not found in DB — skipping"
            )
            return False

        source_url = novel_row["source_url"]
        title = novel_row["title"]
        if not source_url:
            logger.warning(
                f"[refresh_urls] No source_url for novel {novel_id} ('{title}') — skipping"
            )
            return False

        # Only run for ScribbleHub novels (other sites don't change slugs)
        if "scribblehub.com" not in source_url:
            if DEBUG:
                logger.debug(
                    f"[refresh_urls] Not a ScribbleHub novel ('{title}'), skipping"
                )
            return True

        logger.info(f"[refresh_urls] Refreshing chapter URLs for '{title}' (id={novel_id})")

        # --- Scrape current chapter list ---
        try:
            data = self.scrape_novel(source_url)
        except Exception as e:
            logger.error(f"[refresh_urls] scrape_novel() raised for '{title}': {e}")
            return False

        if not data:
            logger.warning(f"[refresh_urls] scrape_novel() returned no data for '{title}'")
            return False

        source_chapters = data.get("chapters", [])
        if not source_chapters:
            logger.info(f"[refresh_urls] Source has 0 chapters for '{title}' — nothing to refresh")
            return True

        source_by_order: dict[int, dict] = {}
        for ch in source_chapters:
            source_by_order[ch["order"]] = {"title": ch["title"], "url": ch["url"]}

        # --- Compare against DB ---
        db_chapters = self.repository.get_novel_chapters(novel_id)

        # Build a reverse lookup: url -> list of {id, order} for this novel's
        # chapters. There may be duplicates (same URL or same order) from
        # previous partial syncs.
        db_by_url: dict[str, list[dict]] = {}
        for order, row in db_chapters.items():
            db_by_url.setdefault(row["url"], []).append({"id": row["id"], "order": order})

        operations = []
        update_count = 0
        insert_count = 0
        delete_count = 0

        for order, src in source_by_order.items():
            if order in db_chapters:
                if db_chapters[order]["url"] != src["url"]:
                    ch_id = db_chapters[order]["id"]
                    new_url = src["url"]

                    # If the new URL already exists as any other row for this
                    # novel, delete the stale old row — the row with the
                    # correct URL already exists (and may have content).
                    if new_url in db_by_url:
                        logger.info(
                            f"[refresh_urls] New URL for ch {order} already "
                            f"exists in DB — deleting stale row {ch_id}"
                        )
                        operations.append((
                            "DELETE FROM chapters WHERE id = ?",
                            (ch_id,),
                        ))
                        delete_count += 1
                        continue

                    operations.append((
                        """UPDATE chapters
                           SET chapter_url = ?,
                               chapter_title = ?,
                               last_updated = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (new_url, src["title"], ch_id),
                    ))
                    update_count += 1
                    logger.info(
                        f"[refresh_urls] URL changed for '{title}' ch {order}: "
                        f"{db_chapters[order]['url']} -> {new_url}"
                    )
            else:
                # Only insert if this URL doesn't already exist for the novel
                if src["url"] in db_by_url:
                    existing = db_by_url[src["url"]][0]
                    logger.info(
                        f"[refresh_urls] Source ch {order} URL already exists "
                        f"as ch {existing['order']} — skipping insert"
                    )
                    continue
                operations.append((
                    """INSERT INTO chapters (novel_id, chapter_title, chapter_hash, chapter_order, chapter_url)
                       VALUES (?, ?, ?, ?, ?)""",
                    (novel_id, src["title"], "PENDING", order, src["url"]),
                ))
                insert_count += 1

        if operations:
            try:
                self.repository.db.execute_transaction(operations)
                logger.info(
                    f"[refresh_urls] '{title}': {update_count} URL(s) updated, "
                    f"{insert_count} inserted, {delete_count} stale deleted"
                )
            except Exception as e:
                logger.error(f"[refresh_urls] Transaction failed for '{title}': {e}")
                return False
        else:
            logger.info(f"[refresh_urls] All URLs current for '{title}'")

        return True

    def refresh_all_chapter_urls(self) -> dict:
        """
        Refreshes chapter URLs for every ScribbleHub novel in the DB.

        Used by the global backfill (fetch_chapters with no novel_id) to
        ensure all pending chapters have current URLs before attempting to
        download content. Without this, stale ScribbleHub slugs cause 404
        errors that waste retries.

        Returns:
            dict: Summary with keys 'novels_processed', 'chapters_updated',
                  'chapters_inserted', 'failed_novels'.

        Called by: fetch_chapters(), backfill_chapters.py
        Depends on: NovelRepository.db.execute(),
                    refresh_chapter_urls_for_novel()
        """
        query = """
            SELECT n.id, n.title
            FROM novels n
            WHERE n.source_url IS NOT NULL
              AND n.status != 'ABANDONED'
              AND n.source_url LIKE '%scribblehub.com%'
            ORDER BY n.id ASC
        """
        rows = self.repository.db.execute(query)
        summary = {
            "novels_processed": 0,
            "chapters_updated": 0,
            "chapters_inserted": 0,
            "failed_novels": 0,
        }

        logger.info(
            f"[refresh_all_urls] Refreshing chapter URLs for {len(rows)} "
            f"ScribbleHub novel(s)..."
        )

        for novel_id, title in rows:
            logger.info(
                f"[refresh_all_urls] [{summary['novels_processed'] + 1}/{len(rows)}] "
                f"'{title}' (id={novel_id})"
            )
            try:
                ok = self.refresh_chapter_urls_for_novel(novel_id)
                if ok:
                    summary["novels_processed"] += 1
                else:
                    summary["failed_novels"] += 1
            except Exception as e:
                logger.error(
                    f"[refresh_all_urls] Unexpected error for '{title}': {e}"
                )
                summary["failed_novels"] += 1

        logger.info(
            f"[refresh_all_urls] Complete: {summary['novels_processed']} novels processed, "
            f"{summary['failed_novels']} failed"
        )
        return summary

    def fetch_chapters(self, novel_id: int = None):
        """
        Downloads plain text + HTML content for all pending (unfetched) chapters.

        Applies a jittered sleep between each chapter to avoid rate-limiting.
        Retries up to FETCH_MAX_RETRIES times with exponential backoff on failure.

        Parameters:
            novel_id (int | None): If set, only fetches chapters for this novel.
                                   If None, fetches all pending chapters globally.

        Returns:
            None

        Called by: main.py, sync_novels.py, backfill_chapters.py
        Depends on: NovelRepository.get_pending_chapters(),
                    NovelRepository.update_chapter_content(),
                    get_adapter(), NetworkClient.get(), RunLogger
        """
        tasks = self.repository.get_pending_chapters(novel_id)
        if not tasks:
            logger.info(
                "[fetch_chapters] All chapters are up to date — nothing to fetch."
            )
            return

        logger.info(f"[fetch_chapters] Starting fetch for {len(tasks)} chapters...")

        # --- Refresh chapter URLs for ScribbleHub novels before fetching ---
        # Per novel: re-scrape the novel's landing page so changed slugs are
        # updated in the DB. Global mode: refresh ALL ScribbleHub novels first.
        if novel_id is not None:
            logger.info(
                f"[fetch_chapters] Refreshing chapter URLs for novel {novel_id}..."
            )
            self.refresh_chapter_urls_for_novel(novel_id)
            # Re-fetch task list in case new chapters were inserted
            tasks = self.repository.get_pending_chapters(novel_id)
            if not tasks:
                logger.info(
                    "[fetch_chapters] All chapters up to date after URL refresh."
                )
                return
        else:
            # Global backfill: refresh URLs for every ScribbleHub novel first
            # so we don't waste retries on stale 404 slugs.
            logger.info(
                "[fetch_chapters] Global backfill — refreshing all ScribbleHub "
                "chapter URLs before fetching..."
            )
            self.refresh_all_chapter_urls()
            # Re-fetch pending list in case inserts/deletes changed it
            tasks = self.repository.get_pending_chapters(novel_id)
            if not tasks:
                logger.info(
                    "[fetch_chapters] All chapters up to date after URL refresh."
                )
                return

        with RunLogger(total_pending=len(tasks)) as log:
            abandoned_novels: set[int] = set()

            for ch_id, title, url in tasks:
                # Skip chapters from novels we've already determined are dead
                ch_novel_id = self._get_chapter_novel_id(ch_id)
                if ch_novel_id in abandoned_novels:
                    logger.info(
                        f"[fetch_chapters] Skipping '{title}' — novel {ch_novel_id} "
                        f"marked ABANDONED (source gone)"
                    )
                    continue

                start_time = time.time()
                success = False
                error_msg = ""

                for attempt in range(1, FETCH_MAX_RETRIES + 2):
                    try:
                        logger.info(
                            f"[fetch_chapters] Fetching: '{title}' (Attempt {attempt}) "
                            f"url={url}"
                        )
                        if DEBUG:
                            logger.debug(f"[fetch_chapters] ch_id={ch_id} url={url}")

                        adapter = get_adapter(url)
                        response = self.network.get(url, timeout=TIMEOUT)

                        if response.status_code != 200:
                            raise Exception(f"HTTP {response.status_code}")

                        soup = BeautifulSoup(response.text, "html.parser")
                        content_data = adapter.parse_chapter_content(soup)

                        if not content_data or "plain_text" not in content_data:
                            raise Exception("Invalid content parsed")

                        content_text = content_data["plain_text"]
                        raw_html = content_data.get("raw_html", "")
                        chapter_hash = hashlib.sha256(
                            content_text.encode("utf-8")
                        ).hexdigest()

                        self.repository.update_chapter_content(
                            ch_id, content_text, raw_html, chapter_hash
                        )

                        elapsed = time.time() - start_time
                        word_count = len(content_text.split())
                        log.ok(ch_id, title, word_count, elapsed)
                        logger.info(f"[fetch_chapters] Saved '{title}'.")
                        success = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        if attempt <= FETCH_MAX_RETRIES:
                            backoff = 5 if attempt == 1 else 15
                            logger.warning(
                                f"[fetch_chapters] Retry {attempt} for '{title}' "
                                f"url={url}: {error_msg} — waiting {backoff}s"
                            )
                            log.retry(ch_id, title, attempt, error_msg)
                            time.sleep(backoff)
                        else:
                            logger.error(
                                f"[fetch_chapters] Failed to fetch '{title}' "
                                f"url={url} after {attempt} attempts: {error_msg}"
                            )
                            log.fail(ch_id, title, error_msg)

                # If all retries failed with 404, check if the novel's source
                # page is also gone. If so, abandon the novel entirely.
                if not success and "404" in error_msg and ch_novel_id is not None:
                    if self._is_novel_gone(ch_novel_id):
                        logger.warning(
                            f"[fetch_chapters] Novel {ch_novel_id} source page is "
                            f"404 — marking ABANDONED and skipping all remaining "
                            f"chapters for this novel"
                        )
                        self.repository.set_novel_status(
                            ch_novel_id, NOVEL_STATUS_ABANDONED
                        )
                        abandoned_novels.add(ch_novel_id)

                # Jittered sleep between chapters — never a predictable fixed interval
                jittered_delay = random.uniform(
                    FETCH_DELAY, FETCH_DELAY + FETCH_DELAY_JITTER
                )
                if DEBUG:
                    logger.debug(
                        f"[fetch_chapters] sleeping {jittered_delay:.1f}s before next"
                    )
                time.sleep(jittered_delay)

    def _get_chapter_novel_id(self, ch_id: int) -> int | None:
        """Returns the novel_id for a chapter row, or None on error."""
        try:
            rows = self.repository.db.execute(
                "SELECT novel_id FROM chapters WHERE id = ?", (ch_id,)
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def _is_novel_gone(self, novel_id: int) -> bool:
        """
        Checks whether a novel's source landing page returns 404.
        Used to detect stubbed/removed novels so we can mark them ABANDONED
        and skip all remaining chapter fetches.
        """
        try:
            rows = self.repository.db.execute(
                "SELECT source_url FROM novels WHERE id = ?", (novel_id,)
            )
            if not rows or not rows[0][0]:
                return False
            source_url = rows[0][0]
            resp = self.network.get(source_url, timeout=15)
            if resp.status_code == 404:
                logger.info(
                    f"[novel_gone] Source URL 404 for novel {novel_id}: {source_url}"
                )
                return True
            return False
        except Exception as e:
            logger.debug(f"[novel_gone] Error checking novel {novel_id}: {e}")
            return False
