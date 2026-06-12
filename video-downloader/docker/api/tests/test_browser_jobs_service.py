import pytest
from fastapi import HTTPException


def test_browser_job_paths_canonicalize_uuid_and_bound_staging(tmp_path):
    from browser_jobs import BrowserJobPaths

    paths = BrowserJobPaths(staging_dir=str(tmp_path), max_segments=100)
    staging = paths.staging_path_for("11111111-2222-3333-4444-AAAAAAAAAAAA")

    assert staging == tmp_path / "11111111-2222-3333-4444-aaaaaaaaaaaa"
    assert paths.segment_path(str(staging.name), "video", 7).name == "seg_00000007.bin"

    with pytest.raises(HTTPException):
        paths.segment_path(str(staging.name), "subtitle", 0)


def test_enforce_plan_url_safety_uses_injected_resolver_for_browser_plans():
    from browser_jobs import enforce_plan_url_safety

    plan = {
        "tracks": {
            "video": {
                "segments": [
                    {"seq": 0, "url": "https://cdn.example.com/v/0.ts"},
                ],
            },
        },
    }

    enforce_plan_url_safety(
        plan,
        resolve_host_ips=lambda host: ["8.8.8.8"],
    )

    with pytest.raises(HTTPException) as exc:
        enforce_plan_url_safety(
            plan,
            resolve_host_ips=lambda host: ["127.0.0.1"],
        )

    assert exc.value.status_code == 422
    assert "non-public IP" in exc.value.detail
