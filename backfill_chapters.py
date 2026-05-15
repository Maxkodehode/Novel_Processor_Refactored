import logging
import sys

# Import the components directly from the core package
from core import DatabaseManager, NovelRepository, NetworkClient
from services import BrowserService, CoverManager, ScraperService

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def run_backfill():
    try:
        # 1. Initialize core infrastructure
        # DatabaseManager handles DB_PATH internally via default arguments
        db_manager = DatabaseManager()
        repo = NovelRepository(db_manager)
        network = NetworkClient()
        browser = BrowserService()
        cover_manager = CoverManager(network, repo)

        # 2. Initialize service
        scraper = ScraperService(network, browser, repo, cover_manager)

        logger.info("Starting backfill for missing chapter content...")

        # 3. Execution
        # This will query the DB for all chapters that have NO content
        # and process them one-by-one with the delay from config.py
        scraper.fetch_chapters(novel_id=None)

        logger.info("Backfill process complete.")

    except Exception as e:
        logger.error(f"Critical error during backfill: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run_backfill()
