"""Worker-side CAS claim tests (Codex review fix #2b).

The /api/jobs/{id}/finalize endpoint pushes to redis BEFORE committing
the DB transition (so an rpush failure doesn't strand the job). The
side effect is that a duplicate finalize POST + retry could enqueue the
same job_id twice. To stop the worker from processing the same job
twice (which would re-mux from a now-cleaned-up staging dir and clobber
the completed file), `process_browser_finalize` opens with a CAS claim:

    UPDATE jobs SET status = 'processing', started_at = :now
    WHERE id = :job_id
      AND status IN ('pending', 'browser_pending', 'browser_uploading')

Whichever pop wins the UPDATE proceeds; the other sees rowcount=0 and
skips. These tests verify that contract.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


WORKER_DIR = Path(__file__).resolve().parents[1]
DOCKER_ROOT = WORKER_DIR.parent


def _utcnow_naive():
    return datetime.now(UTC).replace(tzinfo=None)


def _setup_test_db():
    """Build an in-memory sqlite shared across sessions, with the minimum
    schema process_browser_finalize touches."""
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
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _plant_job(SessionLocal, *, job_id: str, status: str, staging_dir: str):
    db = SessionLocal()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, created_at) "
            "VALUES (:id, :url, :title, :status, 0, :now)"
        ), {"id": job_id, "url": "https://x", "title": "t",
            "status": status, "now": _utcnow_naive()})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": job_id, "sd": staging_dir})
        db.commit()
    finally:
        db.close()


def _read_status(SessionLocal, job_id):
    db = SessionLocal()
    try:
        row = db.execute(sa_text("SELECT status FROM jobs WHERE id = :id"),
                         {"id": job_id}).first()
        return row.status if row else None
    finally:
        db.close()


@pytest.fixture
def worker_with_test_db(monkeypatch):
    """Yields (worker_module, SessionLocal, engine). Replaces the worker
    module's engine + SessionLocal so DownloadWorker uses our test DB."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in sys.path:
        sys.path.insert(0, str(DOCKER_ROOT))

    import importlib
    import worker as worker_module
    importlib.reload(worker_module)

    engine, SessionLocal = _setup_test_db()
    worker_module.engine = engine
    worker_module.SessionLocal = SessionLocal
    # Real redis isn't available in tests; mock with a minimal stub so
    # any incidental access in the SUT doesn't blow up.
    worker_module.redis_client = MagicMock()
    yield worker_module, SessionLocal, engine


