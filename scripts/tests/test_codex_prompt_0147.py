from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from extract_codex_rounds import extract_codex_session  # noqa: E402


SESSION = "019a0000-0000-7000-8000-000000000001"
TURN = "019a0000-0000-7000-8000-00000000000a"


def _rec(ts: str, top: str, payload: dict) -> dict:
    return {"timestamp": f"2026-09-01T10:00:{ts}Z", "type": top, "payload": payload}


def _meta(version: str = "0.150.0") -> list[dict]:
    return [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": version, "source": "cli", "cwd": "/repo"}),
        _rec("00.100", "turn_context", {"turn_id": TURN, "model": "gpt-5.6-sol"}),
    ]


def _usage(ts: str, n: int) -> dict:
    last = {"input_tokens": 1000 + n, "cached_input_tokens": 800, "output_tokens": 50, "reasoning_output_tokens": 10}
    total = {**last, "total_tokens": 5000 + n}
    return _rec(ts, "event_msg", {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total}})


def _prompt_new(ts: str, text: str) -> list[dict]:
    """How Codex CLI >= 0.147 writes a prompt: a role=user response item, then item_completed/UserMessage."""
    return [
        _rec(ts, "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}),
        _rec(ts, "event_msg", {"type": "item_completed",
                                "item": {"type": "UserMessage", "id": "i1", "content": [{"type": "text", "text": text}]}}),
    ]


def _answer(ts: str) -> dict:
    return _rec(ts, "response_item", {"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": "done"}]})


def _write(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / f"rollout-2026-09-01T10-00-00-{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- CLI >= 0.147 prompts


def test_item_completed_user_message_is_the_prompt(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        _rec("00.500", "response_item", {"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": "<environment_context>x"}]}),
        *_prompt_new("01.000", "run the tests"),
        _answer("03.000"),
        _usage("03.100", 1),
    ]))
    (r,) = rounds
    assert r["first_input_event_type"] == "user_message"
    user_events = [e for e in r["timing_events"] if e["event_type"] == "user_message"]
    assert len(user_events) == 1                               # the injected context is not a prompt
    assert user_events[0]["source"] == "event_msg.item_completed"
    assert user_events[0]["content_chars"] == len("run the tests")  # text characters, as for user_message


def test_older_user_message_event_is_unchanged(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta("0.140.0"),
        _rec("01.000", "event_msg", {"type": "user_message", "message": "run the tests"}),
        _answer("03.000"),
        _usage("03.100", 1),
    ]))
    (r,) = rounds
    ev = [e for e in r["timing_events"] if e["event_type"] == "user_message"]
    assert r["first_input_event_type"] == "user_message" and ev[0]["source"] == "event_msg.user_message"


def test_raw_sink_reads_the_item_completed_prompt(tmp_path: Path) -> None:
    sink: dict = {}
    extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "run the tests"),
        _answer("03.000"),
        _usage("03.100", 1),
    ]), raw_sink=sink)
    (entry,) = sink.values()
    assert entry["inputKind"] == "user" and entry["input"] == "run the tests"


def test_malformed_item_completed_prompt_does_not_break_extraction(tmp_path: Path) -> None:
    """A UserMessage whose content is not a list, or whose text parts are not strings, still counts as the prompt."""
    for content in ("run the tests", [{"type": "text", "text": 42}, {"type": "text", "text": "ok"}, "x"], None, 42):
        rounds = extract_codex_session(_write(tmp_path, [
            *_meta(),
            _rec("01.000", "event_msg", {"type": "item_completed",
                                          "item": {"type": "UserMessage", "id": "i1", "content": content}}),
            _answer("03.000"),
            _usage("03.100", 1),
        ]))
        (r,) = rounds
        assert r["first_input_event_type"] == "user_message"
        (event,) = [e for e in r["timing_events"] if e["event_type"] == "user_message"]
        assert event["content_chars"] == (2 if isinstance(content, list) else 0)
