"""Worker startup stale-browser-job reaper tests (Codex review #3).

The extension's `_wv2nasAbortBrowserJob` covers user-visible failures
(404, 5xx mid-segment, finalize fail). It can't cover:
  - tab closed before catch block runs
  - browser/extension crash
  - chrome offscreen evicted before SW heard the failure message

For those, `_reap_stale_browser_jobs` runs on worker boot, walks
job_metadata for browser-mode rows older than 6h still in
browser_pending/browser_uploading, marks them failed, and rmtrees
their staging dirs.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


WORKER_DIR = Path(__file__).resolve().parents[1]
DOCKER_ROOT = WORKER_DIR.parent


def _build_test_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, url TEXT, title TEXT, status TEXT,
                progress INTEGER DEFAULT 0, created_at TIMESTAMP,
                started_at TIMESTAMP, completed_at TIMESTAMP,
                file_path TEXT, file_size INTEGER, error_message TEXT,
                retry_count INTEGER DEFAULT 0
            )
        """))
        conn.execute(sa_text("""
            CREATE TABLE job_metadata (
                job_id TEXT PRIMARY KEY, referer TEXT, headers TEXT,
                source_page TEXT, output_subdir TEXT, duration INTEGER,
                actual_duration INTEGER, suspect_reason TEXT,
                mode TEXT, total_segments INTEGER, staging_dir TEXT,
                finalize_started_at TIMESTAMP
            )
        """))
    return engine


def _plant(engine, *, job_id, status, mode, created_at, staging_dir,
           finalize_started_at=None):
    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, created_at) "
            "VALUES (:id, :url, :title, :status, 0, :ca)"
        ), {"id": job_id, "url": "https://x", "title": "t",
            "status": status, "ca": created_at})
        db.execute(sa_text(
            "INSERT INTO job_metadata "
            "(job_id, mode, total_segments, staging_dir, finalize_started_at) "
            "VALUES (:id, :mode, 1, :sd, :fsa)"
        ), {"id": job_id, "mode": mode, "sd": staging_dir,
            "fsa": finalize_started_at})
        db.commit()
    finally:
        db.close()


def _read_status(engine, job_id):
    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        row = db.execute(sa_text("SELECT status, error_message FROM jobs WHERE id = :id"),
                         {"id": job_id}).first()
        return (row.status, row.error_message) if row else (None, None)
    finally:
        db.close()


@pytest.fixture
def worker_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in sys.path:
        sys.path.insert(0, str(DOCKER_ROOT))

    import importlib
    import worker as worker_module
    importlib.reload(worker_module)

    engine = _build_test_engine()
    worker_module.engine = engine
    # Codex review #7: reaper now reads browser_finalize_queue first to
    # avoid destroying queued staging. Tests that don't care about queue
    # state get an empty list by default; tests asserting queue-aware
    # behavior override this.
    worker_module.redis_client = MagicMock()
    worker_module.redis_client.lrange = MagicMock(return_value=[])
    # Codex adversarial-review: zombie reaper now scans for worker
    # heartbeat keys to avoid clobbering an actively-muxing job. By
    # default the scan returns nothing; tests asserting heartbeat
    # protection override this.
    worker_module.redis_client.scan = MagicMock(return_value=(0, []))
    return worker_module, engine


def test_reaper_marks_old_browser_pending_failed(worker_module, tmp_path):
    """A row in browser_pending older than 6h gets flipped to failed."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-aaaa-aaaa-aaaa-111111111111"
    staging = staging_root / job_id
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x")

    _plant(engine,
           job_id=job_id, status="browser_pending", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(staging))

    mod._reap_stale_browser_jobs()

    status, err = _read_status(engine, job_id)
    assert status == "failed"
    assert "Stale browser job" in (err or "")
    assert not staging.exists()  # staging wiped


def test_reaper_skips_recent_browser_jobs(worker_module, tmp_path):
    """Job created 30min ago must NOT be reaped — user might still be
    actively uploading."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-bbbb-bbbb-bbbb-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(engine,
           job_id=job_id, status="browser_uploading", mode="browser",
           created_at=datetime.utcnow() - timedelta(minutes=30),
           staging_dir=str(staging))

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "browser_uploading"
    assert staging.exists()


def test_reaper_skips_non_browser_mode(worker_module, tmp_path):
    """Even if old, a nas-direct (mode=NULL) job in 'pending' state must
    not be reaped by THIS reaper — it has its own zombie reaper."""
    mod, engine = worker_module
    job_id = "11111111-cccc-cccc-cccc-111111111111"

    _plant(engine,
           job_id=job_id, status="pending", mode=None,
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir="/nonexistent")

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "pending"


