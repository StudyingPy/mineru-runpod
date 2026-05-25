"""Structured logging: JSON / text formatters and the job_id contextvar."""

from __future__ import annotations

import asyncio
import json
import logging

import handler
from worker import logging as worker_logging


def _emit(formatter: logging.Formatter, **extra) -> str:
    """Build a LogRecord, format it, return the result."""
    record = logging.LogRecord(
        name="mineru-worker",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return formatter.format(record)


# -----------------------------------------------------------------------------
# JSON / text formatters
# -----------------------------------------------------------------------------

def test_json_formatter_emits_one_line_json():
    output = _emit(worker_logging.JsonFormatter(), backend="vlm-auto-engine", pages=10)
    assert "\n" not in output
    data = json.loads(output)
    assert data["msg"] == "test message"
    assert data["level"] == "info"
    assert data["backend"] == "vlm-auto-engine"
    assert data["pages"] == 10
    assert "ts" in data
    assert data["ts"].endswith("Z")


def test_json_formatter_includes_logger_name():
    output = _emit(worker_logging.JsonFormatter())
    data = json.loads(output)
    assert data["logger"] == "mineru-worker"


def test_text_formatter_is_human_readable():
    output = _emit(worker_logging.TextFormatter(), backend="pipeline", pages=42)
    assert "test message" in output
    assert "backend=pipeline" in output
    assert "pages=42" in output
    assert "INFO" in output


def test_get_logger_is_idempotent():
    # configure() flips a flag; calling get_logger many times must not stack handlers.
    worker_logging.get_logger("a")
    worker_logging.get_logger("b")
    root_handler_count = len(logging.getLogger().handlers)
    worker_logging.get_logger("c")
    assert len(logging.getLogger().handlers) == root_handler_count


# -----------------------------------------------------------------------------
# job_id contextvar — per RunPod's write-logs guidance, every line should
# carry the request ID so cross-job correlation works.
# -----------------------------------------------------------------------------

def test_job_id_appears_in_json_logs():
    token = worker_logging.job_id_var.set("test-job-abc-123")
    try:
        output = _emit(worker_logging.JsonFormatter())
        data = json.loads(output)
        assert data["job_id"] == "test-job-abc-123"
    finally:
        worker_logging.job_id_var.reset(token)


def test_job_id_appears_in_text_logs():
    token = worker_logging.job_id_var.set("test-job-xyz-456")
    try:
        output = _emit(worker_logging.TextFormatter())
        assert "job_id=test-job-xyz-456" in output
    finally:
        worker_logging.job_id_var.reset(token)


def test_job_id_omitted_when_unset():
    # Default is None — formatters must not emit a "job_id" key.
    worker_logging.job_id_var.set(None)
    output = _emit(worker_logging.JsonFormatter())
    data = json.loads(output)
    assert "job_id" not in data


def test_handler_sets_job_id_contextvar(monkeypatch):
    """End-to-end: handler() pins job["id"] into the contextvar."""
    async def fake_run(file_bytes, *, basename, work_dir, **kwargs):
        out = work_dir / "out"
        out.mkdir()
        (out / f"{basename}.md").write_text("# fake\n", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    captured: dict = {}

    async def spy_handler(job):
        result = await handler.handler(job)
        captured["job_id_during_request"] = worker_logging.job_id_var.get()
        return result

    asyncio.run(spy_handler({
        "id": "queued-job-uuid-789",
        "input": {"file_b64": "JVBERi0xLjQK", "basename": "test"},
    }))

    assert captured["job_id_during_request"] == "queued-job-uuid-789"


def test_handler_uses_fallback_when_no_job_id(monkeypatch):
    """Sync clients without a queued job have no id; handler uses <unknown>."""
    async def fake_run(file_bytes, *, basename, work_dir, **kwargs):
        out = work_dir / "out"
        out.mkdir()
        (out / f"{basename}.md").write_text("# fake\n", encoding="utf-8")
        return out

    monkeypatch.setattr("worker.parse.run_mineru", fake_run)

    captured: dict = {}

    async def spy():
        await handler.handler({
            # No "id" key in the job dict.
            "input": {"file_b64": "JVBERi0xLjQK", "basename": "test"},
        })
        captured["job_id"] = worker_logging.job_id_var.get()

    asyncio.run(spy())
    assert captured["job_id"] == "<unknown>"
