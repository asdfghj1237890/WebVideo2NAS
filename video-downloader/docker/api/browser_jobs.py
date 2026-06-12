"""Browser-side job path and plan-safety helpers for the API gateway."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from shared.security import is_ip_public as default_is_ip_public
from shared.security import normalize_resolved_ips, resolve_host_ips as default_resolve_host_ips


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_STAGED_SEGMENT_FILE_RE = re.compile(r"^seg_(\d{8})\.bin$")
_VALID_TRACKS = ("video", "audio")
_VALID_INIT_LABELS = ("video", "audio")


class BrowserJobPaths:
    """Filesystem path service for browser-side staging jobs."""

    def __init__(self, staging_dir: str, max_segments: int):
        self.staging_dir = staging_dir
        self.max_segments = max_segments

    def canonical_job_id(self, job_id: str) -> str:
        raw = str(job_id or "")
        if not _UUID_RE.fullmatch(raw):
            raise HTTPException(status_code=400, detail="Invalid job_id format")
        try:
            return str(uuid.UUID(raw))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid job_id format")

    def validate_job_id(self, job_id: str) -> None:
        self.canonical_job_id(job_id)

    def staging_path_for(self, job_id: str) -> Path:
        safe_job_id = self.canonical_job_id(job_id)
        base = os.path.normpath(os.path.realpath(self.staging_dir))
        candidate = os.path.normpath(os.path.realpath(os.path.join(base, safe_job_id)))
        try:
            if os.path.commonpath([base, candidate]) != base:
                raise ValueError("candidate escaped staging root")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid staging path")

        base_prefix = base if base.endswith(os.sep) else base + os.sep
        if not candidate.startswith(base_prefix):
            raise HTTPException(status_code=400, detail="Invalid staging path")
        return Path(candidate)

    def metadata_staging_path_for_job(self, job_id: str, staging_dir: str) -> Optional[Path]:
        expected = self.staging_path_for(job_id).resolve()
        actual = Path(staging_dir or "").resolve()
        if actual != expected:
            return None
        return actual

    def segment_path(self, job_id: str, track: str, seq: int) -> Path:
        if track not in _VALID_TRACKS:
            raise HTTPException(status_code=400, detail=f"Invalid track: must be one of {_VALID_TRACKS}")
        if seq < 0 or seq >= self.max_segments:
            raise HTTPException(status_code=400, detail="seq out of range")
        return self.staging_path_for(job_id) / track / f"seg_{seq:08d}.bin"

    def init_segment_path(self, job_id: str, label: str) -> Path:
        if label not in _VALID_INIT_LABELS:
            raise HTTPException(status_code=400, detail=f"Invalid init label: must be one of {_VALID_INIT_LABELS}")
        return self.staging_path_for(job_id) / "init" / f"{label}.bin"


def staged_segment_seq_from_name(name: str) -> Optional[int]:
    match = _STAGED_SEGMENT_FILE_RE.fullmatch(name)
    if not match:
        return None
    return int(match.group(1))


def _collect_plan_urls(plan: Dict) -> set[str]:
    urls: set[str] = set()
    if plan.get("init_segment_url"):
        urls.add(plan["init_segment_url"])
    for track in (plan.get("tracks") or {}).values():
        if track.get("init_segment_url"):
            urls.add(track["init_segment_url"])
        for seg in (track.get("segments") or []):
            if seg.get("url"):
                urls.add(seg["url"])
            key = seg.get("key") or {}
            if key.get("uri"):
                urls.add(key["uri"])
    return urls


def enforce_plan_url_safety(
    plan: Dict,
    *,
    resolve_host_ips: Callable[[str], list] = default_resolve_host_ips,
    is_ip_public: Callable[[object], bool] = default_is_ip_public,
) -> None:
    """Reject browser-side plans whose URLs could target local/private hosts."""
    origins: dict[str, str] = {}
    for url in _collect_plan_urls(plan):
        try:
            parsed = urlparse(url)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Plan URL parse failed: {url[:120]}")
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(
                status_code=422,
                detail=f"Plan URL scheme {parsed.scheme!r} not allowed: {url[:120]}",
            )
        if parsed.scheme == "http":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Plan URL must use HTTPS for browser-side mode "
                    f"(plain HTTP is rejected because DNS rebinding "
                    f"between server-side validation and browser-side "
                    f"fetch is unmitigatable): {url[:120]}"
                ),
            )
        if not parsed.hostname:
            raise HTTPException(status_code=422, detail=f"Plan URL has no host: {url[:120]}")
        origins.setdefault(parsed.hostname.lower(), url)

    for host, sample_url in origins.items():
        if host in ("localhost", "ip6-localhost", "ip6-loopback"):
            raise HTTPException(
                status_code=422,
                detail=f"Plan URL host {host!r} not allowed (localhost): {sample_url[:120]}",
            )
        try:
            ips = normalize_resolved_ips(resolve_host_ips(host))
        except Exception:
            raise HTTPException(
                status_code=422,
                detail=f"Plan URL host {host!r} could not be resolved: {sample_url[:120]}",
            )
        if not ips:
            raise HTTPException(
                status_code=422,
                detail=f"Plan URL host {host!r} could not be resolved: {sample_url[:120]}",
            )
        for ip in ips:
            if not is_ip_public(ip):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Plan URL host {host!r} resolves to non-public IP "
                        f"{ip}; refusing browser-side plan: {sample_url[:120]}"
                    ),
                )
