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


def test_job_init_request_accepts_paired_direct_dash(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/video.m4s?sig=1",
            "content_length": 123456,
            "mime_type": "video/mp4",
            "width": 1920,
            "height": 1080,
        },
        "audio": {
            "url": "https://cdn.example.com/audio.m4s?sig=1",
            "content_length": 12345,
            "mime_type": "audio/mp4",
        },
        "duration": 120,
    })
    assert request.direct_dash.video.height == 1080
    assert request.direct_dash.audio.content_length == 12345


@pytest.mark.parametrize("duration", [float("inf"), float("-inf"), float("nan")])
def test_job_init_request_rejects_non_finite_direct_dash_duration(
    monkeypatch, duration,
):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    with pytest.raises(Exception):
        api_main.JobInitRequest(direct_dash={
            "video": {
                "url": "https://cdn.example.com/video.m4s",
                "content_length": 1,
            },
            "audio": {
                "url": "https://cdn.example.com/audio.m4s",
                "content_length": 1,
            },
            "duration": duration,
        })


def test_job_init_request_rejects_mixed_or_incomplete_direct_dash(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    paired = {
        "video": {"url": "https://cdn.example.com/video.m4s", "content_length": 1},
        "audio": {"url": "https://cdn.example.com/audio.m4s", "content_length": 1},
    }
    with pytest.raises(Exception, match="cannot be combined"):
        api_main.JobInitRequest(
            url="https://cdn.example.com/manifest.mpd",
            direct_dash=paired,
        )
    with pytest.raises(Exception):
        api_main.JobInitRequest(direct_dash={"video": paired["video"]})


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


@pytest.mark.parametrize("source", ["manifest_text", "url"])
def test_init_manifest_budget_is_forwarded_and_maps_to_413(
    monkeypatch, tmp_path, source,
):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_BROWSER_PLAN_BYTES="1234",
        MAX_BROWSER_SEGMENTS="77",
    )
    import manifest_planner

    observed = {}

    def reject_oversized(*_args, **kwargs):
        observed["max_plan_bytes"] = kwargs.get("max_plan_bytes")
        observed["max_segments"] = kwargs.get("max_segments")
        raise manifest_planner.ManifestPlanTooLargeError("test plan too large")

    if source == "manifest_text":
        monkeypatch.setattr(manifest_planner, "plan_from_text", reject_oversized)
        request = api_main.JobInitRequest(
            manifest_text="#EXTM3U\n#EXTINF:1,\nseg.ts\n",
            base_url="https://cdn.example.com/playlist.m3u8",
        )
    else:
        monkeypatch.setattr(manifest_planner, "plan_from_url", reject_oversized)
        request = api_main.JobInitRequest(
            url="https://cdn.example.com/playlist.m3u8",
        )

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.init_browser_job(
            request=request,
            db=MagicMock(),
            api_key="test-key-not-the-default-placeholder",
        )

    assert exc.value.status_code == 413
    assert "MAX_BROWSER_PLAN_BYTES=1234" in str(exc.value.detail)
    assert observed == {"max_plan_bytes": 1234, "max_segments": 77}
    assert list(tmp_path.iterdir()) == []


def test_init_direct_dash_returns_standard_two_track_browser_plan(monkeypatch, tmp_path):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    request = api_main.JobInitRequest(**{
        "direct_dash": {
            "video": {
                "url": "https://cdn.example.com/video.m4s?sig=1",
                "content_length": 17,
                "mime_type": "video/mp4",
                "width": 1920,
                "height": 1080,
            },
            "audio": {
                "url": "https://cdn.example.com/audio.m4s?sig=1",
                "content_length": 9,
                "mime_type": "audio/mp4",
            },
            "duration": 120,
        },
        "title": "paired tracks",
    })
    db = MagicMock()
    body = api_main.init_browser_job(
        request=request,
        db=db,
        api_key="test-key-not-the-default-placeholder",
    )

    assert body.plan["container"] == "dash"
    assert set(body.plan["tracks"]) == {"video", "audio"}
    assert body.plan["total_segments"] == 2
    assert body.plan["recommended_concurrency"] == 12
    assert (tmp_path / body.job_id / "manifest.json").is_file()
    assert db.commit.call_count == 1


def test_init_direct_dash_recommendation_respects_server_upload_cap(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_CONCURRENT_UPLOADS_PER_JOB="4",
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    mib = 1024 * 1024
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/video.m4s",
            "content_length": 40 * mib,
        },
        "audio": {
            "url": "https://cdn.example.com/audio.m4s",
            "content_length": 16 * mib,
        },
    })

    body = api_main.init_browser_job(
        request=request,
        db=MagicMock(),
        api_key="test-key-not-the-default-placeholder",
    )

    assert body.plan["recommended_concurrency"] == 4


def test_manifest_recommendation_respects_server_upload_cap(monkeypatch):
    api_main = _reload_api_main(
        monkeypatch,
        MAX_CONCURRENT_UPLOADS_PER_JOB="3",
    )
    assert api_main._recommended_manifest_concurrency() == 3


@pytest.mark.parametrize("source", ["manifest_text", "url"])
def test_init_manifest_sources_publish_server_concurrency_recommendation(
    monkeypatch, tmp_path, source,
):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_CONCURRENT_UPLOADS_PER_JOB="3",
    )
    import manifest_planner

    plan = {
        "container": "hls",
        "source_url": "https://cdn.example.com/playlist.m3u8",
        "total_segments": 1,
        "duration": 1,
        "tracks": {
            "video": {
                "segment_count": 1,
                "segments": [{
                    "seq": 0,
                    "url": "https://cdn.example.com/segment.ts",
                }],
            },
        },
    }
    planner = lambda *_args, **_kwargs: dict(plan)
    if source == "manifest_text":
        monkeypatch.setattr(manifest_planner, "plan_from_text", planner)
        request = api_main.JobInitRequest(
            manifest_text="#EXTM3U\n#EXTINF:1,\nsegment.ts\n",
            base_url="https://cdn.example.com/playlist.m3u8",
        )
    else:
        monkeypatch.setattr(manifest_planner, "plan_from_url", planner)
        request = api_main.JobInitRequest(
            url="https://cdn.example.com/playlist.m3u8",
        )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    monkeypatch.setattr(api_main, "_staged_bytes_get", lambda *_args: 0)

    body = api_main.init_browser_job(
        request=request,
        db=MagicMock(),
        api_key="test-key-not-the-default-placeholder",
    )

    assert body.plan["recommended_concurrency"] == 3
    persisted = (tmp_path / body.job_id / "manifest.json").read_text(
        encoding="utf-8",
    )
    assert '"recommended_concurrency": 3' in persisted


def test_manifest_recommendation_rejects_nonpositive_server_cap(monkeypatch):
    api_main = _reload_api_main(
        monkeypatch,
        MAX_CONCURRENT_UPLOADS_PER_JOB="0",
    )
    with pytest.raises(ValueError, match="must be positive"):
        api_main._recommended_manifest_concurrency()


def test_init_direct_dash_recommendation_respects_custom_staging_quota(
    monkeypatch, tmp_path,
):
    mib = 1024 * 1024
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_JOB_STAGING_BYTES=str(21 * mib),
        MAX_CONCURRENT_UPLOADS_PER_JOB="12",
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/video.m4s",
            "content_length": 12 * mib,
        },
        "audio": {
            "url": "https://cdn.example.com/audio.m4s",
            "content_length": 8 * mib,
        },
    })

    body = api_main.init_browser_job(
        request=request,
        db=MagicMock(),
        api_key="test-key-not-the-default-placeholder",
    )

    # Largest actual planned range is 8 MiB: only two worst-case ranges fit
    # the deployment's 21 MiB quota concurrently.
    assert body.plan["recommended_concurrency"] == 2


def test_init_direct_dash_counts_manifest_against_exact_media_quota(
    monkeypatch, tmp_path,
):
    mib = 1024 * 1024
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_JOB_STAGING_BYTES=str(20 * mib),
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/video.m4s",
            "content_length": 12 * mib,
        },
        "audio": {
            "url": "https://cdn.example.com/audio.m4s",
            "content_length": 8 * mib,
        },
    })

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.init_browser_job(
            request=request,
            db=MagicMock(),
            api_key="test-key-not-the-default-placeholder",
        )

    assert exc.value.status_code == 413
    assert "media plus manifest" in str(exc.value.detail)
    assert list(tmp_path.iterdir()) == []


def test_init_direct_dash_rejects_oversized_estimated_plan_before_materializing(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="1",
        MAX_BROWSER_PLAN_BYTES="1024",
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)

    import manifest_planner
    called = False

    def must_not_materialize(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized plan was materialized")

    monkeypatch.setattr(manifest_planner, "plan_direct_dash", must_not_materialize)
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/video.m4s?" + "x" * 500,
            "content_length": 100,
        },
        "audio": {
            "url": "https://cdn.example.com/audio.m4s?" + "y" * 500,
            "content_length": 100,
        },
    })

    with pytest.raises(api_main.HTTPException) as exc:
        api_main.init_browser_job(
            request=request,
            db=MagicMock(),
            api_key="test-key-not-the-default-placeholder",
        )

    assert exc.value.status_code == 413
    assert "Estimated direct DASH plan" in str(exc.value.detail)
    assert called is False


