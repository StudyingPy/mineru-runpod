"""Static checks for `.runpod/hub.json`.

The Hub backend stores every `description` in a `varchar(191)` column —
anything >=190 chars is rejected on push with an opaque DB error. We
enforce the limit here so CI catches it before a human does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

HUB_JSON_PATH = Path(__file__).resolve().parents[1] / ".runpod" / "hub.json"

# RunPod's column is varchar(191). Use 189 as the practical max so we keep
# a one-char safety margin for future edits.
MAX_DESCRIPTION_LENGTH = 189


@pytest.fixture(scope="module")
def hub() -> dict:
    assert HUB_JSON_PATH.is_file(), f"hub.json not found at {HUB_JSON_PATH}"
    return json.loads(HUB_JSON_PATH.read_text(encoding="utf-8"))


def test_hub_json_is_valid_json(hub):
    # The fixture already parses it; this test exists so a syntax error
    # surfaces as a clearly-named failure rather than a fixture error.
    assert isinstance(hub, dict)


def test_top_level_description_under_limit(hub):
    desc = hub.get("description", "")
    assert len(desc) <= MAX_DESCRIPTION_LENGTH, (
        f"top-level description is {len(desc)} chars (max {MAX_DESCRIPTION_LENGTH}); "
        f"move long-form guidance to docs/src/content/docs/guides/"
    )


def test_every_env_description_under_limit(hub):
    """Every env-var description must fit in varchar(191).

    Long-form tuning advice belongs in `docs/src/content/docs/guides/`,
    not in the hub.json `description` field.
    """
    env_entries = hub.get("config", {}).get("env", [])
    assert env_entries, "hub.json has no config.env entries — schema regression?"

    too_long: list[tuple[str, int]] = []
    for entry in env_entries:
        key = entry.get("key", "<unknown>")
        desc = entry.get("input", {}).get("description", "")
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            too_long.append((key, len(desc)))

    assert not too_long, (
        f"hub.json env descriptions over {MAX_DESCRIPTION_LENGTH} chars "
        f"(RunPod Hub stores these in varchar(191) and will reject the push): "
        + ", ".join(f"{k}={n}" for k, n in too_long)
        + ". Move long-form guidance to docs/src/content/docs/guides/."
    )


def test_every_env_has_required_fields(hub):
    """Catch missing key/name/description so a malformed entry can't slip through."""
    for entry in hub["config"]["env"]:
        assert "key" in entry, f"env entry missing 'key': {entry}"
        inp = entry.get("input", {})
        assert "name" in inp, f"env {entry['key']!r} missing input.name"
        assert "description" in inp, f"env {entry['key']!r} missing input.description"
        assert inp["description"], f"env {entry['key']!r} has empty description"
