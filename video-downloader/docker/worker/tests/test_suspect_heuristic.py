"""Unit tests for DownloadWorker._compute_suspect_reason.

The function is a pure @staticmethod over (declared_duration,
actual_duration, file_size_bytes, segments_downloaded, total_segments)
so we don't need a worker instance, redis, or a database — just import.
"""
from worker import DownloadWorker

_compute = DownloadWorker._compute_suspect_reason
_resolve = DownloadWorker._resolve_actual_duration


def test_returns_none_when_no_declared_duration():
    assert _compute(None, 100, 100_000_000) is None
    assert _compute(0, 100, 100_000_000) is None


def test_partial_download_still_flagged_as_suspect():
    # Token expiry leaves a stub: declared 1000s, actual 100s, partial
    # segment success. Must still flag.
    reason = _compute(
        declared_duration=1000,
        actual_duration=100,
        file_size_bytes=20_000_000,  # 20MB / 100s = 200 KB/s — healthy bitrate
        segments_downloaded=80,
        total_segments=100,
    )
    assert reason is not None
    assert "10%" in reason  # 100/1000 = 10%
    assert "partial download" in reason


def test_partial_download_unknown_segment_counts_still_flags():
    # Backfill path: segment counts unavailable. Conservatively keep the
    # duration-shortfall check enabled so legacy stubs still get flagged.
    reason = _compute(
        declared_duration=1000,
        actual_duration=100,
        file_size_bytes=20_000_000,
    )
    assert reason is not None
    assert "partial download" in reason


def test_full_segment_success_with_misleading_m3u8_is_not_flagged():
    # The jav101 case: every segment downloaded successfully, file is a
    # real video with healthy bitrate, but the m3u8 over-reports duration.
    # Must NOT flag — there's no partial download to surface.
    # 773 MB / 3158s ≈ 250 KB/s (matches the user's log).
    reason = _compute(
        declared_duration=7299,
        actual_duration=3158,
        file_size_bytes=773 * 1024 * 1024,
        segments_downloaded=1216,
        total_segments=1216,
    )
    assert reason is None


def test_full_segment_success_but_anti_hotlink_thin_file_still_flags():
    # Even with 100% segment success, if the file is implausibly thin
    # (anti-hotlink CDN serving identical PNG for every segment), the
    # bitrate sanity check must still flag it.
    reason = _compute(
        declared_duration=7299,
        actual_duration=3158,
        file_size_bytes=10 * 1024 * 1024,  # 10MB / 3158s ≈ 3 KB/s — way too thin
        segments_downloaded=1216,
        total_segments=1216,
    )
    assert reason is not None
    assert "anti-hotlink" in reason


def test_healthy_complete_download_not_flagged():
    # Declared and actual roughly agree, healthy bitrate.
    reason = _compute(
        declared_duration=600,
        actual_duration=595,
        file_size_bytes=200 * 1024 * 1024,
        segments_downloaded=300,
        total_segments=300,
    )
    assert reason is None


def test_resolve_actual_duration_jav101_case_falls_back_to_declared():
    # The exact scenario from the user's log: ffprobe said 3158s, m3u8
    # declared 7299s, all 1216/1216 segments succeeded, real playback
    # duration is 7299s. _resolve_actual_duration must return 7299.
    assert _resolve(probe_duration=3158, declared_duration=7299,
                    segments_downloaded=1216, total_segments=1216) == 7299


def test_resolve_actual_duration_keeps_probe_when_no_full_success():
    # Partial download: trust probe — not declared. Otherwise we'd hide
    # the very partial-download case the suspect flag is designed to
    # catch.
    assert _resolve(probe_duration=3158, declared_duration=7299,
                    segments_downloaded=900, total_segments=1216) == 3158


def test_resolve_actual_duration_keeps_probe_when_close_to_declared():
    # Healthy job: probe agrees with declared (within 15%). No fallback.
    assert _resolve(probe_duration=595, declared_duration=600,
                    segments_downloaded=300, total_segments=300) == 595


def test_resolve_actual_duration_passes_through_none():
    # ffprobe failed entirely; nothing to override. Caller's existing
    # ffprobe-failed branch in _compute_suspect_reason handles it.
    assert _resolve(probe_duration=None, declared_duration=7299,
                    segments_downloaded=1216, total_segments=1216) is None


def test_resolve_actual_duration_no_declared_keeps_probe():
    # No m3u8 declared duration → nothing to fall back to.
    assert _resolve(probe_duration=3158, declared_duration=None,
                    segments_downloaded=1216, total_segments=1216) == 3158
    assert _resolve(probe_duration=3158, declared_duration=0,
                    segments_downloaded=1216, total_segments=1216) == 3158


def test_resolve_actual_duration_unknown_segment_counts_keeps_probe():
    # Backfill / non-m3u8 paths don't supply segment counts. Conservative
    # default: trust the probe rather than overriding silently.
    assert _resolve(probe_duration=3158, declared_duration=7299,
                    segments_downloaded=None, total_segments=None) == 3158


def test_ffprobe_failure_falls_back_to_declared_bitrate_check():
    # actual_duration is None → declared-bitrate fallback. Real video.
    assert _compute(
        declared_duration=600,
        actual_duration=None,
        file_size_bytes=100 * 1024 * 1024,  # ~170 KB/s — healthy
    ) is None
    # Tiny file → flag.
    reason = _compute(
        declared_duration=600,
        actual_duration=None,
        file_size_bytes=1 * 1024 * 1024,  # ~1.7 KB/s — too thin
    )
    assert reason is not None
    assert "ffprobe could not read duration" in reason
