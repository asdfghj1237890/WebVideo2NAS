"""Unit tests for the v2.5 browser-side endpoints' validation surface.

End-to-end DB+redis flow is covered by manual verification (Case C/D/E in
the plan) — these tests exercise the path-traversal guards, Pydantic
schema, and helper functions that are independent of the storage layer.
"""

import importlib
from pathlib import Path
from urllib.parse import urlparse

import pytest


def _reload_api_main(monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("API_KEY", "test-key-not-the-default-placeholder")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main as api_main

    return importlib.reload(api_main)


# --- Path-validation helpers -----------------------------------------------


def test_validate_job_id_accepts_uuid(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    api_main._validate_job_id("12345678-1234-1234-1234-1234567890ab")  # no raise


def test_validate_job_id_rejects_traversal(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    for bad in ("..", "../other", "abc", "12345678-1234-1234-1234-1234567890ab/../x", ""):
        with pytest.raises(api_main.HTTPException):
            api_main._validate_job_id(bad)


def test_staging_path_for_keeps_under_root(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    p = api_main._staging_path_for(job_id)
    # Resolved path must be under STAGING_DIR
    assert str(p).startswith(str(tmp_path.resolve()))
    assert p.name == job_id


def test_staging_path_for_canonicalizes_uuid_case(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    p = api_main._staging_path_for("11111111-2222-3333-4444-AAAAAAAAAAAA")
    assert p.name == "11111111-2222-3333-4444-aaaaaaaaaaaa"


def test_segment_path_rejects_invalid_track_or_seq(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path),
                                 MAX_BROWSER_SEGMENTS="100")
    job_id = "11111111-2222-3333-4444-555555555555"
    cases = [
        ("invalid track", "../etc", 0),
        ("negative seq", "video", -1),
        ("seq above cap", "video", 100),
    ]
    for name, track, seq in cases:
        with pytest.raises(api_main.HTTPException) as exc:
            api_main._segment_path(job_id, track, seq)
        assert exc.value.status_code >= 400, name
    # 99 still works
    api_main._segment_path(job_id, "video", 99)


def test_segment_path_zero_padded(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    p = api_main._segment_path(job_id, "video", 7)
    assert p.name == "seg_00000007.bin"
    assert p.parent.name == "video"


# --- Pydantic models -------------------------------------------------------


def test_job_init_request_rejects_invalid_forms(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    cases = [
        ("missing url and manifest_text", {}, "url or manifest_text"),
        ("manifest_text without base_url", {"manifest_text": "#EXTM3U\n"}, "base_url is required"),
    ]
    for _name, kwargs, match in cases:
        with pytest.raises(Exception, match=match):
            api_main.JobInitRequest(**kwargs)


def test_job_init_request_accepts_url_and_text_forms(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    url_form = api_main.JobInitRequest(url="https://example.com/v/playlist.m3u8")
    text_form = api_main.JobInitRequest(
        manifest_text="#EXTM3U\n",
        base_url="https://example.com/v/playlist.m3u8",
    )
    parsed = urlparse(str(url_form.url))
    assert parsed.scheme == "https"
    assert parsed.hostname == "example.com"
    assert text_form.manifest_text.startswith("#EXTM3U")


def test_job_init_request_normalizes_output_subdir(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    r = api_main.JobInitRequest(
        url="https://example.com/v/playlist.m3u8",
        output_subdir="/Anime/Work Safe/",
    )
    assert r.output_subdir == "Anime/Work Safe"


def test_job_init_request_text_size_capped(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    huge = "#EXTM3U\n" + ("# pad\n" * (2 * 1024 * 1024))  # ~12 MB
    with pytest.raises(Exception):
        api_main.JobInitRequest(
            manifest_text=huge,
            base_url="https://example.com/v/playlist.m3u8",
        )


def test_init_hls_parser_rejection_returns_422_not_502(monkeypatch):
    from fastapi.testclient import TestClient

    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    media_text = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""

    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/jobs/init",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={
                "manifest_text": media_text,
                "base_url": "https://cdn.example.com/v/playlist.m3u8",
                "title": "unsupported encryption",
            },
        )

    assert resp.status_code == 422
    assert "Manifest plan failed" in resp.text
    assert "Unsupported HLS encryption" in resp.text


# --- Codex review fix #1: finalize completeness check -----------------------
#
# Earlier finalize would enqueue the job whether or not all segments had
# arrived; combined with the worker's filename-based count, a
# premature/concurrent finalize could let the worker concatenate truncated
# bytes from a still-uploading segment. _verify_staging_complete reads the
# staging manifest and refuses to proceed when any expected segment is
# missing or only present as a `.part` file (atomic upload not finished).


import json


def _write_plan(staging_root, plan):
    staging_root.mkdir(parents=True, exist_ok=True)
    (staging_root / "manifest.json").write_text(json.dumps(plan), encoding="utf-8")


def test_verify_staging_complete_passes_when_all_present(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 3}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    for i in range(3):
        (staging / "video" / f"seg_{i:08d}.bin").write_bytes(b"x")
    summary = api_main._verify_staging_complete(staging)
    assert summary == {"video": 3}


def test_verify_staging_complete_rejects_missing_segments(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 5}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    # Only segments 0 and 2 present; 1, 3, 4 missing.
    for i in (0, 2):
        (staging / "video" / f"seg_{i:08d}.bin").write_bytes(b"x")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "missing" in detail
    assert detail["missing"]["video"] == [1, 3, 4]


def test_verify_staging_complete_rejects_malformed_segment_names(monkeypatch, tmp_path):
    """Non-canonical seg_*.bin names should be a clean 409, not an
    int() ValueError escaping as a generic 500 from /finalize."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 1}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x")
    (staging / "video" / "seg_bad.bin").write_bytes(b"stray")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    assert exc.value.detail["bad_segment_names"] == {"video": ["seg_bad.bin"]}


def test_verify_staging_complete_treats_part_file_as_in_flight(monkeypatch, tmp_path):
    """Atomic upload writes <seg>.bin.part during transfer and renames on
    completion. Codex review #6: a `.part` on disk is a hot signal that
    an upload is mid-stream; verify must hard-fail (NOT route through the
    'missing segment' path which would let the user think a retry will
    eventually plug a stable hole). The 'in_flight_partial_files' detail
    tells callers to retry once uploads drain."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 2}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"complete")
    # seg 1 is mid-upload (.part on disk, no final .bin yet).
    (staging / "video" / "seg_00000001.bin.part").write_bytes(b"partial")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "in_flight_partial_files" in detail
    assert any("seg_00000001.bin.part" in name for name in detail["in_flight_partial_files"])


def test_verify_staging_complete_checks_init_segment(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "is_fmp4": True,
        "tracks": {"video": {
            "segment_count": 1,
            "init_segment_url": "https://example.com/init.mp4",
        }},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x")
    # Init segment is missing from /init/video.bin.

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    assert "video:init" in exc.value.detail["missing"]


def test_verify_staging_complete_truncates_long_missing_lists(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 100}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    # No segments uploaded; all 100 missing.

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    missing = exc.value.detail["missing"]["video"]
    # Capped at 20 entries + a "..." sentinel.
    assert len(missing) == 21
    assert missing[-1] == "..."


def test_verify_staging_complete_dash_two_tracks(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "dash",
        "tracks": {
            "video": {"segment_count": 2},
            "audio": {"segment_count": 2},
        },
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "audio").mkdir()
    for i in range(2):
        (staging / "video" / f"seg_{i:08d}.bin").write_bytes(b"v")
    # Audio: only one of two present.
    (staging / "audio" / "seg_00000000.bin").write_bytes(b"a")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.detail["missing"] == {"audio": [1]}
    # Video count came back successfully.
    assert exc.value.detail["received"]["video"] == 2


# --- Codex review fix #2: redis-push-before-DB-commit retry safety -----------
#
# The previous code committed `jobs.status = 'pending'` first and then
# called redis_client.rpush — if rpush failed, the DB row was already in
# 'pending' state, future retries hit the CAS gate (status NOT IN
# (browser_pending, browser_uploading)), and the queue was never pushed.
# Result: completed staging stranded forever. Fix: push first, commit
# second; rpush failure leaves DB unchanged so the user can retry.

from datetime import UTC, datetime
from unittest.mock import MagicMock
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.pool import StaticPool


def _utcnow_naive():
    return datetime.now(UTC).replace(tzinfo=None)


def _build_finalize_test_env(monkeypatch, tmp_path, *, rpush_fails=False, db_commit_fails=False):
    """Spin up an in-memory sqlite DB with the minimum schema finalize
    needs, plus a mocked redis_client. Returns (api_main, job_id).

    StaticPool is required so every SessionLocal() call shares the same
    in-memory DB — without it sqlalchemy gives each session a fresh
    connection (and a fresh empty in-memory DB), so tables created in
    setup are invisible to the FastAPI request handler."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    # Replace the real engine + SessionLocal with a fresh in-memory sqlite
    # that all connections share via StaticPool.
    test_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    api_main.engine = test_engine
    from sqlalchemy.orm import sessionmaker
    api_main.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    with test_engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, url TEXT, title TEXT, status TEXT,
                progress INTEGER, created_at TIMESTAMP,
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

    # Plant a job in browser_uploading state with 1 segment expected.
    job_id = "22222222-3333-4444-5555-666666666666"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x")
    plan = {"container": "hls", "tracks": {"video": {"segment_count": 1}}}
    (staging / "manifest.json").write_text(json.dumps(plan))

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text(
            "INSERT INTO jobs (id, url, title, status, progress, created_at) "
            "VALUES (:id, :url, :title, 'browser_uploading', 0, :now)"
        ), {"id": job_id, "url": "https://x", "title": "t", "now": _utcnow_naive()})
        db.execute(sa_text(
            "INSERT INTO job_metadata (job_id, mode, total_segments, staging_dir) "
            "VALUES (:id, 'browser', 1, :sd)"
        ), {"id": job_id, "sd": str(staging)})
        db.commit()
    finally:
        db.close()

    # Mock redis_client to record the call order; optionally make rpush raise.
    api_main.redis_client = MagicMock()
    if rpush_fails:
        api_main.redis_client.rpush = MagicMock(side_effect=RuntimeError("redis down"))
    else:
        api_main.redis_client.rpush = MagicMock(return_value=1)

    # Optionally make the DB commit fail. We do this by patching the
    # session.commit just before finalize is called — caller decides.
    if db_commit_fails:
        # We need to wrap SessionLocal so the finalize endpoint's session
        # has commit raise after the UPDATE.
        original_session_local = api_main.SessionLocal

        class FailingSession(original_session_local.__class__):
            pass

        commit_called = {"n": 0}

        def make_session():
            sess = original_session_local()
            real_commit = sess.commit

            def commit_with_failure():
                commit_called["n"] += 1
                # Fail only on the SECOND commit (the one inside finalize
                # post-UPDATE — the first is the rate-limit / setup pass).
                if commit_called["n"] >= 2:
                    raise RuntimeError("DB commit failed (simulated)")
                return real_commit()
            sess.commit = commit_with_failure
            return sess
        api_main.SessionLocal = make_session

    return api_main, job_id


def _read_job_status(api_main, job_id):
    db = api_main.SessionLocal()
    try:
        row = db.execute(sa_text("SELECT status FROM jobs WHERE id = :id"),
                         {"id": job_id}).first()
        return row.status if row else None
    finally:
        db.close()


def test_finalize_rpush_failure_leaves_job_at_browser_finalizing(monkeypatch, tmp_path):
    """Codex review #6: rpush failure must leave the job at
    'browser_finalizing' so a retry resumes from the same state machine
    point (skip CAS, redo verify + rpush + commit). Status will NOT be
    'browser_uploading' anymore because the pre-finalize CAS happens
    BEFORE rpush — that's the whole point of the new state, locking out
    new uploads while finalize is in progress."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path, rpush_fails=True)

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 500
    assert "Queue push failed" in resp.json()["detail"]
    # Status MUST be 'browser_finalizing' — uploads are locked out, and
    # a retry resumes verify + rpush + commit from this state.
    assert _read_job_status(api_main, job_id) == "browser_finalizing"
    api_main.redis_client.rpush.assert_called_once()


def test_finalize_retry_from_browser_finalizing_succeeds(monkeypatch, tmp_path):
    """After a transient rpush failure leaves status='browser_finalizing',
    a retry must resume the rpush+commit path without erroring out the
    CAS that already moved the row out of browser_uploading."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    # Manually set the status to browser_finalizing as if a previous
    # finalize attempt got past CAS but failed before commit.
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='browser_finalizing' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    # Successful retry: status flipped to 'pending' and rpush ran.
    assert _read_job_status(api_main, job_id) == "pending"
    api_main.redis_client.rpush.assert_called_once_with("browser_finalize_queue", job_id)


def test_finalize_reports_cancel_if_delete_wins_after_enqueue(monkeypatch, tmp_path):
    """If cancel flips browser_finalizing -> cancelled after rpush but
    before the final pending update, finalize must not return success."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    def cancel_during_rpush(*_args, **_kwargs):
        db = api_main.SessionLocal()
        try:
            db.execute(sa_text("UPDATE jobs SET status='cancelled' WHERE id=:id"),
                       {"id": job_id})
            db.commit()
        finally:
            db.close()
        return 1

    api_main.redis_client.rpush = MagicMock(side_effect=cancel_during_rpush)

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )

    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"]
    assert _read_job_status(api_main, job_id) == "cancelled"


def test_finalize_idempotent_on_already_pending(monkeypatch, tmp_path):
    """If the job is already 'pending' (e.g. a previous finalize fully
    succeeded but the response was lost), a retry must not error or
    double-enqueue — return idempotent success."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='pending' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "pending"
    # Idempotent: rpush NOT called, no double-enqueue.
    api_main.redis_client.rpush.assert_not_called()


# --- Codex review #9: SSRF guard on browser-side plan URLs --------
#
# A hostile or compromised manifest can list segments at private IPs
# (intranet routers, cloud metadata, internal services) — without
# always-on validation, the extension's credentialed cross-origin
# fetches plus DNR CORS-relax become a data-exfiltration channel.
# `_enforce_plan_url_safety` walks every URL in the plan and rejects
# anything resolving to a non-public address.


def _browser_plan(segment_url="https://8.8.8.8/seg.ts", *,
                  init_segment_url=None, track_init_segment_url=None,
                  key_uri=None):
    segment = {"url": segment_url}
    if key_uri is not None:
        segment["key"] = {"uri": key_uri}
    plan = {"tracks": {"video": {"segments": [segment]}}}
    if init_segment_url is not None:
        plan["init_segment_url"] = init_segment_url
    if track_init_segment_url is not None:
        plan["tracks"]["video"]["init_segment_url"] = track_init_segment_url
    return plan


def test_plan_url_safety_accepts_public_origins(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    plan = {
        "init_segment_url": "https://cdn.example.com/init.mp4",
        "tracks": {
            "video": {
                "init_segment_url": "https://cdn.example.com/video/init.mp4",
                "segments": [
                    {"url": "https://cdn.example.com/video/seg0.ts"},
                    {"url": "https://cdn.example.com/video/seg1.ts",
                     "key": {"uri": "https://auth.example.com/keys/abc"}},
                ],
            },
        },
    }
    # Should not raise — both example.com hosts resolve publicly.
    # (DNS resolution is real; if the env can't reach DNS, this skips.)
    try:
        api_main._enforce_plan_url_safety(plan)
    except api_main.HTTPException as e:
        # If DNS is unavailable in the test env, getaddrinfo throws
        # and we get a 422. Accept that as "test environment can't
        # validate"; assert it's NOT a private-IP rejection.
        assert "non-public IP" not in str(e.detail)


def test_plan_url_safety_rejects_unsafe_plan_urls(monkeypatch):
    """Table of the URL safety guard's high-value rejection branches.

    It covers private/link-local targets, non-http(s) schemes, and the
    browser-time DNS rebinding issue where every plan URL must be HTTPS.
    """
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    cases = [
        (
            "localhost name",
            _browser_plan("https://localhost:8080/admin/seg0.ts"),
            ("localhost",),
        ),
        (
            "private literal IP",
            _browser_plan("https://192.168.1.1/router-admin/page.bin"),
            ("non-public",),
        ),
        *(
            (f"RFC1918 {private}", _browser_plan(f"https://{private}/seg.ts"), ())
            for private in ("10.0.0.1", "172.16.0.1", "192.168.5.5")
        ),
        (
            "link-local metadata IP",
            _browser_plan("https://169.254.169.254/latest/meta-data/iam/"),
            (),
        ),
        (
            "non-http scheme",
            _browser_plan("file:///etc/passwd"),
            ("scheme",),
        ),
        (
            "private IP in AES key URI",
            _browser_plan(key_uri="https://10.0.0.1/leak"),
            (),
        ),
        (
            "HTTP segment on public host",
            _browser_plan("http://8.8.8.8/seg.ts"),
            ("https", "rebinding"),
        ),
        (
            "HTTP plan-level init segment",
            _browser_plan(init_segment_url="http://cdn.example.com/init.mp4"),
            ("https",),
        ),
        (
            "HTTP track init segment",
            _browser_plan(track_init_segment_url="http://cdn.example.com/v/init.mp4"),
            ("https",),
        ),
        (
            "HTTP key URI",
            _browser_plan(key_uri="http://keys.example.com/k1"),
            ("https",),
        ),
    ]

    for name, plan, detail_parts in cases:
        with pytest.raises(api_main.HTTPException) as exc:
            api_main._enforce_plan_url_safety(plan)
        assert exc.value.status_code == 422, name
        detail = str(exc.value.detail).lower()
        for part in detail_parts:
            assert part in detail, f"{name}: {detail}"


def test_plan_url_safety_accepts_all_https(monkeypatch):
    """Sanity: a fully-HTTPS plan with public-resolving hosts is
    accepted (or fails only because DNS is unavailable in CI)."""
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    plan = {
        "init_segment_url": "https://cdn.example.com/init.mp4",
        "tracks": {
            "video": {
                "init_segment_url": "https://cdn.example.com/v/init.mp4",
                "segments": [{
                    "url": "https://cdn.example.com/v/seg.ts",
                    "key": {"uri": "https://keys.example.com/k1"},
                }],
            },
        },
    }
    try:
        api_main._enforce_plan_url_safety(plan)
    except api_main.HTTPException as e:
        # No-DNS test envs raise on resolve; that's fine. We only
        # need to verify HTTP-rejection isn't false-firing on HTTPS.
        assert "https" not in str(e.detail).lower() or "rebinding" not in str(e.detail).lower()


# --- Codex review #9: per-job upload slot + reserved-bytes quota -----


def test_claim_release_upload_slot_round_trip(monkeypatch):
    """Each claim increments; release decrements. Slot counts persist
    until the TTL expires — the test mock implements INCR/DECR
    atomically using a dict so we can verify the contract."""
    from collections import defaultdict
    api_main = _reload_api_main(monkeypatch)

    counts = defaultdict(int)
    expirations = {}

    class FakeRedis:
        def incr(self, key):
            counts[key] += 1
            return counts[key]
        def decr(self, key):
            counts[key] -= 1
            return counts[key]
        def expire(self, key, ttl):
            expirations[key] = ttl

    api_main.redis_client = FakeRedis()

    a = api_main._claim_upload_slot("job-A")
    b = api_main._claim_upload_slot("job-A")
    c = api_main._claim_upload_slot("job-A")
    assert a == 1 and b == 2 and c == 3
    api_main._release_upload_slot("job-A")
    api_main._release_upload_slot("job-A")
    assert counts["wv2nas:upload_slots:job-A"] == 1


def test_existing_segment_retry_bypasses_quota_gate(monkeypatch, tmp_path):
    """A retry for an already-published segment must return idempotent
    success before the reserved-bytes gate can reject a near-cap job, while
    still counting against the per-job upload slot cap."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from types import SimpleNamespace

    job_id = "11111111-2222-3333-4444-555555555555"
    target = api_main._segment_path(job_id, "video", 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PRIOR-COMMIT")

    meta = SimpleNamespace(status="browser_uploading", total_segments=1)
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda _db, _job_id: meta)
    claimed = []
    released = []
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id: claimed.append(_job_id) or 1)
    monkeypatch.setattr(api_main, "_release_upload_slot", lambda _job_id: released.append(_job_id))
    monkeypatch.setattr(
        api_main,
        "_staged_bytes_get",
        lambda *_args, **_kwargs: pytest.fail("idempotent retry should not read quota"),
    )

    class _FakeRequest:
        async def stream(self):
            yield b"DIFFERENT-RETRY-BYTES"

    result = asyncio.run(api_main.upload_segment(
        job_id, 0, _FakeRequest(), track="video", db=object(), api_key="x",
    ))

    assert result["idempotent"] is True
    assert target.read_bytes() == b"PRIOR-COMMIT"
    assert claimed == [job_id]
    assert released == [job_id]


def test_existing_init_retry_bypasses_quota_gate(monkeypatch, tmp_path):
    """Init segment retries need the same idempotent-before-quota path
    as media segments; otherwise DASH/fMP4 jobs near cap are unretryable.
    They still consume an upload slot so duplicate bodies cannot fan out
    without the concurrency cap."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from types import SimpleNamespace

    job_id = "11111111-2222-3333-4444-555555555555"
    target = api_main._init_segment_path(job_id, "video")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PRIOR-INIT")

    meta = SimpleNamespace(status="browser_uploading", total_segments=1)
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda _db, _job_id: meta)
    claimed = []
    released = []
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id: claimed.append(_job_id) or 1)
    monkeypatch.setattr(api_main, "_release_upload_slot", lambda _job_id: released.append(_job_id))
    monkeypatch.setattr(
        api_main,
        "_staged_bytes_get",
        lambda *_args, **_kwargs: pytest.fail("idempotent retry should not read quota"),
    )

    class _FakeRequest:
        async def stream(self):
            yield b"DIFFERENT-INIT-RETRY"

    result = asyncio.run(api_main.upload_init_segment(
        job_id, _FakeRequest(), track="video", db=object(), api_key="x",
    ))

    assert result["idempotent"] is True
    assert target.read_bytes() == b"PRIOR-INIT"
    assert claimed == [job_id]
    assert released == [job_id]


# --- Codex review (P2): O(1) staged-bytes counter --------------------
#
# The PUT quota gate previously called _staging_total_bytes() (rglob +
# stat) on every segment upload, making per-PUT cost O(N_already_staged)
# and per-job total O(N²). For 21,600-segment playlists this turns
# uploads into hundreds of millions of stat() calls. The fix maintains
# a redis-backed counter that's INCRBY'd after each successful publish
# and read in O(1) by the gate.


def test_staged_bytes_counter_avoids_per_put_walk(monkeypatch, tmp_path):
    """Cached counter satisfies _staged_bytes_get without touching the
    filesystem. Verifies the hot path is O(1)."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    cache = {}

    class FakeRedis:
        def get(self, key):
            return cache.get(key)
        def set(self, key, value, ex=None):
            cache[key] = str(value)
        def incrby(self, key, n):
            cache[key] = str(int(cache.get(key, "0")) + int(n))
            return int(cache[key])
        def expire(self, key, ttl):
            pass
        def delete(self, key):
            cache.pop(key, None)

    api_main.redis_client = FakeRedis()

    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id

    # First read with empty staging seeds the counter at 0.
    assert api_main._staged_bytes_get(job_id, staging) == 0
    # Subsequent record + read returns from cache without walking.
    api_main._staged_bytes_record(job_id, 1024)
    api_main._staged_bytes_record(job_id, 2048)
    # Even if a stray file appears on disk, the cached counter is
    # authoritative — proving we did NOT rescan the tree.
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "stray.bin").write_bytes(b"x" * 999_999)
    assert api_main._staged_bytes_get(job_id, staging) == 1024 + 2048


def test_staged_bytes_counter_seeds_on_miss_then_caches(monkeypatch, tmp_path):
    """Counter loss (TTL expiry, redis flush) → next read walks once,
    seeds the cache, and subsequent reads stay O(1)."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    cache = {}
    set_calls = []

    class FakeRedis:
        def get(self, key):
            return cache.get(key)
        def set(self, key, value, ex=None):
            cache[key] = str(value)
            set_calls.append((key, int(value), ex))
        def incrby(self, key, n):
            cache[key] = str(int(cache.get(key, "0")) + int(n))
            return int(cache[key])
        def expire(self, key, ttl):
            pass
        def delete(self, key):
            cache.pop(key, None)

    api_main.redis_client = FakeRedis()

    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    (staging / "video").mkdir(parents=True)
    (staging / "video" / "seg_00000000.bin").write_bytes(b"a" * 100)
    (staging / "video" / "seg_00000001.bin").write_bytes(b"b" * 250)

    # Cache cold → walk seeds 350.
    assert api_main._staged_bytes_get(job_id, staging) == 350
    assert len(set_calls) == 1 and set_calls[0][1] == 350
    # Cache warm → no further set.
    assert api_main._staged_bytes_get(job_id, staging) == 350
    assert len(set_calls) == 1


def test_staged_bytes_counter_falls_back_to_walk_on_redis_failure(monkeypatch, tmp_path):
    """Redis read failure → degrade to legacy walk, NOT fail-closed.
    Slot/reserved-bytes gate is the primary defense; bytes-on-disk is
    defense-in-depth."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis down")
        def set(self, key, value, ex=None):
            raise RuntimeError("redis down")
        def incrby(self, key, n):
            raise RuntimeError("redis down")
        def expire(self, key, ttl):
            raise RuntimeError("redis down")
        def delete(self, key):
            raise RuntimeError("redis down")

    api_main.redis_client = BrokenRedis()

    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    (staging / "video").mkdir(parents=True)
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x" * 42)

    assert api_main._staged_bytes_get(job_id, staging) == 42
    # Record + clear are best-effort; must not raise.
    api_main._staged_bytes_record(job_id, 99)
    api_main._staged_bytes_clear(job_id)


# --- Codex review #10: per-track seq bounds ---------------------------
#
# The legacy `seq >= total_segments` check was per-job (video+audio sum
# for DASH). An extra audio-track upload at seq=2 on a 2-segment audio
# track but 4 total_segments would land successfully, then wedge the
# worker at finalize time when _segment_files counts files != expected.


def test_expected_segment_count_for_track_reads_plan(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    plan = {
        "container": "dash",
        "tracks": {
            "video": {"segment_count": 5},
            "audio": {"segment_count": 2},
        },
    }
    (staging / "manifest.json").write_text(json.dumps(plan))
    assert api_main._expected_segment_count_for_track(staging, "video") == 5
    assert api_main._expected_segment_count_for_track(staging, "audio") == 2
    # Unknown track or missing plan: returns None (caller falls back).
    assert api_main._expected_segment_count_for_track(staging, "subtitle") is None
    bogus = tmp_path / "no-plan-here"
    bogus.mkdir()
    assert api_main._expected_segment_count_for_track(bogus, "video") is None


def test_verify_staging_complete_rejects_unexpected_segments(monkeypatch, tmp_path):
    """Codex #10: an extra seg_*.bin file beyond the plan's count must
    fail finalize loudly here, not silently let the worker fail later
    after enqueue."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 2}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    # Plan says 2; client uploaded 3.
    for i in range(3):
        (staging / "video" / f"seg_{i:08d}.bin").write_bytes(b"x")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "unexpected" in detail
    assert detail["unexpected"]["video"] == [2]


def test_verify_staging_complete_dash_unexpected_audio_seq(monkeypatch, tmp_path):
    """The exact Codex #10 scenario: DASH job with total_segments=4
    (video=2 + audio=2). Pre-fix, an extra audio seq=2 PUT would land,
    and only finalize would catch it via worker failure. Now the verify
    rejects it before the queue push."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "dash",
        "tracks": {
            "video": {"segment_count": 2},
            "audio": {"segment_count": 2},
        },
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "audio").mkdir()
    for i in range(2):
        (staging / "video" / f"seg_{i:08d}.bin").write_bytes(b"v")
        (staging / "audio" / f"seg_{i:08d}.bin").write_bytes(b"a")
    # The bad file: extra audio seq 2.
    (staging / "audio" / "seg_00000002.bin").write_bytes(b"extra")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    detail = exc.value.detail
    assert detail["unexpected"]["audio"] == [2]


# --- Codex review #10: staging cleanup on DB-insert failure ----------
#
# init_browser_job creates staging tree + manifest BEFORE the DB insert.
# A DB outage at insert time used to leak the directory forever (no row
# for the stale-browser reaper to find). Now we best-effort rmtree on
# any DB exception, guarded by the same STAGING_DIR containment check.


def test_init_cleans_staging_on_db_insert_failure(monkeypatch, tmp_path):
    """Force the second INSERT to raise; assert the staging tree is
    wiped on the way out so retries during a DB outage don't accumulate
    orphans under /downloads/.staging."""
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path), SSRF_GUARD="false",
                                 API_KEY="test-key-not-the-default-placeholder")

    # Set up an in-memory sqlite with the jobs table but NO job_metadata
    # table, so the second INSERT inside init_browser_job raises and
    # the handler hits the cleanup path.
    test_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    api_main.engine = test_engine
    from sqlalchemy.orm import sessionmaker
    api_main.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    with test_engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, url TEXT, title TEXT, status TEXT,
                progress INTEGER, created_at TIMESTAMP
            )
        """))
        # job_metadata intentionally missing — second INSERT will fail.

    api_main.redis_client = MagicMock()
    api_main.redis_client.lrange = MagicMock(return_value=[])

    # Bypass DNS resolution (test env may have no DNS). Patch the
    # resolver to return a public IP so the always-on SSRF guard
    # passes and execution reaches the DB insert path we want to
    # exercise. 8.8.8.8 (Google DNS) is a real public IP — TEST-NET-3
    # ranges are flagged as is_reserved=True by ipaddress and would
    # be rejected by _is_ip_public.
    import ipaddress as _ip
    api_main._resolve_host_ips = lambda host: [_ip.ip_address("8.8.8.8")]

    # Capture which staging dirs existed during the request.
    media_text = "#EXTM3U\n#EXTINF:10\nseg.ts\n"
    base_url = "https://cdn.example.com/v/playlist.m3u8"

    pre_existing = set(p.name for p in tmp_path.iterdir())

    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/jobs/init",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={
                "manifest_text": media_text,
                "base_url": base_url,
                "title": "doomed",
            },
        )
    # The DB insert failed → 500.
    assert resp.status_code == 500, f"unexpected status {resp.status_code}: {resp.text}"

    # Staging tree must NOT linger. Compare directory contents before and
    # after — no NEW per-job dirs left behind.
    leftover = set(p.name for p in tmp_path.iterdir() if p.is_dir()) - pre_existing
    assert leftover == set(), (
        f"init_browser_job leaked staging dirs after DB failure: {leftover}"
    )


def test_init_cleans_staging_on_allocation_write_failure(monkeypatch, tmp_path):
    """If manifest.json write fails after mkdir succeeded, no DB row exists
    for the reapers, so init must clean the fresh staging dir itself."""
    from fastapi.testclient import TestClient
    import builtins as _builtins
    import ipaddress as _ip

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        SSRF_GUARD="false",
        API_KEY="test-key-not-the-default-placeholder",
    )
    api_main._resolve_host_ips = lambda host: [_ip.ip_address("8.8.8.8")]

    real_open = _builtins.open

    def flaky_open(path, mode="r", *args, **kwargs):
        if str(path).endswith("manifest.json") and "w" in mode:
            raise OSError("simulated manifest write failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(_builtins, "open", flaky_open)
    pre_existing = set(p.name for p in tmp_path.iterdir())

    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/jobs/init",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={
                "manifest_text": "#EXTM3U\n#EXTINF:10\nseg.ts\n",
                "base_url": "https://cdn.example.com/v/playlist.m3u8",
                "title": "allocation-fails",
            },
        )

    assert resp.status_code == 500
    leftover = set(p.name for p in tmp_path.iterdir() if p.is_dir()) - pre_existing
    assert leftover == set(), (
        f"init_browser_job leaked staging dirs after allocation failure: {leftover}"
    )


# --- Codex review #12: per-attempt unique temp filename ---
#
# Two concurrent PUTs for the same (job_id, track, seq) — possible
# from a client-side timeout/retry while the original is still
# streaming — must not share a `.part` file. Sharing leads to
# interleaved bytes and a corrupt segment that finalize accepts
# (count check passes) but produces broken MP4.


# --- Codex review #13: idempotent retries -----------------------------
#
# Scenario: client PUTs segment, server commits (.part-then-replace),
# client times out before seeing 200 → retries the whole fetch+decrypt+
# upload closure. If the retry's bytes differ (token expiry returned
# garbage, signed-URL changed, AES key rotated, etc.), os.replace would
# silently swap the GOOD prior-commit bytes for the BAD retry bytes.
# Finalize's count-only check passes; user gets a corrupt MP4.
#
# Fix: if the final segment file already exists non-empty, the retry
# returns idempotent success WITHOUT overwriting. The body is drained
# so HTTP/1.1 connections aren't wedged.


def test_segment_upload_idempotent_when_target_exists(monkeypatch, tmp_path):
    """The Codex regression: a retry with DIFFERENT bytes must NOT
    replace the prior good commit."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Plant the prior good commit.
    target.write_bytes(b"GOOD-PRIOR-COMMIT-DATA")
    # Simulate a crash/unlink failure after the prior commit published
    # target but before its sibling .part was removed.
    stale_part = target.with_name(f"{target.name}.stale.part")
    stale_part.write_bytes(b"leftover temp bytes")

    class _FakeRequest:
        def __init__(self, body=b""):
            self._body = body
        async def stream(self):
            # Yield in chunks so drain logic exercises a real loop.
            for i in range(0, len(self._body), 1024):
                yield self._body[i:i + 1024]

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    # Retry sends DIFFERENT (corrupted) bytes.
    bad_bytes = b"DIFFERENT-RETRY-BYTES-WOULD-CORRUPT" + b"x" * 4096
    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(bad_bytes),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))

    # Endpoint returned idempotent success.
    assert result["idempotent"] is True
    assert result["received"] == 0
    # Original bytes preserved — retry's bad bytes did NOT replace.
    assert target.read_bytes() == b"GOOD-PRIOR-COMMIT-DATA"
    # No leftover .part files (no token-suffixed temp files).
    leftovers = [p for p in target.parent.iterdir() if ".part" in p.name]
    assert leftovers == [], f"expected no .part leftovers, got {leftovers}"


