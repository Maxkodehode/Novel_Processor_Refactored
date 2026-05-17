# Novel Processor

A comprehensive, modular, service-oriented novel scraper, manager, and reader system. Designed to fetch metadata, covers, and chapter content from various web novel platforms and provide a seamless offline reading experience.

## Features

- **Modular Architecture**: Built with a Service-Based Architecture for easy maintenance and scalability.
- **Multiple Adapters**: Built-in support for:
  - [Royal Road](https://www.royalroad.com/) — fast `curl_cffi` fetch, no browser needed
  - [Scribble Hub](https://www.scribblehub.com/) — chapter lists fetched via direct AJAX POST to `admin-ajax.php` (no Playwright needed). Falls back to static HTML parsing if AJAX fails. Chapter URLs are refreshed before fetch to handle ScribbleHub slug changes.
  - [FanFiction.net](https://www.fanfiction.net/) — uses Playwright with stealth to bypass Cloudflare bot protection. Fast `curl_cffi` fetch is attempted first as a fallback.
- **Mass Discovery Pipeline**: Crawl site-wide ranking lists and automatically hydrate your library with novel metadata and full chapter lists.
- **Cross-Platform Deduplication**: Two-tier deduplication — exact URL matching and intelligent fuzzy title matching (95% similarity) — to avoid inserting the same novel twice across platforms.
- **Stubbed Novel Detection**: During sync, if a novel's source page returns zero chapters but the local database has chapters, the novel is recognised as stubbed/sold and local content is preserved. If neither the source nor the database has chapters, the novel is marked `ABANDONED` and excluded from all future processing.
- **Advanced Sync Service**: Cron-ready script to keep your library up-to-date with the latest chapters. Skips novels updated within the last 7 days to reduce unnecessary requests.
- **Database Maintenance Tools**: Standalone scripts to backfill missing chapter URLs, download missing chapter content, and audit and repair cover images across your entire library.
- **Browser-Based Reader**: A fully offline, high-performance web reader with:
  - Infinite scroll — chapters load and unload automatically as you scroll, keeping memory usage bounded.
  - Multiple themes (Light, Sepia, Dark, AMOLED) and fully customisable typography.
  - Bookmarks, per-chapter reading progress, and personal notes.
  - Tri-state tag filtering (Include / Exclude / Neutral) and sorting by title, last updated, chapter count, or word count.
  - On-demand chapter fetching and updating directly from the UI, with a live progress bar.
- **Robust Infrastructure**:
  - Fast fetch using `curl_cffi` with browser impersonation and forced `gzip/deflate` encoding to avoid curl error 61 on CDNs that serve Brotli or Zstd.
  - Playwright fallback with stealth patches (`playwright-stealth`) for JS-heavy sites and CDN hotlink protection. Only used when the fast path fails or when a site requires a real browser (e.g. FanFiction.net).
  - Persistent Playwright browser context — cookies and session state are reused across requests so the scraper looks like a returning user, not a new one each time.
  - Jittered, rate-limited request delays throughout (configurable per pipeline stage) to avoid being blocked.
  - Repository Pattern for all SQLite access.
  - Structured per-run fetch logging with automatic log rotation (keeps last 10 runs).

## Project Structure

```text
project_root/
│
├── core/                          # Shared infrastructure
│   ├── config.py                  # All delays, paths, and tuning constants
│   ├── database.py                # SQLite Repository and Database Manager
│   ├── network.py                 # curl_cffi client (GET + POST) with impersonation
│   └── run_logger.py              # Structured per-run fetch logging with rotation
│
├── adapters/                      # Site-specific parsing logic
│   ├── base.py                    # Abstract base adapter (needs_browser() classmethod)
│   ├── royalroad.py               # Royal Road metadata + chapter parser
│   ├── scribblehub.py             # ScribbleHub parser (direct AJAX for chapters)
│   ├── fanfiction.py              # FanFiction.net parser (Playwright for Cloudflare)
│   ├── discovery_base.py          # Abstract base discovery adapter
│   └── discovery_adapters.py      # List page parsers for mass discovery
│
├── services/                      # Business logic orchestration
│   ├── browser_service.py         # Playwright lifecycle with stealth + persistent context
│   ├── cover_manager.py           # Cover image download (network + browser fallback)
│   ├── scraper_service.py         # High-level scraping, DB population, chapter fetching
│   ├── discovery_service.py       # Mass discovery orchestration
│   └── novel_update_service.py    # Sync and stubbed-novel detection logic
│
├── reader/                        # Offline Reader Application (FastAPI + Vanilla JS)
│   ├── server.py                  # REST API backend (FastAPI)
│   ├── run.py                     # Launcher (opens browser automatically)
│   └── static/                    # Frontend (index.html, app.js, style.css)
│
├── utils/                         # General utility functions (slugify, etc.)
├── main.py                        # Single (or multi) novel scraper entry point
├── sync_novels.py                 # Library sync entry point (cron-friendly)
├── backfill_chapter_urls.py       # Fix novels missing chapter titles and URLs
├── backfill_chapters.py           # Download missing chapter content library-wide
├── backfill_covers.py             # Audit and repair missing or invalid cover images
└── init_db.py                     # Database schema initialisation and migrations
```

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Maxkodehode/Novel_Processor_Refactored.git
   cd Novel_Processor_Refactored
   ```

2. **Set up a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

4. **Initialise the database**:
   ```bash
   python init_db.py
   python reader/migrate_reader.py
   ```

## Usage

### 1. Single Novel Scraping

Scrape a specific novel by URL:

```bash
# Full pipeline: scrape metadata + chapter list + download all chapter content
python main.py --url https://www.royalroad.com/fiction/12345/novel-title

# Metadata and chapter list only (skip downloading chapter text)
python main.py --url https://www.royalroad.com/fiction/12345/novel-title --no-fetch

# Save a debug copy of the raw HTML and parsed JSON for inspection
python main.py --url https://www.royalroad.com/fiction/12345/novel-title --debug

# Use a locally saved HTML file instead of fetching (dev/offline mode)
python main.py --url https://www.royalroad.com/fiction/12345/novel-title --use-local page.html
```

### 2. Multiple Novels (Batch)

Queue several novels in a single run. Each novel is processed sequentially — scrape → populate DB → fetch chapters — before moving to the next. If one novel fails, it is skipped and processing continues with the rest.

```bash
# Scrape multiple novels at once
python main.py --urls \
  https://www.fanfiction.net/s/13509722/1/Some-FFN-Story \
  https://www.royalroad.com/fiction/21220/mother-of-learning \
  https://www.scribblehub.com/series/123456/some-novel/

# Metadata only for all novels (no chapter content download)
python main.py --urls URL1 URL2 URL3 --no-fetch
```

### 3. Mass Discovery

Hydrate your database with top-rated novels from supported platforms:

```bash
# Discover top 100 novels from Royal Road (20 novels per page)
python -m services.discovery_service --site royalroad --start 1 --end 5

# Discover top 400 novels from ScribbleHub
python -m services.discovery_service --site scribblehub --start 1 --end 20
```

Discovery saves the novel title, author, synopsis, cover, tags, and the full list of chapter titles and URLs. It does **not** download chapter text — that is a separate step. Rate limiting is applied automatically between every list page and between every individual novel hydration request.

### 4. Synchronising Updates

Run this to check for new chapters across your library. Ideal for cron jobs.

```bash
# Check all novels for new chapters (skips novels updated within 7 days)
python sync_novels.py

# Check for new chapters and immediately download their content
python sync_novels.py --fetch-content
```

Novels whose source page returns zero chapters are handled automatically:
- **Source has 0, DB has chapters** → novel was likely sold or stubbed by the author. Local chapters are preserved untouched.
- **Source has 0, DB also has 0** → novel was never populated. Marked `ABANDONED` and excluded from all future syncs.

### 5. Database Maintenance

**Fix novels missing chapter titles and URLs** (e.g. novels discovered before the chapter-URL bug was fixed):

```bash
# Preview which novels would be fixed, without making any changes
python backfill_chapter_urls.py --dry-run

# Fix all novels that have no chapter rows
python backfill_chapter_urls.py

# Fix a single novel by its database ID
python backfill_chapter_urls.py --id 42

# Mark novels as ABANDONED when the source returns 0 chapters and DB also has 0
python backfill_chapter_urls.py --abandon
```

**Download missing chapter content** for chapters that have a URL but no text yet:

```bash
python backfill_chapters.py
```

**Audit and repair cover images** across your entire library:

```bash
# Preview all novels with missing or invalid covers (no downloads performed)
python backfill_covers.py --dry-run

# Fix all novels whose cover is missing, the file is gone, or the file is under 1 KB
python backfill_covers.py

# Fix a single novel by its database ID
python backfill_covers.py --id 42

# Re-scrape landing pages to get a fresh cover URL for novels where the stored URL is stale or missing
python backfill_covers.py --re-scrape

# Treat files under 2 KB as invalid instead of the default 1 KB
python backfill_covers.py --min-size 2048

# Override the inter-novel delay (default is 5–10 seconds)
python backfill_covers.py --delay-min 4 --delay-max 8
```

A cover is considered invalid if `cover_path` is NULL, the file no longer exists on disk, or the file is smaller than the minimum size threshold (default 1 KB — sub-kilobyte files are placeholder responses from the CDN, not real images). The script uses the same two-tier download strategy as the main scraper: fast `curl_cffi` network fetch first, Playwright browser fallback if that fails.

All maintenance scripts are safe to re-run and will not create duplicate entries.

### 6. Reading Offline

Start the web-based reader:

```bash
python reader/run.py
```

This launches a local server at `http://localhost:8765` and opens your default browser automatically. The reader works fully offline once chapters have been downloaded.

**Reader features:**
- **Infinite scroll** — scroll continuously through a novel without clicking next. Chapters outside a ±2 chapter window around your current position are automatically unloaded to keep memory usage bounded, even for very long novels.
- **Reading progress** — your position within each chapter is saved automatically and restored when you return.
- **Themes** — Light, Sepia, Dark, and AMOLED. Background and text colours are also fully customisable with a colour picker.
- **Typography** — font family, font size, line height, paragraph spacing, and column width are all adjustable and persisted across sessions.
- **Bookmarks and notes** — bookmark any chapter and attach per-chapter notes that save automatically as you type.
- **Tag filtering** — click a tag once to include it (green), again to exclude it (red), again to clear. Multiple tags can be combined.
- **Keyboard shortcuts** (in reader view):

  | Key | Action |
  |-----|--------|
  | `→` or `l` | Next chapter |
  | `←` or `h` | Previous chapter |
  | `b` | Toggle bookmark |
  | `n` | Toggle notes panel |
  | `s` | Toggle settings panel |
  | `f` | Toggle fullscreen |
  | `Ctrl+K` | Open search |
  | `Esc` | Close open panels |

## Configuration

All settings are in `core/config.py`:

| Setting | Default | Description |
|---|---|---|
| `DB_PATH` | `novels.db` | SQLite database file path |
| `USER_AGENT` | Chrome 122 on Linux | User-Agent string sent with all requests |
| `FETCH_DELAY` | `8s` | Base delay between chapter content downloads |
| `FETCH_DELAY_JITTER` | `3s` | Max extra seconds added randomly to each chapter delay |
| `FETCH_MAX_RETRIES` | `2` | Retry attempts per chapter before marking it failed |
| `TIMEOUT` | `30s` | Network request timeout |
| `DISCOVERY_PAGE_DELAY_MIN/MAX` | `6–12s` | Jittered delay between discovery list pages |
| `DISCOVERY_NOVEL_DELAY_MIN/MAX` | `8–14s` | Jittered delay between per-novel hydration requests |
| `COVER_FETCH_DELAY` | `2s` | Delay applied inside CoverManager before each image download |
| `COVERS_DIR` | `covers/` | Directory where cover images are saved |

## Known Behaviours

**Royal Road covers and curl error 61** — Royal Road's CDN can respond with Brotli or Zstd content encoding, which some builds of libcurl cannot decode. The network client now forces `Accept-Encoding: gzip, deflate` on all requests to prevent this. If a cover download still fails via the fast fetch path, it automatically falls back to Playwright, which handles encoding transparently.

**ScribbleHub chapter loading** — ScribbleHub renders chapter lists via AJAX. The adapter uses a direct POST to `admin-ajax.php` with `pagenum=-1` to fetch all chapters in a single request, without Playwright. If the AJAX call fails, the adapter falls back to whatever chapters are present in the static HTML (typically the first 15). Before fetching chapter content, the scraper refreshes chapter URLs for ScribbleHub novels to handle slug changes — this only runs for the specific novel being fetched (not the entire library) unless running in global backfill mode.

**FanFiction.net Cloudflare bypass** — FanFiction.net uses Cloudflare bot protection. The adapter uses Playwright with `playwright-stealth` to bypass this. The fast `curl_cffi` fetch path is attempted first; if it returns a Cloudflare challenge page, the Playwright fallback takes over automatically. The `needs_browser()` mechanism ensures FFN always gets browser treatment while other sites use the fast path.

**FFN chapter dropdown** — FFN's `<select>` dropdown on the story landing page sometimes omits the most recently added chapter. The parser now cross-references the `chapter_count` from the stats line and fills in any missing chapters at the end, so the last chapter is never silently dropped.

**Persistent browser context** — The Playwright browser is started once per pipeline run and its context (cookies, session state) is reused across all requests in that run. This makes the scraper look like a returning user rather than spawning a fresh fingerprint for every page, which reduces the chance of bot detection.

**ABANDONED novels** — novels marked `ABANDONED` are excluded from the reader library, all sync runs, and all backfill scripts. They remain in the database for deduplication purposes so the same novel is never re-inserted under a slightly different URL or title. To un-abandon a novel, update its `status` column directly in the database.

**Database maintenance** — The SQLite database can grow over time due to deleted/updated rows leaving free pages. Running `VACUUM` periodically reclaims this space:
```bash
sqlite3 novels.db "VACUUM;"
```
This is safe to run and will not delete any data. Stop the hermes-host service first if it is running.

## License

[MIT](LICENSE)
