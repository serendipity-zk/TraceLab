from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "web_analytics"))

from trace_db import _TOOL_FIELDS, _child_projection, connect, materialize  # noqa: E402
from distributions import _tool_latency_by_category  # noqa: E402


def _round(index: int) -> dict:
    """A small Claude-style round: tools report a wall latency but never an internal one, and no
    timing event carries is_error, so both fields are null in every row of the trace."""
    return {
        "provider": "claude",
        "session_id": "claude:s1",
        "round_id": f"r{index}",
        "round_index": index,
        "model": "claude-test",
        "prefix_tokens": 100,
        "append_tokens": 10,
        "output_tokens": 5,
        "tools": [
            {
                "tool_index": 0,
                "tool_name": "Read",
                "tool_call_id": f"t{index}",
                "emitted_at": f"2026-09-01T10:00:0{index}Z",
                "result_at": f"2026-09-01T10:00:0{index}.500Z",
                "tool_wall_latency_ms": 500,
                "tool_internal_latency_ms": None,
                "is_error": None,
            }
        ],
        "timing_events": [
            {"event_type": "tool_result", "timestamp": f"2026-09-01T10:00:0{index}Z", "is_error": None},
        ],
    }


def test_all_null_number_and_flag_fields_keep_their_documented_types(tmp_path: Path) -> None:
    trace = tmp_path / "small.jsonl"
    trace.write_text("".join(json.dumps(_round(i)) + "\n" for i in (1, 2)), encoding="utf-8")
    db = tmp_path / "small.duckdb"
    materialize(trace, db)

    con = connect(db, read_only=True)
    try:
        types = dict(
            con.execute(
                "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                "WHERE column_name IN ('tool_internal_latency_ms', 'is_error')"
            ).fetchall()
        )
        latency = _tool_latency_by_category(con)
    finally:
        con.close()

    assert types == {
        "tool_calls.tool_internal_latency_ms": "BIGINT",
        "tool_calls.is_error": "BOOLEAN",
        "timing_events.is_error": "BOOLEAN",
    }
    # the effective latency falls back to the wall latency, as for any trace
    assert [(g["name"], g["median"]) for g in latency["groups"]] == [("File read / search", 500.0)]


def test_claude_counts_stay_numbers_in_a_codex_only_trace(tmp_path: Path) -> None:
    rounds = []
    for i in (1, 2):
        r = _round(i)
        r.update(provider="codex", session_id="codex:s1", claude_uncached_input_tokens=None,
                 claude_cache_creation_input_tokens=None, claude_cache_read_input_tokens=None)
        rounds.append(r)
    trace = tmp_path / "codex.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in rounds), encoding="utf-8")
    db = tmp_path / "codex.duckdb"
    materialize(trace, db)

    con = connect(db, read_only=True)
    try:
        types = dict(
            con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'rounds' AND column_name IN ("
                "'claude_uncached_input_tokens', 'claude_cache_creation_input_tokens', "
                "'claude_cache_read_input_tokens')"
            ).fetchall()
        )
        # arithmetic on the columns works (on a JSON column it is a BinderException)
        total = con.execute(
            "SELECT sum(coalesce(claude_cache_read_input_tokens, 0)"
            " + coalesce(claude_uncached_input_tokens, 0)"
            " + coalesce(claude_cache_creation_input_tokens, 0)) FROM rounds"
        ).fetchone()[0]
    finally:
        con.close()

    assert types == {
        "claude_uncached_input_tokens": "BIGINT",
        "claude_cache_creation_input_tokens": "BIGINT",
        "claude_cache_read_input_tokens": "BIGINT",
    }
    assert total == 0


def test_projection_is_unchanged_when_no_field_is_all_null() -> None:
    present = {name: "BIGINT" if null_type == "BIGINT" else "VARCHAR" for name, null_type, _ in _TOOL_FIELDS}
    assert _child_projection("tc", present, _TOOL_FIELDS) == [
        f"{native_expr} AS {name}" for name, _, native_expr in _TOOL_FIELDS
    ]