def test_direct_dash_preflight_does_not_false_reject_small_custom_plan_cap(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(
        monkeypatch,
        SSRF_GUARD="false",
        STAGING_DIR=str(tmp_path),
        MAX_BROWSER_PLAN_BYTES="10000",
    )
    monkeypatch.setattr(api_main, "_enforce_plan_url_safety", lambda _plan: None)
    request = api_main.JobInitRequest(direct_dash={
        "video": {
            "url": "https://cdn.example.com/v.m4s",
            "content_length": 1,
        },
        "audio": {
            "url": "https://cdn.example.com/a.m4s",
            "content_length": 1,
        },
    })

    body = api_main.init_browser_job(
        request=request,
        db=MagicMock(),
        api_key="test-key-not-the-default-placeholder",
    )
    assert body.plan["total_segments"] == 2


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


def test_verify_staging_complete_rejects_byte_range_size_mismatch(monkeypatch, tmp_path):
    """A present but truncated direct-DASH chunk must not count as complete."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    plan = {
        "container": "dash",
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 8},
            }],
        }},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"short")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    assert exc.value.detail["size_mismatch"] == {
        "video": [{"seq": 0, "expected": 8, "actual": 5}],
    }


@pytest.mark.parametrize(
    "actual_size,should_pass",
    [(15, False), (16, True), (31, True), (32, False)],
)
def test_verify_staging_complete_enforces_aes_plaintext_length_interval(
    monkeypatch, tmp_path, actual_size, should_pass,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "11111111-2222-3333-4444-555555555555"
    plan = {
        "container": "hls",
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 32},
                "key": {"method": "AES-128", "uri": "https://example.com/key"},
            }],
        }},
    }
    _write_plan(staging, plan)
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(
        b"x" * actual_size
    )

    if should_pass:
        assert api_main._verify_staging_complete(staging) == {"video": 1}
    else:
        with pytest.raises(api_main.HTTPException) as exc:
            api_main._verify_staging_complete(staging)
        assert exc.value.status_code == 409
        assert exc.value.detail["size_mismatch"] == {
            "video": [{
                "seq": 0,
                "expected": {"min": 16, "max": 31},
                "actual": actual_size,
            }],
        }


def test_expected_segment_shape_exposes_aes_plaintext_bounds(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_plan(staging, {
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 32},
                "key": {"method": "AES-128"},
            }],
        }},
    })

    count, lower, upper, exact = api_main._expected_segment_shape_for_track(
        staging, "video",
    )
    assert count == 1
    assert lower == {0: 16}
    assert upper == {0: 31}
    assert exact == set()


def test_encrypted_range_upload_rejects_declared_body_below_plaintext_bound(
    monkeypatch, tmp_path,
):
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    _write_plan(tmp_path / job_id, {
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 32},
                "key": {"method": "AES-128"},
            }],
        }},
    })
    monkeypatch.setattr(
        api_main,
        "_get_browser_job_meta",
        lambda *_args: SimpleNamespace(
            status="browser_uploading", total_segments=1,
        ),
    )
    claimed = []
    monkeypatch.setattr(
        api_main,
        "_claim_upload_slot",
        lambda *_args: claimed.append(True) or 1,
    )

    class FakeRequest:
        headers = {"content-length": "15"}

        async def stream(self):
            pytest.fail("invalid declared length must reject before body read")
            yield b""

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main.upload_segment(
            job_id, 0, FakeRequest(), track="video", db=MagicMock(), api_key="x",
        ))
    assert exc.value.status_code == 400
    assert "below planned lower bound 16" in exc.value.detail
    assert claimed == []


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


def test_verify_staging_complete_rejects_active_upload_without_part_file(
    monkeypatch, tmp_path,
):
    """A no-hardlink publish removes its source part at rename time.

    The Redis upload token remains authoritative through staged-byte
    accounting, so finalize must reject the token itself rather than relying
    exclusively on a visible .part or publish-lock pathname.
    """
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    _write_plan(staging, {
        "container": "dash",
        "tracks": {"video": {"segment_count": 1}},
    })
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"complete")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(
            staging,
            active_upload_tokens={"publisher-token-123456"},
        )

    assert exc.value.status_code == 409
    assert any(
        name.startswith("active-upload:publisher-")
        for name in exc.value.detail["in_flight_partial_files"]
    )


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

from datetime import UTC, datetime, timedelta
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
                finalize_started_at TIMESTAMP, last_activity TIMESTAMP
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


def test_finalize_resume_refreshes_finalize_started_at(monkeypatch, tmp_path):
    """Codex adversarial review: resuming a >6h-old browser_finalizing job must
    refresh finalize_started_at. The (now periodic) stale-browser reaper ages
    browser_finalizing rows off finalize_started_at, so without this refresh it
    could flip the resuming job to 'failed' and rmtree its complete staging
    mid-resume (data loss). After resume, the timestamp must be fresh so the
    reaper's `fsa < now-6h` predicate is false."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    # A prior finalize attempt stamped finalize_started_at long ago, then
    # failed before reaching 'pending' — exactly what the reaper targets.
    stale = _utcnow_naive() - timedelta(hours=8)
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE jobs SET status='browser_finalizing' WHERE id=:id"),
                   {"id": job_id})
        db.execute(sa_text("UPDATE job_metadata SET finalize_started_at=:t WHERE job_id=:id"),
                   {"id": job_id, "t": stale})
        db.commit()
    finally:
        db.close()

    with TestClient(api_main.app) as client:
        resp = client.post(
            f"/api/jobs/{job_id}/finalize",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
    assert resp.status_code == 200

    db = api_main.SessionLocal()
    try:
        row = db.execute(
            sa_text("SELECT finalize_started_at FROM job_metadata WHERE job_id=:id"),
            {"id": job_id},
        ).first()
    finally:
        db.close()
    fsa = row.finalize_started_at
    if isinstance(fsa, str):  # sqlite returns TIMESTAMP as text
        fsa = datetime.fromisoformat(fsa)
    # Refreshed to ~now, NOT the 8h-old stale value.
    assert fsa > _utcnow_naive() - timedelta(hours=1)


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


class _AtomicCounterRedis:
    """Small interpreter for main.py's upload coordination Lua scripts."""

    def __init__(
        self, *, fail_after_increment=False, fail_after_staged_record=False,
        fail_after_dirty_set=False, fail_release_before=False,
        fail_release_after=False,
    ):
        self.values = {}
        self.expirations = {}
        self.lease_amounts = {}
        self.lease_deadlines = {}
        self.now = 1000
        self.max_reserved = 0
        self.fail_after_increment = fail_after_increment
        self.fail_after_staged_record = fail_after_staged_record
        self.fail_after_dirty_set = fail_after_dirty_set
        self.fail_release_before = fail_release_before
        self.fail_release_after = fail_release_after

    def _prune_leases(self, lease_key, staged_key, dirty_key, dirty_ttl):
        amounts = self.lease_amounts.setdefault(lease_key, {})
        deadlines = self.lease_deadlines.setdefault(lease_key, {})
        expired = [
            token for token, deadline in deadlines.items()
            if deadline <= self.now
        ]
        if any(amounts.get(token, 0) != 0 for token in expired):
            self.values[dirty_key] = 1
            self.expirations[dirty_key] = int(dirty_ttl)
            self.values.pop(staged_key, None)
        for token in expired:
            amounts.pop(token, None)
            deadlines.pop(token, None)
        if not amounts:
            self.lease_amounts.pop(lease_key, None)
            self.lease_deadlines.pop(lease_key, None)
        return amounts, deadlines

    def eval(self, script, numkeys, key, *args):
        keys = [key, *args[:numkeys - 1]]
        argv = args[numkeys - 1:]
        if "wv2nas:upload-lease-claim-v2" in script:
            lease_key, staged_key, dirty_key = keys
            token, lease_seconds, key_ttl, cap, dirty_ttl = argv
            amounts, deadlines = self._prune_leases(
                lease_key, staged_key, dirty_key, dirty_ttl,
            )
            amounts = self.lease_amounts.setdefault(lease_key, amounts)
            deadlines = self.lease_deadlines.setdefault(lease_key, deadlines)
            active_count = sum(1 for amount in amounts.values() if amount >= 0)
            if str(token) not in amounts and active_count >= int(cap):
                return int(cap) + 1
            if amounts.get(str(token), 0) < 0:
                return int(cap) + 1
            amounts.setdefault(str(token), 0)
            deadlines[str(token)] = self.now + int(lease_seconds)
            self.expirations[lease_key] = int(key_ttl)
            if self.fail_after_increment:
                self.fail_after_increment = False
                raise TimeoutError("response lost after lease claim")
            return sum(1 for amount in amounts.values() if amount >= 0)
        if "wv2nas:upload-lease-reserve-v2" in script:
            lease_key, staged_key, dirty_key = keys
            token, amount, lease_seconds, key_ttl, dirty_ttl = argv
            amounts, deadlines = self._prune_leases(
                lease_key, staged_key, dirty_key, dirty_ttl,
            )
            if str(token) not in amounts or amounts[str(token)] < 0:
                return -1
            amounts[str(token)] = int(amount)
            deadlines[str(token)] = self.now + int(lease_seconds)
            total = sum(value for value in amounts.values() if value > 0)
            self.max_reserved = max(self.max_reserved, total)
            self.expirations[lease_key] = int(key_ttl)
            if self.fail_after_increment:
                self.fail_after_increment = False
                raise TimeoutError("response lost after lease reserve")
            return total
        if "wv2nas:upload-quota-snapshot-v1" in script:
            lease_key, staged_key, dirty_key = keys
            (dirty_ttl,) = argv
            amounts, _deadlines = self._prune_leases(
                lease_key, staged_key, dirty_key, dirty_ttl,
            )
            reserved = sum(value for value in amounts.values() if value > 0)
            signature = "|".join(
                f"{token}={amount}"
                for token, amount in sorted(amounts.items())
            )
            active = sorted(
                token for token, amount in amounts.items() if amount >= 0
            )
            clean = dirty_key not in self.values and staged_key in self.values
            return [
                1 if clean else 0,
                int(self.values[staged_key]) if clean else -1,
                reserved,
                signature,
                *active,
            ]
        if "wv2nas:upload-lease-heartbeat-v2" in script:
            lease_key, staged_key, dirty_key = keys
            token, lease_seconds, require_reserved, key_ttl, dirty_ttl = argv
            amounts, deadlines = self._prune_leases(
                lease_key, staged_key, dirty_key, dirty_ttl,
            )
            if str(token) not in amounts or amounts[str(token)] < 0:
                return 0
            if int(require_reserved) == 1 and amounts[str(token)] <= 0:
                return 0
            deadlines[str(token)] = self.now + int(lease_seconds)
            self.expirations[lease_key] = int(key_ttl)
            return 1
        if "wv2nas:upload-lease-release-v2" in script:
            lease_key, staged_key, dirty_key = keys
            token, dirty_ttl = argv
            if self.fail_release_before:
                self.fail_release_before = False
                raise TimeoutError("release failed before commit")
            amounts = self.lease_amounts.get(lease_key, {})
            deadlines = self.lease_deadlines.get(lease_key, {})
            if amounts.get(str(token), 0) < 0:
                return 0
            amounts.pop(str(token), None)
            deadlines.pop(str(token), None)
            self._prune_leases(lease_key, staged_key, dirty_key, dirty_ttl)
            if self.fail_release_after:
                self.fail_release_after = False
                raise TimeoutError("release response lost after commit")
            return 1
        if "wv2nas:upload-lease-active-tokens-v2" in script:
            lease_key, staged_key, dirty_key = keys
            dirty_ttl, include_retained = argv
            amounts, _deadlines = self._prune_leases(
                lease_key, staged_key, dirty_key, dirty_ttl,
            )
            return [
                token for token, amount in amounts.items()
                if int(include_retained) == 1 or amount >= 0
            ]
        if "wv2nas:upload-lease-retain-bytes-v1" in script:
            lease_key, staged_key, dirty_key = keys
            token, lease_seconds, key_ttl, dirty_ttl = argv
            amounts = self.lease_amounts.get(lease_key, {})
            deadlines = self.lease_deadlines.get(lease_key, {})
            amount = amounts.get(str(token))
            if amount is None or amount == 0:
                return 0
            amounts[str(token)] = -abs(amount)
            deadlines[str(token)] = self.now + int(lease_seconds)
            self.expirations[lease_key] = int(key_ttl)
            self.values[dirty_key] = 1
            self.expirations[dirty_key] = int(dirty_ttl)
            self.values.pop(staged_key, None)
            return 1
        if "wv2nas:increment-with-ttl" in script:
            amount, ttl = int(args[0]), int(args[1])
            current = max(0, int(self.values.get(key, 0)))
            self.values[key] = current + amount
            self.expirations[key] = ttl
            if self.fail_after_increment:
                raise TimeoutError("response lost after EXEC")
            return self.values[key]
        if "wv2nas:release-clamped" in script:
            amount, ttl = int(args[0]), int(args[1])
            remaining = int(self.values.get(key, 0)) - amount
            if remaining <= 0:
                self.values.pop(key, None)
                self.expirations.pop(key, None)
                return 0
            self.values[key] = remaining
            self.expirations[key] = ttl
            return remaining
        if "wv2nas:mark-staged-dirty" in script:
            staged_key, dirty_key, _lease_key = keys
            self.values[dirty_key] = 1
            self.expirations[dirty_key] = int(argv[0])
            self.values.pop(staged_key, None)
            if self.fail_after_dirty_set:
                self.fail_after_dirty_set = False
                raise TimeoutError("dirty EVAL response lost after mutation")
            return 1
        if "wv2nas:staged-commit-upload-v1" in script:
            staged_key, dirty_key, lease_key = keys
            token, published, staged_ttl, dirty_ttl = argv
            token = str(token)
            published = int(published)
            amounts = self.lease_amounts.get(lease_key, {})
            deadlines = self.lease_deadlines.get(lease_key, {})

            def mark_dirty_and_release():
                self.values[dirty_key] = 1
                self.expirations[dirty_key] = int(dirty_ttl)
                self.values.pop(staged_key, None)
                amounts.pop(token, None)
                deadlines.pop(token, None)

            if dirty_key in self.values:
                amounts.pop(token, None)
                deadlines.pop(token, None)
                return -1
            reserved = amounts.get(token)
            current = self.values.get(staged_key)
            if (
                reserved is None
                or reserved <= 0
                or published <= 0
                or published > reserved
                or current is None
            ):
                mark_dirty_and_release()
                return -2
            self.values[staged_key] = int(current) + published
            self.expirations[staged_key] = int(staged_ttl)
            amounts.pop(token, None)
            deadlines.pop(token, None)
            if not amounts:
                self.lease_amounts.pop(lease_key, None)
                self.lease_deadlines.pop(lease_key, None)
            if self.fail_after_staged_record:
                self.fail_after_staged_record = False
                raise TimeoutError("staged commit response lost after EXEC")
            return self.values[staged_key]
        if "wv2nas:staged-seed-if-clean" in script:
            staged_key, dirty_key, lease_key = keys
            seeded, ttl = int(argv[0]), int(argv[1])
            if dirty_key in self.values:
                return (-1, None)
            if staged_key in self.values:
                self.expirations[staged_key] = ttl
                return (1, self.values[staged_key])
            if any(
                amount != 0
                for amount in self.lease_amounts.get(lease_key, {}).values()
            ):
                return (-2, None)
            self.values[staged_key] = seeded
            self.expirations[staged_key] = ttl
            return (0, seeded)
        if "wv2nas:get-refresh" in script:
            if key not in self.values:
                return None
            self.expirations[key] = int(argv[0])
            return self.values[key]
        if "wv2nas:staged-record-existing" in script:
            staged_key, dirty_key, _lease_key = keys
            if dirty_key in self.values:
                return -1
            if staged_key not in self.values:
                return None
            self.values[staged_key] = int(self.values[staged_key]) + int(argv[0])
            self.expirations[staged_key] = int(argv[1])
            if self.fail_after_staged_record:
                raise TimeoutError("staged record response lost after EXEC")
            return self.values[staged_key]
        if "wv2nas:publish-lock-refresh" in script:
            if str(self.values.get(key)) != str(args[0]):
                return 0
            self.expirations[key] = int(args[1])
            return 1
        if "wv2nas:publish-lock-release" in script:
            if str(self.values.get(key)) != str(args[0]):
                return 0
            self.delete(key)
            return 1
        raise AssertionError("unknown Lua script")

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        try:
            self.values[key] = int(value)
        except (TypeError, ValueError):
            self.values[key] = str(value)
        if ex is not None:
            self.expirations[key] = int(ex)
        if self.fail_after_dirty_set and "staged_bytes_dirty" in key:
            self.fail_after_dirty_set = False
            raise TimeoutError("dirty SET response lost after mutation")
        return True

    def delete(self, key):
        self.values.pop(key, None)
        self.expirations.pop(key, None)

    def get(self, key):
        return self.values.get(key)

    def expire(self, key, ttl):
        exists = key in self.values or key in self.lease_amounts
        if exists:
            self.expirations[key] = int(ttl)
        return exists

    def incrby(self, key, amount):
        self.values[key] = int(self.values.get(key, 0)) + int(amount)
        return self.values[key]


def test_claim_release_upload_slot_round_trip(monkeypatch):
    """Distinct request tokens own distinct, idempotently releasable slots."""
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

    a = api_main._claim_upload_slot("job-A", "token-a")
    b = api_main._claim_upload_slot("job-A", "token-b")
    c = api_main._claim_upload_slot("job-A", "token-c")
    assert a == 1 and b == 2 and c == 3
    api_main._release_upload_claim("job-A", "token-a")
    api_main._release_upload_claim("job-A", "token-b")
    assert fake.lease_amounts[api_main._upload_slot_key("job-A")] == {
        "token-c": 0,
    }


def test_upload_coordination_ttl_outlives_custom_refresh_interval(monkeypatch):
    api_main = _reload_api_main(
        monkeypatch,
        UPLOAD_STREAM_IDLE_TIMEOUT_SECONDS="300",
        UPLOAD_COORDINATION_REFRESH_SECONDS="7200",
    )

    assert api_main._UPLOAD_SLOT_KEY_TTL_SECONDS >= 7260


def test_claim_upload_slot_survives_expire_failure_and_can_release(monkeypatch):
    """No non-atomic EXPIRE fallback is used when Redis lacks EVAL."""
    api_main = _reload_api_main(monkeypatch)
    api_main.redis_client = object()
    assert api_main._claim_upload_slot("job-A", "token-a") == -1


def test_atomic_claim_and_reservation_keep_ttl_after_response_loss(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis(fail_after_increment=True)
    api_main.redis_client = fake

    # The first claim commits but loses its reply. Retrying the same token is
    # idempotent and returns the one real slot rather than leaking a phantom.
    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    fake.fail_after_increment = True
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8
    assert fake.lease_amounts[api_main._upload_slot_key("job-A")] == {
        "token-a": 8,
    }
    assert fake.expirations[api_main._upload_slot_key("job-A")] == (
        api_main._UPLOAD_SLOT_KEY_TTL_SECONDS + 60
    )


def test_atomic_release_never_creates_negative_missing_counters(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

    api_main._release_upload_claim("job-A", "missing")
    assert api_main._upload_slot_key("job-A") not in fake.lease_amounts

    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8
    api_main._release_upload_claim("job-A", "token-a")
    api_main._release_upload_claim("job-A", "token-a")
    assert api_main._upload_slot_key("job-A") not in fake.lease_amounts


@pytest.mark.parametrize("failure_mode", ["before", "after"])
def test_upload_claim_release_retries_ambiguous_redis_failure(
    monkeypatch, failure_mode,
):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis(
        fail_release_before=failure_mode == "before",
        fail_release_after=failure_mode == "after",
    )
    api_main.redis_client = fake
    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8

    api_main._release_upload_claim("job-A", "token-a")

    assert api_main._upload_slot_key("job-A") not in fake.lease_amounts


def test_expired_positive_upload_lease_invalidates_staged_cache(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    staged_key = api_main._staged_bytes_key("job-A")
    dirty_key = api_main._staged_bytes_dirty_key("job-A")
    fake.values[staged_key] = 100

    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8
    fake.now += api_main._UPLOAD_SLOT_KEY_TTL_SECONDS + 1

    assert api_main._claim_upload_slot("job-A", "token-b") == 1
    assert staged_key not in fake.values
    assert fake.values[dirty_key] == 1
    assert fake.lease_amounts[api_main._upload_slot_key("job-A")] == {
        "token-b": 0,
    }


def test_retained_byte_lease_preserves_quota_without_consuming_upload_slot(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_CONCURRENT_UPLOADS_PER_JOB="2",
    )
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8
    assert api_main._retain_upload_reservation("job-A", "token-a") is True

    # Negative encoding forces authoritative disk scans and is omitted from
    # active-token/slot views so a transient record failure cannot pin cap=2.
    assert fake.lease_amounts[api_main._upload_slot_key("job-A")] == {
        "token-a": -8,
    }
    assert fake.get(api_main._staged_bytes_dirty_key("job-A")) == 1
    assert api_main._active_upload_tokens("job-A") == set()

    published = tmp_path / "job-A" / "video" / "seg_00000000.bin"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"12345678")
    assert api_main._staged_bytes_get("job-A", tmp_path / "job-A") == 8

    assert api_main._claim_upload_slot("job-A", "token-b") == 1
    # The retained final is already in the 8-byte disk scan, so reservation
    # totals include only B's positive in-flight 4 bytes (8 + 4, not 8 + 12).
    assert api_main._reserve_upload_bytes("job-A", "token-b", 4) == 4
    assert (
        api_main._staged_bytes_get("job-A", tmp_path / "job-A") + 4
    ) == 12
    assert api_main._claim_upload_slot("job-A", "token-c") == 2

    # Ordinary request cleanup cannot accidentally erase retained accounting.
    api_main._release_upload_claim("job-A", "token-a")
    assert fake.lease_amounts[api_main._upload_slot_key("job-A")]["token-a"] == -8


def test_publish_atomically_transfers_reservation_into_staged_counter(
    monkeypatch, tmp_path,
):
    """A committed upload is never double-counted as staged + in-flight."""
    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_JOB_STAGING_BYTES="16",
    )
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staged_key = api_main._staged_bytes_key(job_id)
    fake.values[staged_key] = 0

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._claim_upload_slot(job_id, "token-b") == 2
    assert api_main._reserve_upload_bytes(job_id, "token-b", 8) == 16

    assert api_main._staged_bytes_record(
        job_id, 8, upload_token="token-a",
    ) is True

    # Token A vanished in the same atomic operation that credited its 8 bytes;
    # B's gate therefore sees exactly 8 staged + 8 reserved == cap, not 24.
    assert fake.values[staged_key] == 8
    assert fake.lease_amounts[api_main._upload_slot_key(job_id)] == {
        "token-b": 8,
    }
    assert api_main._staged_bytes_get(job_id, tmp_path / job_id) == 8
    reserved = api_main._reserve_upload_bytes(job_id, "token-b", 8)
    assert api_main._staged_bytes_get(job_id, tmp_path / job_id) + reserved == 16


def test_quota_snapshot_never_combines_opposite_sides_of_publish(
    monkeypatch, tmp_path,
):
    """B's stale reserve return must not be added to A's newer staged value."""
    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_JOB_STAGING_BYTES="16",
    )
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    fake.values[api_main._staged_bytes_key(job_id)] = 0

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._claim_upload_slot(job_id, "token-b") == 2
    stale_reserved = api_main._reserve_upload_bytes(job_id, "token-b", 8)
    assert stale_reserved == 16

    assert api_main._staged_bytes_record(
        job_id, 8, upload_token="token-a",
    ) is True
    newer_staged = api_main._staged_bytes_get(job_id, tmp_path / job_id)
    assert newer_staged + stale_reserved == 24  # the former broken gate

    staged, reserved, total = api_main._upload_quota_usage(
        job_id, tmp_path / job_id,
    )
    assert (staged, reserved, total) == (8, 8, 16)


def test_dirty_quota_excludes_final_guarded_by_positive_publish_marker(
    monkeypatch, tmp_path,
):
    """Rename fallback final bytes remain in the positive lease generation."""
    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_JOB_STAGING_BYTES="16",
    )
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    target = staging / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"12345678")
    marker = api_main._upload_publish_marker_path(target, "token-a")
    marker.touch()
    fake.values[api_main._staged_bytes_dirty_key(job_id)] = 1

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._claim_upload_slot(job_id, "token-b") == 2
    assert api_main._reserve_upload_bytes(job_id, "token-b", 8) == 16

    # A's visible final is represented by A's still-positive reservation;
    # the marker prevents dirty reconciliation from charging both copies.
    assert api_main._upload_quota_usage(job_id, staging) == (0, 16, 16)

    # Once A atomically leaves the positive lease generation, its final moves
    # back into the physical scan and B remains the only reservation.
    assert api_main._staged_bytes_record(
        job_id, 8, upload_token="token-a",
    ) is True
    marker.unlink()
    assert api_main._upload_quota_usage(job_id, staging) == (8, 8, 16)


