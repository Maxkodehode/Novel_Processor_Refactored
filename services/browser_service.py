# =============================================================================
# CHANGES:
#   - get_page_content(): When keep_page_open=True, the page-level block route
#     handler is now unrouted before returning the page to the caller.
#     Previously the "**/*" block handler remained active on the page permanently,
#     which could interfere with subsequent page.route() calls installed by
#     callers. Unrouting on keep_page_open=True is safe because the caller takes
#     ownership of the page and manages its own routes.
#   - get_page_content(): Added `wait_until` parameter (default "domcontentloaded")
#     so callers can request "load" or "networkidle" without changing behaviour
#     for other sites.
#   - get_page_content(): Under DEBUG mode, logs which URL stealth was applied to
#     and logs every blocked resource type.
#   - All other behaviour unchanged.
# =============================================================================

import logging
import sys

print(f"DEBUG: Python executable: {sys.executable}")
print(f"DEBUG: sys.path: {sys.path}")
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync

    _STEALTH_AVAILABLE = True
except Exception as e:
    _STEALTH_AVAILABLE = False
    print(f"DEBUG: Stealth import failed: {e}")

from core.config import USER_AGENT, TIMEOUT

logger = logging.getLogger(__name__)

DEBUG = False

# Resource types to block during scraping — not needed for HTML content
_BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}


class BrowserService:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None  # Persistent context — reused across all requests

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        """
        Launches the browser and creates a single persistent context.

        The context is reused for all subsequent get_page_content() calls so
        that cookies and session state are preserved across requests, making
        the scraper look like a returning user rather than a new one each time.

        Called by: __enter__(), get_page_content() (auto-start)
        Depends on: playwright, USER_AGENT
        """
        if self._playwright:
            return  # Already started

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        logger.info("Playwright browser and persistent context started.")
        if not _STEALTH_AVAILABLE:
            logger.warning(
                "playwright-stealth not installed — navigator.webdriver will be visible. "
                "Run: pip install playwright-stealth"
            )

    def stop(self):
        """
        Closes the persistent context, browser, and Playwright instance.

        Called by: __exit__(), manual teardown
        Depends on: _context, _browser, _playwright
        """
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        logger.info("Playwright browser stopped.")

    def get_page_content(
        self,
        url: str,
        wait_selector: str = None,
        timeout: int = TIMEOUT,
        block_resources: bool = True,
        keep_page_open: bool = False,
        wait_until: str = "domcontentloaded",
    ) -> tuple[str, object]:
        """
        Fetches a URL using the persistent browser context.

        Applies stealth patches to hide headless browser artifacts. Blocks
        images, media, fonts, and stylesheets by default to reduce bandwidth
        and avoid triggering tracking/ad networks.

        When keep_page_open=True, the block route handler is unrouted before
        the page is returned to the caller. This is essential when the caller
        needs to install their own page.route() handlers (e.g. ScribbleHub
        adapter intercepting admin-ajax.php): Playwright fires route handlers
        in registration order, and the "**/*" block handler's route.continue_()
        would consume every route event before the caller's handler could run.

        Parameters:
            url (str): The URL to navigate to.
            wait_selector (str | None): Optional CSS selector to wait for after load.
            timeout (int): Navigation timeout in seconds.
            block_resources (bool): If True, block images/media/fonts/CSS during
                                    initial page load. Always unrouted before the
                                    page is handed to the caller when keep_page_open
                                    is True.
            keep_page_open (bool): If True, the caller is responsible for closing
                                   the returned page. Used by ScribbleHub adapter
                                   which needs the live page for JS evaluation.
                                   If False, the page is closed before returning
                                   and the returned page object must not be used.
            wait_until (str): Playwright navigation event to wait for before
                              returning. Default is "domcontentloaded" (fast).
                              Pass "load" for pages that require the full JS bundle
                              to execute (e.g. ScribbleHub). Pass "networkidle"
                              for pages with heavy async data loading.

        Returns:
            tuple[str, Page]: (html_content, playwright_page). If keep_page_open
                              is False, the page is already closed — only the html
                              string is usable.

        Called by: ScraperService.scrape_novel(), CoverManager._download_via_browser()
        Depends on: _context, stealth_sync, _BLOCKED_RESOURCES
        """
        if not self._context:
            self.start()

        page = self._context.new_page()

        # Apply stealth patches before any navigation
        if _STEALTH_AVAILABLE:
            stealth_sync(page)
            if DEBUG:
                logger.debug(f"[get_page_content] stealth applied for {url}")

        # Block unnecessary resource types to reduce footprint.
        # Keep a reference to the handler so we can unroute it later.
        _block_handler = None
        if block_resources:

            def _block_handler(route):
                if route.request.resource_type in _BLOCKED_RESOURCES:
                    if DEBUG:
                        logger.debug(
                            f"[get_page_content] blocked {route.request.resource_type}: "
                            f"{route.request.url[:80]}"
                        )
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", _block_handler)

        try:
            if DEBUG:
                logger.debug(
                    f"[get_page_content] navigating to {url} (wait_until={wait_until})"
                )

            page.goto(url, wait_until=wait_until, timeout=timeout * 1000)

            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15_000)
                except Exception:
                    logger.debug(
                        f"[get_page_content] selector '{wait_selector}' wait timed out "
                        f"for {url}, continuing."
                    )

            content = page.content()

            if keep_page_open:
                # Unroute the block handler BEFORE returning the page.
                # The caller will install their own route handlers and the
                # "**/*" glob would otherwise intercept and consume every
                # route event before the caller's handlers could run.
                if _block_handler is not None:
                    try:
                        page.unroute("**/*", _block_handler)
                        if DEBUG:
                            logger.debug(
                                f"[get_page_content] block handler unrouted for {url}"
                            )
                    except Exception as e:
                        logger.debug(
                            f"[get_page_content] unroute failed (non-fatal): {e}"
                        )
                return content, page

            # Close the page now — caller only needs the HTML string
            page.close()
            return content, None

        except Exception as e:
            logger.error(f"[get_page_content] Playwright error for {url}: {e}")
            try:
                page.close()
            except Exception:
                pass
            raise
