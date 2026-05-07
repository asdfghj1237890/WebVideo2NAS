"""Backward-compat shim. Implementation moved to `shared.parsers.m3u8` in v2.5."""
from shared.parsers.m3u8 import (  # noqa: F401
    M3U8Parser,
    parse_m3u8,
    BROTLI_AVAILABLE,
)
