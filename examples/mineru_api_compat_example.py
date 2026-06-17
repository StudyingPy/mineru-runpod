"""Minimal example: parse a PDF using the MinerU-API-compatible client.

`MineruApiClient` mirrors the official MinerU cloud API (`create_task` /
`get_task`) but runs against your self-hosted RunPod endpoint — the migration
from mineru.net is import + constructor + auth. See
docs/getting-started/migrate-from-mineru-api for the full mapping.

Note: `full_zip_url` is produced by the worker's `s3` transport, so the
endpoint must be deployed with BUCKET_* env vars (any S3-compatible store).

Usage:
    set RUNPOD_API_KEY=...
    set RUNPOD_ENDPOINT_ID=...
    python examples/mineru_api_compat_example.py https://example.com/report.pdf
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mineru_client import MineruApiClient


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: mineru_api_compat_example.py <file_url>", file=sys.stderr)
        return 2
    url = sys.argv[1]

    client = MineruApiClient(
        endpoint_id=os.environ["RUNPOD_ENDPOINT_ID"],
        api_key=os.environ["RUNPOD_API_KEY"],
    )

    # Same lifecycle as the official API: create a task, poll until it finishes.
    created = client.create_task(url, model_version="vlm", page_ranges="1-5")
    task_id = created["data"]["task_id"]
    print(f"task_id={task_id}")

    done = client.wait_for_task(task_id)        # convenience: poll to completion
    data = done["data"]
    print(f"state={data['state']}")

    if data["state"] != "done":
        print(f"failed: {data.get('err_msg')}", file=sys.stderr)
        return 1

    print(f"full_zip_url={data['full_zip_url']}")
    dest = Path("./out/mineru_api_compat_example")
    client.download_results(done, dest)         # unpacks the archive (.zip) for you
    print(f"Extracted to: {dest.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