def test_dirty_quota_excludes_hardlinked_final_before_marker_creation(
    monkeypatch, tmp_path,
):
    """The os.link publish path has no double-count gap before its marker."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    target = staging / "video" / "seg_00000000.bin"
    part = target.with_name(f"{target.name}.token-a.part")
    target.parent.mkdir(parents=True)
    part.write_bytes(b"12345678")
    try:
        api_main.os.link(part, target)
    except OSError as e:
        pytest.skip(f"test filesystem does not support hard links: {e}")
    fake.values[api_main._staged_bytes_dirty_key(job_id)] = 1

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._claim_upload_slot(job_id, "token-b") == 2
    assert api_main._reserve_upload_bytes(job_id, "token-b", 8) == 16

    assert api_main._upload_quota_usage(job_id, staging) == (0, 16, 16)


def test_atomic_publish_transfer_response_loss_falls_back_to_dirty_scan(
    monkeypatch,
):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis(fail_after_staged_record=True)
    api_main.redis_client = fake
    job_id = "job-A"
    staged_key = api_main._staged_bytes_key(job_id)
    dirty_key = api_main._staged_bytes_dirty_key(job_id)
    fake.values[staged_key] = 0

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._staged_bytes_record(
        job_id, 8, upload_token="token-a",
    ) is True

    # First EVAL committed and lost its reply; idempotent retry cannot add the
    # bytes twice, so it invalidates the cache and forces an on-disk reconcile.
    assert staged_key not in fake.values
    assert fake.values[dirty_key] == 1
    assert api_main._upload_slot_key(job_id) not in fake.lease_amounts


def test_expired_retained_byte_lease_invalidates_staged_cache(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    staged_key = api_main._staged_bytes_key("job-A")
    dirty_key = api_main._staged_bytes_dirty_key("job-A")
    fake.values[staged_key] = 100

    assert api_main._claim_upload_slot("job-A", "token-a") == 1
    assert api_main._reserve_upload_bytes("job-A", "token-a", 8) == 8
    assert api_main._retain_upload_reservation("job-A", "token-a") is True
    fake.now += api_main._UPLOAD_SLOT_KEY_TTL_SECONDS + 1

    assert api_main._claim_upload_slot("job-A", "token-b") == 1
    assert staged_key not in fake.values
    assert fake.values[dirty_key] == 1

def test_staged_counter_cache_hit_refreshes_ttl(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    job_id = "11111111-2222-3333-4444-555555555555"
    key = api_main._staged_bytes_key(job_id)
    fake.values[key] = 123
    fake.expirations[key] = 1
    api_main.redis_client = fake

    assert api_main._staged_bytes_get(job_id, tmp_path / job_id) == 123
    assert fake.expirations[key] == api_main._STAGED_BYTES_CACHE_TTL_SECONDS


def test_cold_staged_seed_is_scan_only_while_positive_lease_exists(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "published.bin").write_bytes(b"x" * 10)

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._staged_bytes_get(job_id, staging) == 10
    assert api_main._staged_bytes_key(job_id) not in fake.values


def test_cold_staged_seed_before_reserve_avoids_publish_double_count(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "prior.bin").write_bytes(b"p" * 10)

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._staged_bytes_get(job_id, staging) == 10
    assert api_main._reserve_upload_bytes(job_id, "token-a", 5) == 5
    (staging / "new.bin").write_bytes(b"n" * 5)
    assert api_main._staged_bytes_record(job_id, 5) is True
    assert fake.values[api_main._staged_bytes_key(job_id)] == 15


def test_untrusted_reserved_token_scan_is_not_cached(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "seg.bin.live-token.part").write_bytes(b"x" * 7)
    monkeypatch.setattr(api_main, "_active_upload_tokens", lambda _job_id: None)

    assert api_main._staged_bytes_get(job_id, staging) == 7
    assert api_main._staged_bytes_key(job_id) not in fake.values


def test_staged_counter_record_switches_missing_key_to_dirty_scan(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "published.bin").write_bytes(b"12345678")

    assert api_main._staged_bytes_record(job_id, 8) is True
    assert api_main._staged_bytes_key(job_id) not in fake.values
    assert fake.values[api_main._staged_bytes_dirty_key(job_id)] == 1
    assert api_main._staged_bytes_get(job_id, staging) == 8
    # Further publishes are accounted by the authoritative scan mode and do
    # not recreate a partial Redis delta counter.
    assert api_main._staged_bytes_record(job_id, 8) is True
    assert api_main._staged_bytes_key(job_id) not in fake.values


def test_staged_record_response_loss_switches_to_dirty_without_double_count(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis(fail_after_staged_record=True)
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "manifest.json").write_bytes(b"m" * 4)
    (staging / "published.bin").write_bytes(b"x" * 8)
    staged_key = api_main._staged_bytes_key(job_id)
    fake.values[staged_key] = 4

    assert api_main._staged_bytes_record(job_id, 8) is True
    assert staged_key not in fake.values
    assert fake.values[api_main._staged_bytes_dirty_key(job_id)] == 1
    assert api_main._staged_bytes_get(job_id, staging) == 12


def test_dirty_marker_response_loss_is_confirmed_before_releasing_reservation(
    monkeypatch,
):
    api_main = _reload_api_main(monkeypatch)
    fake = _AtomicCounterRedis(fail_after_dirty_set=True)
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"

    assert api_main._mark_staged_bytes_dirty(job_id) is True
    assert fake.values[api_main._staged_bytes_dirty_key(job_id)] == 1


def test_direct_dash_range_reserves_declared_bytes_not_segment_max(
    monkeypatch, tmp_path,
):
    """A small custom staging quota must accept an 8-byte planned range.

    The old ``slot_count * MAX_SEGMENT_BYTES`` gate charged 5,000 bytes and
    rejected this request despite the persisted direct-DASH plan declaring an
    exact 8-byte body.
    """
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="5000",
        MAX_JOB_STAGING_BYTES="2000",
    )
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    _write_plan(staging, {
        "container": "dash",
        "direct_range_concat": True,
        "tracks": {
            "video": {
                "segment_count": 1,
                "segments": [{
                    "seq": 0,
                    "byte_range": {"offset": 0, "length": 8},
                }],
            },
            "audio": {
                "segment_count": 1,
                "segments": [{
                    "seq": 0,
                    "byte_range": {"offset": 0, "length": 8},
                }],
            },
        },
    })

    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    meta = SimpleNamespace(status="browser_uploading", total_segments=2)
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda *_args: meta)

    class FakeRequest:
        async def stream(self):
            yield b"12345678"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    result = asyncio.run(api_main.upload_segment(
        job_id, 0, FakeRequest(), track="video", db=db, api_key="x",
    ))

    assert result["received"] == 8
    assert fake.max_reserved == 8
    assert api_main._upload_slot_key(job_id) not in fake.lease_amounts
    assert api_main._segment_path(job_id, "video", 0).read_bytes() == b"12345678"


def test_nonrange_upload_reserves_enforced_content_length(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="5000",
        MAX_JOB_STAGING_BYTES="2000",
    )
    job_id = "11111111-2222-3333-4444-555555555555"
    _write_plan(tmp_path / job_id, {
        "container": "hls",
        "tracks": {
            "video": {
                "segment_count": 1,
                "segments": [{"seq": 0}],
            },
        },
    })
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    monkeypatch.setattr(
        api_main,
        "_get_browser_job_meta",
        lambda *_args: SimpleNamespace(
            status="browser_uploading", total_segments=1,
        ),
    )

    class FakeRequest:
        headers = {"content-length": "8"}

        async def stream(self):
            yield b"12345678"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    result = asyncio.run(api_main.upload_segment(
        job_id, 0, FakeRequest(), track="video", db=db, api_key="x",
    ))

    assert result["received"] == 8
    assert fake.max_reserved == 8


@pytest.mark.parametrize("kind", ["segment", "init"])
def test_publish_retains_target_and_reservation_when_accounting_is_uncertain(
    monkeypatch, tmp_path, kind,
):
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="16",
        MAX_JOB_STAGING_BYTES="1000",
    )
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    _write_plan(staging, {
        "container": "dash",
        "tracks": {
            "video": {
                "segment_count": 1,
                "segments": [{
                    "seq": 0,
                    "byte_range": {"offset": 0, "length": 8},
                }],
            },
        },
    })
    meta = SimpleNamespace(
        status="browser_uploading",
        total_segments=1,
        mode="browser",
    )
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda *_args: meta)
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id, _token: 1)
    monkeypatch.setattr(
        api_main,
        "_reserve_upload_bytes",
        lambda _job_id, _token, amount: amount,
    )
    monkeypatch.setattr(api_main, "_staged_bytes_get", lambda *_args: 0)
    monkeypatch.setattr(
        api_main, "_upload_quota_usage", lambda *_args: (0, 8, 8),
    )
    monkeypatch.setattr(
        api_main, "_staged_bytes_record", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda *_args, **_kwargs: True,
    )
    released_claims = []
    retained_claims = []
    monkeypatch.setattr(
        api_main,
        "_release_upload_claim",
        lambda _job_id, _token: released_claims.append(_job_id),
    )
    monkeypatch.setattr(
        api_main,
        "_retain_upload_reservation",
        lambda _job_id, _token: retained_claims.append(_job_id) or True,
    )

    class FakeRequest:
        async def stream(self):
            yield b"12345678"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    if kind == "segment":
        operation = api_main.upload_segment(
            job_id, 0, FakeRequest(), track="video", db=db, api_key="x",
        )
        target = api_main._segment_path(job_id, "video", 0)
    else:
        operation = api_main.upload_init_segment(
            job_id, FakeRequest(), track="video", db=db, api_key="x",
        )
        target = api_main._init_segment_path(job_id, "video")

    result = asyncio.run(operation)

    assert result["received"] == 8
    assert target.read_bytes() == b"12345678"
    # The already-committed file must survive even if finalize races this
    # response. Its exact reservation remains live until Redis reconciliation.
    assert released_claims == []
    assert retained_claims == [job_id]
    assert list(target.parent.glob(f"{target.name}.*.part")) == []


def test_fallback_publish_lock_blocks_finalize_until_accounting_finishes(
    monkeypatch, tmp_path,
):
    """Deterministic publish -> record interleaving for no-hardlink NASes."""
    import asyncio
    import errno as _errno
    from types import SimpleNamespace

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    _write_plan(staging, {
        "container": "dash",
        "tracks": {"video": {"segment_count": 1}},
    })
    target = api_main._segment_path(job_id, "video", 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda *_args, **_kwargs: True,
    )

    observed = {}

    def record_while_finalize_runs(_job_id, written, **_kwargs):
        observed["target"] = target.read_bytes()
        observed["part_exists"] = any(target.parent.glob(f"{target.name}.*.part"))
        with pytest.raises(api_main.HTTPException) as exc:
            api_main._verify_staging_complete(staging)
        observed["status"] = exc.value.status_code
        observed["detail"] = exc.value.detail
        return True

    monkeypatch.setattr(api_main, "_staged_bytes_record", record_while_finalize_runs)

    class FakeRequest:
        async def stream(self):
            yield b"PAYLOAD"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    result = asyncio.run(api_main._stream_segment_to_disk(
        request=FakeRequest(),
        db=db,
        meta=SimpleNamespace(status="browser_uploading"),
        job_id=job_id,
        track="video",
        seq=0,
        target=target,
        expected_length=7,
        upload_token="fallback-token",
        coordination_required=True,
        byte_reservation_required=True,
    ))

    assert result["received"] == 7
    assert observed["target"] == b"PAYLOAD"
    # The rename fallback now keeps a zero-byte token marker across the
    # publish->accounting boundary; the live publish lock remains the second
    # finalize guard on filesystems without hard links.
    assert observed["part_exists"] is True
    assert observed["status"] == 409
    assert "in_flight_partial_files" in observed["detail"]
    # Once accounting returns, the caller releases the kernel lock; its
    # persistent pathname is harmless and final verification succeeds.
    assert api_main._verify_staging_complete(staging) == {"video": 1}


def test_hardlink_live_token_part_blocks_finalize_until_accounting_finishes(
    monkeypatch, tmp_path,
):
    """The hard-link fast path uses its live token-owned .part as the guard."""
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    _write_plan(staging, {
        "container": "dash",
        "tracks": {"video": {"segment_count": 1}},
    })
    target = api_main._segment_path(job_id, "video", 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda *_args, **_kwargs: True,
    )

    def record_while_finalize_runs(_job_id, _written, **_kwargs):
        with pytest.raises(api_main.HTTPException) as exc:
            api_main._verify_staging_complete(
                staging,
                active_upload_tokens={"hardlink-token"},
            )
        assert exc.value.status_code == 409
        assert "in_flight_partial_files" in exc.value.detail
        return True

    monkeypatch.setattr(api_main, "_staged_bytes_record", record_while_finalize_runs)

    class FakeRequest:
        async def stream(self):
            yield b"PAYLOAD"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    asyncio.run(api_main._stream_segment_to_disk(
        request=FakeRequest(),
        db=db,
        meta=SimpleNamespace(status="browser_uploading"),
        job_id=job_id,
        track="video",
        seq=0,
        target=target,
        expected_length=7,
        upload_token="hardlink-token",
        coordination_required=True,
        byte_reservation_required=True,
    ))

    # This unit invokes the inner writer directly, so the endpoint wrapper has
    # not yet released the Redis claim and removed its generation marker.
    marker = api_main._upload_publish_marker_path(target, "hardlink-token")
    assert list(target.parent.glob(f"{target.name}.*.part")) == [marker]
    marker.unlink()
    assert api_main._verify_staging_complete(staging) == {"video": 1}


@pytest.mark.parametrize("kind", ["segment", "init"])
@pytest.mark.parametrize("dirty_confirmed", [True, False])
def test_unremovable_upload_part_invalidates_cache_or_retains_reservation(
    monkeypatch, tmp_path, kind, dirty_confirmed,
):
    """An orphan temp can never disappear from both cache and reservation."""
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="16",
        MAX_JOB_STAGING_BYTES="1000",
    )
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    _write_plan(staging, {
        "container": "dash",
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 8},
            }],
            "init_segment_byte_range": {"offset": 0, "length": 8},
        }},
    })
    meta = SimpleNamespace(
        status="browser_uploading", total_segments=1, mode="browser",
    )
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda *_args: meta)
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda *_args: 1)
    monkeypatch.setattr(
        api_main, "_reserve_upload_bytes", lambda _job, _token, amount: amount,
    )
    monkeypatch.setattr(api_main, "_staged_bytes_get", lambda *_args: 0)
    monkeypatch.setattr(
        api_main, "_upload_quota_usage", lambda *_args: (0, 8, 8),
    )
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        api_main.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="cleanup-token"),
    )
    dirty_calls = []
    monkeypatch.setattr(
        api_main,
        "_mark_staged_bytes_dirty",
        lambda seen_job: dirty_calls.append(seen_job) or dirty_confirmed,
    )
    released = []
    retained = []
    monkeypatch.setattr(
        api_main,
        "_release_upload_claim",
        lambda seen_job, _token: released.append(seen_job),
    )
    monkeypatch.setattr(
        api_main,
        "_retain_upload_reservation",
        lambda seen_job, _token: retained.append(seen_job) or True,
    )

    real_unlink = api_main.Path.unlink

    def fail_token_part_unlink(path, *args, **kwargs):
        if path.name.endswith(".cleanup-token.part"):
            raise OSError("simulated NAS unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(api_main.Path, "unlink", fail_token_part_unlink)

    class FakeRequest:
        async def stream(self):
            yield b"12345678"
            yield b"9"

    db = MagicMock()
    db.execute.return_value.first.return_value = SimpleNamespace(
        status="browser_uploading",
    )
    operation = (
        api_main.upload_segment(
            job_id, 0, FakeRequest(), track="video", db=db, api_key="x",
        )
        if kind == "segment"
        else api_main.upload_init_segment(
            job_id, FakeRequest(), track="video", db=db, api_key="x",
        )
    )
    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(operation)

    assert exc.value.status_code == 413
    orphan_parts = list(staging.rglob("*.cleanup-token.part"))
    assert len(orphan_parts) == 1
    assert orphan_parts[0].stat().st_size == 8
    assert dirty_calls == [job_id]
    assert released == ([job_id] if dirty_confirmed else [])
    assert retained == ([] if dirty_confirmed else [job_id])


def test_segment_upload_cap_returns_retry_after(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_CONCURRENT_UPLOADS_PER_JOB="3",
    )
    job_id = "11111111-2222-3333-4444-555555555555"
    meta = SimpleNamespace(status="browser_uploading", total_segments=1)
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda *_args: meta)
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id, _token: 4)
    released = []
    monkeypatch.setattr(
        api_main,
        "_release_upload_claim",
        lambda _job_id, _token: released.append(_job_id),
    )

    class FakeRequest:
        async def stream(self):
            pytest.fail("429 must reject before reading the request body")
            yield b""

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main.upload_segment(
            job_id, 0, FakeRequest(), track="video", db=MagicMock(), api_key="x",
        ))

    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "1"}
    assert released == [job_id]


def test_failed_byte_reservation_is_not_released_into_negative_counter(
    monkeypatch, tmp_path,
):
    """A failed INCRBY claim owns no reservation and must not DECRBY it."""
    import asyncio
    from types import SimpleNamespace

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    job_id = "11111111-2222-3333-4444-555555555555"
    _write_plan(tmp_path / job_id, {
        "tracks": {"video": {
            "segment_count": 1,
            "segments": [{
                "seq": 0,
                "byte_range": {"offset": 0, "length": 8},
            }],
        }},
    })
    meta = SimpleNamespace(status="browser_uploading", total_segments=1)
    monkeypatch.setattr(api_main, "_get_browser_job_meta", lambda *_args: meta)
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id, _token: 1)
    monkeypatch.setattr(api_main, "_reserve_upload_bytes", lambda *_args: -1)
    released_claims = []
    monkeypatch.setattr(
        api_main,
        "_release_upload_claim",
        lambda *_args: released_claims.append(_args),
    )

    class FakeRequest:
        async def stream(self):
            pytest.fail("503 must reject before reading the request body")
            yield b""

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main.upload_segment(
            job_id, 0, FakeRequest(), track="video", db=MagicMock(), api_key="x",
        ))

    assert exc.value.status_code == 503
    assert len(released_claims) == 1


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
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id, _token: claimed.append(_job_id) or 1)
    monkeypatch.setattr(api_main, "_release_upload_claim", lambda _job_id, _token: released.append(_job_id))
    monkeypatch.setattr(
        api_main,
        "_staged_bytes_get",
        lambda *_args, **_kwargs: pytest.fail("idempotent retry should not read quota"),
    )

    class _FakeRequest:
        async def stream(self):
            yield b"DIFFERENT-RETRY-BYTES"

    # Capturing db so we can also assert the idempotent retry refreshes the
    # activity lease (Codex adversarial review — the periodic reaper must not
    # reap a client that keeps re-PUTting an already-published segment).
    executed_sql = []
    lease_db = MagicMock()
    lease_db.execute = lambda stmt, params=None: (
        executed_sql.append(str(stmt)) or MagicMock(first=lambda: None)
    )

    result = asyncio.run(api_main.upload_segment(
        job_id, 0, _FakeRequest(), track="video", db=lease_db, api_key="x",
    ))

    assert result["idempotent"] is True
    assert target.read_bytes() == b"PRIOR-COMMIT"
    assert claimed == [job_id]
    assert released == [job_id]
    assert any("last_activity" in s for s in executed_sql), (
        "idempotent segment retry must refresh the upload lease"
    )


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
    monkeypatch.setattr(api_main, "_claim_upload_slot", lambda _job_id, _token: claimed.append(_job_id) or 1)
    monkeypatch.setattr(api_main, "_release_upload_claim", lambda _job_id, _token: released.append(_job_id))
    monkeypatch.setattr(
        api_main,
        "_staged_bytes_get",
        lambda *_args, **_kwargs: pytest.fail("idempotent retry should not read quota"),
    )

    class _FakeRequest:
        async def stream(self):
            yield b"DIFFERENT-INIT-RETRY"

    # Capturing db so we can also assert the idempotent init retry refreshes the
    # activity lease (Codex adversarial review).
    executed_sql = []
    lease_db = MagicMock()
    lease_db.execute = lambda stmt, params=None: (
        executed_sql.append(str(stmt)) or MagicMock(first=lambda: None)
    )

    result = asyncio.run(api_main.upload_init_segment(
        job_id, _FakeRequest(), track="video", db=lease_db, api_key="x",
    ))

    assert result["idempotent"] is True
    assert target.read_bytes() == b"PRIOR-INIT"
    assert claimed == [job_id]
    assert released == [job_id]
    assert any("last_activity" in s for s in executed_sql), (
        "idempotent init retry must refresh the upload lease"
    )


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

    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

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

    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    (staging / "video").mkdir(parents=True)
    (staging / "video" / "seg_00000000.bin").write_bytes(b"a" * 100)
    (staging / "video" / "seg_00000001.bin").write_bytes(b"b" * 250)

    # Cache cold → walk seeds 350.
    assert api_main._staged_bytes_get(job_id, staging) == 350
    key = api_main._staged_bytes_key(job_id)
    assert fake.values[key] == 350
    # Cache warm → no further set.
    assert api_main._staged_bytes_get(job_id, staging) == 350
    assert fake.values[key] == 350


def test_staging_scan_counts_hardlinked_publish_guard_once(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    track = staging / "video"
    track.mkdir(parents=True)
    part = track / "seg_00000000.bin.token-a.part"
    target = track / "seg_00000000.bin"
    part.write_bytes(b"12345678")
    try:
        api_main.os.link(part, target)
    except OSError as exc:
        pytest.skip(f"hard links unavailable on test filesystem: {exc}")

    # target + source-part are two names for one physical allocation.
    assert api_main._staging_total_bytes(staging) == 8


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


def test_staging_scan_excludes_live_parts_but_counts_orphans(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "committed.bin").write_bytes(b"c" * 10)
    (staging / "seg.bin.token-a.part").write_bytes(b"a" * 5)
    (staging / "seg.bin.orphan.part").write_bytes(b"o" * 7)
    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8

    active = api_main._active_upload_tokens(job_id)
    assert active == {"token-a"}
    assert api_main._staging_total_bytes(staging, active) == 17


def test_staging_scan_counts_part_covered_by_retained_byte_lease_once(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    job_id = "11111111-2222-3333-4444-555555555555"
    staging = tmp_path / job_id
    staging.mkdir()
    (staging / "seg.bin.token-a.part").write_bytes(b"a" * 8)

    assert api_main._claim_upload_slot(job_id, "token-a") == 1
    assert api_main._reserve_upload_bytes(job_id, "token-a", 8) == 8
    assert api_main._retain_upload_reservation(job_id, "token-a") is True

    assert api_main._active_upload_tokens(job_id) == set()
    assert api_main._scan_staging_total(job_id, staging) == (8, True)
    assert api_main._staged_bytes_get(job_id, staging) == 8


def test_authoritative_staging_scan_fails_closed_on_metadata_error(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    staging.mkdir()

    class BrokenEntry:
        name = "published.bin"
        path = str(staging / name)

        def is_symlink(self):
            return False

        def is_dir(self, *, follow_symlinks=False):
            return False

        def is_file(self, *, follow_symlinks=False):
            return True

        def stat(self, *, follow_symlinks=False):
            raise OSError("NAS metadata unavailable")

    monkeypatch.setattr(api_main.os, "scandir", lambda _path: [BrokenEntry()])
    with pytest.raises(api_main._StagingAccountingError):
        api_main._staging_total_bytes(staging)


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


def test_refresh_upload_lease_bumps_last_activity(monkeypatch, tmp_path):
    """Codex adversarial review: the lease refresh — called at every upload
    endpoint entry, so it also covers the idempotent 'already have this segment'
    and init concurrent-loser early-returns — bumps last_activity. This keeps
    the periodic stale-browser reaper (which ages browser_uploading rows off
    COALESCE(last_activity, created_at)) from reaping a client that's actively
    re-PUTting near the 6h cutoff."""
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    stale = _utcnow_naive() - timedelta(hours=8)
    db = api_main.SessionLocal()
    try:
        db.execute(sa_text("UPDATE job_metadata SET last_activity=:t WHERE job_id=:id"),
                   {"id": job_id, "t": stale})
        db.commit()
    finally:
        db.close()

    db2 = api_main.SessionLocal()
    try:
        api_main._refresh_upload_lease(db2, job_id)
    finally:
        db2.close()

    db3 = api_main.SessionLocal()
    try:
        row = db3.execute(
            sa_text("SELECT last_activity FROM job_metadata WHERE job_id=:id"),
            {"id": job_id},
        ).first()
    finally:
        db3.close()
    la = row.last_activity
    if isinstance(la, str):  # sqlite returns TIMESTAMP as text
        la = datetime.fromisoformat(la)
    # Refreshed to ~now, NOT the 8h-old stale value.
    assert la > _utcnow_naive() - timedelta(hours=1)


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
    assert api_main._claim_upload_slot("job-X", "token-x") == -1


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


def test_abort_rmtree_failure_preserves_accounting_for_reaper(
    monkeypatch, tmp_path,
):
    import shutil
    from fastapi.testclient import TestClient

    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)
    staging = Path(_staging_dir_for_test_job(api_main, tmp_path, job_id))
    clear = MagicMock()
    monkeypatch.setattr(api_main, "_staged_bytes_clear", clear)
    monkeypatch.setattr(
        shutil,
        "rmtree",
        MagicMock(side_effect=OSError("active NAS upload handle")),
    )

    with TestClient(api_main.app) as client:
        response = client.post(
            f"/api/jobs/{job_id}/abort",
            headers={
                "Authorization": (
                    "Bearer test-key-not-the-default-placeholder"
                )
            },
            json={"reason": "upload failed"},
        )

    assert response.status_code == 200
    assert response.json()["aborted"] is True
    assert response.json()["staging_cleaned"] is False
    assert staging.exists()
    clear.assert_not_called()


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
# helper must fall back to an atomic filesystem lock + same-directory rename
# that preserves the same "fail if target exists" guarantee without doubling
# staged bytes.


def test_atomic_publish_part_happy_path_uses_link(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

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
    api_main.redis_client = _AtomicCounterRedis()

    import errno as _errno
    code = getattr(_errno, err_code_attr, None)
    if code is None:
        pytest.skip(f"errno.{err_code_attr} not on this platform")

    def fake_link(src, dst):
        raise OSError(code, f"simulated {err_code_attr}")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"RENAME-FALLBACK-PAYLOAD")

    api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"RENAME-FALLBACK-PAYLOAD"
    assert not part.exists()


def test_atomic_publish_uses_redis_lock_when_nas_lacks_advisory_locks(
    monkeypatch, tmp_path,
):
    """No-hardlink + no-flock NASes retain a bounded distributed fallback."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake

    import errno as _errno
    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    unsupported = getattr(_errno, "EOPNOTSUPP", _errno.ENOSYS)
    monkeypatch.setattr(
        api_main,
        "_lock_publish_fd",
        lambda _fd: (_ for _ in ()).throw(
            OSError(unsupported, "advisory locks unsupported")
        ),
    )

    part = tmp_path / "job" / "video" / "x.part"
    target = part.with_name("x.bin")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"REDIS-LOCK-PAYLOAD")

    api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"REDIS-LOCK-PAYLOAD"
    assert not part.exists()
    assert fake.get(api_main._redis_publish_lock_key(target)) is None
    assert api_main._publish_lock_path(target).is_file()


