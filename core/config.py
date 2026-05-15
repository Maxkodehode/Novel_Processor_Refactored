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
FETCH_DELAY = 8  # Base seconds between chapter fetches
FETCH_DELAY_JITTER = 3  # Max extra seconds added randomly to FETCH_DELAY
TIMEOUT = 30
FETCH_MAX_RETRIES = 2

# Discovery-specific delays (conservative — you hit list pages AND novel pages)
DISCOVERY_PAGE_DELAY_MIN = 6  # Min seconds between discovery list pages
DISCOVERY_PAGE_DELAY_MAX = 12  # Max seconds between discovery list pages
DISCOVERY_NOVEL_DELAY_MIN = 8  # Min seconds between per-novel hydration requests
DISCOVERY_NOVEL_DELAY_MAX = 14  # Max seconds between per-novel hydration requests

# Cover download delay (covers can fire in a tight loop during discovery)
COVER_FETCH_DELAY = 2  # seconds before each cover download attempt

# Files
COVERS_DIR = "covers"

# DB Config
COMMIT_BATCH_SIZE = 10