def test_segment_upload_proceeds_when_target_absent(monkeypatch, tmp_path):
    """Verify the idempotency check doesn't false-positive — first
    PUT (no prior file) MUST go through the normal stream + replace
    path."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    # No prior file.

    class _FakeRequest:
        def __init__(self, body=b""):
            self._body = body
        async def stream(self):
            yield self._body

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    fresh_bytes = b"FRESH-FIRST-PUT-BYTES"
    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(fresh_bytes),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))

    # Normal success — bytes streamed and committed.
    assert result.get("idempotent") is None or result.get("idempotent") is False
    assert result["received"] == len(fresh_bytes)
    assert target.read_bytes() == fresh_bytes


def test_segment_upload_overwrites_zero_byte_target(monkeypatch, tmp_path):
    """A zero-byte file at the final path is treated as 'no prior
    commit' — atomic flow shouldn't produce these but disk-full /
    weird FS edge cases could. Allow retry to actually write."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Plant zero-byte placeholder (e.g. from a crashed prior attempt).
    target.write_bytes(b"")

    class _FakeRequest:
        async def stream(self):
            yield b"actual-content"

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))

    # Zero-byte file was NOT treated as a prior commit; retry wrote.
    assert result.get("idempotent") is None or result.get("idempotent") is False
    assert target.read_bytes() == b"actual-content"


