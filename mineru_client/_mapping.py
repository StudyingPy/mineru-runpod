"""Pure translation between the official MinerU v4 API and the worker contract.

No network, no SDK — just dict/string transforms, so every rule here is unit
testable in isolation. ``MineruApiClient`` (in ``api_compat.py``) is the thin
I/O shell around these functions.

Why this exists: callers of the official MinerU cloud API
(``mineru.net/api/v4/...``) speak a different vocabulary than the RunPod MinerU
worker. The worker's HTTP envelope is fixed by RunPod (``/run`` + ``{"input":
...}``), so we cannot host MinerU's REST paths; instead we reproduce its
request params and response dicts here and route them onto the worker.

Mapping summary (MinerU → worker):
    url            -> file_url
    model_version  -> backend           (pipeline->pipeline, vlm->vlm-auto-engine)
    enable_formula -> formula_enable
    enable_table   -> table_enable
    language       -> lang              (pipeline-backend only, like MinerU)
    page_ranges    -> start_page/end_page  (single contiguous 1-based range)

And MinerU's task lifecycle onto RunPod's:
    POST /extract/task        -> endpoint.run()      (returns a job id == task_id)
    GET  /extract/task/{id}   -> GET /status/{id}    (status -> MinerU state)
    full_zip_url              -> results[0].tarball_url  (transport="s3")
"""

from __future__ import annotations

from typing import Any


# MinerU `model_version` -> worker `backend`. MinerU exposes three values;
# `MinerU-HTML` has no equivalent on this worker (no HTML-specialised model).
MODEL_VERSION_TO_BACKEND: dict[str, str] = {
    "pipeline": "pipeline",
    "vlm": "vlm-auto-engine",
}

# RunPod job status -> MinerU task state. RunPod's terminal states beyond
# COMPLETED (FAILED / TIMED_OUT / CANCELLED) all collapse to MinerU `failed`.
RUNPOD_STATUS_TO_STATE: dict[str, str] = {
    "IN_QUEUE": "pending",
    "IN_PROGRESS": "running",
    "COMPLETED": "done",
    "FAILED": "failed",
    "TIMED_OUT": "failed",
    "CANCELLED": "failed",
}


def model_version_to_backend(model_version: str | None) -> str:
    """Translate a MinerU ``model_version`` to a worker ``backend``.

    Raises ValueError on ``MinerU-HTML`` (unsupported) or any unknown value.
    """
    mv = model_version or "pipeline"
    if mv == "MinerU-HTML":
        raise ValueError(
            "model_version 'MinerU-HTML' is not supported by the RunPod MinerU "
            "worker (no HTML-specialised model). Use 'pipeline' or 'vlm'."
        )
    try:
        return MODEL_VERSION_TO_BACKEND[mv]
    except KeyError:
        raise ValueError(
            f"unknown model_version {mv!r}; expected one of "
            f"{sorted(MODEL_VERSION_TO_BACKEND)}. For the worker's richer native "
            f"backends (hybrid-*, *-http-client) use MineruClient directly."
        ) from None


def parse_page_ranges(page_ranges: str) -> tuple[int, int]:
    """Translate a MinerU ``page_ranges`` string to ``(start_page, end_page)``.

    MinerU page ranges are **1-based** page numbers; the worker uses **0-based**
    inclusive ``start_page`` / ``end_page``. Only a single contiguous range is
    supported, because the worker takes one slice per job:

        "5"    -> (4, 4)     # the 5th page only
        "2-6"  -> (1, 5)     # pages 2..6 inclusive

    Multi-range (``"2,4-6"``), open-ended (``"2-"``) and relative-to-end
    (``"2--2"``) forms raise ValueError — the worker can't express them.
    """
    s = (page_ranges or "").strip()
    if not s:
        raise ValueError("page_ranges is empty; omit it to parse the whole document")
    if "," in s:
        raise ValueError(
            f"page_ranges {page_ranges!r} selects non-contiguous pages; the RunPod "
            f"worker parses one contiguous slice per job. Submit separate tasks, "
            f"or use a single range like '2-6'."
        )

    def _page(num: str) -> int:
        if not num.isdigit():
            raise ValueError(
                f"page_ranges {page_ranges!r} is not a simple 1-based range; "
                f"open-ended ('2-') and relative-to-end ('2--2') forms are not "
                f"supported. Use 'N' or 'N-M' with positive page numbers."
            )
        n = int(num)
        if n < 1:
            raise ValueError(f"page_ranges page numbers are 1-based; got {n}")
        return n

    if "-" in s:
        parts = s.split("-")
        if len(parts) != 2:
            raise ValueError(
                f"page_ranges {page_ranges!r} is not a simple 'N-M' range"
            )
        start, end = _page(parts[0]), _page(parts[1])
        if end < start:
            raise ValueError(f"page_ranges {page_ranges!r}: end precedes start")
        return start - 1, end - 1

    page = _page(s)
    return page - 1, page - 1


