# =============================================================================
# CHANGES:
#   - parse(): Added WARNING log when window.chapters JSON is malformed or
#     fails to parse. Previously the json.JSONDecodeError was silently caught
#     and swallowed — the fallback to HTML table rows fired but nothing in the
#     logs indicated the primary chapter source had failed.
#   - Added module-level logger — royalroad.py previously had no logger.
#   - Added DEBUG log when the HTML table row fallback is used so it is clear
#     which source produced the chapter list.
#   - All parsing logic unchanged.
# =============================================================================

import re
import json
import logging

from bs4 import BeautifulSoup

from .base import BaseAdapter
from utils.text import slugify

logger = logging.getLogger(__name__)

DEBUG = False


class RoyalRoadAdapter(BaseAdapter):
    HOSTS = ["royalroad.com"]

    def parse(self, soup: BeautifulSoup, url: str) -> dict:
        """
        Parses a Royal Road fiction page into a structured data dict.

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the fiction landing page.
            url (str): Canonical URL of the fiction.

        Returns:
            dict: Novel data including title, author, cover_url, chapters, etc.

        Called by: ScraperService.scrape_novel()
        Depends on: slugify(), json, re
        """
        # --- Title & author ---
        title = self._text(soup.select_one("h1.font-white"))
        author_tag = soup.select_one("h4 a.font-white") or soup.select_one(
            "a[href^='/profile/']"
        )
        author = self._text(author_tag)

        # --- Cover ---
        cover = soup.select_one("img[data-type='cover']")
        cover_url = cover["src"] if cover else None

        # --- Tags ---
        tags = [self._text(a) for a in soup.select("a.fiction-tag")]

        # --- Status ---
        status = None
        for span in soup.select("span.label.label-default"):
            t = self._text(span).upper()
            if t in ("COMPLETED", "ONGOING", "HIATUS", "STUB"):
                status = t
                break

        # --- Synopsis ---
        syn_div = soup.select_one("div.description div.hidden-content")
        synopsis = syn_div.get_text(separator="\n", strip=True) if syn_div else None

        # --- Scores ---
        scores = {}
        for label in (
            "Overall Score",
            "Style Score",
            "Story Score",
            "Grammar Score",
            "Character Score",
        ):
            span = soup.find("span", attrs={"data-original-title": label})
            if span:
                m = re.search(r"([\d.]+)\s*/\s*5", span.get("data-content", ""))
                if m:
                    key = label.replace(" Score", "").lower()
                    scores[key] = float(m.group(1))
        meta = soup.find("meta", {"property": "books:rating:value"})
        if meta:
            scores["overall_meta"] = float(meta["content"])

        # --- Stats ---
        stats = {}
        stats_div = soup.select_one("div.fiction-stats")
        if stats_div:
            cols = stats_div.select("div.col-sm-6")
            stat_col = cols[1] if len(cols) > 1 else stats_div
            lis = stat_col.select("li.bold.uppercase")
            for i in range(0, len(lis) - 1, 2):
                label = lis[i].get_text(strip=True).rstrip(" :")
                value = lis[i + 1].get_text(strip=True)
                if label and value:
                    key = label.lower().replace(" ", "_")
                    stats[key] = value

            icon = stats_div.select_one("i.popovers[data-content]")
            if icon:
                m = re.search(r"from\s+([\d,]+)\s+words", icon.get("data-content", ""))
                if m:
                    stats["word_count"] = m.group(1)

        # --- Chapter count ---
        count_span = soup.select_one("span.label.label-default.pull-right")
        chapter_count = None
        if count_span:
            m = re.search(r"(\d+)\s+Chapters?", count_span.get_text())
            if m:
                chapter_count = int(m.group(1))

        # --- Chapter list from embedded JSON ---
        chapters = []
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r"window\.chapters\s*=\s*(\[.*?\]);", text, re.DOTALL)
            if m:
                try:
                    raw = json.loads(m.group(1))
                    for entry in raw:
                        chapters.append(
                            {
                                "id": entry.get("id"),
                                "order": entry.get("order", 0),
                                "title": entry.get("title", ""),
                                "url": self._abs(entry.get("url", ""), url),
                                "published": entry.get("date"),
                            }
                        )
                    if DEBUG:
                        logger.debug(
                            f"[parse] Loaded {len(chapters)} chapters from "
                            f"window.chapters JSON"
                        )
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[parse] window.chapters JSON parse failed for {url}: {e}. "
                        f"Falling back to HTML table row extraction."
                    )
                break

        # Fallback: visible table rows
        if not chapters:
            if DEBUG:
                logger.debug(
                    f"[parse] Using HTML table row fallback for chapter list: {url}"
                )
            for i, row in enumerate(soup.select("tr.chapter-row")):
                link = row.select_one("td a[href]")
                time_tag = row.select_one("time")
                if link:
                    chapters.append(
                        {
                            "id": None,
                            "order": i,
                            "title": self._text(link),
                            "url": self._abs(link["href"], url),
                            "published": time_tag["datetime"] if time_tag else None,
                        }
                    )

        return {
            "site": "royalroad",
            "url": url,
            "title": title,
            "slug": slugify(title) if title else None,
            "author": author,
            "cover_url": cover_url,
            "status": status,
            "tags": tags,
            "synopsis": synopsis,
            "language": "en",
            "scores": scores,
            "stats": stats,
            "chapter_count": chapter_count or len(chapters),
            "chapters": chapters,
        }

    def parse_chapter_content(self, soup: BeautifulSoup) -> dict:
        """
        Extracts plain text and raw HTML from a Royal Road chapter page.

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the chapter page.

        Returns:
            dict: {'plain_text': str, 'raw_html': str}

        Called by: ScraperService.fetch_chapters()
        Depends on: BeautifulSoup selector '.chapter-inner'
        """
        content_tag = soup.select_one(".chapter-inner")
        return {
            "plain_text": content_tag.get_text(separator="\n", strip=True)
            if content_tag
            else "",
            "raw_html": str(content_tag) if content_tag else "",
        }