def test_verify_rejects_zero_byte_segment_files(monkeypatch, tmp_path):
    """Defense in depth (Codex #13): if a 0-byte seg_*.bin slips into
    staging (atomic flow shouldn't allow it but covers FS-edge cases),
    verify must reject before enqueueing finalize so the worker doesn't
    hit ffmpeg with empty-file mid-mux."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 2}},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"good")
    # Zero-byte segment.
    (staging / "video" / "seg_00000001.bin").write_bytes(b"")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "zero_byte" in detail
    assert detail["zero_byte"]["video"] == [1]


def test_verify_ignores_recoverable_stale_parts_after_publish(monkeypatch, tmp_path):
    """Committed seg/init targets with leftover sibling .part files should
    not permanently block finalize."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "hls",
        "tracks": {
            "video": {
                "segment_count": 1,
                "init_segment_url": "https://cdn.example.com/init.mp4",
            },
        },
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "init").mkdir()
    seg = staging / "video" / "seg_00000000.bin"
    init = staging / "init" / "video.bin"
    seg.write_bytes(b"committed-segment")
    init.write_bytes(b"committed-init")
    seg_part = seg.with_name(f"{seg.name}.stale.part")
    init_part = init.with_name(f"{init.name}.stale.part")
    seg_part.write_bytes(b"leftover segment part")
    init_part.write_bytes(b"leftover init part")

    assert api_main._verify_staging_complete(staging) == {"video": 1}
    assert not seg_part.exists()
    assert not init_part.exists()


