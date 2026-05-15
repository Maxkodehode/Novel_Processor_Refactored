# =============================================================================
# CHANGES:
#   - download_and_save(): Expanded the FanFiction.net Referer guard to also
#     match CDN subdomains used by FFN: "ffnet", "ff.net", and "ffn.io".
#     Previously only "fanfiction.net" was checked, so covers served from
#     ffnet.b-cdn.net, img.ffn.io, etc. never received the required Referer
#     header, causing 403s or 1x1 pixel placeholder responses.
#   - download_and_save(): When a sub-1KB response is received, now logs the
#     actual Content-Type and final response URL (after any redirects) so it
#     is clear whether the server returned a placeholder image or an HTML
#     error page.
#   - _download_via_browser(): Added a specific actionable log message when
#     the exception text indicates Playwright/Chromium is not installed
#     ("Executable doesn't exist" or "playwright install"), telling the user
#     exactly what command to run.
#   - All other logic unchanged.
# =============================================================================

import os
import logging
import time

from core.config import COVERS_DIR, USER_AGENT, COVER_FETCH_DELAY
from core.network import NetworkClient
from core.database import NovelRepository

logger = logging.getLogger(__name__)

DEBUG = False

# CDN domains used by FanFiction.net — all require the same Referer header
_FFN_CDN_DOMAINS = ("fanfiction.net", "ffnet", "ff.net", "ffn.io")


def _is_ffn_url(url: str) -> bool:
    """
    Returns True if the URL belongs to FanFiction.net or any of its CDN domains.

    Parameters:
        url (str): The URL to check.

    Returns:
        bool

    Called by: CoverManager.download_and_save()
    Depends on: _FFN_CDN_DOMAINS
    """
    lower = url.lower()
    return any(domain in lower for domain in _FFN_CDN_DOMAINS)