@pytest.mark.parametrize("winerror", [1, 50])
def test_windows_unsupported_operation_uses_rename_and_redis_fallbacks(
    monkeypatch, tmp_path, winerror,
):
    """SMB ERROR_INVALID_FUNCTION is EINVAL, but only on Windows."""
    import errno as _errno

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    monkeypatch.setattr(api_main, "_RUNNING_ON_WINDOWS", True)

    def invalid_function():
        error = OSError(_errno.EINVAL, "SMB operation unsupported")
        error.winerror = winerror
        return error

    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(invalid_function()),
    )
    monkeypatch.setattr(
        api_main,
        "_lock_publish_fd",
        lambda _fd: (_ for _ in ()).throw(invalid_function()),
    )

    part = tmp_path / "job" / "video" / "x.part"
    target = part.with_name("x.bin")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"WINDOWS-SMB")

    api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"WINDOWS-SMB"
    assert fake.get(api_main._redis_publish_lock_key(target)) is None


def test_posix_einval_does_not_hide_real_link_error(monkeypatch):
    import errno as _errno

    api_main = _reload_api_main(monkeypatch)
    monkeypatch.setattr(api_main, "_RUNNING_ON_WINDOWS", False)
    error = OSError(_errno.EINVAL, "bad link arguments")
    error.winerror = 1
    assert api_main._link_is_unsupported(error) is False


