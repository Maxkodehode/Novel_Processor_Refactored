# =============================================================================
# CHANGES:
#   - get(): Fixed crash when headers=None (the default). Previously the code
#     did headers["Accept-Encoding"] = ... before checking if headers existed,
#     causing TypeError: 'NoneType' object does not support item assignment on
#     every call that didn't explicitly pass headers (e.g. fetch_chapters()).
#     Fix: initialise headers to {} if None before any mutation.
#   - get(): Accept-Encoding is now set to "gzip, deflate" on all requests so
#     Royal Road (and other sites) cannot respond with Brotli/Zstd which
#     libcurl may not support, avoiding curl error 61.
#   - get(): FFN Referer injection now also guarded against headers being None
#     (redundant after the fix above, but kept explicit for clarity).
#   - post(): New method — same browser impersonation and compression handling
#     as get(), used by ScribbleHubAdapter for direct AJAX chapter fetching.
# =============================================================================

import logging
from curl_cffi import requests as cur_requests
from .config import TIMEOUT

logger = logging.getLogger(__name__)


class NetworkClient:
    def __init__(self, impersonate="chrome"):
        self.impersonate = impersonate

    def get(self, url: str, timeout: int = TIMEOUT, headers: dict | None = None):
        """
        Performs a GET request using curl_cffi with browser impersonation.

        Always sets Accept-Encoding to gzip/deflate to prevent servers from
        responding with Brotli or Zstd compression that libcurl may not support
        (avoids curl error 61 on Royal Road and similar CDNs).

        Parameters:
            url (str): The URL to fetch.
            timeout (int): Request timeout in seconds.
            headers (dict | None): Optional extra headers. Safe to pass None.

        Returns:
            Response: curl_cffi response object.

        Raises:
            Exception: Re-raises any network or HTTP error after logging.

        Called by: CoverManager.download_and_save(), ScraperService.fetch_chapters(),
                   DiscoveryService.discover()
        Depends on: curl_cffi, TIMEOUT
        """
        # Guard: always work with a real dict, never mutate None
        if headers is None:
            headers = {}

        # Force compatible compression — prevents curl error 61 (Brotli/Zstd)
        headers["Accept-Encoding"] = "gzip, deflate"

        # Inject Referer for FanFiction.net to avoid hotlink blocking
        if "fanfiction.net" in url.lower() and "Referer" not in headers:
            headers["Referer"] = "https://www.fanfiction.net/"

        try:
            response = cur_requests.get(
                url,
                impersonate=self.impersonate,
                timeout=timeout,
                headers=headers,
            )
            response.raise_for_status()
            return response
        except Exception as e:
            logger.error(f"Network error getting {url}: {e}")
            raise

    def post(self, url: str, data: dict, timeout: int = TIMEOUT, headers: dict | None = None):
        """
        Performs a POST request using curl_cffi with browser impersonation.

        Parameters:
            url (str): The URL to post to.
            data (dict): Form data to send.
            timeout (int): Request timeout in seconds.
            headers (dict | None): Optional extra headers. Safe to pass None.

        Returns:
            Response: curl_cffi response object.

        Raises:
            Exception: Re-raises any network or HTTP error after logging.

        Called by: ScribbleHubAdapter._fetch_all_chapters_via_ajax()
        Depends on: curl_cffi, TIMEOUT
        """
        if headers is None:
            headers = {}

        headers["Accept-Encoding"] = "gzip, deflate"

        try:
            response = cur_requests.post(
                url,
                data=data,
                impersonate=self.impersonate,
                timeout=timeout,
                headers=headers,
            )
            response.raise_for_status()
            return response
        except Exception as e:
            logger.error(f"Network error posting to {url}: {e}")
            raise