class CoverManager:
    def __init__(self, network_client: NetworkClient, repository: NovelRepository):
        self.network = network_client
        self.repository = repository
        os.makedirs(COVERS_DIR, exist_ok=True)

    def download_and_save(self, cover_url: str, novel_id: int, slug: str) -> str | None:
        """
        Downloads a cover image and saves it to disk, then records the path in the DB.

        Uses a two-tier strategy:
          Tier 1: Fast network fetch via curl_cffi (NetworkClient).
          Tier 2: Playwright browser fallback if network fetch fails for any reason.

        Skips generic placeholder images and handles relative URLs for Royal Road
        and FanFiction.net. Injects the correct Referer header for FFN CDN domains
        (fanfiction.net, ffnet, ff.net, ffn.io) which enforce hotlink protection.

        Parameters:
            cover_url (str): URL of the cover image to download.
            novel_id (int): DB id of the novel (used for file naming and DB update).
            slug (str): URL-safe novel slug (used for file naming).

        Returns:
            str | None: Local file path if saved successfully, None otherwise.

        Called by: ScraperService.populate_novel(), backfill_covers.py fix_cover()
        Depends on: NetworkClient.get(), _download_via_browser(),
                    NovelRepository.update_cover_path(), _is_ffn_url()
        """
        # Guard: cover_url must be a non-empty string
        if not cover_url:
            logger.warning(
                f"[download_and_save] Called with empty cover_url for novel {novel_id}"
            )
            return None

        # 1. Resolve relative URLs (Royal Road / FanFiction.net)
        if cover_url.startswith("/"):
            if "royalroad" in cover_url or "royalroad" in slug:
                cover_url = f"https://www.royalroad.com{cover_url}"
            elif "fanfiction" in cover_url or "fanfiction" in slug:
                cover_url = f"https://www.fanfiction.net{cover_url}"

        # 2. Skip generic placeholder images
        placeholders = ["d_60_90.jpg", "nocover-new-min.png", "default-cover"]
        if any(p in cover_url.lower() for p in placeholders):
            logger.info(
                f"[download_and_save] Skipping generic placeholder for novel "
                f"{novel_id}: {cover_url}"
            )
            return None

        # 3. Remove stale cover file to prevent orphaned files on disk
        try:
            old_path_row = self.repository.db.execute(
                "SELECT cover_path FROM novels WHERE id = ?", (novel_id,)
            )
            if old_path_row:
                old_path = old_path_row[0][0] if old_path_row[0] else None
                if old_path and os.path.exists(old_path):
                    os.remove(old_path)
                    if DEBUG:
                        logger.debug(
                            f"[download_and_save] Removed stale cover: {old_path}"
                        )
        except Exception as e:
            logger.warning(
                f"[download_and_save] Could not remove old cover for novel "
                f"{novel_id}: {e}"
            )

        # 4. Determine save path from URL extension
        ext = os.path.splitext(cover_url.split("?")[0])[-1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".jpg"

        filename = f"{slug}_{novel_id}{ext}"
        save_path = os.path.join(COVERS_DIR, filename)

        # 5. Polite delay before fetch (rate limiting for CDN requests)
        if COVER_FETCH_DELAY > 0:
            time.sleep(COVER_FETCH_DELAY)

        # 6. Tier 1: Fast network fetch
        # Inject Referer for sites that enforce hotlink protection.
        # FFN CDN uses multiple subdomains — check all known variants.
        headers = {"User-Agent": USER_AGENT}
        if _is_ffn_url(cover_url):
            headers["Referer"] = "https://www.fanfiction.net/"
            if DEBUG:
                logger.debug(
                    f"[download_and_save] Injecting FFN Referer for: {cover_url}"
                )
        if "royalroad" in cover_url.lower():
            headers["Referer"] = "https://www.royalroad.com/"

        try:
            if DEBUG:
                logger.debug(f"[download_and_save] Tier 1 fetch: {cover_url}")

            response = self.network.get(cover_url, headers=headers)

            if len(response.content) < 1024:
                # Log content type and final URL to distinguish placeholder
                # images from HTML error pages in the logs.
                content_type = response.headers.get("Content-Type", "unknown")
                final_url = getattr(response, "url", cover_url)
                raise ValueError(
                    f"Response too small ({len(response.content)} bytes) — "
                    f"Content-Type: {content_type}, final URL: {final_url}"
                )

            # Reconcile extension with actual Content-Type
            content_type = response.headers.get("Content-Type", "")
            if "image/webp" in content_type and not save_path.endswith(".webp"):
                save_path = save_path.rsplit(".", 1)[0] + ".webp"
            elif "image/png" in content_type and not save_path.endswith(".png"):
                save_path = save_path.rsplit(".", 1)[0] + ".png"

            with open(save_path, "wb") as f:
                f.write(response.content)

            logger.info(
                f"[download_and_save] Cover saved (Network): {save_path} "
                f"({len(response.content)} bytes)"
            )
            self.repository.update_cover_path(novel_id, save_path)
            return save_path

        except Exception as e:
            # Log the real error (e.g. curl error 61, 403, too-small response)
            # before falling through to the browser fallback.
            logger.warning(
                f"[download_and_save] Network fetch failed for novel {novel_id} "
                f"({cover_url}): {e}. Trying browser fallback..."
            )
            return self._download_via_browser(cover_url, novel_id, save_path)

    def _download_via_browser(
        self, cover_url: str, novel_id: int, save_path: str
    ) -> str | None:
        """
        Downloads a cover image via a headless Playwright browser.

        Used as a fallback when the fast network fetch fails (e.g. due to
        Brotli/Zstd encoding errors, CAPTCHA-guarded CDNs, or hotlink protection
        that checks for a real browser User-Agent).

        Opens a fresh BrowserService context so this method is safe to call
        without a running browser. Navigates once and reads the raw response
        body from Playwright's network interception — no double-navigation.

        Parameters:
            cover_url (str): URL of the cover image.
            novel_id (int): DB id of the novel.
            save_path (str): Full local path where the image should be saved.

        Returns:
            str | None: save_path if saved successfully, None otherwise.

        Called by: download_and_save()
        Depends on: BrowserService, NovelRepository.update_cover_path()
        """
        from services.browser_service import BrowserService

        if DEBUG:
            logger.debug(
                f"[_download_via_browser] Attempting browser fetch: {cover_url}"
            )

        try:
            # Use a fresh context — block_resources=False so image bytes are
            # not intercepted and aborted by the resource blocker.
            with BrowserService(headless=True) as browser:
                browser.start()
                page = browser._context.new_page()

                try:
                    # Single navigation — read body from this response object
                    response = page.goto(
                        cover_url,
                        timeout=30_000,
                        wait_until="networkidle",
                    )

                    if response is None:
                        logger.warning(
                            f"[_download_via_browser] Browser navigation returned "
                            f"None for {cover_url} (novel {novel_id})"
                        )
                        return None

                    buffer = response.body()

                    if buffer and len(buffer) > 1024:
                        with open(save_path, "wb") as f:
                            f.write(buffer)
                        logger.info(
                            f"[_download_via_browser] Cover saved (Browser): "
                            f"{save_path} ({len(buffer)} bytes)"
                        )
                        self.repository.update_cover_path(novel_id, save_path)
                        return save_path
                    else:
                        logger.warning(
                            f"[_download_via_browser] Browser response body too "
                            f"small for novel {novel_id} "
                            f"({len(buffer) if buffer else 0} bytes)"
                        )
                        return None

                finally:
                    page.close()

        except Exception as e:
            error_str = str(e)
            # Provide an actionable message if Playwright/Chromium is not installed
            if (
                "Executable doesn't exist" in error_str
                or "playwright install" in error_str
            ):
                logger.error(
                    "[_download_via_browser] Chromium not found. "
                    "Run: playwright install chromium"
                )
            else:
                logger.error(
                    f"[_download_via_browser] Browser fallback failed for novel "
                    f"{novel_id} ({cover_url}): {e}"
                )
            return None