# Codex review #17: even with unique per-attempt .part filenames
# (Codex #12), if both concurrent PUTs see the target absent at
# request start, both pass the start-of-request idempotency check
# (Codex #13) and stream their bytes. The atomic publish step (now
# os.link-based instead of os.replace) ensures only one wins —
# the loser sees FileExistsError and discards its bytes. This
# closes the gap where a stale/expired retry could carry different
# bytes that overwrite a valid first commit.


def test_concurrent_retry_preserves_first_commit_via_atomic_link(monkeypatch, tmp_path):
    """Two PUTs racing for the same (job, track, seq) — first one
    publishes via os.link, second one's link fails with EEXIST →
    discards bytes → first commit's content preserved."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        def __init__(self, body):
            self._body = body
        async def stream(self):
            yield self._body

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    # First PUT — target absent at start, publishes successfully.
    first_bytes = b"FIRST-COMMIT-BYTES"
    r1 = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(first_bytes),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))
    assert r1.get("idempotent") is None or r1.get("idempotent") is False
    assert target.read_bytes() == first_bytes

    # Second PUT (stale retry) — target exists with non-zero size
    # at request start → start-of-request idempotency check
    # short-circuits → return idempotent (drained).
    bad_bytes = b"DIFFERENT-RETRY-BYTES-WOULD-CORRUPT" * 8
    r2 = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(bad_bytes),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))
    assert r2.get("idempotent") is True
    assert target.read_bytes() == first_bytes  # PRESERVED


def test_concurrent_link_race_loser_returns_idempotent_concurrent(monkeypatch, tmp_path):
    """Simulate the link race directly: target was absent at start
    (idempotency check passed), but ANOTHER writer published in
    between. Our os.link must fail with EEXIST and return
    idempotent_concurrent=True, NOT overwrite."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        def __init__(self, body):
            self._body = body
        async def stream(self):
            # While the body is streaming, simulate a concurrent
            # writer publishing to the target. After this generator
            # yields, the streaming code reaches the os.link step
            # and finds the target already exists.
            yield self._body
            # Plant the "first commit" right before the link attempt.
            target.write_bytes(b"FIRST-COMMIT")

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    bad_bytes = b"OUR-RETRY-BYTES" * 4
    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(bad_bytes),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))

    # Lost the race — should report idempotent_concurrent.
    assert result.get("idempotent_concurrent") is True
    # First commit preserved.
    assert target.read_bytes() == b"FIRST-COMMIT"


