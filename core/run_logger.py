# =============================================================================
# CHANGES:
#   - _rotate_logs(): Moved from __exit__() to __enter__() so log rotation
#     happens at the START of each run rather than the end. Previously, if the
#     process was killed mid-run (Ctrl+C, OOM, crash), __exit__() was never
#     called, rotation never fired, and old log files accumulated indefinitely.
#     Now rotation always happens before a new log file is opened regardless
#     of how the previous run ended.
#   - __exit__(): Still writes the summary line and closes the file cleanly,
#     but no longer calls _rotate_logs().
#   - All other logic unchanged.
# =============================================================================

import os
import time
from datetime import datetime
from core.config import DB_PATH


class RunLogger:
    def __init__(self, total_pending):
        self.total_pending = total_pending
        self.ok_count = 0
        self.failed_count = 0
        self.start_time = time.time()

        # Derive project root from DB_PATH
        db_abs = os.path.abspath(DB_PATH)
        project_root = os.path.dirname(db_abs)
        self.logs_dir = os.path.join(project_root, "logs")

        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = f"fetch_{timestamp}.log"
        self.filepath = os.path.join(self.logs_dir, self.filename)
        self.file = None

    def __enter__(self):
        # Rotate BEFORE opening the new log so a killed run's partial log
        # is preserved and old logs are cleaned up regardless of exit path.
        self._rotate_logs()
        self.file = open(self.filepath, "w", encoding="utf-8")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.file.write(f"[START] {timestamp} — {self.total_pending} chapters queued\n")
        self.file.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.file:
            total_time = time.time() - self.start_time
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.file.write(f"[END] {timestamp}\n")
            self.file.write(
                f"Total: {self.total_pending} | OK: {self.ok_count} | "
                f"Failed: {self.failed_count} | Elapsed: {total_time:.1f}s\n"
            )
            self.file.close()
            # Rotation already happened in __enter__ — do not call it again here

    def ok(self, ch_id, title, word_count, elapsed):
        """
        Records a successful chapter fetch.

        Parameters:
            ch_id (int): DB id of the chapter.
            title (str): Chapter title.
            word_count (int): Number of words in the fetched content.
            elapsed (float): Seconds taken for this chapter.

        Called by: ScraperService.fetch_chapters()
        """
        self.ok_count += 1
        self.file.write(
            f'[OK]    ch_id={ch_id} "{title}" ({word_count} words) +{elapsed:.1f}s\n'
        )
        self.file.flush()

    def retry(self, ch_id, title, attempt, error):
        """
        Records a retry attempt for a chapter fetch.

        Parameters:
            ch_id (int): DB id of the chapter.
            title (str): Chapter title.
            attempt (int): Attempt number (1-based).
            error (str): Error string from the failed attempt.

        Called by: ScraperService.fetch_chapters()
        """
        self.file.write(
            f'[RETRY] ch_id={ch_id} "{title}" attempt {attempt} — {error}\n'
        )
        self.file.flush()

    def fail(self, ch_id, title, error):
        """
        Records a permanently failed chapter fetch.

        Parameters:
            ch_id (int): DB id of the chapter.
            title (str): Chapter title.
            error (str): Error string from the final failed attempt.

        Called by: ScraperService.fetch_chapters()
        """
        self.failed_count += 1
        self.file.write(f'[FAIL]  ch_id={ch_id} "{title}" — {error}\n')
        self.file.flush()

    def _rotate_logs(self):
        """
        Deletes the oldest fetch_*.log files if more than 10 exist.

        Called at the start of each run (in __enter__) so old logs are
        cleaned up even if the previous run was killed before completing.

        Called by: __enter__()
        Depends on: os.listdir(), os.path.getmtime(), os.remove()
        """
        try:
            files = [
                os.path.join(self.logs_dir, f)
                for f in os.listdir(self.logs_dir)
                if f.startswith("fetch_") and f.endswith(".log")
            ]
            files.sort(key=os.path.getmtime)

            while len(files) > 10:
                oldest_file = files.pop(0)
                os.remove(oldest_file)
        except Exception as e:
            # Fallback if rotation fails — don't crash the main process
            print(f"Error rotating logs: {e}")
