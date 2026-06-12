"""Download job classification for worker processing."""

from __future__ import annotations

from enum import Enum
from urllib.parse import unquote


class JobKind(str, Enum):
    DIRECT = "direct"
    MPD = "mpd"
    M3U8 = "m3u8"


def classify_job_kind(url: str, format_hint: str = "") -> JobKind:
    """Classify a queued job using the worker's legacy routing semantics."""
    raw_url = str(url or "")
    hint = str(format_hint or "").lower()
    url_lower = raw_url.lower()
    url_decoded = unquote(url_lower)

    is_mpd = hint == "mpd" or ".mpd" in url_lower or ".mpd" in url_decoded
    if is_mpd:
        return JobKind.MPD

    is_m3u8 = hint == "m3u8" or ".m3u8" in url_lower
    if is_m3u8:
        return JobKind.M3U8

    def matches_direct_ext(ext: str) -> bool:
        return (
            url_lower.endswith(ext)
            or f"{ext}?" in url_lower
            or f"{ext}&" in url_lower
            or url_decoded.endswith(ext)
            or f"{ext}?" in url_decoded
            or f"{ext}&" in url_decoded
            or ("file=" in url_lower and ext in url_decoded)
        )

    if matches_direct_ext(".mp4") or matches_direct_ext(".mov"):
        return JobKind.DIRECT

    # Historical fallback: anything not direct/DASH is handled by HLS parser.
    return JobKind.M3U8
