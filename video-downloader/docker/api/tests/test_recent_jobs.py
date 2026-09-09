"""Exercise the production jobs query against a database, including old work."""
import asyncio
import importlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, create_engine, insert, update


@pytest.fixture
def jobs_api(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    import main
    api = importlib.reload(main)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    jobs = Table("jobs", metadata,
                 Column("id", String, primary_key=True), Column("url", String),
                 Column("title", String), Column("status", String), Column("progress", Integer),
                 Column("created_at", DateTime), Column("file_size", Integer),
                 Column("file_path", String), Column("error_message", String))
    Table("job_metadata", metadata, Column("job_id", String, primary_key=True),
          Column("duration", Float), Column("actual_duration", Float),
          Column("suspect_reason", String), Column("source_page", String), Column("mode", String))
    metadata.create_all(engine)
    with engine.connect() as connection:
        class Database:
            def execute(self, statement, params):
                # PostgreSQL returns native datetimes for text queries;
                # SQLite needs the result type specified explicitly.
                return connection.execute(statement.columns(created_at=DateTime()), params)

        def add(id, status, age):
            connection.execute(insert(jobs).values(
                id=id, title=id, url="https://example.com/video.mp4", status=status,
                progress=40 if status not in ("completed", "failed", "cancelled") else 100,
                created_at=datetime(2026, 9, 9) - timedelta(minutes=age)))

        def read(**kwargs):
            response = api.Response()
            result = asyncio.run(api.list_jobs(db=Database(), api_key="test-key", response=response, **kwargs))
            assert (response.headers.get("X-WV2N-Active-Jobs") == "complete") == (
                kwargs.get("include_active", False) and not kwargs.get("status"))
            return result

        yield add, read, connection, jobs
    engine.dispose()


def test_old_unfinished_jobs_survive_recent_history_limit(jobs_api):
    add, read, _, _ = jobs_api
    active = ("pending", "downloading", "processing", "merging",
              "browser_pending", "browser_uploading", "browser_finalizing")
    for i, status in enumerate(active):
        add(status, status, 100 + i)
    for status in ("completed", "failed", "cancelled"):
        add("old-" + status, status, 200)
    for i in range(20):
        add(f"recent-{i}", "completed", i)
    rows = read(limit=20, include_active=True)
    assert len(rows) == 27
    assert set(active) <= {job.id for job in rows}
    assert not any(job.id.startswith("old-") for job in rows)
    assert [job.created_at for job in rows] == sorted((job.created_at for job in rows), reverse=True)


def test_recent_active_rows_are_not_duplicated_and_old_completion_expires(jobs_api):
    add, read, connection, jobs = jobs_api
    add("new-active", "downloading", 0)
    add("recent", "completed", 1)
    add("old-active", "browser_uploading", 100)
    assert [job.id for job in read(limit=2, include_active=True)] == ["new-active", "recent", "old-active"]
    connection.execute(update(jobs).where(jobs.c.id == "old-active").values(status="completed"))
    assert [job.id for job in read(limit=2, include_active=True)] == ["new-active", "recent"]


def test_default_and_explicit_status_filters_keep_their_limit(jobs_api):
    add, read, _, _ = jobs_api
    add("latest", "completed", 0)
    add("queued-1", "pending", 1)
    add("queued-2", "pending", 2)
    add("active", "downloading", 3)
    assert [job.id for job in read(limit=1)] == ["latest"]
    assert [job.id for job in read(status="pending", limit=1, include_active=True)] == ["queued-1"]


def test_zero_history_still_includes_unfinished_work(jobs_api):
    add, read, _, _ = jobs_api
    add("history", "completed", 0)
    add("active", "downloading", 100)
    assert [job.id for job in read(limit=0, include_active=True)] == ["active"]