def test_reaper_skips_finished_browser_jobs(worker_module, tmp_path):
    """browser-mode jobs in completed/failed are not re-touched."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    for terminal in ("completed", "failed", "cancelled"):
        job_id = f"11111111-dddd-{terminal[:4]}-dddd-111111111111"
        staging = staging_root / job_id
        staging.mkdir()
        _plant(engine,
               job_id=job_id, status=terminal, mode="browser",
               created_at=datetime.utcnow() - timedelta(hours=48),
               staging_dir=str(staging))

    mod._reap_stale_browser_jobs()

    for terminal in ("completed", "failed", "cancelled"):
        job_id = f"11111111-dddd-{terminal[:4]}-dddd-111111111111"
        status, _ = _read_status(engine, job_id)
        assert status == terminal


def test_reaper_refuses_to_rmtree_outside_staging_root(worker_module, tmp_path):
    """Defense-in-depth: if a row's staging_dir somehow points outside
    STAGING_DIR (e.g. someone manually set it via psql), the reaper
    must NOT rmtree it. The DB flip still happens; cleanup is skipped
    with a logged warning."""
    mod, engine = worker_module
    # Path OUTSIDE the configured STAGING_DIR.
    outside = tmp_path / "definitely-not-staging"
    outside.mkdir()
    sentinel = outside / "do-not-delete.txt"
    sentinel.write_text("important")

    job_id = "11111111-eeee-eeee-eeee-111111111111"
    _plant(engine,
           job_id=job_id, status="browser_uploading", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(outside))

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "failed"
    # The outside-the-root directory MUST still exist.
    assert outside.is_dir()
    assert sentinel.is_file()


def test_reaper_handles_missing_staging_dir(worker_module, tmp_path):
    """Row says staging_dir=X but X was already deleted (by abort
    endpoint, by manual cleanup, ...). Reaper just flips the row; rmtree
    of a non-existent path is a no-op."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-ffff-ffff-ffff-111111111111"
    fake_staging = staging_root / job_id  # never created on disk

    _plant(engine,
           job_id=job_id, status="browser_pending", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(fake_staging))

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "failed"


def test_reaper_no_op_when_no_stale_rows(worker_module):
    """Empty / no-stale-rows path must not raise."""
    mod, _ = worker_module
    mod._reap_stale_browser_jobs()  # should not throw


# Codex review #16: a row in browser_finalizing whose finalize CAS
# happened RECENTLY must NOT be reaped — even if the job's created_at
# is older than the stale threshold (e.g., user uploaded slowly for
# hours, then finally called finalize).


def test_reaper_preserves_browser_finalizing_with_recent_finalize_started_at(
    worker_module, tmp_path,
):
    """The Codex regression: created_at is 24h ago (job was uploading
    slowly for a day), but finalize CAS just committed (finalize_started_at
    is now-ish). Reaper must NOT delete this — it'd race the rpush and
    destroy a fully-uploaded job."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-finalize-fresh-cas-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(
        engine, job_id=job_id, status="browser_finalizing", mode="browser",
        created_at=datetime.utcnow() - timedelta(hours=24),  # old
        finalize_started_at=datetime.utcnow() - timedelta(seconds=30),  # fresh CAS
        staging_dir=str(staging),
    )

    mod._reap_stale_browser_jobs()

    # NOT reaped — fresh finalize_started_at means the CAS just happened,
    # the rpush may still be in flight or just landed.
    status, _ = _read_status(engine, job_id)
    assert status == "browser_finalizing"
    assert staging.is_dir()


def test_reaper_reaps_browser_finalizing_with_old_finalize_started_at(
    worker_module, tmp_path,
):
    """Conversely: a stuck browser_finalizing whose CAS happened >6h ago
    (rpush failed and user never retried) IS stale — reap it."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-finalize-stuck-cas-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(
        engine, job_id=job_id, status="browser_finalizing", mode="browser",
        created_at=datetime.utcnow() - timedelta(hours=24),
        finalize_started_at=datetime.utcnow() - timedelta(hours=8),  # old
        staging_dir=str(staging),
    )

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "failed"
    assert not staging.exists()


def test_reaper_falls_back_to_created_at_for_legacy_browser_finalizing(
    worker_module, tmp_path,
):
    """Pre-Codex-#16 rows have NULL finalize_started_at. COALESCE
    fallback should still reap them based on created_at age."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-legacy-no-fsa-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(
        engine, job_id=job_id, status="browser_finalizing", mode="browser",
        created_at=datetime.utcnow() - timedelta(hours=24),
        finalize_started_at=None,  # legacy — never set
        staging_dir=str(staging),
    )

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "failed"


def test_reaper_covers_browser_finalizing_state(worker_module, tmp_path):
    """Codex review #6: the new 'browser_finalizing' state means a job
    that finished CAS but hung in verify/rpush. Reaper must include it
    in the stale set so a server crash mid-finalize doesn't strand the
    job + its staging dir indefinitely."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-aaaa-bbbb-cccc-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(engine,
           job_id=job_id, status="browser_finalizing", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(staging))

    # Empty redis queue (default fixture) — job NOT queued, safe to reap.
    mod._reap_stale_browser_jobs()

    status, err = _read_status(engine, job_id)
    assert status == "failed"
    assert "Stale browser job" in (err or "")
    assert not staging.exists()


