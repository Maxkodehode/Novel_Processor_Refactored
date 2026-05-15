"""
main.py — Novel scraper pipeline orchestrator

Usage:
    # Full pipeline (scrape → populate DB → fetch chapter content)
    python main.py --url https://www.royalroad.com/fiction/12345/some-novel

    # Scrape and populate only (skip chapter content fetching)
    python main.py --url https://www.royalroad.com/fiction/12345/some-novel --no-fetch

    # Save a debug copy of the raw HTML and parsed JSON
    python main.py --url https://www.royalroad.com/fiction/12345/some-novel --debug

    # Use a locally saved HTML file instead of fetching (dev mode)
    python main.py --url https://www.royalroad.com/fiction/12345/some-novel --use-local page.html
"""

import argparse
import json
import logging
import sys

from core import DatabaseManager, NovelRepository, NetworkClient
from services import BrowserService, CoverManager, ScraperService
from init_db import create_pure_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Novel scraper pipeline")
    parser.add_argument("--url", required=True, help="Novel landing page URL")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip chapter content fetching after metadata insert",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw HTML (page.html) and parsed JSON (output.json) for inspection",
    )
    parser.add_argument(
        "--use-local",
        metavar="FILE",
        default=None,
        help="Load HTML from a local file instead of fetching (dev mode)",
    )
    args = parser.parse_args()

    # ── Step 1: Ensure DB schema exists ──────────────────────────────────────
    logger.info("Initialising database schema...")
    create_pure_schema()

    # ── Step 2: Initialize Services ──────────────────────────────────────────
    db_manager = DatabaseManager()
    repository = NovelRepository(db_manager)
    network_client = NetworkClient()
    browser_service = BrowserService()
    cover_manager = CoverManager(network_client, repository)

    scraper = ScraperService(network_client, browser_service, repository, cover_manager)

    # ── Step 3: Scrape the novel landing page ────────────────────────────────
    logger.info(f"Scraping: {args.url}")
    save_html = "page.html" if args.debug else None

    with browser_service:
        data = scraper.scrape_novel(
            url=args.url, use_local=args.use_local, save_html=save_html
        )

        if not data or not data.get("title"):
            logger.error("Scrape returned no usable data. Check the URL or adapter.")
            sys.exit(1)

        logger.info(
            f"Scraped: '{data['title']}' — {len(data.get('chapters', []))} chapters found"
        )

        if args.debug:
            with open("output.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Debug JSON saved to output.json")

        # ── Step 4: Populate DB ──────────────────────────────────────────────────
        logger.info("Inserting metadata into database...")
        novel_id = scraper.populate_novel(data)

        if novel_id is None:
            logger.error("DB populate failed — aborting.")
            sys.exit(1)

        # ── Step 5: Fetch chapter content ────────────────────────────────────────
        if args.no_fetch:
            logger.info("--no-fetch set: skipping chapter content fetching.")
        else:
            logger.info("Starting chapter content fetch...")
            scraper.fetch_chapters()

    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()
