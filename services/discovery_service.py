# =============================================================================
# CHANGES:
#   - discover(): Replaced hardcoded random.uniform(2, 5) page delay with
#     DISCOVERY_PAGE_DELAY_MIN/MAX from config (now 6-12s). 2-5s was too
#     aggressive for repeatedly hitting a site's ranking pages.
#   - discover(): Added a per-novel delay of DISCOVERY_NOVEL_DELAY_MIN/MAX
#     (8-14s) between each novel hydration call inside the page loop. Previously
#     there was NO delay between hydrations — if a page had 20 novels, 20
#     scrape requests fired back-to-back instantly.
#   - discover(): Fixed populate_novel() call — was passing metadata_only=True
#     which caused chapter titles and URLs (already scraped, no extra requests)
#     to be discarded. Changed to metadata_only=False so chapter list is saved.
#     content_status is still set to 'metadata' separately to correctly reflect
#     that chapter *content* has not been downloaded yet.
# =============================================================================

import re
import time
import random
import logging
import argparse
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, utils

from core.database import DatabaseManager, NovelRepository
from core.network import NetworkClient
from core.config import (
    DISCOVERY_PAGE_DELAY_MIN,
    DISCOVERY_PAGE_DELAY_MAX,
    DISCOVERY_NOVEL_DELAY_MIN,
    DISCOVERY_NOVEL_DELAY_MAX,
)
from services.browser_service import BrowserService
from services.cover_manager import CoverManager
from services.scraper_service import ScraperService
from adapters.discovery_adapters import (
    RoyalRoadDiscoveryAdapter,
    ScribbleHubDiscoveryAdapter,
)
from utils.text import slugify

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

DEBUG = False


class DiscoveryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        network_client: NetworkClient,
        browser_service: BrowserService,
        scraper_service: ScraperService,
    ):
        self.db = db_manager
        self.repo = NovelRepository(db_manager)
        self.network = network_client
        self.browser = browser_service
        self.scraper = scraper_service
        self.adapters = {
            "royalroad": RoyalRoadDiscoveryAdapter(),
            "scribblehub": ScribbleHubDiscoveryAdapter(),
        }

    def _normalize_title(self, title: str) -> str:
        """
        Strips bracket tags and normalises a title for fuzzy comparison.

        Parameters:
            title (str): Raw title string.

        Returns:
            str: Lowercased, stripped, bracket-free title.

        Called by: discover()
        Depends on: rapidfuzz.utils.default_process
        """
        title = re.sub(r"[\[\(].*?[\]\)]", "", title)
        return utils.default_process(title)

    def discover(self, site: str, start_page: int, end_page: int):
        """
        Crawls paginated ranking lists on a supported site and hydrates new
        novels into the database.

        Deduplication is done in two tiers:
          1. Exact URL match (fast DB lookup)
          2. Fuzzy title match at 95% similarity (in-memory)

        Rate limiting:
          - DISCOVERY_PAGE_DELAY_MIN/MAX seconds between list pages.
          - DISCOVERY_NOVEL_DELAY_MIN/MAX seconds between per-novel hydrations.

        Parameters:
            site (str): Site key, e.g. 'royalroad' or 'scribblehub'.
            start_page (int): First list page to crawl (inclusive).
            end_page (int): Last list page to crawl (inclusive).

        Returns:
            None

        Called by: __main__ block, external callers
        Depends on: discovery adapters, ScraperService, NovelRepository, rapidfuzz
        """
        adapter = self.adapters.get(site)
        if not adapter:
            logger.error(f"No discovery adapter for site: {site}")
            return

        total_new = 0
        total_exact_skipped = 0
        total_fuzzy_merged = 0
        total_errors = 0

        existing_novels = self.repo.get_all_novels_for_fuzzy()
        processed_existing = [
            (nid, self._normalize_title(title)) for nid, title in existing_novels
        ]

        for page in range(start_page, end_page + 1):
            logger.info(f"Processing {site} page {page}...")
            url = adapter.get_list_url(page)

            html = None
            try:
                response = self.network.get(url)
                if response.status_code == 200:
                    html = response.text
                else:
                    logger.warning(
                        f"Fast fetch failed with status {response.status_code}. Trying browser..."
                    )
            except Exception as e:
                logger.warning(f"Fast fetch failed: {e}. Trying browser...")

            if not html:
                try:
                    html, _ = self.browser.get_page_content(url)
                except Exception as e:
                    logger.error(f"Browser fetch failed for {url}: {e}")
                    total_errors += 1
                    continue

            if not html:
                logger.error(f"Failed to fetch {url}")
                total_errors += 1
                continue

            soup = BeautifulSoup(html, "html.parser")
            found_novels = adapter.parse_list_page(soup)

            page_new = 0
            page_exact_skipped = 0
            page_fuzzy_merged = 0

            for novel in found_novels:
                title = novel["title"]
                source_url = novel["url"]

                # Tier 1: Exact URL match — no request needed, just skip
                if self.repo.is_url_known(source_url):
                    page_exact_skipped += 1
                    continue

                # Tier 2: Fuzzy title match — also no request needed
                norm_title = self._normalize_title(title)
                match_found = False
                for nid, ex_norm in processed_existing:
                    if fuzz.ratio(norm_title, ex_norm) >= 95.0:
                        logger.info(
                            f"Fuzzy match: '{title}' → existing novel ID {nid}. Adding source."
                        )
                        self.repo.add_novel_source(nid, site, source_url)
                        page_fuzzy_merged += 1
                        match_found = True
                        break

                if match_found:
                    continue

                # New novel — insert + hydrate metadata
                try:
                    novel_id = self.repo.insert_discovered_novel(
                        title, source_url, slugify(title)
                    )
                    logger.info(f"Inserted: '{title}' (ID: {novel_id})")

                    # --- FIX: Delay BEFORE the hydration request ---
                    # Without this, all novels on a page fire requests back-to-back.
                    novel_delay = random.uniform(
                        DISCOVERY_NOVEL_DELAY_MIN, DISCOVERY_NOVEL_DELAY_MAX
                    )
                    logger.info(
                        f"Waiting {novel_delay:.1f}s before hydrating '{title}'..."
                    )
                    if DEBUG:
                        logger.debug(
                            f"[discover] novel_delay={novel_delay:.1f}s for url={source_url}"
                        )
                    time.sleep(novel_delay)

                    logger.info(f"Hydrating metadata for '{title}'...")
                    scrape_data = self.scraper.scrape_novel(source_url)
                    if scrape_data:
                        # metadata_only=False so chapter titles+URLs are saved.
                        # They come free from the novel page scrape — no extra
                        # requests needed. content_status is then set to 'metadata'
                        # to correctly signal that chapter *content* is not yet downloaded.
                        populated_id = self.scraper.populate_novel(
                            scrape_data, metadata_only=False
                        )
                        if populated_id:
                            self.repo.update_content_status(populated_id, "metadata")
                        logger.info(
                            f"Metadata hydrated for '{title}' — "
                            f"{len(scrape_data.get('chapters', []))} chapters indexed."
                        )
                    else:
                        logger.warning(f"Failed to hydrate metadata for '{title}'.")

                    processed_existing.append((novel_id, norm_title))
                    page_new += 1

                except Exception as e:
                    logger.error(f"Error inserting novel '{title}': {e}")
                    total_errors += 1

            total_new += page_new
            total_exact_skipped += page_exact_skipped
            total_fuzzy_merged += page_fuzzy_merged

            logger.info(
                f"Page {page} Summary: {page_new} new, "
                f"{page_exact_skipped} exact skipped, "
                f"{page_fuzzy_merged} fuzzy merged."
            )

            # --- FIX: Use config constants instead of hardcoded 2-5s ---
            if page < end_page:
                page_delay = random.uniform(
                    DISCOVERY_PAGE_DELAY_MIN, DISCOVERY_PAGE_DELAY_MAX
                )
                logger.info(f"Waiting {page_delay:.1f}s before next page...")
                time.sleep(page_delay)

        logger.info("Discovery Run Final Summary:")
        logger.info(f"New novels inserted: {total_new}")
        logger.info(f"Exact duplicates skipped: {total_exact_skipped}")
        logger.info(f"Fuzzy/cross-platform merges: {total_fuzzy_merged}")
        logger.info(f"Errors: {total_errors}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Mass Discovery Pipeline for Novel_Processor"
    )
    parser.add_argument(
        "--site",
        choices=["royalroad", "scribblehub"],
        required=True,
        help="Site to discover from",
    )
    parser.add_argument("--start", type=int, default=1, help="Start page")
    parser.add_argument("--end", type=int, default=50, help="End page")

    args = parser.parse_args()

    db_manager = DatabaseManager()
    network = NetworkClient()
    browser = BrowserService()
    repo = NovelRepository(db_manager)
    cover = CoverManager(network, repo)
    scraper = ScraperService(network, browser, repo, cover)

    service = DiscoveryService(db_manager, network, browser, scraper)

    try:
        service.discover(args.site, args.start, args.end)
    finally:
        browser.stop()