# Codex review #7: a browser_finalizing job that's STILL in the redis
# finalize queue must NOT be reaped. The worker explicitly allows
# claiming from browser_finalizing to recover the rpush-success/
# DB-commit-fail window; if reaper deletes the row + staging before
# the worker pops the queue, the eventual CAS fails and the user
# loses a fully-staged download.

def test_reaper_preserves_queued_browser_finalizing_job(worker_module, tmp_path):
    """The whole regression Codex flagged: worker down >6h after
    finalize enqueued. Job is browser_finalizing, created 24h ago,
    AND its id is in the redis queue. Reaper must skip it so the
    worker can claim it via CAS once the run loop drains the queue."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-bbbb-cccc-dddd-111111111111"
    staging = staging_root / job_id
    staging.mkdir()
    sentinel = staging / "video"
    sentinel.mkdir()
    (sentinel / "seg_00000000.bin").write_bytes(b"fully uploaded segment")

    _plant(engine,
           job_id=job_id, status="browser_finalizing", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(staging))

    # Simulate redis still holding the queued finalize entry.
    mod.redis_client.lrange = MagicMock(return_value=[job_id])

    mod._reap_stale_browser_jobs()

    # Status MUST still be browser_finalizing — worker can claim it.
    status, _ = _read_status(engine, job_id)
    assert status == "browser_finalizing"
    # Staging is intact — segments survive for the worker.
    assert staging.is_dir()
    assert (sentinel / "seg_00000000.bin").is_file()


def test_reaper_only_preserves_queued_browser_finalizing_not_other_states(
    worker_module, tmp_path
):
    """Queue protection is scoped to the browser_finalizing state. A
    browser_pending or browser_uploading job that somehow ended up in
    the queue (shouldn't happen under normal flow but defense in
    depth) is still reapable — those states mean the API never made
    it past the pre-finalize CAS, so the queue entry is bogus."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-cccc-dddd-eeee-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(engine,
           job_id=job_id, status="browser_uploading", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(staging))

    # Simulate the (unusual) case of a stale browser_uploading id in
    # the queue. Reaper must still flip the status — the queue
    # protection only applies to browser_finalizing.
    mod.redis_client.lrange = MagicMock(return_value=[job_id])

    mod._reap_stale_browser_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "failed"
    assert not staging.exists()