def test_cas_claim_skips_already_completed_job(worker_with_test_db, tmp_path):
    """Duplicate enqueue of a finished job: CAS WHERE clause excludes
    'completed', so rowcount=0 and worker bails out without touching
    the staged data (which by now has been wiped on first run)."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-1111-1111-1111-111111111111"
    _plant_job(SessionLocal, job_id=job_id, status="completed",
               staging_dir=str(tmp_path / "nonexistent"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    # Status MUST stay 'completed'. If CAS claim were missing, the worker
    # would walk into the meta lookup, find staging_dir missing, and flip
    # status to 'failed' — masking the prior completion.
    assert _read_status(SessionLocal, job_id) == "completed"


def test_cas_claim_skips_cancelled_job(worker_with_test_db, tmp_path):
    """User cancellation lands as status='cancelled'. A delayed redis
    pop that runs after cancellation must not resurrect the job to
    'processing'."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-2222-2222-2222-222222222222"
    _plant_job(SessionLocal, job_id=job_id, status="cancelled",
               staging_dir=str(tmp_path / "x"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()
    assert _read_status(SessionLocal, job_id) == "cancelled"


def test_cas_claim_skips_already_processing_job(worker_with_test_db, tmp_path):
    """Concurrent worker pop scenario: another worker has already
    claimed the job and set it to 'processing'. Second pop must no-op."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-3333-3333-3333-333333333333"
    _plant_job(SessionLocal, job_id=job_id, status="processing",
               staging_dir=str(tmp_path / "x"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()
    # Status unchanged — no error_message written, no progress updated.
    assert _read_status(SessionLocal, job_id) == "processing"


def test_cas_claim_succeeds_on_pending(worker_with_test_db, tmp_path):
    """Happy-path entry state: status='pending' (set by API after
    redis push). CAS flips it to 'processing' and the rest of
    finalize tries to run. We don't have ffmpeg in this test path so
    finalize will error after CAS — what we verify is that the CAS
    DID succeed (status moved away from 'pending')."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-4444-4444-4444-444444444444"
    # Staging dir doesn't exist → process_browser_finalize will mark
    # the job 'failed' AFTER CAS. That's fine — what we want to prove
    # is that the CAS DID transition out of 'pending'.
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(tmp_path / "missing-staging"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    # The CAS moved status to 'processing'. Then the missing staging
    # dir caused failure → ended up at 'failed'. Either way it's NOT
    # 'pending' anymore.
    final = _read_status(SessionLocal, job_id)
    assert final in ("failed",), f"expected 'failed' after staging-missing path, got {final!r}"


def test_cas_claim_skips_browser_uploading_state(worker_with_test_db, tmp_path):
    """Codex review #6 tightened the CAS allowed-from set: claiming
    directly from 'browser_uploading' would let the worker race
    against in-flight uploads. The API now requires going through
    'browser_finalizing' first (uploads locked out), so the worker
    refuses to claim a job that's still showing browser_uploading."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-5555-5555-5555-555555555555"
    _plant_job(SessionLocal, job_id=job_id, status="browser_uploading",
               staging_dir=str(tmp_path / "missing"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()
    # Status unchanged: the worker bailed out at the CAS without
    # touching the row. (Anything in 'browser_uploading' should only
    # ever be drained by the API's finalize CAS.)
    assert _read_status(SessionLocal, job_id) == "browser_uploading"


def test_cas_claim_handles_browser_finalizing_state(worker_with_test_db, tmp_path):
    """If the API succeeded at rpush but failed the post-rpush DB
    commit, the row stays at 'browser_finalizing'. CAS WHERE clause
    includes that state so the worker can still claim and process —
    redis already has the entry, no point stranding the job."""
    worker_module, SessionLocal, _ = worker_with_test_db
    job_id = "33333333-6666-6666-6666-666666666666"
    _plant_job(SessionLocal, job_id=job_id, status="browser_finalizing",
               staging_dir=str(tmp_path / "missing"))

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()
    final = _read_status(SessionLocal, job_id)
    # CAS flipped browser_finalizing → processing → (staging missing) failed.
    # Important: we DID claim the job and walk the rest of the path.
    assert final == "failed"


# Codex adversarial-review (high): when finalize() fails after the
# worker claims ownership, the staging tree was previously stranded
# forever. The job is now terminally 'failed', which excludes it from
# the stale-browser reaper, so up to MAX_JOB_STAGING_BYTES of decrypted
# segments could pile up under STAGING_DIR. The fix: cleanup the
# staging dir in BOTH failure branches with the same containment guard
# the reapers use.


def _make_browser_job_dirs(staging_root: Path, job_id: str):
    """Build a plausible staged tree under STAGING_DIR/<job_id>."""
    staging = staging_root / job_id
    staging.mkdir(parents=True)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"decrypted-segment")
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 1}},
    }
    import json as _json
    (staging / "manifest.json").write_text(_json.dumps(plan))
    return staging


def _setup_outputs_dir(monkeypatch, worker_module, tmp_path):
    """Worker also calls resolve_output_dir(...) for the candidate
    placeholder. resolve_output_dir hard-codes /downloads, which we
    don't want to touch in tests — monkeypatch it to return our
    tmp_path/outputs."""
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(worker_module, "resolve_output_dir", lambda _subdir: out)
    return out


def test_failed_finalize_releases_staging_browser_finalize_error(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """Codex regression: finalize() raises BrowserFinalizeError →
    job marked failed AND staging dir rmtree'd (containment-guarded)."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    job_id = "44444444-bfe1-bfe1-bfe1-444444444444"
    staging = _make_browser_job_dirs(staging_root, job_id)
    sentinel = staging / "video" / "seg_00000000.bin"
    assert sentinel.is_file()  # planted

    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(staging))

    # Force finalize() to raise. The import inside _do_browser_finalize
    # picks up our patched module, so we patch the symbol at module-
    # load time by injecting a fake browser_finalize.finalize.
    import browser_finalize
    real_cleanup = browser_finalize.cleanup_staging
    real_error = browser_finalize.BrowserFinalizeError

    def _fail_finalize(_staging, _candidate, **_kwargs):
        raise real_error("simulated mux failure (corrupt segments)")

    monkeypatch.setattr(browser_finalize, "finalize", _fail_finalize)
    # cleanup_staging in the success path stays real; the failure
    # path uses _safe_cleanup_browser_staging in worker.py.

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    assert _read_status(SessionLocal, job_id) == "failed"
    # The Codex regression: staging tree must be GONE.
    assert not staging.exists(), (
        "BrowserFinalizeError path stranded staging dir; the failed "
        "row is excluded from the stale-browser reaper, so this would "
        "leak forever (up to MAX_JOB_STAGING_BYTES per failed job)."
    )
    # And the staging-root parent is intact.
    assert staging_root.is_dir()
    # cleanup function name still resolvable (sanity that monkeypatch
    # didn't blow it away).
    assert real_cleanup is browser_finalize.cleanup_staging


def test_failed_finalize_releases_staging_unexpected_error(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """Same regression on the generic Exception branch."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    job_id = "44444444-uerr-uerr-uerr-444444444444"
    staging = _make_browser_job_dirs(staging_root, job_id)
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(staging))

    import browser_finalize

    def _kaboom(_staging, _candidate, **_kwargs):
        raise RuntimeError("ffmpeg returned non-zero in an unhandled way")

    monkeypatch.setattr(browser_finalize, "finalize", _kaboom)

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    assert _read_status(SessionLocal, job_id) == "failed"
    assert not staging.exists()


def test_cancelled_finalize_keeps_status_cancelled_and_cleans_staging(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """Codex review (P2): when finalize raises BrowserFinalizeCancelled
    (user DELETE'd the job mid-mux), the worker MUST:
      - keep status='cancelled' (NOT clobber to 'failed')
      - unlink the reserved placeholder
      - rmtree the staged segment tree
      - NOT publish the partial MP4 at the user-visible path
    """
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    out_dir = _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    job_id = "44444444-canc-canc-canc-444444444444"
    staging = _make_browser_job_dirs(staging_root, job_id)
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(staging))

    import browser_finalize

    def _user_cancelled(_staging, _candidate, **_kwargs):
        # Simulate the user hitting cancel during the mux poll loop.
        raise browser_finalize.BrowserFinalizeCancelled(
            "Browser finalize cancelled during ffmpeg mux"
        )

    monkeypatch.setattr(browser_finalize, "finalize", _user_cancelled)

    # Pre-CAS: plant the row, then bump it to 'cancelled' to mirror what
    # the cancel endpoint does. In production the CAS in
    # process_browser_finalize would have already flipped pending →
    # processing; we approximate the post-CAS state by setting status
    # to 'processing' first, then to 'cancelled' to mirror the user's
    # DELETE landing while finalize is running.
    db = SessionLocal()
    try:
        db.execute(sa_text(
            "UPDATE jobs SET status='cancelled' WHERE id=:id"
        ), {"id": job_id})
        db.commit()
    finally:
        db.close()

    w = worker_module.DownloadWorker()
    try:
        # process_browser_finalize CAS predicates require a non-cancelled
        # claim state, so bypass it and call _do_browser_finalize
        # directly — this is the path the worker takes once the CAS has
        # already claimed the job. We fake-flip status back so the call
        # progresses to finalize, then the patched finalize raises.
        db = SessionLocal()
        try:
            db.execute(sa_text(
                "UPDATE jobs SET status='processing' WHERE id=:id"
            ), {"id": job_id})
            db.commit()
        finally:
            db.close()

        # Override is_job_cancelled to simulate the DELETE arriving
        # mid-finalize: cancelled when finalize itself queries it, AND
        # cancelled when the post-finalize sanity gate queries it. The
        # patched _user_cancelled above already raises on its own, so
        # the post-finalize gate isn't reached on this path; the
        # important assertion is "status stays cancelled, no MP4
        # published."
        db = SessionLocal()
        try:
            db.execute(sa_text(
                "UPDATE jobs SET status='cancelled' WHERE id=:id"
            ), {"id": job_id})
            db.commit()
        finally:
            db.close()

        w._do_browser_finalize(job_id)
    finally:
        w.db.close()

    # Status preserved.
    assert _read_status(SessionLocal, job_id) == "cancelled"
    # Staging dir wiped.
    assert not staging.exists()
    # No MP4 published anywhere under the output dir.
    mp4s = list(out_dir.glob("*.mp4"))
    assert mp4s == [], f"cancellation must not publish output, got {mp4s}"


def test_cancel_after_publish_before_completed_update_discards_output(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """If DELETE lands after finalize() publishes but before the worker
    commits completed status, the completed update must lose and the
    already-visible MP4 must be removed."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    out_dir = _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    job_id = "44444444-race-race-race-444444444444"
    staging = _make_browser_job_dirs(staging_root, job_id)
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(staging))

    import browser_finalize

    def _publish_success(_staging, candidate, **_kwargs):
        candidate.write_bytes(b"published mp4 bytes")
        return {
            "output_path": str(candidate),
            "file_size": candidate.stat().st_size,
        }

    monkeypatch.setattr(browser_finalize, "finalize", _publish_success)

    def _probe_then_cancel(_output_path):
        db = SessionLocal()
        try:
            db.execute(sa_text(
                "UPDATE jobs SET status='cancelled' WHERE id=:id"
            ), {"id": job_id})
            db.commit()
        finally:
            db.close()
        return 42

    w = worker_module.DownloadWorker()
    monkeypatch.setattr(w, "_probe_duration_seconds", _probe_then_cancel)
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    assert _read_status(SessionLocal, job_id) == "cancelled"
    assert not staging.exists()
    assert list(out_dir.glob("*.mp4")) == []


def test_completed_update_db_error_preserves_output_and_staging(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """If the final completed update errors after publish, do not treat it
    like a cancellation race and delete the user's MP4."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    out_dir = _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    job_id = "44444444-dbxx-dbxx-dbxx-444444444444"
    staging = _make_browser_job_dirs(staging_root, job_id)
    sentinel = staging / "keep-for-retry.txt"
    sentinel.write_text("staging should remain after DB update failure")
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(staging))

    import browser_finalize

    def _publish_success(_staging, candidate, **_kwargs):
        candidate.write_bytes(b"published mp4 bytes")
        return {
            "output_path": str(candidate),
            "file_size": candidate.stat().st_size,
        }

    monkeypatch.setattr(browser_finalize, "finalize", _publish_success)

    w = worker_module.DownloadWorker()
    monkeypatch.setattr(w, "_probe_duration_seconds", lambda _path: 42)
    real_update_job_status = w.update_job_status

    def _completed_update_fails(*args, **kwargs):
        assert args[0] == job_id
        if args[1] == "completed":
            return None
        return real_update_job_status(*args, **kwargs)

    monkeypatch.setattr(w, "update_job_status", _completed_update_fails)
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    assert _read_status(SessionLocal, job_id) == "processing"
    mp4s = list(out_dir.glob("*.mp4"))
    assert len(mp4s) == 1
    assert mp4s[0].read_bytes() == b"published mp4 bytes"
    assert staging.exists()
    assert sentinel.exists()


def test_failed_finalize_refuses_to_rmtree_outside_staging_root(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """Defense in depth: if job_metadata.staging_dir was poisoned
    (manual psql edit, prior bug) and points OUTSIDE the configured
    STAGING_DIR, the cleanup must NOT rmtree it. Mirrors the reapers'
    containment check."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    # OUTSIDE the configured root — must not be wiped.
    outside = tmp_path / "definitely-not-staging"
    outside.mkdir()
    sentinel = outside / "do-not-delete.txt"
    sentinel.write_text("important file the worker must not touch")

    job_id = "44444444-poison-poison-poison-444444444444"
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(outside))

    import browser_finalize

    def _kaboom(_staging, _candidate, **_kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(browser_finalize, "finalize", _kaboom)

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    # Job still flipped to failed.
    assert _read_status(SessionLocal, job_id) == "failed"
    # Outside-root directory + sentinel must still exist.
    assert outside.is_dir()
    assert sentinel.is_file()


def test_safe_cleanup_refuses_sibling_staging_dir(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """A path under STAGING_DIR can still belong to another job; cleanup
    must require the exact STAGING_DIR/<job_id> path."""
    worker_module, _, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))

    job_id = "44444444-good-good-good-444444444444"
    expected = staging_root / job_id
    expected.mkdir()

    sibling = staging_root / "44444444-evil-evil-evil-444444444444"
    sibling.mkdir()
    sentinel = sibling / "do-not-delete.txt"
    sentinel.write_text("belongs to another job")

    worker_module._safe_cleanup_browser_staging(str(sibling), job_id)

    assert sentinel.is_file()
    assert expected.is_dir()


def test_browser_finalize_refuses_outside_staging_before_finalize(
    worker_with_test_db, tmp_path, monkeypatch,
):
    """A poisoned staging_dir must be rejected before finalize reads it."""
    worker_module, SessionLocal, _ = worker_with_test_db
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))
    _setup_outputs_dir(monkeypatch, worker_module, tmp_path)

    outside = tmp_path / "definitely-not-staging-success"
    outside.mkdir()
    sentinel = outside / "do-not-delete.txt"
    sentinel.write_text("important file the worker must not touch")

    job_id = "55555555-poison-poison-poison-555555555555"
    _plant_job(SessionLocal, job_id=job_id, status="pending",
               staging_dir=str(outside))

    import browser_finalize

    finalize_spy = MagicMock(
        side_effect=AssertionError("finalize must not read a poisoned staging_dir")
    )
    monkeypatch.setattr(browser_finalize, "finalize", finalize_spy)

    w = worker_module.DownloadWorker()
    try:
        w.process_browser_finalize(job_id)
    finally:
        w.db.close()

    assert _read_status(SessionLocal, job_id) == "failed"
    finalize_spy.assert_not_called()
    assert outside.is_dir()
    assert sentinel.is_file()


def test_safe_cleanup_browser_staging_unit_happy_path(tmp_path, monkeypatch):
    """Direct unit test for the helper: same-root staging gets wiped."""
    import sys as _sys
    if str(WORKER_DIR) not in _sys.path:
        _sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in _sys.path:
        _sys.path.insert(0, str(DOCKER_ROOT))
    import worker as wm

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))

    sd = staging_root / "job-x"
    sd.mkdir()
    (sd / "data.bin").write_bytes(b"x")
    wm._safe_cleanup_browser_staging(sd, "job-x")
    assert not sd.exists()


def test_safe_cleanup_browser_staging_unit_refuses_outside(tmp_path, monkeypatch):
    """Direct unit test for the containment guard."""
    import sys as _sys
    if str(WORKER_DIR) not in _sys.path:
        _sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in _sys.path:
        _sys.path.insert(0, str(DOCKER_ROOT))
    import worker as wm

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("important")
    wm._safe_cleanup_browser_staging(outside, "job-evil")
    assert outside.is_dir()
    assert (outside / "keep.txt").is_file()


def test_safe_cleanup_browser_staging_unit_no_op_on_missing(tmp_path, monkeypatch):
    """Missing dir is a no-op (no raise) — keeps the failure path
    resilient to a partially-cleaned staging tree."""
    import sys as _sys
    if str(WORKER_DIR) not in _sys.path:
        _sys.path.insert(0, str(WORKER_DIR))
    if str(DOCKER_ROOT) not in _sys.path:
        _sys.path.insert(0, str(DOCKER_ROOT))
    import worker as wm

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    monkeypatch.setenv("STAGING_DIR", str(staging_root))

    # Path under staging_root but doesn't exist on disk.
    wm._safe_cleanup_browser_staging(
        staging_root / "vanished", "job-gone",
    )  # no raise