def build_worker_payload(
    *,
    url: str,
    model_version: str = "pipeline",
    enable_formula: bool = True,
    enable_table: bool = True,
    language: str = "ch",
    page_ranges: str | None = None,
    extra_formats: list[str] | None = None,
    transport: str = "s3",
    archive_format: str = "zip",
) -> dict[str, Any]:
    """Build the worker ``input`` payload from MinerU ``create_task`` params.

    Raises ValueError for features the worker can't honour (``MinerU-HTML``,
    ``extra_formats``, non-contiguous ``page_ranges``) so the caller fails fast
    with an actionable message rather than getting a silently-wrong parse.
    """
    if extra_formats:
        raise ValueError(
            "extra_formats (docx/html/latex) is not produced by the RunPod MinerU "
            "worker; it emits markdown + content_list + middle + images only."
        )
    payload: dict[str, Any] = {
        "file_url": url,
        "backend": model_version_to_backend(model_version),
        "formula_enable": bool(enable_formula),
        "table_enable": bool(enable_table),
        "lang": language,
        "transport": transport,
        # Request a real .zip so full_zip_url matches the official cloud API
        # (the worker defaults to .tar.gz; the compat client overrides to zip).
        "archive_format": archive_format,
    }
    if page_ranges is not None:
        start_page, end_page = parse_page_ranges(page_ranges)
        payload["start_page"] = start_page
        payload["end_page"] = end_page
    return payload


def runpod_status_to_state(status: str | None) -> str:
    """Map a RunPod job status to a MinerU task state.

    Unknown statuses map to ``running`` (poll-safe: callers keep polling until a
    terminal state or their own timeout).
    """
    return RUNPOD_STATUS_TO_STATE.get(status or "", "running")


def extract_full_zip_url(output: Any) -> str | None:
    """Pull the presigned archive URL out of a worker success response.

    The worker's ``transport:"s3"`` puts a presigned ``tarball_url`` on the first
    (only, for single-file) entry of ``results``; this returns it verbatim as
    MinerU's ``full_zip_url``. The container is whatever the task requested — the
    compat client requests ``archive_format="zip"``, so it is normally a ``.zip``.
    """
    if not isinstance(output, dict):
        return None
    results = output.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0].get("tarball_url")
    return None


def build_create_response(task_id: str) -> dict[str, Any]:
    """Shape a MinerU ``POST /extract/task`` success response."""
    return {"code": 0, "msg": "ok", "trace_id": task_id, "data": {"task_id": task_id}}


def build_task_response(
    task_id: str,
    raw_status: Any,
    *,
    data_id: str | None = None,
) -> dict[str, Any]:
    """Shape a MinerU ``GET /extract/task/{id}`` response from a RunPod status.

    ``raw_status`` is the RunPod ``/status/{id}`` JSON: ``{"status": ...,
    "output": ...}``. Returns the MinerU envelope ``{code, msg, trace_id,
    data:{task_id, state, ...}}``.

    Two worker-specific subtleties are folded in here:

    - The worker signals a soft failure (bad input, parse error) by returning
      ``{"ok": false, "error": ...}`` — sometimes on a job RunPod still reports
      COMPLETED. We treat ``ok is False`` as ``state="failed"``.
    - A ``done`` with results but no ``tarball_url`` means the endpoint lacks the
      ``BUCKET_*`` config that ``transport:"s3"`` needs; a ``done`` with no result
      payload at all gets a generic message. Both are surfaced as ``failed``
      rather than a useless ``done`` with nothing to download.
    """
    status = raw_status.get("status") if isinstance(raw_status, dict) else None
    output = raw_status.get("output") if isinstance(raw_status, dict) else None
    state = runpod_status_to_state(status)

    data: dict[str, Any] = {"task_id": task_id, "state": state, "err_msg": ""}
    if data_id is not None:
        data["data_id"] = data_id

    if state == "done":
        if isinstance(output, dict) and output.get("ok") is False:
            data["state"] = "failed"
            data["err_msg"] = str(output.get("error") or "parse failed")
        else:
            url = extract_full_zip_url(output)
            if url:
                data["full_zip_url"] = url
            elif isinstance(output, dict) and output.get("results"):
                # Completed with results but no tarball_url → object storage is
                # not configured on the endpoint.
                data["state"] = "failed"
                data["err_msg"] = (
                    "endpoint returned no downloadable archive URL. Deploy the "
                    "RunPod endpoint with BUCKET_* env vars so transport='s3' "
                    "produces a presigned archive (surfaced as full_zip_url)."
                )
            else:
                # Completed but no usable result payload at all (empty/odd output).
                data["state"] = "failed"
                data["err_msg"] = "job completed but returned no result payload"
    elif state == "failed":
        # Hard failures (worker crash, OOM, timeout) surface the reason in a
        # top-level `error`; handler-returned errors may sit in `output`. Prefer
        # whichever is present before falling back to the generic status string.
        err = output.get("error") if isinstance(output, dict) else None
        if not err and isinstance(raw_status, dict):
            err = raw_status.get("error")
        data["err_msg"] = str(err) if err else f"job {status}"

    return {"code": 0, "msg": "ok", "trace_id": task_id, "data": data}
