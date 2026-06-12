from job_strategy import JobKind, classify_job_kind


def test_classify_job_kind_prefers_explicit_format_hint():
    assert classify_job_kind(
        "https://cdn.example.com/video.bin?name=movie.mp4",
        "mpd",
    ) is JobKind.MPD

    assert classify_job_kind(
        "https://cdn.example.com/video.bin?fallback=playlist",
        "m3u8",
    ) is JobKind.M3U8


def test_classify_job_kind_keeps_legacy_mpd_url_precedence():
    assert classify_job_kind(
        "https://cdn.example.com/playlist.mpd?fallback=.m3u8",
        "m3u8",
    ) is JobKind.MPD


def test_classify_job_kind_uses_url_extension_without_query_false_positives():
    assert classify_job_kind("https://cdn.example.com/v/movie.mp4?token=abc") is JobKind.DIRECT
    assert classify_job_kind("https://cdn.example.com/v/movie.mp4.jpg?token=abc") is JobKind.M3U8
    assert classify_job_kind("https://cdn.example.com/v/master.mpd?token=abc") is JobKind.MPD
