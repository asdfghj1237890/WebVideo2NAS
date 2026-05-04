"""Retroactively probe completed jobs and flag suspect files.

Goes through every `completed` job that has a `file_path` on disk, runs
ffprobe to get the actual playback duration, and writes:

  job_metadata.actual_duration
  job_metadata.suspect_reason

`suspect_reason` is set when the actual duration is materially shorter
than `job_metadata.duration` (the m3u8 EXTINF total recorded at submission
time), or when the file is implausibly small for its declared duration.
The same heuristics as DownloadWorker._compute_suspect_reason — kept in
sync by importing it.

Why not just rerun the worker? Because the source m3u8 tokens are long
expired. We can't re-download. What we CAN do is identify which existing
files are wrong, so the user knows which ones to manually re-fetch via
the chrome sidepanel's "Re-fetch from source page" button (added in the
same change that introduced this script).

Usage (inside the worker container):
  docker compose exec worker python /app/worker/backfill_suspect.py

  # dry-run mode prints what it would do without writing anything:
  docker compose exec worker python /app/worker/backfill_suspect.py --dry-run

  # only print the suspects, no DB writes:
  docker compose exec worker python /app/worker/backfill_suspect.py --report-only

Idempotent. Safe to re-run; previously-flagged jobs get their
suspect_reason refreshed against current heuristics.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Reuse the worker's helpers so the heuristic stays in lockstep.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from worker import DownloadWorker  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/m3u8_db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("backfill_suspect")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="probe + decide suspect, but skip DB writes")
    parser.add_argument("--report-only", action="store_true", help="alias for --dry-run")
    parser.add_argument("--limit", type=int, default=0, help="process at most N jobs (0 = no limit)")
    parser.add_argument("--rescan-flagged", action="store_true",
                        help="re-probe even jobs already carrying a suspect_reason "
                             "(useful after tightening the heuristic)")
    args = parser.parse_args()

    dry = args.dry_run or args.report_only

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Both helpers are @staticmethod on DownloadWorker, so we can invoke
    # them directly off the class without instantiating a worker (which
    # would connect to redis / DB twice). The previous _Shim approach
    # hoisted the staticmethods as instance attributes, which Python
    # rebinds as bound methods — so `shim._compute_suspect_reason(
    # declared_duration=...)` got `shim` slotted into `declared_duration`
    # and crashed with "got multiple values for argument".
    probe_duration = DownloadWorker._probe_duration_seconds
    compute_suspect_reason = DownloadWorker._compute_suspect_reason

    where_filter = (
        "WHERE j.status = 'completed' AND j.file_path IS NOT NULL"
    )
    if not args.rescan_flagged:
        where_filter += " AND (jm.suspect_reason IS NULL)"

    limit_clause = f" LIMIT {int(args.limit)}" if args.limit > 0 else ""

    with Session() as db:
        rows = db.execute(text(f"""
            SELECT j.id, j.file_path, j.title, j.file_size,
                   jm.duration, jm.actual_duration, jm.suspect_reason
            FROM jobs j
            LEFT JOIN job_metadata jm ON j.id = jm.job_id
            {where_filter}
            ORDER BY j.completed_at DESC NULLS LAST, j.created_at DESC
            {limit_clause}
        """)).all()

    log.info("Found %d completed jobs to check (dry-run=%s)", len(rows), dry)

    flagged = 0
    cleared = 0
    missing = 0
    unchanged = 0
    failed_probe = 0

    for row in rows:
        job_id = str(row.id)
        path = row.file_path
        if not path or not Path(path).exists():
            log.warning("Job %s: file_path missing on disk: %s", job_id[:8], path)
            missing += 1
            continue

        actual = probe_duration(path)
        if actual is None:
            failed_probe += 1
        # File size from disk (more authoritative than DB column for backfill)
        try:
            size_bytes = Path(path).stat().st_size
        except OSError:
            size_bytes = row.file_size or 0

        reason = compute_suspect_reason(
            declared_duration=row.duration,
            actual_duration=actual,
            file_size_bytes=size_bytes,
        )

        prev = row.suspect_reason
        if reason and not prev:
            flagged += 1
            log.warning("[SUSPECT] %s — %s\n            file: %s", job_id[:8], reason, path)
        elif reason and prev:
            unchanged += 1
        elif not reason and prev:
            cleared += 1
            log.info("[CLEAR ] %s — was flagged, now passing heuristic", job_id[:8])
        else:
            unchanged += 1

        if dry:
            continue

        # Persist actual_duration + suspect_reason. Insert-or-update the
        # job_metadata row; some legacy jobs may not have a row yet.
        with Session() as db:
            try:
                db.execute(
                    text("""
                        INSERT INTO job_metadata (job_id, actual_duration, suspect_reason)
                        VALUES (:job_id, :actual, :reason)
                        ON CONFLICT (job_id)
                        DO UPDATE SET
                          actual_duration = EXCLUDED.actual_duration,
                          suspect_reason  = EXCLUDED.suspect_reason
                    """),
                    {"job_id": job_id, "actual": actual, "reason": reason},
                )
                db.commit()
            except Exception as e:
                log.error("Failed to update %s: %s", job_id[:8], e)
                db.rollback()

    log.info(
        "Summary: flagged=%d, cleared=%d, missing-on-disk=%d, "
        "ffprobe-failed=%d, unchanged=%d, total=%d (dry-run=%s)",
        flagged, cleared, missing, failed_probe, unchanged, len(rows), dry,
    )

    if dry and flagged:
        log.info("Re-run without --dry-run to persist these flags.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
