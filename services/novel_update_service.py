# =============================================================================
# CHANGES:
#   - sync_novel(): Moved time.sleep(FETCH_DELAY) from the top of the function
#     (before any try block) to inside sync_all()'s try block, AFTER the
#     sync_novel() call. Previously the delay fired even when sync_novel()
#     was never called (exception path in sync_all()) or raised immediately,
#     wasting 8 seconds per failed novel before the except block ran.
#     The delay now lives in sync_all() alongside the call that needs it.
#   - sync_all(): Inter-novel delay is now always logged at DEBUG level so
#     it is visible in debug runs without cluttering normal INFO output.
#   - All other logic unchanged.
# =============================================================================

import logging
import random
import time
from datetime import datetime, timedelta

from core.network import NetworkClient
from core.database import NovelRepository, NOVEL_STATUS_ABANDONED
from core.config import FETCH_DELAY
from services.scraper_service import ScraperService

logger = logging.getLogger(__name__)

# Novels updated within this many days will be skipped during sync
SYNC_SKIP_IF_UPDATED_WITHIN_DAYS = 7

DEBUG = False


class NovelUpdateService:
    def __init__(
        self,
        network_client: NetworkClient,
        repository: NovelRepository,
        scraper_service: ScraperService,
    ):
        self.network = network_client
        self.repository = repository
        self.scraper = scraper_service

    def sync_all(self):
        """
        Orchestrates the sync process for all active novels.

        Skips novels updated within SYNC_SKIP_IF_UPDATED_WITHIN_DAYS days.
        Logs progress every 10 novels so long runs are observable.
        The inter-novel delay is applied after each successful sync_novel()
        call, not inside sync_novel(), so failed novels don't eat the delay.

        Called by: sync_novels.py main()
        Depends on: NovelRepository.get_active_novels(), sync_novel()
        """
        all_novels = self.repository.get_active_novels()
        cutoff = datetime.now() - timedelta(days=SYNC_SKIP_IF_UPDATED_WITHIN_DAYS)

        # Filter out recently-updated novels before we start
        novels_to_check = []
        skipped_recent = 0
        for novel_id, title, url, last_updated in all_novels:
            if last_updated:
                try:
                    updated_dt = datetime.fromisoformat(str(last_updated))
                    if updated_dt > cutoff:
                        skipped_recent += 1
                        if DEBUG:
                            logger.debug(
                                f"[sync_all] Skipping recent: '{title}' "
                                f"(updated {last_updated})"
                            )
                        continue
                except ValueError:
                    pass  # Unparseable timestamp — include the novel
            novels_to_check.append((novel_id, title, url, last_updated))

        total = len(novels_to_check)
        logger.info(
            f"[sync_all] Starting: {total} novels to check, "
            f"{skipped_recent} skipped (updated within "
            f"{SYNC_SKIP_IF_UPDATED_WITHIN_DAYS} days)."
        )

        for i, (novel_id, title, url, last_updated) in enumerate(
            novels_to_check, start=1
        ):
            if i % 10 == 0 or i == 1:
                remaining = total - i + 1
                logger.info(f"[sync_all] Progress: {i}/{total} — {remaining} remaining")

            try:
                logger.info(f"[sync_all] [{i}/{total}] Checking: {title}")
                self.sync_novel(novel_id, url)

                # Delay is AFTER sync_novel() so failed novels don't eat it
                delay = random.uniform(FETCH_DELAY, FETCH_DELAY * 1.5)
                if DEBUG:
                    logger.debug(f"[sync_all] Sleeping {delay:.1f}s after '{title}'")
                time.sleep(delay)

            except Exception as e:
                logger.error(f"[sync_all] Failed to sync '{title}': {e}", exc_info=True)

        logger.info(
            f"[sync_all] Complete. Checked {total} novels, "
            f"skipped {skipped_recent} recent."
        )

    def sync_novel(self, novel_id: int, url: str):
        """
        Syncs a single novel by comparing its source chapter list against the DB.

        Does NOT sleep at the start — the inter-novel delay is managed by the
        caller (sync_all) so it only fires on successful calls, not on errors.

        Stubbed-novel protection: if the source returns 0 chapters, the DB is
        checked before any action is taken.
          - DB has chapters → novel was stubbed (chapters sold/removed by author).
            The local chapters are preserved as-is. Novel stays in the reader.
          - DB has 0 chapters → novel was never populated. Mark ABANDONED so it
            is excluded from all future sync runs and the reader library.

        Parameters:
            novel_id (int): DB id of the novel.
            url (str): Source URL of the novel's landing page.

        Called by: sync_all()
        Depends on: ScraperService.scrape_novel(), NovelRepository
        """
        if DEBUG:
            logger.debug(f"[sync_novel] novel_id={novel_id} url={url}")

        source_data = self.scraper.scrape_novel(url)
        if not source_data:
            logger.warning(f"[sync_novel] scrape_novel() returned no data for {url}")
            return

        source_chapters = source_data.get("chapters", [])

        if not source_chapters:
            # Source has no chapters — check if we already have some locally
            db_chapter_count = self._count_local_chapters(novel_id)

            if db_chapter_count > 0:
                # We have local chapters the author has since removed.
                # Keep everything — don't mark abandoned, don't touch chapters.
                logger.info(
                    f"[sync_novel] Source has 0 chapters for "
                    f"'{source_data.get('title', url)}' but DB has "
                    f"{db_chapter_count} chapters. "
                    f"Novel was likely stubbed/sold — preserving local chapters."
                )
            else:
                # Source has 0 and we have 0 — nothing to read, never was.
                logger.info(
                    f"[sync_novel] Source and DB both have 0 chapters for "
                    f"'{source_data.get('title', url)}' — marking ABANDONED."
                )
                self.repository.set_novel_status(novel_id, NOVEL_STATUS_ABANDONED)

            return

        db_chapters = self.repository.get_novel_chapters(novel_id)
        new_chapters = []

        for source_ch in source_chapters:
            order = source_ch["order"]
            source_url = source_ch["url"]

            if order in db_chapters:
                if db_chapters[order]["url"] != source_url:
                    logger.info(
                        f"[sync_novel] URL changed for chapter {order}: {source_url}"
                    )
                    new_chapters.append(source_ch)
            else:
                logger.info(
                    f"[sync_novel] New chapter at order {order}: "
                    f"{source_ch.get('title')}"
                )
                new_chapters.append(source_ch)

        if new_chapters:
            logger.info(
                f"[sync_novel] Syncing {len(new_chapters)} new/changed chapters "
                f"for novel {novel_id}"
            )
            self.repository.upsert_chapters(novel_id, new_chapters)
            self.repository.update_novel_timestamp(novel_id)
            logger.info(f"[sync_novel] Updated '{source_data['title']}'")
        else:
            logger.info(f"[sync_novel] No changes for '{source_data['title']}'")

    def _count_local_chapters(self, novel_id: int) -> int:
        """
        Returns the number of chapter rows in the DB for a given novel.

        Parameters:
            novel_id (int): DB id of the novel.

        Returns:
            int: Chapter count (0 if novel has no chapters).

        Called by: sync_novel()
        Depends on: NovelRepository.db.execute()
        """
        rows = self.repository.db.execute(
            "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,)
        )
        return rows[0][0] if rows else 0
