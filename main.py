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

    # Queue multiple novels at once
    python main.py --urls URL1 URL2 URL3 [--no-fetch]

    # Re-scrape even if URL already exists
    python main.py --url https://www.royalroad.com/fiction/12345/some-novel --force
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
    parser.add_argument("--url", default=None, help="Novel landing page URL")
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
        "--force",
        action="store_true",
        help="Re-scrape even if the URL is already in the database",
    )
    parser.add_argument(
        "--use-local",
        metavar="FILE",
        default=None,
        help="Load HTML from a local file instead of fetching (dev mode)",
    )
    parser.add_argument(
        "--urls",
        nargs="+",
        metavar="URL",
        default=None,
        help="One or more novel landing page URLs to scrape in sequence",
    )
    args = parser.parse_args()

    # Normalize: single --url or multiple --urls
    urls = args.urls if args.urls else ([args.url] if args.url else [])
    if not urls:
        parser.error("Provide --url URL or --urls URL [URL ...]")

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

    # ── Process each URL ─────────────────────────────────────────────────────
    total = len(urls)
    for i, url in enumerate(urls, 1):
        logger.info(f"{'=' * 60}")
        logger.info(f"[{i}/{total}] Processing: {url}")
        logger.info(f"{'=' * 60}")

        # ── Step 3: Scrape the novel landing page ────────────────────────────
        save_html = "page.html" if args.debug else None

        with browser_service:
            data = scraper.scrape_novel(
                url=url, use_local=args.use_local, save_html=save_html
            )

            if not data or not data.get("title"):
                logger.error(f"Scrape returned no usable data for {url}. Skipping.")
                continue

            logger.info(
                f"Scraped: '{data['title']}' — {len(data.get('chapters', []))} chapters found"
            )

            if args.debug:
                debug_file = f"output_{i}.json"
                with open(debug_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info(f"Debug JSON saved to {debug_file}")

            # ── Step 4: Check for duplicates ────────────────────────────────────
            if not args.force and repository.is_url_known(url):
                logger.warning(f"URL already in database: {url} — skipping. Use --force to re-scrape.")
                continue

            # ── Step 5: Populate DB ────────────────────────────────────────────
            logger.info("Inserting metadata into database...")
            novel_id = scraper.populate_novel(data)

            if novel_id is None:
                logger.error(f"DB populate failed for {url} — skipping.")
                continue

            # ── Step 6: Fetch chapter content ──────────────────────────────────
            if args.no_fetch:
                logger.info("--no-fetch set: skipping chapter content fetching.")
            else:
                logger.info("Starting chapter content fetch...")
                scraper.fetch_chapters(novel_id=novel_id)

        logger.info(f"[{i}/{total}] Complete: {data.get('title', url)}")

    logger.info(f"Pipeline complete — {total} novel(s) processed.")


if __name__ == "__main__":
    main()