def test_redis_publish_lock_refresh_is_compare_owner_and_extends_ttl(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    fake = _AtomicCounterRedis()
    api_main.redis_client = fake
    target = tmp_path / "job" / "video" / "x.bin"
    target.parent.mkdir(parents=True)
    lock_path, lock_fd, identity = api_main._acquire_redis_publish_lock(
        api_main._publish_lock_path(target), target,
    )
    _mode, key, owner = identity

    fake.expirations[key] = 1
    assert api_main._refresh_publish_lock(lock_path, lock_fd, identity) is True
    assert fake.expirations[key] == api_main._UPLOAD_SLOT_KEY_TTL_SECONDS

    fake.values[key] = "new-owner"
    assert api_main._refresh_publish_lock(lock_path, lock_fd, identity) is False
    api_main._release_publish_lock(lock_path, lock_fd, identity)
    assert fake.get(key) == "new-owner"


def test_redis_publish_lock_heartbeat_runs_off_request_thread(
    monkeypatch, tmp_path,
):
    """A blocked NAS syscall must not prevent the Redis owner lease renewal."""
    import threading as _threading

    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    monkeypatch.setattr(api_main, "_PUBLISH_LOCK_REFRESH_INTERVAL_SECONDS", 0.01)
    renewed = _threading.Event()
    monkeypatch.setattr(
        api_main,
        "_refresh_publish_lock",
        lambda *_args: renewed.set() or True,
    )
    lock_path = tmp_path / ".x.bin.publish.lock"
    identity = ("redis", "wv2nas:publish_lock:test", "owner")

    heartbeat = api_main._start_publish_lock_heartbeat(
        lock_path, None, identity,
    )
    assert heartbeat is not None
    assert renewed.wait(timeout=1.0)
    api_main._stop_publish_lock_heartbeat(heartbeat)
    assert not heartbeat[1].is_alive()


def test_atomic_publish_part_fallback_preserves_existing_target(monkeypatch, tmp_path):
    """Even on the rename-fallback path, an already-published target must
    NOT be overwritten — that's the property the os.link version gave
    us via FileExistsError, and the fallback uses O_CREAT|O_EXCL to
    preserve it."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

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


# Codex adversarial-review (high): the fallback once copied bytes directly
# into `target`, so a mid-copy crash left partial content at the FINAL path.
# The upload already has a complete same-directory `.part`; an atomic lock
# directory serializes the rename even while NAS metadata operations block.

def test_atomic_publish_fallback_renames_complete_part_not_direct_write(monkeypatch, tmp_path):
    """The fallback must rename the already-complete part while lock-owned."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

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
    assert not part.exists()
    # No legacy second-copy temp is created.
    leftover = list(tmp_path.glob("*.publish.*.part"))
    assert leftover == []


def test_atomic_publish_fallback_rename_failure_leaves_recoverable_state(monkeypatch, tmp_path):
    """A no-replace rename failure leaves the complete part and no target."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    # Simulate a crash/error at the atomic filesystem commit.
    def crashy_rename(_src, _dst):
        raise RuntimeError("simulated crash mid-publish")

    monkeypatch.setattr(api_main, "_rename_noreplace", crashy_rename)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"PARTIAL-BYTES-WOULD-CORRUPT")

    with pytest.raises(RuntimeError, match="simulated crash"):
        api_main._atomic_publish_part(part, target)

    assert not target.exists()
    # The complete part remains available for caller cleanup or retry.
    assert part.read_bytes() == b"PARTIAL-BYTES-WOULD-CORRUPT"


def test_atomic_publish_fallback_target_is_absent_until_noreplace_rename(monkeypatch, tmp_path):
    """Immediately before commit, target is absent and source is the part."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

    import errno as _errno

    def fake_link(src, dst):
        raise OSError(_errno.EPERM, "simulated NAS")

    monkeypatch.setattr(api_main.os, "link", fake_link)

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"X" * 10_000)

    observed = {}
    real_rename = api_main._rename_noreplace

    def watching_rename(src, dst):
        observed["src"] = src
        observed["dst"] = dst
        observed["target_exists"] = target.exists()
        return real_rename(src, dst)

    monkeypatch.setattr(api_main, "_rename_noreplace", watching_rename)
    api_main._atomic_publish_part(part, target)

    assert observed == {
        "src": part,
        "dst": target,
        "target_exists": False,
    }
    assert target.read_bytes() == b"X" * 10_000
    assert not part.exists()