def test_reaper_defers_when_redis_unavailable(worker_module, tmp_path):
    """Cannot read the queue → don't reap anything. Better to defer
    one boot cycle than risk destroying queued staging."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "11111111-dddd-eeee-ffff-111111111111"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(engine,
           job_id=job_id, status="browser_finalizing", mode="browser",
           created_at=datetime.utcnow() - timedelta(hours=24),
           staging_dir=str(staging))

    # Redis raises on lrange.
    mod.redis_client.lrange = MagicMock(
        side_effect=RuntimeError("redis connection refused")
    )

    mod._reap_stale_browser_jobs()  # must NOT raise

    # Nothing was reaped — status / staging intact.
    status, _ = _read_status(engine, job_id)
    assert status == "browser_finalizing"
    assert staging.is_dir()


# Codex adversarial-review: SELECT→UPDATE race fixes.
#
# Old code: SELECT ids matching stale predicate, then UPDATE WHERE id IN :ids.
# Between the two, another transaction could (a) complete a 'downloading'
# zombie [zombie reaper], or (b) CAS-flip a 'browser_uploading' row to fresh
# 'browser_finalizing' with a recent finalize_started_at [stale reaper]. The
# old UPDATE clobbered both because it filtered by id only. New code uses
# UPDATE...RETURNING with the predicate re-evaluated at UPDATE time, plus a
# follow-up SELECT for staging metadata of rows that actually transitioned.


def _flip_status_before_update(mod, target_id, *, new_status, fsa=None):
    """Install a SQLAlchemy before_execute listener that flips the row's
    status RIGHT BEFORE the reaper's UPDATE runs. Simulates a concurrent
    transaction landing in the SELECT→UPDATE gap that the old code had.
    Returns a function to remove the listener."""
    from sqlalchemy import event

    fired = {"count": 0}

    def _before(conn, clauseelement, multiparams, params, execution_options):
        sql = str(clauseelement).strip().upper()
        # Fire only on the reaper's UPDATE (not on the follow-up SELECT
        # nor on test setup statements). The reaper UPDATE starts with
        # "UPDATE JOBS SET STATUS = 'FAILED'".
        if sql.startswith("UPDATE JOBS") and fired["count"] == 0:
            fired["count"] += 1
            updates = ["status = :s"]
            ps = {"id": target_id, "s": new_status}
            if fsa is not None:
                updates.append("finalize_started_at_set = 1")
            conn.execute(sa_text(
                "UPDATE jobs SET status = :s WHERE id = :id"
            ), ps)
            if fsa is not None:
                conn.execute(sa_text(
                    "UPDATE job_metadata SET finalize_started_at = :fsa "
                    "WHERE job_id = :id"
                ), {"fsa": fsa, "id": target_id})

    event.listen(mod.engine, "before_execute", _before)
    return lambda: event.remove(mod.engine, "before_execute", _before)


def test_zombie_reaper_skips_job_that_completed_during_select_to_update_gap(
    worker_module, tmp_path,
):
    """Old bug: SELECT found row in 'downloading' >2h ago, then a worker
    finished it (status='completed') before reaper's UPDATE WHERE id IN
    :ids ran → completed got clobbered to 'failed'. New code: conditional
    UPDATE re-checks status, leaves completed rows alone."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    completed_id = "22222222-comp-comp-comp-222222222222"
    zombie_id = "22222222-zomb-zomb-zomb-222222222222"
    completed_staging = staging_root / completed_id
    zombie_staging = staging_root / zombie_id
    completed_staging.mkdir()
    (completed_staging / "important.mp4").write_bytes(b"finished output")
    zombie_staging.mkdir()

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        for jid, status, started in (
            (completed_id, "downloading",
             datetime.utcnow() - timedelta(hours=3)),
            (zombie_id, "downloading",
             datetime.utcnow() - timedelta(hours=3)),
        ):
            db.execute(sa_text(
                "INSERT INTO jobs (id, url, title, status, progress, started_at) "
                "VALUES (:id, 'https://x', 't', :status, 0, :sa)"
            ), {"id": jid, "status": status, "sa": started})
            db.execute(sa_text(
                "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
                "VALUES (:id, 'browser', 1, :sd)"
            ), {"id": jid, "sd": str(staging_root / jid)})
        db.commit()
    finally:
        db.close()

    # Race: between the reaper's notional SELECT and its UPDATE, the
    # completed worker finishes its job and commits status='completed'.
    remove = _flip_status_before_update(
        mod, completed_id, new_status="completed",
    )
    try:
        mod._reap_zombie_jobs()
    finally:
        remove()

    # Completed row preserved — NOT clobbered to failed, staging intact.
    status, _ = _read_status(engine, completed_id)
    assert status == "completed", (
        "Old SELECT→UPDATE race regression: a job that completed during "
        "the gap got overwritten to failed by the reaper's UPDATE WHERE id IN :ids"
    )
    assert completed_staging.is_dir()
    assert (completed_staging / "important.mp4").is_file()

    # Real zombie still reaped.
    z_status, _ = _read_status(engine, zombie_id)
    assert z_status == "failed"


def test_stale_browser_reaper_skips_finalizing_with_fresh_fsa_during_gap(
    worker_module, tmp_path,
):
    """Old bug: SELECT found row in 'browser_uploading' created 24h ago,
    then a /finalize CAS commits (status→'browser_finalizing', fresh
    finalize_started_at) before the reaper's UPDATE WHERE id IN :ids
    ran → fully-staged job destroyed. New code: conditional UPDATE
    re-checks the fsa freshness, leaves the row alone."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "33333333-race-race-race-333333333333"
    staging = staging_root / job_id
    staging.mkdir()
    (staging / "video").mkdir()
    sentinel = staging / "video" / "seg_00000000.bin"
    sentinel.write_bytes(b"every byte fully uploaded")

    # Plant in 'browser_uploading' with old created_at — the OLD reaper's
    # SELECT would have included this row.
    _plant(
        engine, job_id=job_id, status="browser_uploading", mode="browser",
        created_at=datetime.utcnow() - timedelta(hours=24),
        staging_dir=str(staging),
    )

    # Race: between the reaper's notional SELECT and its UPDATE, the
    # API's /finalize CAS commits — status flips to 'browser_finalizing'
    # and finalize_started_at is set to now-ish.
    fresh_fsa = datetime.utcnow() - timedelta(seconds=10)
    remove = _flip_status_before_update(
        mod, job_id, new_status="browser_finalizing", fsa=fresh_fsa,
    )
    try:
        mod._reap_stale_browser_jobs()
    finally:
        remove()

    # Row preserved — NOT clobbered to failed.
    status, _ = _read_status(engine, job_id)
    assert status == "browser_finalizing", (
        "Old SELECT→UPDATE race regression: /finalize CAS landed in the "
        "gap, but reaper's UPDATE WHERE id IN :ids destroyed the just-"
        "queued job and would have led to the fully-staged segments "
        "being rmtree'd"
    )
    assert staging.is_dir()
    assert sentinel.is_file()


def test_stale_browser_reaper_skips_completed_during_gap(
    worker_module, tmp_path,
):
    """A row that was in 'browser_uploading' at SELECT time but that
    the worker (somehow) finalized → set to 'completed' before reaper's
    UPDATE must NOT be reaped. Conditional UPDATE excludes rows whose
    status no longer matches any branch of the predicate."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "44444444-comp-mid-reap-444444444444"
    staging = staging_root / job_id
    staging.mkdir()

    _plant(
        engine, job_id=job_id, status="browser_uploading", mode="browser",
        created_at=datetime.utcnow() - timedelta(hours=24),
        staging_dir=str(staging),
    )

    remove = _flip_status_before_update(mod, job_id, new_status="completed")
    try:
        mod._reap_stale_browser_jobs()
    finally:
        remove()

    status, _ = _read_status(engine, job_id)
    assert status == "completed"
    # Staging untouched — only rows actually transitioned by the UPDATE
    # are eligible for rmtree.
    assert staging.is_dir()