def test_concurrent_uploads_use_unique_temp_paths(monkeypatch, tmp_path):
    """Inspect _stream_segment_to_disk's choice of part_target across
    concurrent invocations. Each attempt MUST get a distinct temp
    filename so writes don't interleave."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    # We can't easily run the FastAPI endpoint with two true-concurrent
    # streams in pytest, but we can run _stream_segment_to_disk twice
    # back-to-back with controlled streams and verify the temp paths
    # they chose were different. Capture them via a fake file open.
    import asyncio
    from unittest.mock import MagicMock as _MM

    captured_part_paths: list = []

    real_open = open

    def tracking_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".part"):
            captured_part_paths.append(str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)

    # Set up minimal env: a target path under tmp_path, mock request
    # and meta, mock db.
    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        def __init__(self, body=b""):
            self._body = body
        async def stream(self):
            yield self._body

    class _FakeRow:
        status = "browser_uploading"

    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    meta = _MM()
    meta.status = "browser_uploading"

    async def run_one():
        await api_main._stream_segment_to_disk(
            request=_FakeRequest(b"hello"),
            db=db, meta=meta,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video", seq=0,
            target=target,
        )

    # Run two streams sequentially — they should still use distinct
    # temp paths even though the second one "wins" the os.replace.
    asyncio.run(run_one())
    target.unlink(missing_ok=True)  # reset for the second run
    asyncio.run(run_one())

    # Both runs created a unique .part path.
    assert len(captured_part_paths) >= 2
    distinct = set(captured_part_paths)
    assert len(distinct) == len(captured_part_paths), (
        f"Expected unique .part paths per attempt, got duplicates: "
        f"{captured_part_paths}"
    )
    # All ended in `.part` and contained a token segment between the
    # final `.bin` and `.part`.
    for p in captured_part_paths:
        assert p.endswith(".part")
        # Token is hex of 8 bytes = 16 chars between `.bin.` and `.part`.
        assert ".bin." in p and ".part" in p


def test_claim_upload_slot_returns_minus1_when_redis_unavailable(monkeypatch):
    """Fail-closed: redis down → slot claim returns -1, caller rejects
    upload with 503. We never accept an upload without coordination."""
    api_main = _reload_api_main(monkeypatch)

    class BrokenRedis:
        def incr(self, key):
            raise RuntimeError("redis down")
        def decr(self, key):
            raise RuntimeError("redis down")
        def expire(self, key, ttl):
            raise RuntimeError("redis down")

    api_main.redis_client = BrokenRedis()
    assert api_main._claim_upload_slot("job-X") == -1


def test_finalize_rejected_when_part_file_exists_rolls_back_to_browser_uploading(
    monkeypatch, tmp_path,
):
    """Codex review #11: a still-streaming pre-CAS upload's .part file
    causes verify to fail with 409. Status MUST roll back to
    'browser_uploading' (NOT stuck at 'browser_finalizing') so:
      * subsequent uploads' post-stream re-check passes
      * a retried finalize can re-verify a now-complete staging tree
    Without rollback, the job is locked until the stale reaper kicks
    in 6h later — recovery effectively impossible from the client."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = _staging_dir_for_test_job(api_main, tmp_path, job_id)
    # Plant a .part file simulating an in-flight upload.
    (staging / "video" / "seg_00000000.bin.part").write_bytes(b"streaming...")

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "in_flight_partial_files" in detail
    # CRITICAL: status rolled back to browser_uploading so the user can
    # resume uploading + retry finalize.
    assert _read_job_status(api_main, job_id) == "browser_uploading"
    # rpush NOT called — verify failed before that step.
    api_main.redis_client.rpush.assert_not_called()


def test_finalize_after_rollback_can_succeed_on_retry(monkeypatch, tmp_path):
    """End-to-end Codex #11 regression: simulate the recovery flow.
    First finalize fails on .part; status rolls back; .part disappears
    (simulating upload completion); retry finalize succeeds."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = _staging_dir_for_test_job(api_main, tmp_path, job_id)
    part = staging / "video" / "seg_00000000.bin.part"
    part.write_bytes(b"streaming...")

    with TestClient(api_main.app) as client:
        # First call: fails because .part is present.
        r1 = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
        assert r1.status_code == 409
        assert _read_job_status(api_main, job_id) == "browser_uploading"

        # Simulate upload completing — .part renamed to .bin.
        part.unlink()
        # The fixture already planted seg_00000000.bin; the .part was
        # extra. After unlink, staging is back to the complete state.

        # Retry finalize succeeds.
        r2 = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
        assert r2.status_code == 200
        assert _read_job_status(api_main, job_id) == "pending"


# --- Codex review fix #3: abort endpoint cleanup ----------------------------
#
# /api/jobs/{id}/abort marks a browser-mode job as failed and removes its
# staging dir. Called by the extension on any failure after /init has
# staged a job (segment 403, key 403, finalize 5xx, tab close).


def test_abort_failed_browser_job_marks_failed_and_wipes_staging(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    assert staging.is_dir()  # setup planted segments here

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "user closed tab"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["aborted"] is True
    assert body["staging_cleaned"] is True
    assert _read_job_status(api_main, job_id) == "failed"
    # Staging dir must be wiped — it could be 50GB worth of orphaned files.
    assert not staging.exists()


def test_abort_already_completed_job_no_op(monkeypatch, tmp_path):
    """Abort of a completed job should NOT clobber the completion. The
    CAS WHERE clause excludes 'completed', so aborted=False but the
    endpoint still returns 200 (idempotent) and attempts staging
    cleanup (which is already gone)."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    # Flip job to completed before calling abort.
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='completed' WHERE id=:id"), {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={},
        )
    assert resp.status_code == 200
    assert resp.json()["aborted"] is False
    # Status MUST stay 'completed' — abort doesn't molest finished jobs.
    assert _read_job_status(api_main, job_id) == "completed"


def test_abort_unknown_job_returns_404(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    api_main, _ = _build_finalize_test_env(monkeypatch, tmp_path)
    with TestClient(api_main.app) as client:
        resp = client.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "fishing for nonexistent jobs"},
        )
    assert resp.status_code == 404


def test_abort_invalid_job_id_returns_400(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    api_main, _ = _build_finalize_test_env(monkeypatch, tmp_path)
    with TestClient(api_main.app) as client:
        # Path-traversal attempt embedded in job_id; URL-encoded slashes
        # mean fastapi sees one segment but it isn't a UUID.
        resp = client.post(
            "/api/jobs/not-a-uuid/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={},
        )
    assert resp.status_code == 400


# Codex review #4: this is THE regression. Once finalize commits server-
# side, status flips browser_uploading → pending and the job is on the
# redis queue. If the client doesn't see the response (timeout / network
# drop) and calls abort, the OLD code would transition pending → failed
# and rmtree the staging dir, destroying a queued, otherwise-complete
# job. Fix: 'pending' is no longer in the abortable WHERE set, so abort
# is a no-op on queued jobs, AND staging cleanup is gated on the row
# update succeeding so the worker's data is preserved.

def test_abort_on_pending_job_is_noop_and_preserves_staging(monkeypatch, tmp_path):
    """Two-generals scenario: finalize was already accepted server-side
    (status='pending', queued in redis) when the client calls abort.
    Abort MUST NOT destroy the queued job."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = _staging_dir_for_test_job(api_main, tmp_path, job_id)
    assert staging.is_dir()
    sentinel = staging / "video" / "seg_00000000.bin"
    assert sentinel.is_file()

    # Simulate finalize already having transitioned the job to 'pending'.
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='pending' WHERE id=:id"), {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "client saw timeout but server already committed"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # Critical: the abort MUST report aborted=False (no transition) and
    # staging_cleaned=False (we left the worker's data alone).
    assert body["aborted"] is False
    assert body["staging_cleaned"] is False
    # Job remains 'pending' so the worker can still claim it.
    assert _read_job_status(api_main, job_id) == "pending"
    # Staging is untouched — segments survive for the worker to mux.
    assert staging.is_dir()
    assert sentinel.is_file()


def test_abort_on_processing_job_preserves_staging(monkeypatch, tmp_path):
    """Worker has CAS-claimed and is mid-mux. Abort must not race in and
    rmtree the staging dir from underneath."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = _staging_dir_for_test_job(api_main, tmp_path, job_id)

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='processing' WHERE id=:id"), {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "racing"},
        )
    assert resp.status_code == 200
    assert resp.json()["aborted"] is False
    assert resp.json()["staging_cleaned"] is False
    assert _read_job_status(api_main, job_id) == "processing"
    # Worker's data is intact.
    assert staging.is_dir()