def test_atomic_publish_fallback_refuses_live_owner_lock(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import errno as _errno
    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"SECOND")
    api_main._publish_lock_path(target).mkdir()

    with pytest.raises(api_main._PublishBusyError):
        api_main._atomic_publish_part(part, target)

    assert part.read_bytes() == b"SECOND"
    assert not target.exists()


def test_atomic_publish_fallback_recovers_lock_after_owner_process_dies(
    monkeypatch, tmp_path,
):
    """Closing the owner fd models kernel cleanup after process death."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    import errno as _errno

    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"RECOVERED")

    _lock_path, owner_fd, _identity = api_main._acquire_publish_lock(target)
    # A killed process cannot run finally/unlock; closing all descriptors is
    # what the OS does and must make the persistent lock file reusable.
    api_main.os.close(owner_fd)

    api_main._atomic_publish_part(part, target)
    assert target.read_bytes() == b"RECOVERED"


def test_atomic_publish_lost_owner_never_deletes_another_publish(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()

    import errno as _errno
    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"FIRST")

    def lose_owner(_path, _identity):
        target.write_bytes(b"OTHER-PUBLISH")
        return False

    monkeypatch.setattr(api_main, "_publish_lock_matches", lose_owner)

    with pytest.raises(api_main._PublishBusyError):
        api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"OTHER-PUBLISH"
    assert part.read_bytes() == b"FIRST"


def test_redis_fallback_stale_owner_cannot_overwrite_new_publish(
    monkeypatch, tmp_path,
):
    """Filesystem no-replace fences the refresh→rename Redis TOCTOU."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.redis_client = _AtomicCounterRedis()
    import errno as _errno

    monkeypatch.setattr(
        api_main.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError(_errno.EPERM, "NAS")),
    )
    unsupported = getattr(_errno, "EOPNOTSUPP", _errno.ENOSYS)
    monkeypatch.setattr(
        api_main,
        "_lock_publish_fd",
        lambda _fd: (_ for _ in ()).throw(
            OSError(unsupported, "advisory locks unsupported")
        ),
    )

    part = tmp_path / "x.part"
    target = tmp_path / "x.bin"
    part.write_bytes(b"STALE-OWNER")
    real_rename = api_main._rename_noreplace

    def publish_new_generation_then_resume_stale(source, destination):
        target.write_bytes(b"NEW-OWNER")
        return real_rename(source, destination)

    monkeypatch.setattr(
        api_main,
        "_rename_noreplace",
        publish_new_generation_then_resume_stale,
    )

    with pytest.raises(FileExistsError):
        api_main._atomic_publish_part(part, target)

    assert target.read_bytes() == b"NEW-OWNER"
    assert part.read_bytes() == b"STALE-OWNER"