def test_zombie_reaper_no_op_when_no_zombies(worker_module):
    """Empty-table sanity check for the new RETURNING-based path."""
    mod, _ = worker_module
    mod._reap_zombie_jobs()  # must NOT raise


def test_zombie_reaper_reaps_qualified_row(worker_module, tmp_path):
    """Happy path: a single zombie >2h old gets flipped + browser
    staging cleaned via the post-UPDATE metadata lookup."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "55555555-zomb-only-zomb-555555555555"
    staging = staging_root / job_id
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"abandoned")

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
        ), {"id": job_id, "sa": datetime.utcnow() - timedelta(hours=5)})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": job_id, "sd": str(staging)})
        db.commit()
    finally:
        db.close()

    mod._reap_zombie_jobs()

    status, err = _read_status(engine, job_id)
    assert status == "failed"
    assert "zombie reaped" in (err or "")
    # Browser zombie → staging wiped post-flip.
    assert not staging.exists()


def test_zombie_reaper_skips_recent_in_flight(worker_module, tmp_path):
    """Boundary: a job started 30 min ago is NOT a zombie."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "66666666-recnt-recnt-recnt-666666666666"
    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'downloading', 5, :sa)"
        ), {"id": job_id, "sa": datetime.utcnow() - timedelta(minutes=30)})
        db.commit()
    finally:
        db.close()

    mod._reap_zombie_jobs()

    status, _ = _read_status(engine, job_id)
    assert status == "downloading"


# Codex adversarial-review: heartbeat-aware zombie reaper.
#
# Background: a browser-mode finalize on a 50 GB job over slow NAS
# storage can legitimately exceed the 2h zombie cutoff. Pre-fix, a peer
# worker's startup pass would flip the live row to 'failed' AND rmtree
# the staging dir under the active process. The fix is a Redis
# heartbeat written by the muxing worker (`worker_alive:<id>`) that the
# reaper SCANs for and excludes from its UPDATE.


def _scan_returning(*items):
    """Build a fake redis SCAN that returns the given keys in one batch.
    SCAN is iterator-based; signal completion by returning cursor=0."""
    items = list(items)
    return MagicMock(return_value=(0, items))


def test_zombie_reaper_skips_job_with_live_heartbeat(worker_module, tmp_path):
    """The Codex finding: a long browser-mode finalize whose worker is
    still alive (heartbeat present in Redis) must NOT be reaped, even
    when started_at is older than the 2h cutoff."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    alive_id = "77777777-alive-alive-alive-777777777777"
    alive_staging = staging_root / alive_id
    alive_staging.mkdir()
    (alive_staging / "video").mkdir()
    sentinel = alive_staging / "video" / "seg_00000000.bin"
    sentinel.write_bytes(b"50GB-of-staged-bytes")

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
        ), {"id": alive_id, "sa": datetime.utcnow() - timedelta(hours=5)})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": alive_id, "sd": str(alive_staging)})
        db.commit()
    finally:
        db.close()

    # Heartbeat key present → worker still alive.
    mod.redis_client.scan = _scan_returning(
        f"{mod.WORKER_HEARTBEAT_KEY_PREFIX}{alive_id}"
    )

    mod._reap_zombie_jobs()

    # Row preserved — staging untouched (the whole point of the fix:
    # don't yank a 50GB staging tree out from under an active mux).
    status, _ = _read_status(engine, alive_id)
    assert status == "processing", (
        "Heartbeat-aware reaper regression: a worker still publishing "
        "a liveness key got its row clobbered to 'failed', which would "
        "have triggered an rmtree on the active mux's staging dir"
    )
    assert alive_staging.is_dir()
    assert sentinel.is_file()


def test_zombie_reaper_reaps_dead_worker_alongside_alive_one(
    worker_module, tmp_path,
):
    """Mixed batch: one row has a heartbeat (skip), one doesn't (reap).
    Ensures the heartbeat exclusion is precisely scoped — it doesn't
    leak protection to unrelated zombies."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    alive_id = "88888888-alive-alive-alive-888888888888"
    dead_id = "88888888-dead-dead-dead-888888888888"
    alive_staging = staging_root / alive_id
    dead_staging = staging_root / dead_id
    alive_staging.mkdir()
    dead_staging.mkdir()

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        for jid, sd in ((alive_id, alive_staging), (dead_id, dead_staging)):
            db.execute(sa_text(
                "INSERT INTO jobs (id, url, title, status, progress, started_at) "
                "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
            ), {"id": jid, "sa": datetime.utcnow() - timedelta(hours=5)})
            db.execute(sa_text(
                "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
                "VALUES (:id, 'browser', 1, :sd)"
            ), {"id": jid, "sd": str(sd)})
        db.commit()
    finally:
        db.close()

    # Only alive_id has a heartbeat.
    mod.redis_client.scan = _scan_returning(
        f"{mod.WORKER_HEARTBEAT_KEY_PREFIX}{alive_id}"
    )

    mod._reap_zombie_jobs()

    alive_status, _ = _read_status(engine, alive_id)
    dead_status, _ = _read_status(engine, dead_id)
    assert alive_status == "processing"
    assert dead_status == "failed"
    assert alive_staging.is_dir()  # protected
    assert not dead_staging.exists()  # cleaned


