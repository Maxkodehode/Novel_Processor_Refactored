"""
sync_novels.py — CLI tool for syncing novel updates (ideal for cron jobs)

Usage:
    python sync_novels.py
"""

import logging
import argparse
from core import DatabaseManager, NovelRepository, NetworkClient
from services import BrowserService, CoverManager, ScraperService, NovelUpdateService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Sync novel updates")
    parser.add_argument(
        "--fetch-content",
        action="store_true",
        help="Automatically fetch content for new chapters",
    )
    args = parser.parse_args()

    # 1. Initialize Services
    db_manager = DatabaseManager()
    repository = NovelRepository(db_manager)
    network_client = NetworkClient()
    browser_service = BrowserService()
    cover_manager = CoverManager(network_client, repository)
    scraper_service = ScraperService(
        network_client, browser_service, repository, cover_manager
    )

    update_service = NovelUpdateService(network_client, repository, scraper_service)

    # 2. Run Sync
    logger.info("Starting novel sync process...")
    update_service.sync_all()

    # 3. Optional: Fetch content for all pending chapters
    if args.fetch_content:
        logger.info("Fetching content for new chapters...")
        scraper_service.fetch_chapters()

    logger.info("Sync process complete.")


if __name__ == "__main__":
    main()