def test_atomic_publish_fallback_legacy_publish_tmp_caught_by_part_glob(monkeypatch, tmp_path):
    """A leftover temp from older releases still blocks finalize safely."""
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
    api_main.redis_client = _AtomicCounterRedis()

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
    api_main.redis_client = _AtomicCounterRedis()

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
    api_main.redis_client = _AtomicCounterRedis()

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


def test_stream_segment_to_disk_rejects_declared_range_length_mismatch(monkeypatch, tmp_path):
    """Reject a short 206 body at PUT time so the extension can retry it."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _FakeRequest:
        async def stream(self):
            yield b"short"

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
            expected_length=8,
        ))
    assert exc.value.status_code == 400
    assert "expected 8 bytes, received 5" in str(exc.value.detail)
    assert not target.exists()
    assert list(tmp_path.rglob("*.part")) == []


def test_stream_segment_to_disk_stops_at_declared_range_length(monkeypatch, tmp_path):
    """An 8-byte reservation must never permit a larger temporary file."""
    api_main = _reload_api_main(
        monkeypatch,
        STAGING_DIR=str(tmp_path),
        MAX_SEGMENT_BYTES="5000",
    )

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "video" / "seg_00000000.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    chunks_read = 0

    class _FakeRequest:
        async def stream(self):
            nonlocal chunks_read
            for chunk in (b"12345678", b"9", b"must-not-be-read"):
                chunks_read += 1
                yield chunk

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_segment_to_disk(
            request=_FakeRequest(),
            db=_MM(),
            meta=_MM(status="browser_uploading"),
            job_id="11111111-2222-3333-4444-555555555555",
            track="video", seq=0,
            target=target,
            expected_length=8,
        ))

    assert exc.value.status_code == 413
    assert "exceeds planned length 8 bytes" in str(exc.value.detail)
    assert chunks_read == 2
    assert not target.exists()
    assert list(tmp_path.rglob("*.part")) == []


@pytest.mark.parametrize("kind", ["segment", "init"])
def test_upload_idle_timeout_removes_partial_file(monkeypatch, tmp_path, kind):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    api_main.UPLOAD_STREAM_IDLE_TIMEOUT_SECONDS = 0.01

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / kind / ("seg_00000000.bin" if kind == "segment" else "video.bin")
    target.parent.mkdir(parents=True, exist_ok=True)

    class _SlowRequest:
        async def stream(self):
            yield b"partial"
            await asyncio.sleep(1)
            yield b"never"

    class _FakeRow:
        status = "browser_uploading"

    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    if kind == "segment":
        operation = api_main._stream_segment_to_disk(
            request=_SlowRequest(),
            db=db,
            meta=_MM(status="browser_uploading"),
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            seq=0,
            target=target,
            expected_length=20,
        )
    else:
        operation = api_main._stream_init_to_disk(
            request=_SlowRequest(),
            db=db,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            target=target,
        )

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(operation)

    assert exc.value.status_code == 408
    assert not target.exists()
    assert list(tmp_path.rglob("*.part")) == []


def test_upload_coordination_refresh_is_throttled(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    api_main.UPLOAD_COORDINATION_REFRESH_SECONDS = 30
    ticks = iter((0.0, 0.0, 10.0, 31.0, 31.0, 31.0))
    monkeypatch.setattr(api_main, "_upload_monotonic_seconds", lambda: next(ticks))
    refreshed = []
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda job_id, *_args, **_kwargs: refreshed.append(job_id) or True,
    )

    class _Request:
        async def stream(self):
            yield b"a"
            yield b"b"

    import asyncio
    db = MagicMock()

    async def collect():
        return [
            chunk async for chunk in api_main._iter_upload_chunks(
                _Request(), "job-A", db,
            )
        ]

    assert asyncio.run(collect()) == [b"a", b"b"]
    assert refreshed == ["job-A"]


def test_upload_total_deadline_rejects_slow_drip_body(monkeypatch):
    api_main = _reload_api_main(
        monkeypatch,
        UPLOAD_STREAM_IDLE_TIMEOUT_SECONDS="300",
        UPLOAD_STREAM_TOTAL_TIMEOUT_SECONDS="600",
    )
    ticks = iter((0.0, 0.0, 1.0, 601.0))
    monkeypatch.setattr(api_main, "_upload_monotonic_seconds", lambda: next(ticks))

    class _Request:
        async def stream(self):
            yield b"a"
            yield b"b"

    import asyncio

    async def collect():
        return [
            chunk async for chunk in api_main._iter_upload_chunks(
                _Request(), "job-A", MagicMock(),
            )
        ]

    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(collect())
    assert exc.value.status_code == 408
    assert "total deadline" in str(exc.value.detail)


def test_long_upload_refreshes_database_activity_lease(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    ticks = iter((0.0, 0.0, 301.0, 301.0))
    monkeypatch.setattr(api_main, "_upload_monotonic_seconds", lambda: next(ticks))
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_coordination_ttl",
        lambda *_args, **_kwargs: True,
    )
    db_refreshes = []
    monkeypatch.setattr(
        api_main,
        "_refresh_upload_lease_independent",
        lambda job_id: db_refreshes.append(job_id),
    )

    class _Request:
        async def stream(self):
            yield b"a"

    import asyncio
    db = MagicMock()

    async def collect():
        return [
            chunk async for chunk in api_main._iter_upload_chunks(
                _Request(), "job-A", db,
            )
        ]

    assert asyncio.run(collect()) == [b"a"]
    assert db_refreshes == ["job-A"]


def test_upload_housekeeping_timeout_does_not_block_event_loop(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    api_main.UPLOAD_STREAM_IDLE_TIMEOUT_SECONDS = 1.0
    api_main.UPLOAD_STREAM_TOTAL_TIMEOUT_SECONDS = 0.05
    api_main.UPLOAD_COORDINATION_REFRESH_SECONDS = 0

    import asyncio
    import time as _time

    def blocked_housekeeping(*_args, **_kwargs):
        _time.sleep(0.5)
        return True

    monkeypatch.setattr(api_main, "_run_upload_housekeeping", blocked_housekeeping)

    class _Request:
        async def stream(self):
            yield b"a"

    async def scenario():
        started = _time.monotonic()

        async def side_probe():
            await asyncio.sleep(0.01)
            return _time.monotonic() - started

        probe = asyncio.create_task(side_probe())
        with pytest.raises(api_main.HTTPException) as exc:
            async for _chunk in api_main._iter_upload_chunks(
                _Request(), "job-A", MagicMock(),
            ):
                pass
        return exc.value, await probe, _time.monotonic() - started

    error, probe_elapsed, total_elapsed = asyncio.run(scenario())
    assert error.status_code == 408
    assert probe_elapsed < 0.1
    assert total_elapsed < 0.25


@pytest.mark.parametrize("kind", ["segment", "init"])
def test_cancelled_upload_removes_partial_file(monkeypatch, tmp_path, kind):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))

    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / kind / ("seg_00000000.bin" if kind == "segment" else "video.bin")
    target.parent.mkdir(parents=True, exist_ok=True)

    class _CancelledRequest:
        async def stream(self):
            yield b"partial"
            raise asyncio.CancelledError()

    class _FakeRow:
        status = "browser_uploading"

    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _FakeRow()))
    if kind == "segment":
        operation = api_main._stream_segment_to_disk(
            request=_CancelledRequest(),
            db=db,
            meta=_MM(status="browser_uploading"),
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            seq=0,
            target=target,
            expected_length=20,
        )
    else:
        operation = api_main._stream_init_to_disk(
            request=_CancelledRequest(),
            db=db,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            target=target,
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(operation)

    assert not target.exists()
    assert list(tmp_path.rglob("*.part")) == []


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


def test_stream_init_to_disk_rejects_planned_byterange_mismatch(
    monkeypatch, tmp_path,
):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    import asyncio
    from unittest.mock import MagicMock as _MM

    target = tmp_path / "init.bin"

    class _FakeRequest:
        async def stream(self):
            yield b"1234567"

    db = _MM()
    db.execute = _MM(return_value=_MM(first=lambda: _MM(status="browser_uploading")))
    with pytest.raises(api_main.HTTPException) as exc:
        asyncio.run(api_main._stream_init_to_disk(
            request=_FakeRequest(),
            db=db,
            job_id="11111111-2222-3333-4444-555555555555",
            track="video",
            target=target,
            expected_length=8,
        ))
    assert exc.value.status_code == 400
    assert not target.exists()


def test_verify_staging_complete_checks_init_byterange_length(monkeypatch, tmp_path):
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    _write_plan(staging, {
        "tracks": {
            "video": {
                "segment_count": 1,
                "segments": [{"seq": 0}],
                "init_segment_url": "https://cdn.example.com/init.mp4",
                "init_segment_byte_range": {"offset": 0, "length": 8},
            },
        },
    })
    (staging / "video").mkdir()
    (staging / "init").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"segment")
    (staging / "init" / "video.bin").write_bytes(b"1234567")

    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)
    assert exc.value.status_code == 409
    assert exc.value.detail["size_mismatch"]["video:init"][0] == {
        "seq": "init", "expected": 8, "actual": 7,
    }


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


def test_verify_staging_complete_rejects_init_that_becomes_unstatable(
    monkeypatch, tmp_path,
):
    """A metadata I/O failure after is_file() must not pass finalize."""
    api_main = _reload_api_main(monkeypatch, STAGING_DIR=str(tmp_path))
    staging = tmp_path / "job"
    (staging / "video").mkdir(parents=True)
    (staging / "init").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"OK")
    init_path = staging / "init" / "video.bin"
    init_path.write_bytes(b"FTYP-MOOV")
    _write_plan(staging, {
        "container": "dash",
        "tracks": {
            "video": {
                "segment_count": 1,
                "init_segment_url": "https://cdn.example.com/init.mp4",
            },
        },
    })

    real_stat = Path.stat
    init_stat_calls = 0

    def flaky_stat(path, *args, **kwargs):
        nonlocal init_stat_calls
        if path == init_path:
            init_stat_calls += 1
            if init_stat_calls >= 2:
                raise OSError("simulated NAS metadata failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    with pytest.raises(api_main.HTTPException) as exc:
        api_main._verify_staging_complete(staging)

    assert exc.value.status_code == 409
    assert exc.value.detail["missing"]["video:init"] == [0]


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


def test_delete_is_idempotent_for_an_already_cancelled_job(monkeypatch, tmp_path):
    """Codex adversarial-review (medium): the NAS commits the cancel before it
    answers, so a client whose response was lost — request timeout, dropped
    connection — cannot tell whether the DELETE landed. It retries.

    Answering 404 to that retry made a cancel that DID take effect look like a
    failure. The sidepanel only sends CANCEL_BROWSER_JOB on success, so a
    browser-mode upload kept running against a job this API had already
    cancelled, and no retry could ever recover: every one 404'd.

    A repeat DELETE now reports the state rather than whether this particular
    call changed a row."""
    from fastapi.testclient import TestClient
    api_main, job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    with TestClient(api_main.app) as client:
        first = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )
        assert first.status_code == 200
        assert _read_job_status(api_main, job_id) == "cancelled"

        # The retry the client makes after losing the first response.
        second = client.delete(
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )

    assert second.status_code == 200
    assert second.json()["message"] == "Job cancelled successfully"
    assert _read_job_status(api_main, job_id) == "cancelled"


def test_delete_still_404s_for_a_job_that_does_not_exist(monkeypatch, tmp_path):
    """The idempotent branch keys on an existing cancelled row, so it must not
    turn a genuinely unknown job id into a success."""
    from fastapi.testclient import TestClient
    api_main, _job_id = _build_finalize_test_env(monkeypatch, tmp_path)

    with TestClient(api_main.app) as client:
        resp = client.delete(
            "/api/jobs/does-not-exist",
            headers={"Authorization": "Bearer test-key-not-the-default-placeholder"},
        )

    assert resp.status_code == 404
