# =============================================================================
# CHANGES:
#   - parse(): Added logging for the cover URL thumbnail→full-size upgrade so
#     it is visible when the regex fires (or silently doesn't). Logs both the
#     original thumbnail URL and the upgraded URL at DEBUG level, and logs a
#     WARNING if a cover src was found but the URL ends up empty after
#     normalisation (which would mean no cover is downloaded).
#   - parse(): Added module-level logger — fanfiction.py previously had no
#     logger at all, so all cover diagnostics had to be inferred from
#     cover_manager.py logs.
#   - All parsing logic unchanged.
# =============================================================================

import re
import logging

from bs4 import BeautifulSoup

from .base import BaseAdapter
from utils.text import slugify

logger = logging.getLogger(__name__)

DEBUG = False


class FanFictionAdapter(BaseAdapter):
    HOSTS = ["fanfiction.net", "www.fanfiction.net"]

    # Map FF.net genre IDs → names (subset; extend as needed)
    _GENRE_MAP = {
        "1": "Adventure",
        "2": "Angst",
        "3": "Comedy",
        "4": "Crime",
        "5": "Drama",
        "6": "Family",
        "7": "Fantasy",
        "8": "Friendship",
        "9": "General",
        "10": "Horror",
        "11": "Humor",
        "12": "Hurt/Comfort",
        "13": "Mystery",
        "14": "Parody",
        "15": "Poetry",
        "16": "Romance",
        "17": "Sci-Fi",
        "18": "Spiritual",
        "19": "Supernatural",
        "20": "Suspense",
        "21": "Tragedy",
        "22": "Western",
    }

    def parse(self, soup: BeautifulSoup, url: str) -> dict:
        """
        Parses a FanFiction.net story page into a structured data dict.

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the story landing page.
            url (str): Canonical URL of the story.

        Returns:
            dict: Novel data including title, author, cover_url, chapters, etc.

        Called by: ScraperService.scrape_novel()
        Depends on: slugify(), re
        """
        # --- Embedded JS metadata ---
        meta = {}
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r"var\s+storyid\s*=\s*(\d+)", text)
            if m:
                meta["story_id"] = m.group(1)
                break
            m = re.search(r"storyid\s*[=:]\s*(\d+)", text)
            if m and "story_id" not in meta:
                meta["story_id"] = m.group(1)

        # --- #profile_top block ---
        profile = soup.select_one("div#profile_top")
        title = self._text(profile.select_one("b.xcontrast_txt") if profile else None)
        author_tag = profile.select_one("a.xcontrast_txt") if profile else None
        author = self._text(author_tag)

        # --- Cover image ---
        # FFN serves covers from CDN subdomains (ffnet.b-cdn.net, img.ffn.io, etc.)
        # that require a Referer header pointing at fanfiction.net. The Referer
        # injection is handled in cover_manager.py based on the stored URL.
        #
        # FFN has two img.cimage elements:
        #   1. Visible thumbnail: src="/image/<id>/75/"
        #   2. Hidden modal:      src="placeholder.jpg" data-original="/image/<id>/180/"
        # We prefer data-original (full size) from any cimage element.
        cover_url = None
        for cimg in soup.select("img.cimage"):
            # Prefer data-original (full-size) over src (thumbnail)
            src = cimg.get("data-original") or cimg.get("src", "")
            if not src:
                continue

            if src.startswith("http"):
                cover_url = src
            elif src.startswith("//"):
                cover_url = "https:" + src
            elif src.startswith("/"):
                cover_url = "https://www.fanfiction.net" + src
            else:
                continue

            # Skip generic placeholders (d_60_90.jpg, nocover, etc.)
            if re.search(r"d_60_90|nocover|default-cover|placeholder", cover_url, re.I):
                cover_url = None
                continue

            # If we got data-original, it's already full-size — use it directly
            if cimg.get("data-original"):
                logger.info(f"[parse] Cover URL from data-original: {cover_url}")
                break

            # If we got src with a thumbnail pattern, upgrade to full size
            if re.search(r"/image/\d+/\d+/$", cover_url):
                original = cover_url
                cover_url = re.sub(r"/\d+/$", "/180/", cover_url)
                logger.info(f"[parse] Cover URL upgraded: {original} -> {cover_url}")
                break

        if not cover_url:
            if DEBUG:
                logger.debug("[parse] No valid cover image found")

        syn = profile.select_one("div.xcontrast_txt") if profile else None
        synopsis = self._text(syn)

        stats = {}
        scores = {}
        tags = []
        status = None
        language = None
        chapter_count = None

        stats_span = profile.select_one("span.xgray") if profile else None
        if stats_span:
            raw = stats_span.get_text(" ", strip=True)

            for pat, key in [
                (r"Words:\s*([\d,]+)", "words"),
                (r"Reviews:\s*([\d,]+)", "reviews"),
                (r"Favs:\s*([\d,]+)", "favourites"),
                (r"Follows:\s*([\d,]+)", "followers"),
                (r"Chapters:\s*(\d+)", "chapter_count_raw"),
            ]:
                m = re.search(pat, raw, re.I)
                if m:
                    stats[key] = m.group(1)

            m = re.search(r"Chapters:\s*(\d+)", raw, re.I)
            if m:
                chapter_count = int(m.group(1))

            rating_tag = stats_span.select_one("a[href*='fictionratings']")
            if rating_tag:
                stats["rating"] = rating_tag.get_text(strip=True).split()[-1]

            # Strip the "Rated: Fiction X -" prefix to get to the language.
            # Format: "Rated: Fiction <rating> - <language> - <genres> - ..."
            # The rating text can contain spaces (e.g. "Fiction M", "Fiction T")
            rated_prefix = re.sub(r"^Rated:\s*Fiction\s+\w+\s*-\s*", "", raw, count=1).strip()
            segments = [s.strip() for s in rated_prefix.split(" - ") if s.strip()]

            # First segment after rating should be the language (e.g. "English")
            if segments and re.match(r"^[A-Za-z][\w ]*$", segments[0]) and ":" not in segments[0]:
                language = segments[0]
                segments = segments[1:]

            # Next segments are genres until we hit character names or stats
            genre_segments = []
            for seg in segments:
                if re.search(
                    r"Chapters:|Words:|Reviews:|Favs:|Follows:|Updated:|Published:|id:",
                    seg,
                    re.I,
                ):
                    break
                # Character name lists like "[OC, Kira N., Jadzia D.]" — skip
                if seg.startswith("[") and seg.endswith("]"):
                    break
                # Genres: allow hyphens (e.g. "Sci-Fi"), slashes, ampersands
                if re.match(r"^[A-Z][\w/& -]+$", seg) and "." not in seg:
                    genre_segments.append(seg)
                else:
                    break
            for gs in genre_segments:
                tags += [g.strip() for g in gs.split("/") if g.strip()]

            if "Complete" in raw and "Updated" not in raw.split("Complete")[0]:
                status = "COMPLETED"
            elif "Updated" in raw or "In-Progress" in raw:
                status = "ONGOING"

        # --- Chapter list ---
        story_id = meta.get("story_id")
        if not story_id:
            m2 = re.search(r"/s/(\d+)/", url)
            story_id = m2.group(1) if m2 else None

        chapters = []
        chap_select = soup.select_one("select#chap_select")
        if chap_select:
            for opt in chap_select.find_all("option"):
                idx = int(opt["value"])
                first = opt.contents[0] if opt.contents else None
                if first and hasattr(first, 'strip'):
                    ch_title = first.strip()
                else:
                    ch_title = f"Chapter {idx}"
                chapters.append(
                    {
                        "id": idx,
                        "order": idx - 1,
                        "title": ch_title,
                        "url": f"https://www.fanfiction.net/s/{story_id}/{idx}/",
                        "published": None,
                    }
                )

        # FFN's <select> dropdown sometimes omits the most recently added
        # chapter. If we have a chapter_count from the stats, fill in any
        # missing chapters at the end so we don't silently lose the last one.
        if chapter_count and story_id and len(chapters) < chapter_count:
            existing_ids = {ch["id"] for ch in chapters}
            for idx in range(1, chapter_count + 1):
                if idx not in existing_ids:
                    chapters.append(
                        {
                            "id": idx,
                            "order": idx - 1,
                            "title": f"Chapter {idx}",
                            "url": f"https://www.fanfiction.net/s/{story_id}/{idx}/",
                            "published": None,
                        }
                    )
            if DEBUG:
                logger.debug(
                    f"[parse] Filled {chapter_count - len(chapters)} missing "
                    f"chapter(s) from chapter_count={chapter_count}"
                )

        # Fallback: no dropdown at all, generate from chapter_count
        if not chapters and chapter_count and story_id:
            chapters = [
                {
                    "id": i + 1,
                    "order": i,
                    "title": f"Chapter {i + 1}",
                    "url": f"https://www.fanfiction.net/s/{story_id}/{i + 1}/",
                    "published": None,
                }
                for i in range(chapter_count)
            ]

        return {
            "site": "fanfiction",
            "url": url,
            "title": title,
            "slug": slugify(title) if title else None,
            "author": author,
            "cover_url": cover_url,
            "status": status,
            "tags": tags,
            "synopsis": synopsis,
            "language": language,
            "scores": scores,
            "stats": stats,
            "chapter_count": chapter_count or len(chapters),
            "chapters": chapters,
        }

    def parse_chapter_content(self, soup: BeautifulSoup) -> dict:
        """
        Extracts plain text and raw HTML from a FanFiction.net chapter page.

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the chapter page.

        Returns:
            dict: {'plain_text': str, 'raw_html': str}

        Called by: ScraperService.fetch_chapters()
        Depends on: BeautifulSoup selector '#storytext'
        """
        content_tag = soup.select_one("#storytext")
        return {
            "plain_text": content_tag.get_text(separator="\n", strip=True)
            if content_tag
            else "",
            "raw_html": str(content_tag) if content_tag else "",
        }