def test_zombie_reaper_defers_browser_mode_when_redis_scan_fails(
    worker_module, tmp_path,
):
    """Codex review (P1): when Redis SCAN fails, we cannot exclude
    alive workers from reaping. A still-running browser-mode finalize
    (slow NAS mux >2h) MUST NOT be flipped to 'failed' and have its
    staging rmtree'd just because we can't see its heartbeat. The
    reaper defers browser-mode rows to a later run; non-browser rows
    are still eligible (covered separately).

    Pre-fix: the row was reaped + staging wiped, destroying live work.
    """
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    job_id = "99999999-rdfail-rdfail-9999-999999999999"
    staging = staging_root / job_id
    staging.mkdir()
    (staging / "video").mkdir()
    sentinel = staging / "video" / "seg_00000000.bin"
    sentinel.write_bytes(b"50GB-of-staged-bytes-still-being-muxed")

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
        ), {"id": job_id, "sa": datetime.utcnow() - timedelta(hours=5)})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": job_id, "sd": str(staging)})
        db.commit()
    finally:
        db.close()

    mod.redis_client.scan = MagicMock(
        side_effect=RuntimeError("redis connection refused")
    )

    mod._reap_zombie_jobs()  # must NOT raise

    # Browser-mode row preserved; staging intact. The heartbeat-blind
    # reaper's only safe option is to defer.
    status, _ = _read_status(engine, job_id)
    assert status == "processing", (
        "Codex P1 regression: browser-mode zombie was reaped while "
        "Redis was unreachable, which would have rmtree'd the live "
        "mux's staging dir under it"
    )
    assert staging.is_dir()
    assert sentinel.is_file()


def test_zombie_reaper_still_reaps_non_browser_when_redis_scan_fails(
    worker_module, tmp_path,
):
    """Counterpart to the P1 deferral: non-browser-mode rows (legacy
    yt-dlp / nas-direct) DON'T have a multi-hour mux pattern, so the
    'maybe-still-alive' concern doesn't apply to them. The reaper
    still cleans those when the heartbeat scan failed — otherwise a
    permanently-dead legacy worker leaks rows forever.
    """
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    legacy_id = "99999999-legacy-legacy-99999999999"

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
        ), {"id": legacy_id, "sa": datetime.utcnow() - timedelta(hours=5)})
        # mode != 'browser' (NULL or 'nas-direct' / legacy yt-dlp)
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments) "
            "VALUES (:id, NULL, 1)"
        ), {"id": legacy_id})
        db.commit()
    finally:
        db.close()

    mod.redis_client.scan = MagicMock(
        side_effect=RuntimeError("redis connection refused")
    )

    mod._reap_zombie_jobs()  # must NOT raise

    # Legacy zombie still cleaned even with Redis down — no live-mux
    # case to protect against.
    status, _ = _read_status(engine, legacy_id)
    assert status == "failed"


