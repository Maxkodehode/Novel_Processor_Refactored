# =============================================================================
# CHANGES:
#   - Added DISCOVERY_PAGE_DELAY_MIN/MAX to replace hardcoded 2-5s in
#     discovery_service.py. Raised to 6-12s to be more conservative.
#   - Added DISCOVERY_NOVEL_DELAY_MIN/MAX for the per-novel hydration delay
#     in discovery_service.py (was 0s — completely missing).
#   - Added FETCH_DELAY_JITTER so fetch_chapters() adds randomness to its
#     fixed 8s sleep, making request patterns less fingerprint-able.
#   - Added COVER_FETCH_DELAY for a small pause before cover image downloads.
# =============================================================================

import os

# Database
# Resolve project root from the config file's location (works regardless of cwd or install path)
PROJECT_ROOT = os.environ.get(
    "NOVEL_PROCESSOR_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DB_PATH = os.path.join(PROJECT_ROOT, "novels.db")

# Network
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Chapter fetch delays — optimized for speed while staying within safe limits.
# Research shows Cloudflare-protected sites (FFN, RR, SH) tolerate ~1 req/s
# sustained. We target ~0.25 req/s average (3-5s per chapter) to stay well
# within limits while cutting total backfill time from weeks to days.
#
# For 172k chapters: 3-5s avg = ~6-10 days total (vs 16-22 days at 8-11s).
FETCH_DELAY = 3          # Base seconds between chapter fetches
FETCH_DELAY_JITTER = 2   # Max extra seconds (range: 3-5s, avg ~4s)
FETCH_MAX_RETRIES = 1    # One retry is enough; if it fails twice, move on
TIMEOUT = 30

# Per-site delay overrides (seconds added to base delay).
# These are applied as additional jitter ranges per site.
SITE_FETCH_EXTRA = {
    "fanfiction.net": 2.0,   # FFN is most aggressive — add 0-2s extra
    "royalroad.com": 1.0,    # RR is moderate — add 0-1s extra
    "scribblehub.com": 0.0,  # SH is most relaxed — no extra delay
}

# Discovery-specific delays (conservative — you hit list pages AND novel pages)
DISCOVERY_PAGE_DELAY_MIN = 4   # Min seconds between discovery list pages
DISCOVERY_PAGE_DELAY_MAX = 8   # Max seconds between discovery list pages
DISCOVERY_NOVEL_DELAY_MIN = 5  # Min seconds between per-novel hydration requests
DISCOVERY_NOVEL_DELAY_MAX = 10 # Max seconds between per-novel hydration requests

# Cover download delay (covers can fire in a tight loop during discovery)
COVER_FETCH_DELAY = 1  # seconds before each cover download attempt

# Files
COVERS_DIR = "covers"

# DB Config
COMMIT_BATCH_SIZE = 10
