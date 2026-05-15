# =============================================================================
# ScribbleHub adapter — direct AJAX approach.
#
# Strategy:
#   1. Parse static HTML for metadata + post ID + chapter count.
#   2. POST to admin-ajax.php with action=wi_getreleases_pagination,
#      pagenum=-1 to get ALL chapters in one request.
#   3. Fall back to static HTML chapters only if AJAX fails.
#
# This replaces the old 3-step fallback chain (toc_fic_show_all via
# Playwright, AJAX pagination via Playwright, chapter-by-chapter via
# curl_cffi) which was fragile and Cloudflare-sensitive.
#
# The AJAX endpoint does NOT require login or cookies — it works with
# a plain POST from curl_cffi with browser impersonation headers.
# =============================================================================

import logging
import re
import time

from bs4 import BeautifulSoup

from .base import BaseAdapter
from utils.text import slugify

logger = logging.getLogger(__name__)

DEBUG = False

# ScribbleHub AJAX endpoint for chapter listing
_AJAX_URL = "https://www.scribblehub.com/wp-admin/admin-ajax.php"

# Seconds between page requests (used by fallback)
_PAGE_DELAY = 2.0


class ScribbleHubAdapter(BaseAdapter):
    HOSTS = ["scribblehub.com"]

    # Injected by ScraperService before parse() — kept for backward compat
    # but no longer needed for the primary AJAX path.
    _pw_page = None

    @staticmethod
    def _extract_post_id(soup: BeautifulSoup, url: str) -> str | None:
        """
        Extracts the ScribbleHub post ID from the page HTML or URL.

        The post ID is used as the 'mypostid' parameter in AJAX calls.
        It is found in:
          1. <input id="mypostid" value="..."> in the HTML
          2. The URL path: /series/2229102/...

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the novel landing page.
            url (str): The novel's canonical URL.

        Returns:
            str | None: The post ID string, or None if not found.
        """
        # Method 1: hidden input element
        input_el = soup.select_one("#mypostid")
        if input_el and input_el.get("value"):
            return input_el["value"]

        # Method 2: URL path
        m = re.search(r"/series/(\d+)/", url)
        if m:
            return m.group(1)

        return None

    def _fetch_all_chapters_via_ajax(
        self, network_client, post_id: str
    ) -> list[dict] | None:
        """
        Fetches ALL chapters in one request via ScribbleHub's AJAX endpoint.

        Calls admin-ajax.php with action=wi_getreleases_pagination and
        pagenum=-1, which returns the complete chapter list as HTML.
        The response is a <div class="wi_fic_table main"> containing
        <li class="toc_w"> elements — the same format as the static page.

        This does NOT require login, cookies, or JavaScript execution.
        curl_cffi with browser impersonation is sufficient.

        Parameters:
            network_client: NetworkClient instance (curl_cffi-based).
            post_id (str): The ScribbleHub post/series ID.

        Returns:
            list[dict] | None: Chapter dicts (order, title, url, published),
                               or None on failure.
        """
        logger.info(f"[SH] Fetching all chapters via AJAX (post_id={post_id})")

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.scribblehub.com/series/{post_id}/",
        }

        data = {
            "action": "wi_getreleases_pagination",
            "pagenum": "-1",
            "mypostid": post_id,
        }

        try:
            response = network_client.post(
                _AJAX_URL, data=data, headers=headers, timeout=30
            )
        except Exception as e:
            logger.warning(f"[SH] AJAX request failed: {e}")
            return None

        html = response.text
        if not html or html.strip() in ("0", "-1", ""):
            logger.warning("[SH] AJAX returned empty/error response")
            return None

        # The JS frontend does t.slice(0, -1) to trim trailing whitespace
        html = html.rstrip()

        soup = BeautifulSoup(html, "html.parser")
        chapters = self._extract_from_soup(soup)

        if not chapters:
            logger.warning("[SH] AJAX response parsed but no chapters found")
            return None

        logger.info(f"[SH] AJAX returned {len(chapters)} chapters")
        return chapters

    def _extract_from_soup(self, soup) -> list[dict]:
        """
        Extracts chapter dicts from a BeautifulSoup object containing li.toc_w
        elements. Works on both full page soup and AJAX fragment soup.

        Parameters:
            soup (BeautifulSoup): Soup to extract from.

        Returns:
            list[dict]: Chapter dicts with keys: order, title, url, published.
        """
        chapters = []
        for li in soup.select("li.toc_w"):
            link = li.select_one("a")
            time_tag = li.select_one("span.fic_date_pub")
            if not link:
                continue

            order_attr = li.get("order")
            order_val = int(order_attr) if order_attr and order_attr.isdigit() else None
            published = None
            if time_tag:
                published = time_tag.get("title") or time_tag.get_text(strip=True)

            ch = {
                "order": order_val,
                "title": link.get_text(strip=True),
                "url": link.get("href", ""),
                "published": published,
            }
            chapters.append(ch)

            if DEBUG:
                logger.debug(
                    f"[_extract_from_soup] order={order_val} title='{ch['title']}'"
                )

        return chapters

    def parse(
        self, soup: BeautifulSoup, url: str, network_client=None
    ) -> dict:
        """
        Parses a ScribbleHub novel landing page into a structured data dict.

        Chapter list strategy:
          1. POST to admin-ajax.php with pagenum=-1 to get ALL chapters
             at once via curl_cffi (fast, reliable, no login needed).
          2. Fall back to whatever chapters are in the static HTML
             (page 1 only, typically 15 chapters).

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the novel landing page.
            url (str): The novel's canonical URL.
            network_client: NetworkClient instance (curl_cffi-based).
                           Injected by ScraperService.

        Returns:
            dict: Novel data including title, author, tags, chapters, etc.
        """
        # --- Basic metadata ---
        title = self._text(soup.select_one("div.fic_title"))
        author = self._text(soup.select_one("span.auth_name_fic"))
        cover = soup.select_one("div.fic_image img")
        cover_url = cover["src"] if cover else None

        tags = [self._text(a) for a in soup.select("a.fic_genre")]
        tags += [self._text(a) for a in soup.select("a.stag")]
        tags = [t for t in tags if t]

        status = None
        status_tag = soup.select_one(
            "span.ss-completed, span.ss-ongoing, span.ss-hiatus"
        )
        if status_tag:
            status = status_tag.get_text(strip=True).upper()

        syn = soup.select_one("div.wi_fic_desc")
        synopsis = syn.get_text(separator="\n", strip=True) if syn else None

        stats = {}
        for item in soup.select("div.widget_fic_similar li"):
            spans = item.select("span")
            if len(spans) >= 2:
                k = spans[0].get_text(strip=True).lower().replace(" ", "_").rstrip(":")
                v = spans[1].get_text(strip=True)
                stats[k] = v

        scores = {}
        rating_tag = soup.select_one("span#ratig-count")
        if rating_tag:
            try:
                scores["overall"] = float(rating_tag.get_text(strip=True))
            except ValueError:
                pass

        # --- Chapter count from badge ---
        chapter_count = None
        cnt_tag = soup.select_one("span.cnt_toc")
        if cnt_tag:
            try:
                chapter_count = int(cnt_tag.get_text(strip=True).replace(",", ""))
            except ValueError:
                pass

        # --- Extract post ID for AJAX call ---
        post_id = self._extract_post_id(soup, url)
        if DEBUG:
            logger.debug(f"[parse] post_id={post_id} chapter_count={chapter_count}")

        # --- Get chapters ---
        chapters_by_order: dict[int, dict] = {}

        # Step 1: Try AJAX for all chapters (fast, single request)
        if post_id and network_client:
            ajax_chapters = self._fetch_all_chapters_via_ajax(network_client, post_id)
            if ajax_chapters:
                for ch in ajax_chapters:
                    if ch["order"] is not None:
                        # Convert 1-based HTML order to 0-based
                        zero_based = ch["order"] - 1
                        ch["order"] = zero_based
                        chapters_by_order[zero_based] = ch

        # Step 2: Fall back to static HTML chapters
        if not chapters_by_order:
            logger.info("[SH] Falling back to static HTML chapters")
            for ch in self._extract_from_soup(soup):
                if ch["order"] is not None:
                    zero_based = ch["order"] - 1
                    ch["order"] = zero_based
                    chapters_by_order[zero_based] = ch

        # --- Re-index to 0-based order, sorted ascending ---
        chapters_sorted = sorted(chapters_by_order.values(), key=lambda c: c["order"])
        for i, ch in enumerate(chapters_sorted):
            ch["order"] = i

        logger.info(f"[parse] Final chapter count: {len(chapters_sorted)}")

        if chapter_count and len(chapters_sorted) < chapter_count:
            logger.warning(
                f"[parse] Expected {chapter_count} chapters, "
                f"got {len(chapters_sorted)}."
            )

        return {
            "site": "scribblehub",
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
            "chapter_count": chapter_count or len(chapters_sorted),
            "chapters": chapters_sorted,
        }

    def parse_chapter_content(self, soup: BeautifulSoup) -> dict:
        """
        Extracts plain text and raw HTML from a ScribbleHub chapter page.

        Parameters:
            soup (BeautifulSoup): Parsed HTML of the chapter page.

        Returns:
            dict: {'plain_text': str, 'raw_html': str}
        """
        content_tag = soup.select_one("#chp_raw")
        return {
            "plain_text": content_tag.get_text(separator="\n", strip=True)
            if content_tag
            else "",
            "raw_html": str(content_tag) if content_tag else "",
        }