def test_zombie_reaper_defers_browser_but_reaps_legacy_in_same_batch(
    worker_module, tmp_path,
):
    """Mixed batch with Redis down: browser row deferred, legacy row
    reaped. Verifies the SQL clause is precisely scoped to mode
    ='browser' and doesn't accidentally protect legacy rows."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    browser_id = "99999999-mix-browser-9999-999999999999"
    legacy_id = "99999999-mix-legacy--9999-999999999999"
    browser_staging = staging_root / browser_id
    browser_staging.mkdir()
    (browser_staging / "important").write_bytes(b"keep-me")

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        for jid, mode, sd in (
            (browser_id, "browser", str(browser_staging)),
            (legacy_id, None, None),
        ):
            db.execute(sa_text(
                "INSERT INTO jobs (id, url, title, status, progress, started_at) "
                "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
            ), {"id": jid, "sa": datetime.utcnow() - timedelta(hours=5)})
            db.execute(sa_text(
                "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
                "VALUES (:id, :mode, 1, :sd)"
            ), {"id": jid, "mode": mode, "sd": sd})
        db.commit()
    finally:
        db.close()

    mod.redis_client.scan = MagicMock(
        side_effect=RuntimeError("redis connection refused")
    )

    mod._reap_zombie_jobs()

    browser_status, _ = _read_status(engine, browser_id)
    legacy_status, _ = _read_status(engine, legacy_id)
    assert browser_status == "processing", "browser row should have been deferred"
    assert legacy_status == "failed", "legacy row should still be reaped"
    assert browser_staging.is_dir()
    assert (browser_staging / "important").is_file()


def test_zombie_reaper_scan_paginates(worker_module, tmp_path):
    """SCAN returns results in batches with a non-zero cursor between
    them. Reaper must keep iterating until cursor=0; otherwise alive
    workers in the second page would lose protection."""
    mod, engine = worker_module
    staging_root = Path(os.environ["STAGING_DIR"])
    staging_root.mkdir(parents=True, exist_ok=True)

    alive_id = "aaaaaaaa-page2-page2-page2-aaaaaaaaaaaa"
    alive_staging = staging_root / alive_id
    alive_staging.mkdir()

    Sess = sessionmaker(bind=engine)
    db = Sess()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, started_at) "
            "VALUES (:id, 'https://x', 't', 'processing', 50, :sa)"
        ), {"id": alive_id, "sa": datetime.utcnow() - timedelta(hours=5)})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": alive_id, "sd": str(alive_staging)})
        db.commit()
    finally:
        db.close()

    # First scan returns no matches with cursor != 0; second returns
    # the alive id with cursor=0. Reaper must combine both pages.
    scan_results = [
        (42, [f"{mod.WORKER_HEARTBEAT_KEY_PREFIX}unrelated-id"]),
        (0, [f"{mod.WORKER_HEARTBEAT_KEY_PREFIX}{alive_id}"]),
    ]
    mod.redis_client.scan = MagicMock(side_effect=scan_results)

    mod._reap_zombie_jobs()

    status, _ = _read_status(engine, alive_id)
    assert status == "processing"
    assert alive_staging.is_dir()


# _WorkerHeartbeat context manager: minimal coverage that the redis
# key gets set on enter and deleted on exit.


def test_worker_heartbeat_sets_and_deletes_key():
    """Smoke test for the heartbeat helper. Uses a fake redis to assert
    the lifecycle: set on __enter__, delete on __exit__."""
    import sys as _sys
    if str(WORKER_DIR) not in _sys.path:
        _sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in _sys.path:
        _sys.path.insert(0, str(DOCKER_ROOT))
    import worker as wm

    fake_redis = MagicMock()

    job_id = "bbbbbbbb-hb-hb-hb-bbbbbbbbbbbb"
    # Tiny interval so the loop can fire at least once if it does
    # iterate, but we also assert the lifecycle with a clean exit.
    with wm._WorkerHeartbeat(fake_redis, job_id, ttl=600, interval=120):
        # set called at least once on enter.
        assert fake_redis.set.called
        args, kwargs = fake_redis.set.call_args
        # Key shape: "worker_alive:<job_id>"
        assert args[0] == f"{wm.WORKER_HEARTBEAT_KEY_PREFIX}{job_id}"
        assert kwargs.get("ex") == 600

    # Exit deletes the key — no grace for a worker that has finished.
    fake_redis.delete.assert_called_with(
        f"{wm.WORKER_HEARTBEAT_KEY_PREFIX}{job_id}"
    )


def test_worker_heartbeat_swallows_redis_failure():
    """A flaky redis must not break the worker — heartbeat is best-effort.
    Set raises on enter; lifecycle still completes cleanly."""
    import sys as _sys
    if str(WORKER_DIR) not in _sys.path:
        _sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in _sys.path:
        _sys.path.insert(0, str(DOCKER_ROOT))
    import worker as wm

    fake_redis = MagicMock()
    fake_redis.set = MagicMock(side_effect=RuntimeError("redis down"))
    fake_redis.delete = MagicMock(side_effect=RuntimeError("redis down"))

    # Must not raise.
    with wm._WorkerHeartbeat(fake_redis, "job-x", ttl=600, interval=120):
        pass


# Codex adversarial-review: BLPOP returns from the FIRST non-empty key
# in its argument list. The pre-fix order was always
# ['download_queue', 'browser_finalize_queue'], which means a
# sustained legacy-download backlog would starve every browser-finalize
# job (each holding up to 50 GB of staging) indefinitely. The fix
# rotates the queue list every iteration so neither queue can pin
# priority to itself.


def test_run_loop_alternates_queue_priority(worker_module, monkeypatch):
    """The exact regression: queue priority rotates each iteration so
    BLPOP can't always favor the same queue."""
    mod, _engine = worker_module

    # Stub SessionLocal so DownloadWorker.__init__ doesn't open a real DB.
    monkeypatch.setattr(mod, "SessionLocal", MagicMock(return_value=MagicMock()))

    captured: list = []

    def fake_blpop(queues, timeout=None):
        captured.append(list(queues))
        if len(captured) >= 4:
            mod.shutdown_flag = True
        return None  # No job; loop iterates without dispatch.

    mod.redis_client.blpop = MagicMock(side_effect=fake_blpop)
    mod.shutdown_flag = False
    try:
        worker = mod.DownloadWorker()
        worker.run()
    finally:
        # Don't leak shutdown state into peer tests.
        mod.shutdown_flag = False

    assert len(captured) >= 2, "Loop didn't iterate enough to verify rotation"

    # Every captured list must cover both queues.
    for qs in captured:
        assert set(qs) == {"download_queue", "browser_finalize_queue"}, (
            f"Unexpected queue list: {qs!r}"
        )

    # The first queue must alternate iteration-to-iteration. Without
    # the rotation fix this list would be all 'download_queue'.
    first_in_each = [qs[0] for qs in captured]
    for i in range(1, len(first_in_each)):
        assert first_in_each[i] != first_in_each[i - 1], (
            f"Queue priority did not rotate at iteration {i}: "
            f"{first_in_each!r}"
        )

    # Browser finalize should be the very first priority on the first
    # iteration so any pre-boot backlog drains immediately.
    assert first_in_each[0] == "browser_finalize_queue"


