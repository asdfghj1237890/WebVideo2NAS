"""Backward-compat shim. Implementation moved to `shared.parsers.dash` in v2.5.

Re-exports private helpers (`_iso8601_duration_to_seconds`,
`_substitute_template`) too because the existing worker test suite imports
them by name. Adding them here is cheaper than editing the tests; the
private prefix still signals "do not import from outside the parser".
"""
from shared.parsers.dash import (  # noqa: F401
    MAX_SEGMENTS_PER_TRACK,
    MPDParseError,
    extract_all_mpd_urls,
    parse_mpd,
    _iso8601_duration_to_seconds,
    _substitute_template,
    _strip_ns,
    _resolve_base_url,
    _build_segment_urls_from_template,
    _pick_best_representation,
    _max_representation_bandwidth,
    _is_trickmode_adapt_set,
    _merge_segment_templates,
    _parse_one_track,
)
