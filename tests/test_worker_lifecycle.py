"""Worker-process behaviors that aren't request-shape semantics.

- Cumulative refresh counters (REFRESH_WORKER_AFTER_JOBS / _PAGES)
- Concurrency modifier env-var parsing (MINERU_MAX_CONCURRENCY)
- SIGTERM shutdown event
"""

from __future__ import annotations

import asyncio

import pytest

import handler


@pytest.fixture(autouse=True)
def reset_counters(monkeypatch):
    """Each test starts with clean counters and no thresholds set."""
    handler._jobs_processed = 0
    handler._pages_processed_total = 0
    monkeypatch.delenv("REFRESH_WORKER_AFTER_JOBS", raising=False)
    monkeypatch.delenv("REFRESH_WORKER_AFTER_PAGES", raising=False)
    yield


# -----------------------------------------------------------------------------
# Refresh counters
# -----------------------------------------------------------------------------

def test_refresh_disabled_by_default():
    # With no thresholds set, every job returns False (no recycle).
    for _ in range(5):
        assert handler._record_job(10) is False


def test_refresh_jobs_threshold_crosses(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "3")
    assert handler._record_job(0) is False
    assert handler._record_job(0) is False
    assert handler._record_job(0) is True  # third job crosses
    assert handler._jobs_processed == 3


def test_refresh_pages_threshold_crosses(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "50")
    assert handler._record_job(20) is False
    assert handler._record_job(20) is False  # 40 cumulative
    assert handler._record_job(20) is True   # 60 cumulative, crosses 50
    assert handler._pages_processed_total == 60


def test_refresh_unbounded_jobs_do_not_count_pages(monkeypatch):
    # End-page=-1 jobs pass pages=0; jobs counter still increments.
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "10")
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "2")
    assert handler._record_job(0) is False  # job count 1, pages 0
    assert handler._record_job(0) is True   # job count 2, crosses jobs threshold


def test_refresh_either_threshold_trips(monkeypatch):
    # If BOTH thresholds are set, whichever trips first wins.
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "100")
    monkeypatch.setenv("REFRESH_WORKER_AFTER_PAGES", "5")
    assert handler._record_job(3) is False  # pages 3
    assert handler._record_job(3) is True   # pages 6, crosses


def test_refresh_malformed_env_var_treated_as_disabled(monkeypatch):
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "not-a-number")
    assert handler._record_job(0) is False


def test_refresh_worker_signal_via_full_handler_path(monkeypatch):
    """End-to-end: set threshold=1, run handler, confirm refresh_worker key."""
    monkeypatch.setenv("REFRESH_WORKER_AFTER_JOBS", "1")

    async def fake_run(file_bytes, *, basename, work_dir, **kwargs):
        out = work_dir / "fake-out"
        out.mkdir()
        (out / f"{basename}.md").write_text("# fake\n", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    result = asyncio.run(handler.handler({
        "input": {"file_b64": "JVBERi0xLjQK", "basename": "test"}  # %PDF-1.4
    }))
    assert result.get("ok") is True
    assert result.get("refresh_worker") is True


# -----------------------------------------------------------------------------
# Concurrency modifier
# -----------------------------------------------------------------------------

def test_concurrency_default_is_one(monkeypatch):
    monkeypatch.delenv("MINERU_MAX_CONCURRENCY", raising=False)
    assert handler._concurrency_modifier(0) == 1


def test_concurrency_from_env(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "3")
    assert handler._concurrency_modifier(0) == 3


def test_concurrency_clamps_to_one(monkeypatch):
    # Zero / negative makes no sense for a serverless worker; coerce to 1.
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "0")
    assert handler._concurrency_modifier(0) == 1
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "-5")
    assert handler._concurrency_modifier(0) == 1


def test_concurrency_malformed_env_var_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("MINERU_MAX_CONCURRENCY", "auto")
    assert handler._concurrency_modifier(0) == 1


# -----------------------------------------------------------------------------
# SIGTERM shutdown breadcrumb
# -----------------------------------------------------------------------------

def test_check_shutdown_raises_when_event_set():
    handler._shutting_down.set()
    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            handler._check_shutdown()
    finally:
        handler._shutting_down.clear()


def test_check_shutdown_is_noop_when_clear():
    handler._shutting_down.clear()
    handler._check_shutdown()  # should not raise


def test_on_sigterm_sets_event():
    handler._shutting_down.clear()
    try:
        handler._on_sigterm(15, None)
        assert handler._shutting_down.is_set()
    finally:
        handler._shutting_down.clear()