def test_run_loop_dispatches_browser_finalize_when_popped(worker_module, monkeypatch):
    """Sanity: when blpop returns from browser_finalize_queue, the
    worker dispatches to process_browser_finalize, not process_job."""
    mod, _engine = worker_module
    monkeypatch.setattr(mod, "SessionLocal", MagicMock(return_value=MagicMock()))

    iters = {"n": 0}

    def fake_blpop(queues, timeout=None):
        iters["n"] += 1
        if iters["n"] == 1:
            return ("browser_finalize_queue", "j1")
        mod.shutdown_flag = True
        return None

    mod.redis_client.blpop = MagicMock(side_effect=fake_blpop)
    mod.shutdown_flag = False
    try:
        worker = mod.DownloadWorker()
        worker.process_browser_finalize = MagicMock()
        worker.process_job = MagicMock()
        worker.run()
    finally:
        mod.shutdown_flag = False

    worker.process_browser_finalize.assert_called_once_with("j1")
    worker.process_job.assert_not_called()


def test_run_loop_dispatches_download_when_popped(worker_module, monkeypatch):
    """Counterpart: download_queue pop → process_job."""
    mod, _engine = worker_module
    monkeypatch.setattr(mod, "SessionLocal", MagicMock(return_value=MagicMock()))

    iters = {"n": 0}

    def fake_blpop(queues, timeout=None):
        iters["n"] += 1
        if iters["n"] == 1:
            return ("download_queue", "j2")
        mod.shutdown_flag = True
        return None

    mod.redis_client.blpop = MagicMock(side_effect=fake_blpop)
    mod.shutdown_flag = False
    try:
        worker = mod.DownloadWorker()
        worker.process_browser_finalize = MagicMock()
        worker.process_job = MagicMock()
        worker.run()
    finally:
        mod.shutdown_flag = False

    worker.process_job.assert_called_once_with("j2")
    worker.process_browser_finalize.assert_not_called()


# Codex review (P2): the stale-browser reaper DEFERS entirely if Redis
# is unavailable (it must read browser_finalize_queue to avoid
# destroying still-queued jobs). Compose-style boot ordering can have
# the worker start before redis-server, so reaping at boot needs to
# happen AFTER the Redis-readiness loop succeeds. Anything else leaves
# stale browser_pending / browser_uploading staging dirs on disk
# until the next worker restart.

def test_main_boot_order_reaps_stale_browser_after_redis_ready():
    """Source-inspection guard: in worker.py's main(), the stale-
    browser reaper must be invoked AFTER the redis_client.ping()
    readiness loop. Verified by line-position; comment-only edits
    are fine, but moving the call before the ping loop must fail
    this test."""
    src_path = WORKER_DIR / "worker.py"
    src = src_path.read_text(encoding="utf-8")

    main_idx = src.index("def main():")
    main_src = src[main_idx:]

    # Find the redis ping readiness loop. Pattern: `redis_client.ping()`
    # inside a `for i in range(max_retries):` block.
    ping_idx = main_src.index("redis_client.ping()")
    # Find the call to _reap_stale_browser_jobs.
    reap_idx = main_src.index("_reap_stale_browser_jobs()")

    assert reap_idx > ping_idx, (
        f"_reap_stale_browser_jobs() must be invoked AFTER the "
        f"redis_client.ping() readiness loop in main(). Otherwise the "
        f"reaper defers because Redis is unreachable and never "
        f"retries. (ping@{ping_idx}, reap@{reap_idx} relative to "
        f"main()'s start)"
    )