def test_abort_on_browser_uploading_still_wipes_staging(monkeypatch, tmp_path):
    """Validate the legitimate-abort path didn't regress: a job that
    fails BEFORE finalize commit (status still 'browser_uploading')
    DOES get its staging wiped. This is the failure mode abort was
    designed for."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = _staging_dir_for_test_job(api_main, tmp_path, job_id)
    assert staging.is_dir()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "segment 47 returned 403"},
        )
    assert resp.status_code == 200
    assert resp.json()["aborted"] is True
    assert resp.json()["staging_cleaned"] is True
    assert _read_job_status(api_main, job_id) == "failed"
    assert not staging.exists()


def test_abort_truncates_long_reason(monkeypatch, tmp_path):
    """Pydantic max_length=500 validation. Anything longer is rejected
    at the request layer."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
            json={"reason": "x" * 1000},
        )
    # 422 or 200 depending on Pydantic version semantics; either is fine
    # as long as it's not a server error.
    assert resp.status_code in (200, 422)


def _staging_dir_for_test_job(api_main, tmp_path, job_id):
    """The test env builder always uses tmp_path/{job_id} as staging."""
    return tmp_path / job_id


def test_finalize_rpush_called_before_db_commit(monkeypatch, tmp_path):
    """Verify ordering: rpush is invoked, and only after that does the
    DB transition happen. We assert call-order via a sequence list."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    call_order = []
    real_rpush = api_main.redis_client.rpush
    api_main.redis_client.rpush = MagicMock(side_effect=lambda *a, **kw: (call_order.append("rpush"), real_rpush.return_value)[1])

    # Wrap the engine.execute path on the SessionLocal to record commits.
    real_session_factory = api_main.SessionLocal

    def factory_with_trace():
        sess = real_session_factory()
        real_commit = sess.commit

        def traced_commit():
            call_order.append("db_commit")
            return real_commit()
        sess.commit = traced_commit
        return sess
    api_main.SessionLocal = factory_with_trace

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    # The first 'db_commit' is the meta lookup (no-op commit; SQLAlchemy
    # may or may not include it). The important assertion: rpush appears
    # BEFORE the finalize-side UPDATE commit.
    assert "rpush" in call_order
    rpush_idx = call_order.index("rpush")
    # Any commit AFTER rpush is fine; what we don't want is a commit at
    # an index < rpush_idx that represents the finalize transition.
    # Because the only DB write the endpoint does is the UPDATE
    # post-rpush, all commits after the meta lookup MUST come at or
    # after rpush_idx.
    commits_after_first_meta = [i for i, c in enumerate(call_order) if c == "db_commit" and i > 0]
    if commits_after_first_meta:
        assert min(commits_after_first_meta) > rpush_idx, (
            f"Got DB commit at idx {min(commits_after_first_meta)} before "
            f"rpush at idx {rpush_idx}; ordering broken: {call_order!r}"
        )


# --- _atomic_publish_part: filesystem-portable publish ----------------------
# Codex adversarial-review finding: the publish primitive used os.link as
# the sole atomic create. NAS bind mounts (SMB/CIFS/SSHFS) and various FUSE
# filesystems refuse link() with EPERM/EOPNOTSUPP/ENOSYS; the staging tree
# default is /downloads which is exactly where users mount their NAS. The
# helper must fall back to a copy-based publish that preserves the same
# "fail if target exists" guarantee.


def test_atomic_publish_part_happy_path_uses_link(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"PAYLOAD")

    api_main._atomic_publish_part(part, target)

    assert target.is_file()
    assert target.read_bytes() == b"PAYLOAD"
    # Hard link → both inodes point at the same data; .part still exists
    # until the caller unlinks it (matches the contract documented in
    # _stream_segment_to_disk).
    assert part.is_file()


def test_atomic_publish_part_raises_when_target_exists(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"NEW")
    target.write_bytes(b"EXISTING")

    with pytest.raises(FileExistsError):
        api_main._atomic_publish_part(part, target)
    # Existing target untouched.
    assert target.read_bytes() == b"EXISTING"


@pytest.mark.parametrize("err_code_attr", ["EPERM", "EOPNOTSUPP", "EXDEV", "ENOSYS"])
def test_atomic_publish_part_falls_back_when_link_unsupported(monkeypatch, tmp_path, err_code_attr):
    """Simulate a NAS / FUSE filesystem that refuses link() with the
    given errno. The fallback must still publish the bytes."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno
    code = getattr(_errno, err_code_attr, None)
    if code is None:
        pytest.skip(f"errno.{err_code_attr} not on this platform")

    def fake_link(src, dst):
        raise OSError(code, f"simulated {err_code_attr}")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"COPY-FALLBACK-PAYLOAD")

    api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"COPY-FALLBACK-PAYLOAD"


def test_atomic_publish_part_fallback_preserves_existing_target(monkeypatch, tmp_path):
    """Even on the copy-fallback path, an already-published target must
    NOT be overwritten — that's the property the os.link version gave
    us via FileExistsError, and the fallback uses O_CREAT|O_EXCL to
    preserve it."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"WOULD-CORRUPT")
    target.write_bytes(b"FIRST-COMMIT")

    with pytest.raises(FileExistsError):
        api_main._atomic_publish_part(part, target)
    assert target.read_bytes() == b"FIRST-COMMIT"


def test_atomic_publish_part_propagates_unexpected_oserror(monkeypatch, tmp_path):
    """ENOSPC etc. are real failures, not link-unsupported signals —
    they must surface so the caller returns 500, not silently fall
    back into a copy that will also fail."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.ENOSPC, "disk full")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"DATA")

    with pytest.raises(OSError) as exc:
        api_main._atomic_publish_part(part, target)
    assert exc.value.errno == _errno.ENOSPC
    assert not target.exists()


# Codex adversarial-review (high): the fallback used to copy bytes
# directly into `target`. A mid-copy crash (process killed, OOM,
# disk full, container restart) left a partial-content file at the
# FINAL path. Retry idempotency would accept it as committed. The
# two-stage publish writes to `<target>.publish.<token>.part`
# first, then atomically renames over the 0-byte sentinel target.

def test_atomic_publish_fallback_uses_publish_temp_not_direct_write(monkeypatch, tmp_path):
    """Verify the fallback path opens an O_EXCL sentinel + writes to
    a `.publish.<token>.part` tmp + atomic-renames. Specifically:
    the bytes are NEVER written through the sentinel fd."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"FULL-PAYLOAD-BYTES")

    api_main._atomic_publish_part(part, target)

    assert target.is_file()
    assert target.read_bytes() == b"FULL-PAYLOAD-BYTES"
    # No leftover publish.part — the atomic rename consumed it.
    leftover = list(tmp_path.glob("*.publish.*.part"))
    assert leftover == []


def test_atomic_publish_fallback_crash_mid_copy_leaves_recoverable_state(monkeypatch, tmp_path):
    """The Codex regression: simulate a crash AFTER the sentinel
    O_EXCL but BEFORE the atomic os.replace. State on disk MUST be:
      - target exists but is 0-byte (sentinel)
      - publish.part exists (with partial-or-full bytes)
    Both signals make the verify pass reject:
      - zero_byte for the sentinel target
      - in-flight `.part` for the publish tmp
    So finalize refuses to mux corrupt bytes."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    # Simulate a crash by making os.replace raise (mid-publish).
    real_replace = api_main.os.replace

    def crashy_replace(_src, _dst):
        raise RuntimeError("simulated crash mid-publish")

    monkeypatch.setattr(api_main.os, "replace", crashy_replace)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"PARTIAL-BYTES-WOULD-CORRUPT")

    with pytest.raises(RuntimeError, match="simulated crash"):
        api_main._atomic_publish_part(part, target)

    # The except BaseException should clean up; if it did:
    #   - target is gone OR is 0-byte sentinel
    #   - publish.part is gone
    # Either way: NEVER a non-zero target with partial bytes.
    if target.exists():
        # If cleanup raced, the worst case is a 0-byte sentinel —
        # rejected by zero-byte check at finalize time.
        assert target.stat().st_size == 0, (
            "Crash-mid-copy left a NON-EMPTY target — that's the "
            "exact corruption the two-stage publish was supposed "
            "to prevent."
        )

    # Restore real replace for cleanup.
    monkeypatch.setattr(api_main.os, "replace", real_replace)


def test_atomic_publish_fallback_writes_publish_temp_not_target_during_copy(monkeypatch, tmp_path):
    """Stronger guarantee: at NO point during the copy does the
    final `target` path contain non-zero bytes. We instrument the
    src.read() call to fail mid-copy and verify target is still 0
    bytes when cleanup runs (or non-existent post-cleanup)."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"X" * 10_000)

    # Wrap open() so we can crash on the second read of the SOURCE
    # file. By then, the publish_tmp exists with some bytes BUT
    # target should still be 0-byte sentinel.
    real_open = api_main.builtins.open if hasattr(api_main, "builtins") else open
    target_size_during_copy = {"snapshot": None}

    real_global_open = open
    crashed = {"flag": False}

    def watching_open(path, mode="r", *args, **kwargs):
        f = real_global_open(path, mode, *args, **kwargs)
        path_str = str(path)
        if path_str == str(part) and "rb" in mode:
            real_read = f.read

            def watching_read(n=-1, *_):
                # On the first read, snapshot target's size — it
                # should be 0 (sentinel) since bytes are flowing
                # into publish.part, not target.
                if not crashed["flag"]:
                    try:
                        target_size_during_copy["snapshot"] = target.stat().st_size
                    except OSError:
                        target_size_during_copy["snapshot"] = -1
                    crashed["flag"] = True
                    raise RuntimeError("simulated crash mid-source-read")
                return real_read(n)
            f.read = watching_read
        return f

    # Use a context to swap open during the call.
    import builtins as _b
    monkeypatch.setattr(_b, "open", watching_open)

    with pytest.raises(RuntimeError):
        api_main._atomic_publish_part(part, target)

    # The CRITICAL assertion: while bytes were being copied,
    # `target` was 0-byte. Pre-fix, target was the destination of
    # the copy and would have grown.
    assert target_size_during_copy["snapshot"] == 0, (
        "While the publish copy was in progress, target was "
        f"{target_size_during_copy['snapshot']} bytes (expected 0). "
        "The two-stage publish should keep target as a 0-byte "
        "sentinel until the atomic os.replace runs."
    )


def test_atomic_publish_fallback_publish_tmp_caught_by_part_glob(monkeypatch, tmp_path):
    """The publish tmp is named `<target>.publish.<token>.part` so
    the existing in-flight upload guard (`*.part` glob in
    _verify_staging_complete) treats a leftover stale tmp as
    'upload still in flight' and rejects finalize."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    # Simulate a leftover tmp from a prior crash.
    staging = tmp_path / "job"
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin.publish.abc123.part").write_bytes(
        b"partial bytes from a crashed write"
    )
    plan = {
        "container": "hls",
        "tracks": {"video": {"segment_count": 1}},
    }
    (staging / "manifest.json").write_text(json.dumps(plan))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert "in_flight_partial_files" in detail
    assert any(
        "publish" in name and "part" in name
        for name in detail["in_flight_partial_files"]
    )


# Codex review (P2): the init upload's FileExistsError handler used
# to return idempotent_concurrent without checking if the existing
# target was a 0-byte sentinel. After a crash mid-publish in the
# no-hardlink fallback, a retry would see the empty sentinel,
# unlink its .part, and report success — leaving init/*.bin at 0
# bytes for finalize to fail on. The segment handler already
# handled this; init now mirrors it.

def test_init_retry_replaces_zero_byte_sentinel_from_crashed_publish(monkeypatch, tmp_path):
    """The Codex regression: a crashed prior init upload left a
    0-byte sentinel at the final path. The retry hits FileExistsError
    in _atomic_publish_part, but the new init handler detects the
    0-byte case and overwrites via os.replace instead of returning
    idempotent_concurrent."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init" / "video.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Plant the 0-byte sentinel as if a prior attempt crashed.
    target.write_bytes(b"")
    assert target.is_file()
    assert target.stat().st_size == 0

    class _FakeRequest:
        async def stream(self):
            yield b"FMP4-INIT-MOOV-BYTES"

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    result = asyncio.run(api_main._stream_init_to_disk(
        request=_FakeRequest(),
        db=db,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video",
        target=target,
    ))

    # The retry must actually replace the sentinel, not silently
    # report idempotent success.
    assert target.is_file()
    assert target.read_bytes() == b"FMP4-INIT-MOOV-BYTES"
    assert target.stat().st_size > 0
    # Not idempotent_concurrent — this WAS the real publish.
    assert result.get("idempotent_concurrent") is None


def test_init_retry_idempotent_when_existing_target_is_complete(monkeypatch, tmp_path):
    """Sanity: when the existing target has REAL bytes (a successful
    prior commit), the retry returns idempotent_concurrent and does
    NOT overwrite. Init is deterministic per URL so duplicate retry
    bytes are equivalent — preserving prior commit avoids any
    same-bytes-but-different-token edge case."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init" / "video.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PRIOR-COMMIT-BYTES")
    stale_part = target.with_name(f"{target.name}.stale.part")
    stale_part.write_bytes(b"leftover temp bytes")
    prior_size = target.stat().st_size

    class _FakeRequest:
        async def stream(self):
            yield b"DIFFERENT-RETRY-BYTES"

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    result = asyncio.run(api_main._stream_init_to_disk(
        request=_FakeRequest(),
        db=db,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video",
        target=target,
    ))

    # Note: the start-of-request idempotency check at top of
    # _stream_init_to_disk also short-circuits when target exists +
    # non-zero. So this test exercises the EARLIER short-circuit
    # path. Both paths preserve the prior commit; that's the
    # invariant we care about.
    assert target.read_bytes() == b"PRIOR-COMMIT-BYTES"
    assert target.stat().st_size == prior_size
    assert not stale_part.exists()
    assert result.get("idempotent") is True


def test_segment_publish_succeeds_on_link_unsupported_filesystem(monkeypatch, tmp_path):
    """End-to-end: streaming a segment on a filesystem where link()
    raises EPERM (NAS bind mount) must still publish the bytes —
    NOT 500 with 'Segment write failed' as before the fix."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    import errno as _errno
    from unittest.mock import MagicMock as _MM

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated SMB share")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    target = tmp_path / "seg.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        def __init__(self, body):
            self._body = body
        async def stream(self):
            yield self._body

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    payload = b"NAS-DEPLOYMENT-SEGMENT-BYTES"
    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(payload),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))

    assert result.get("received") == len(payload)
    assert result.get("idempotent_concurrent") is None
    assert target.read_bytes() == payload
    # .part should be cleaned up.
    leftover = list(tmp_path.rglob("*.part"))
    assert leftover == []


def test_init_publish_succeeds_on_link_unsupported_filesystem(monkeypatch, tmp_path):
    """Same end-to-end check for the init-segment path (line 1429
    in the Codex finding) — fMP4/DASH must work on NAS too."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    import errno as _errno
    from unittest.mock import MagicMock as _MM

    def fake_link(src, dst):
        raise OSError(_errno.EOPNOTSUPP, "simulated FUSE")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    target = tmp_path / "init.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        def __init__(self, body):
            self._body = body
        async def stream(self):
            yield self._body

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    payload = b"FMP4-INIT-MOOV-BYTES"
    result = asyncio.run(api_main._stream_init_to_disk(
        request=_FakeRequest(payload),
        db=db,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video",
        target=target,
    ))

    assert result.get("received") == len(payload)
    assert target.read_bytes() == payload


# Codex review (P2): zero-byte init segment is valid neither for the
# fMP4 nor the DASH worker mux. The PUT endpoint now rejects empty
# bodies BEFORE publishing (fail-fast at the upload boundary), and
# _verify_staging_complete defends in depth against a 0-byte init
# that somehow ended up on disk via a legacy bug.

def test_stream_segment_to_disk_rejects_empty_body(monkeypatch, tmp_path):
    """Codex review (P2): a successful HTTP 200 with an empty body
    from the CDN must NOT be published as a 0-byte seg_*.bin. The
    extension treats PUT 200 as success and never retries; verify
    only catches it at /finalize time, by which point the upload
    retry window has closed. Reject at PUT for fail-fast retry."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            # Empty stream → written stays at 0.
            if False:
                yield b""

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_segment_to_disk(
            request=_FakeRequest(),
            db=db, meta=meta,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video", seq=0,
            target=target,
        ))
    assert exc.value.status_code == 400
    assert "empty" in str(exc.value.detail).lower()
    # Target must NOT have been published.
    assert not target.exists()
    leftover = list(tmp_path.rglob("*.part"))
    assert leftover == []


def test_stream_segment_to_disk_rejects_only_empty_chunks(monkeypatch, tmp_path):
    """Edge case: stream yields empty bytes objects. `if not chunk:
    continue` skips them; written stays 0; reject."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            yield b""
            yield b""

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_segment_to_disk(
            request=_FakeRequest(),
            db=db, meta=meta,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video", seq=0,
            target=target,
        ))
    assert exc.value.status_code == 400


def test_stream_segment_to_disk_accepts_non_empty_body(monkeypatch, tmp_path):
    """Sanity: a real (non-zero) segment body still publishes cleanly."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            yield b"REAL-SEGMENT-BYTES"

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    meta = _MM()
    meta.status = "browser_uploading"

    result = asyncio.run(api_main._stream_segment_to_disk(
        request=_FakeRequest(),
        db=db, meta=meta,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video", seq=0,
        target=target,
    ))
    assert result.get("received") == len(b"REAL-SEGMENT-BYTES")
    assert target.is_file()
    assert target.stat().st_size > 0


def test_stream_init_to_disk_rejects_empty_body(monkeypatch, tmp_path):
    """Init upload with a 0-byte body must 400 — published 0-byte
    init slips past _verify_staging_complete's old .is_file() check
    and fails much later at finalize-then-mux time."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            # Empty stream — `if not chunk: continue` skips, written stays 0.
            if False:
                yield b""

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_init_to_disk(
            request=_FakeRequest(),
            db=db,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            target=target,
        ))
    assert exc.value.status_code == 400
    assert "empty" in str(exc.value.detail).lower()
    # Target must NOT have been published.
    assert not target.exists()
    # No leftover .part either.
    leftover = list(tmp_path.rglob("*.part"))
    assert leftover == []


def test_stream_init_to_disk_rejects_only_empty_chunks(monkeypatch, tmp_path):
    """Edge case: stream yields empty bytes objects. `if not chunk:
    continue` skips them; written ends up 0; reject."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            yield b""
            yield b""

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_init_to_disk(
            request=_FakeRequest(),
            db=db,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            target=target,
        ))
    assert exc.value.status_code == 400


def test_stream_init_to_disk_accepts_non_empty_body(monkeypatch, tmp_path):
    """Sanity: a real (non-zero) init body still succeeds."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            yield b"FMP4-INIT-MOOV-BYTES"

    class _FakeRow:
        status = "browser_uploading"
    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))

    result = asyncio.run(api_main._stream_init_to_disk(
        request=_FakeRequest(),
        db=db,
        job_id="11111111-2222-3333-4444-555555555555",
        track="video",
        target=target,
    ))
    assert result.get("received") == len(b"FMP4-INIT-MOOV-BYTES")
    assert target.is_file()
    assert target.stat().st_size > 0


def test_verify_staging_complete_rejects_zero_byte_init(monkeypatch, tmp_path):
    """Defense-in-depth: even if a 0-byte init landed via some other
    path (legacy bug, manual filesystem corruption), _verify must
    reject it instead of letting finalize enqueue a doomed job."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "init").mkdir()
    # 1 expected segment, present + non-zero.
    (staging / "video" / "seg_00000000.bin").write_bytes(b"OK")
    # Init declared but ZERO BYTES on disk.
    (staging / "init" / "video.bin").write_bytes(b"")
    plan = {
        "container": "hls",
        "tracks": {
            "video": {
                "segment_count": 1,
                "init_segment_url": "https://cdn.example.com/init.mp4",
            },
        },
    }
    (staging / "manifest.json").write_text(json.dumps(plan))

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    detail = exc.value.detail
    # The init's zero-byte report key is "<track>:init".
    assert "zero_byte" in detail
    assert "video:init" in detail["zero_byte"]


def test_verify_staging_complete_accepts_nonzero_init(monkeypatch, tmp_path):
    """Sanity: a non-zero init alongside a complete segment set
    passes verify."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    staging.mkdir()
    (staging / "video").mkdir()
    (staging / "init").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"OK")
    (staging / "init" / "video.bin").write_bytes(b"FTYP-MOOV")
    plan = {
        "container": "hls",
        "tracks": {
            "video": {
                "segment_count": 1,
                "init_segment_url": "https://cdn.example.com/init.mp4",
            },
        },
    }
    (staging / "manifest.json").write_text(json.dumps(plan))

    summary = api_main._verify_staging_complete(staging)
    assert summary == {"video": 1}


# Codex adversarial-review: cancelling a `pending` browser-mode job
# (segments fully staged, finalize-queue entry waiting) used to leave
# the staging dir on disk forever — the worker pops, fails its CAS
# (status='cancelled' is outside the allowed-from set), skips, and
# the stale-browser-reaper doesn't cover 'cancelled'. Up to
# MAX_JOB_STAGING_BYTES (50 GB) per cancelled job got stranded.
# The fix: CAS-cancel from 'pending' specifically, then LREM the
# queue + rmtree the staging dir while we still own them.


def _flip_to_pending(api_main, job_id):
    """Promote the planted browser_uploading row to 'pending' to
    simulate a fully-finalized browser job sitting in the queue."""
    from sqlalchemy import text as _sa_text
    db = api_main.SessionLocal()
    try:
        db.execute(_sa_text("UPDATE jobs SET status='pending' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()


def test_cancel_pending_browser_job_cleans_staging_and_dequeues(monkeypatch, tmp_path):
    """Happy path: user cancels a fully-finalized browser job before
    the worker picks it up. Status flips to 'cancelled', staging dir
    is rmtree'd, and the finalize-queue entry is LREM'd."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    _flip_to_pending(api_main, job_id)

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    sentinel = staging / "video" / "seg_00000000.bin"
    assert sentinel.is_file()  # planted by _build_finalize_test_env

    api_main.redis_client.lrem = MagicMock(return_value=1)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Staging dir wiped — must not strand 50GB of segments.
    assert not staging.exists()
    # Queue LREM called with the job id (count=0 = remove all matches).
    api_main.redis_client.lrem.assert_called_with(
        "browser_finalize_queue", 0, job_id
    )


def test_cancel_pending_browser_job_refuses_rmtree_outside_staging_root(monkeypatch, tmp_path):
    """Defense in depth: if staging_dir somehow got set to a path
    outside STAGING_DIR (manual psql edit, prior bug), the cancel
    cleanup must NOT rmtree it. DB flip still happens; the foreign
    directory is left untouched with a logged warning."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    _flip_to_pending(api_main, job_id)

    # Re-point staging_dir to somewhere outside STAGING_DIR.
    outside = tmp_path.parent / "definitely-not-staging-cancel"
    outside.mkdir(parents=True, exist_ok=True)
    sentinel = outside / "do-not-delete.txt"
    sentinel.write_text("important")

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text(
            "UPDATE job_metadata SET staging_dir=:sd WHERE job_id=:id"
        ), {"id": job_id, "sd": str(outside)})
        db.commit()
    finally:
        db.close()

    api_main.redis_client.lrem = MagicMock(return_value=0)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Foreign directory + sentinel must still exist.
    assert outside.is_dir()
    assert sentinel.is_file()


def test_cancel_pending_browser_job_refuses_rmtree_sibling_staging_dir(monkeypatch, tmp_path):
    """Containment is insufficient: STAGING_DIR/<other-job> must not
    be cleaned for this job just because a poisoned metadata row points
    at it."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    _flip_to_pending(api_main, job_id)

    original_staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    sibling = tmp_path / "99999999-9999-9999-9999-999999999999"
    sibling.mkdir()
    sentinel = sibling / "do-not-delete.txt"
    sentinel.write_text("belongs to another job")

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text(
            "UPDATE job_metadata SET staging_dir=:sd WHERE job_id=:id"
        ), {"id": job_id, "sd": str(sibling)})
        db.commit()
    finally:
        db.close()

    api_main.redis_client.lrem = MagicMock(return_value=0)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    assert sentinel.is_file()
    assert original_staging.is_dir()


def test_cancel_pending_non_browser_job_skips_staging_cleanup(monkeypatch, tmp_path):
    """Nas-direct mode (mode != 'browser') should NOT trigger the
    LREM or rmtree paths — that pipeline doesn't use STAGING_DIR."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    _flip_to_pending(api_main, job_id)

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text(
            "UPDATE job_metadata SET mode=NULL WHERE job_id=:id"
        ), {"id": job_id})
        db.commit()
    finally:
        db.close()

    api_main.redis_client.lrem = MagicMock(return_value=0)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Cleanup paths must NOT have fired — non-browser jobs don't use
    # browser_finalize_queue or STAGING_DIR.
    api_main.redis_client.lrem.assert_not_called()


def test_cancel_processing_job_does_not_touch_staging(monkeypatch, tmp_path):
    """Race-loser path: worker has already CAS'd pending → processing.
    Our 'pending' CAS misses, falls through to the broader cancel which
    flips status. Staging stays intact (worker owns it now) and LREM
    is NOT called."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='processing' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    api_main.redis_client.lrem = MagicMock(return_value=0)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Staging untouched — worker is muxing it.
    assert staging.is_dir()
    api_main.redis_client.lrem.assert_not_called()


def test_cancel_unknown_job_returns_404(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    api_main, _ = _build_finalize_test_env(monkeypatch, tmp_path)
    with TestClient(api_main.app) as client:
        resp = client.delete(
            "/api/jobs/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 404


def test_cancel_already_completed_returns_404(monkeypatch, tmp_path):
    """Completed jobs are NOT cancelable — preserves finished state."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='completed' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 404
    assert _read_job_status(api_main, job_id) == "completed"


def test_cancel_pending_browser_job_with_redis_failure_still_marks_cancelled(monkeypatch, tmp_path):
    """LREM failure (redis down) must not block the cancel — we'd
    rather leave the queue entry (worker will skip it via failed CAS)
    than refuse to cancel."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    _flip_to_pending(api_main, job_id)

    api_main.redis_client.lrem = MagicMock(
        side_effect=RuntimeError("redis connection refused")
    )

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Staging still cleaned (rmtree happens after the LREM attempt
    # but is independent — both are best-effort).
    assert not staging.exists()


# Codex review (P3): DELETE used to ignore browser_pending, leaving
# users without a way to cancel a brand-new browser-side job that
# hasn't started uploading yet. The DELETE endpoint now CAS-cancels
# from browser_pending too, with the same staging cleanup as the
# pending case.

def test_cancel_browser_pending_job_cleans_staging(monkeypatch, tmp_path):
    """User clicks cancel right after /init returned but before the
    extension started uploading. Status flips to 'cancelled' and the
    (mostly empty) staging dir is rmtree'd. No queue entry to LREM
    yet — that only happens at /finalize time."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    # Default fixture leaves status as 'browser_uploading'; reset to
    # browser_pending (right after /init).
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='browser_pending' WHERE id=:id"),
                   {"id": job_id})
        db.commit()
    finally:
        db.close()

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    assert staging.is_dir()  # /init created it

    api_main.redis_client.lrem = MagicMock(return_value=0)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Staging dir wiped (it was empty anyway, but the rmtree path runs).
    assert not staging.exists()


def test_cancel_browser_uploading_job_via_delete_flips_status_and_cleans_staging(monkeypatch, tmp_path):
    """Codex adversarial-review (medium): browser_uploading IS now
    DELETE-cancellable from the sidepanel. The extension owns the
    upload session, but flipping status to 'cancelled' makes future
    PUTs return 409 at the entry status check, so no new bytes accrue.
    The sidepanel pairs this DELETE with a CANCEL_BROWSER_JOB message
    that fires the offscreen AbortController for in-flight PUTs.
    Server cleans up staging while it owns the row."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    # Fixture default state IS browser_uploading.

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    sentinel = staging / "video" / "seg_00000000.bin"
    assert sentinel.is_file()

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    # Staging tree is wiped by the early-CAS branch.
    assert not staging.exists()


def test_cancel_browser_finalizing_job_via_delete_flips_status_and_cleans_staging(monkeypatch, tmp_path):
    """Codex adversarial-review (medium): browser_finalizing is the
    brief window before the API flips the row to 'pending'. DELETE
    treats it identically to 'pending' / 'browser_pending' — flip to
    cancelled, drop the queue entry, rmtree the staging tree."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    # Force the test job into browser_finalizing.
    db = api_main.SessionLocal()
    try:
        db.execute(api_main.text(
            "UPDATE jobs SET status='browser_finalizing' WHERE id=:id"
        ), {"id": job_id})
        db.commit()
    finally:
        db.close()

    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    assert staging.exists()

    with TestClient(api_main.app) as client:
        resp = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200
    assert _read_job_status(api_main, job_id) == "cancelled"
    assert not staging.exists()
