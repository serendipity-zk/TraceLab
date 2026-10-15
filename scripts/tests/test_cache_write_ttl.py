from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in ("scripts", "artifacts/utils", "artifacts/web_analytics"):
    sys.path.insert(0, str(REPO_ROOT / _sub))

from extract_claude_rounds import extract_session_with_key, round_key  # noqa: E402
from extract_codex_rounds import extract_codex_session  # noqa: E402
from sanitize_round_trace import StableIdSanitizer, sanitize_row  # noqa: E402
from trace_privacy import find_sensitive  # noqa: E402
from trace_db import connect, materialize  # noqa: E402
from rounds import load_rounds  # noqa: E402
from cost_breakdown import build_cost  # noqa: E402
import pricing  # noqa: E402

FIELD = "claude_cache_creation_1h_input_tokens"
SESSION = "22222222-2222-4222-8222-222222222222"
OPUS = pricing.price_for("claude", "claude-opus-4-8")


def _assistant(msg_id: str, usage: dict, model: str = "claude-opus-4-8") -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:01.000Z",
        "sessionId": SESSION,
        "message": {"id": msg_id, "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": "ok"}], "usage": usage},
    }


def _usage(write: int, split: dict | None) -> dict:
    usage = {"input_tokens": 10, "cache_creation_input_tokens": write,
             "cache_read_input_tokens": 1000, "output_tokens": 20}
    if split is not None:
        usage["cache_creation"] = split
    return usage


def _extract(tmp_path: Path, usages: list[dict]) -> list[dict]:
    path = tmp_path / f"{SESSION}.jsonl"
    records = [{"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "sessionId": SESSION,
                "message": {"role": "user", "content": "go"}}]
    records += [_assistant(f"msg_{i:02d}", u) for i, u in enumerate(usages)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    rows = extract_session_with_key(path, "demo", SESSION)
    for r in rows:
        r["trace_key"] = round_key(r)
    return rows


def _db(tmp_path: Path, rows: list[dict]):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    db = tmp_path / "trace.duckdb"
    materialize(trace, db)
    return connect(db, read_only=True)


# --------------------------------------------------------------------------- extraction


@pytest.mark.parametrize(
    ("split", "expected"),
    [
        ({"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 700}, 700),
        ({"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}, 0),
        (None, None),  # older Claude Code: no TTL split recorded
        ({"ephemeral_1h_input_tokens": 5000}, 1000),  # never more than the write itself
        ({"ephemeral_1h_input_tokens": True}, None),  # a boolean is not a token count
    ],
)
def test_claude_rounds_carry_the_1h_part_of_the_cache_write(tmp_path: Path, split, expected) -> None:
    (row,) = _extract(tmp_path, [_usage(1000, split)])
    assert row["claude_cache_creation_input_tokens"] == 1000
    assert row[FIELD] == expected


def test_codex_rounds_have_no_ttl_split(tmp_path: Path) -> None:
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10, "total_tokens": 110}
    records = [
        {"timestamp": "2026-01-01T00:00:00.000Z", "type": "session_meta",
         "payload": {"id": "33333333-3333-7333-8333-333333333333", "source": "cli"}},
        {"timestamp": "2026-01-01T00:00:00.001Z", "type": "turn_context",
         "payload": {"turn_id": "44444444-4444-7444-8444-444444444444", "model": "codex-test"}},
        {"timestamp": "2026-01-01T00:00:00.002Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "go"}},
        {"timestamp": "2026-01-01T00:00:00.100Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    rounds = extract_codex_session(path)
    assert rounds and all(FIELD in r and r[FIELD] is None for r in rounds)


def test_the_field_survives_sanitization_without_a_privacy_hit(tmp_path: Path) -> None:
    (row,) = _extract(tmp_path, [_usage(1000, {"ephemeral_1h_input_tokens": 700})])
    clean = sanitize_row(row, StableIdSanitizer("seed-a"))
    assert clean[FIELD] == 700
    assert find_sensitive(clean) == []


# --------------------------------------------------------------------------- pricing


def test_without_1h_tokens_cost_is_unchanged() -> None:
    old = 900 * 5 / 1e6 + 1000 * 6.25 / 1e6 + 5000 * 0.5 / 1e6 + 20 * 25 / 1e6
    rc = pricing.round_cost(OPUS, 5000, 1900, 20, cache_write_tokens=1000)
    assert rc["total"] == pytest.approx(old, rel=1e-12)
    assert pricing.round_cost(OPUS, 5000, 1900, 20, cache_write_tokens=1000, cache_write_1h_tokens=0) == rc


def test_1h_writes_are_billed_at_the_1h_rate() -> None:
    rc = pricing.round_cost(OPUS, 0, 1000, 0, cache_write_tokens=1000, cache_write_1h_tokens=700)
    assert rc["cacheWriteCost"] == pytest.approx((300 * 6.25 + 700 * 10) / 1e6, rel=1e-12)
    assert rc["total"] == pytest.approx(rc["cacheWriteCost"], rel=1e-12)


def test_1h_tokens_are_clamped_to_the_write() -> None:
    capped = pricing.round_cost(OPUS, 0, 1000, 0, cache_write_tokens=1000, cache_write_1h_tokens=9999)
    assert capped["cacheWriteCost"] == pytest.approx(1000 * 10 / 1e6, rel=1e-12)


def test_a_price_without_a_1h_rate_bills_1h_writes_at_the_5m_rate() -> None:
    price = {"inputPerM": 5, "cacheWrite5mInputPerM": 6.25, "cachedInputPerM": 0.5, "outputPerM": 25}
    rc = pricing.round_cost(price, 0, 1000, 0, cache_write_tokens=1000, cache_write_1h_tokens=700)
    assert rc["cacheWriteCost"] == pytest.approx(1000 * 6.25 / 1e6, rel=1e-12)


def test_price_table_write_rates_follow_the_input_rate() -> None:
    table = json.loads((REPO_ROOT / "artifacts/utils/pricing.json").read_text(encoding="utf-8"))
    # a null exact entry means "no API price" (unpriced); it has no rates to check
    entries = [e for e in table["exact"].values() if e is not None] + table["family"]
    for e in entries:
        if "cacheWrite5mInputPerM" in e:
            assert e["cacheWrite5mInputPerM"] == pytest.approx(1.25 * e["inputPerM"])
            assert e["cacheWrite1hInputPerM"] == pytest.approx(2 * e["inputPerM"])
        else:
            assert "cacheWrite1hInputPerM" not in e
    assert pricing.price_for("claude", "claude-sonnet-future")["cacheWrite1hInputPerM"] == 6  # family path


# --------------------------------------------------------------------------- DuckDB + analytics


def test_bulk_cost_adds_exactly_the_1h_premium(tmp_path: Path) -> None:
    rows = _extract(tmp_path, [
        _usage(1000, {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 700}),
        _usage(400, None),
    ])
    con = _db(tmp_path, rows)
    assert con.execute(f"SELECT data_type FROM information_schema.columns "
                       f"WHERE table_name = 'rounds' AND column_name = '{FIELD}'").fetchone()[0] == "BIGINT"
    loaded = load_rounds(con)
    assert sorted(r["cache_write_1h"] for r in loaded) == [0, 700]
    (model,) = build_cost(loaded)["byModel"]
    as_5m = sum(pricing.round_cost(OPUS, r["prefix"], r["append"], r["output"],
                                   cache_write_tokens=r["cache_write"])["total"] for r in loaded)
    assert model["costUsd"] - as_5m == pytest.approx(700 * (10 - 6.25) / 1e6, rel=1e-9)


def test_all_null_column_is_pinned_to_bigint(tmp_path: Path) -> None:
    con = _db(tmp_path, _extract(tmp_path, [_usage(1000, None)]))
    assert con.execute(f"SELECT data_type FROM information_schema.columns "
                       f"WHERE table_name = 'rounds' AND column_name = '{FIELD}'").fetchone()[0] == "BIGINT"
    assert load_rounds(con)[0]["cache_write_1h"] == 0


def test_traces_without_the_column_price_as_before(tmp_path: Path) -> None:
    rows = _extract(tmp_path, [_usage(1000, {"ephemeral_1h_input_tokens": 700})])
    for r in rows:
        del r[FIELD]
    con = _db(tmp_path, rows)
    loaded = load_rounds(con)
    assert loaded[0]["cache_write_1h"] == 0
    (model,) = build_cost(loaded)["byModel"]
    r = loaded[0]
    assert model["costUsd"] == pytest.approx(pricing.round_cost(
        OPUS, r["prefix"], r["append"], r["output"], cache_write_tokens=r["cache_write"])["total"], rel=1e-12)


def test_old_databases_expose_the_new_column_as_a_typed_null(tmp_path: Path) -> None:
    rows = _extract(tmp_path, [_usage(1000, {"ephemeral_1h_input_tokens": 700})])
    for r in rows:
        del r[FIELD]
    con = _db(tmp_path, rows)  # opened through trace_db.connect, like every analysis
    types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                             "WHERE table_name = 'rounds'").fetchall())
    assert types.get(FIELD) == "BIGINT"
    assert con.execute(f"SELECT count(*) FROM rounds WHERE {FIELD} IS NOT NULL").fetchone()[0] == 0
    assert load_rounds(con)[0]["cache_write_1h"] == 0


# Anthropic prompt-caching price table (docs, as of 2026-09): input / 5m write / 1h write / cache read / output.
# These models differ from their family default (Fable 5.1 and Opus 5.5 also read cache below 0.1x input), so
# each must resolve through an exact entry rather than the family fallback.
@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("claude-fable-5-1", (10, 12.5, 20, 0.25, 50)),
        ("claude-opus-5-5", (4, 5, 8, 0.2, 20)),
        ("claude-opus-5", (5, 6.25, 10, 0.5, 25)),
        ("claude-sonnet-5", (2, 2.5, 4, 0.2, 10)),
        ("claude-opus-4-1-20250805", (15, 18.75, 30, 1.5, 75)),  # the "opus" family default is 5 / 25
    ],
)
def test_newer_claude_models_have_exact_official_prices(model: str, rates: tuple) -> None:
    assert f"claude:{model}" in pricing._EXACT
    p = pricing.price_for("claude", model)
    assert (p["inputPerM"], p["cacheWrite5mInputPerM"], p["cacheWrite1hInputPerM"], p["cachedInputPerM"],
            p["outputPerM"]) == rates


# OpenAI API pricing page (as of 2026-09), standard tier, short context: input / cached input / output.
@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("gpt-6-astra", (10, 1, 50)),
        ("gpt-6-sol", (2, 0.2, 10)),
        ("gpt-6-luna", (0.1, 0.01, 0.5)),
        ("gpt-5.6-cyber", (12.5, 1.25, 75)),
        ("gpt-5.6-sol", (4, 0.4, 20)),
        ("gpt-5.6-terra", (2, 0.2, 12)),
        ("gpt-5.6-luna", (0.2, 0.02, 1.2)),
        ("gpt-5.5", (5, 0.5, 30)),
        ("gpt-5.4", (2.5, 0.25, 15)),
        ("gpt-5.4-mini", (0.75, 0.075, 4.5)),
        ("gpt-5.3-codex", (1.75, 0.175, 14)),
        ("gpt-5.2-codex", (1.75, 0.175, 14)),
        ("gpt-5.2", (1.75, 0.175, 14)),
        ("gpt-5.1-codex-max", (1.25, 0.125, 10)),
        ("gpt-5.1-codex-mini", (0.25, 0.025, 2)),
        ("gpt-5.1-codex", (1.25, 0.125, 10)),
        ("gpt-5.1", (1.25, 0.125, 10)),
        ("gpt-5-codex", (1.25, 0.125, 10)),
        ("gpt-5", (1.25, 0.125, 10)),
    ],
)
def test_codex_models_have_exact_official_prices(model: str, rates: tuple) -> None:
    assert f"codex:{model}" in pricing._EXACT
    p = pricing.price_for("codex", model)
    assert (p["inputPerM"], p["cachedInputPerM"], p["outputPerM"]) == rates


def test_a_null_exact_entry_is_unpriced_not_family_priced() -> None:
    # gpt-5.3-codex-spark has no public API price; the "gpt-5" family default must not price it.
    assert pricing._EXACT["codex:gpt-5.3-codex-spark"] is None
    assert pricing.price_for("codex", "gpt-5.3-codex-spark") is None
    assert pricing.price_for("codex", "gpt-5.9-unknown") is not None  # other gpt-5.x still use the family


# --------------------------------------------------------------------------- every cost consumer passes the 1h part


def _load(name: str, relpath: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _pair(tmp_path: Path):
    """The same trace twice: 1M cache-write tokens, 700K of them 1-hour in the first, all 5-minute in the second."""
    out = []
    for name, split in (("h1", {"ephemeral_5m_input_tokens": 300_000, "ephemeral_1h_input_tokens": 700_000}),
                        ("m5", {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 0})):
        d = tmp_path / name
        d.mkdir()
        out.append(_db(d, _extract(d, [_usage(1_000_000, split), _usage(400, None)])))
    return out


PREMIUM = 700_000 * (10 - 6.25) / 1e6   # 1-hour minus 5-minute write rate, Opus 4.8


def test_daily_sessions_and_facts_include_the_1h_premium(tmp_path: Path) -> None:
    from daily import build_per_day
    from facts import build_facts
    from sessions import build_sessions

    got = []
    for con in _pair(tmp_path):
        rows = load_rounds(con)
        per_day = build_per_day(rows, 0)
        sessions = build_sessions(con, rows)
        facts = build_facts(con, rows, build_cost(rows), sessions, per_day, 0)
        priciest = next(f for f in facts if f["id"] == "priciest-round")["value"]
        got.append((sum(d["costUsd"] for d in per_day), sum(s["costUsd"] for s in sessions),
                    float(priciest.lstrip("$").replace(",", ""))))
    (day_h1, sess_h1, fact_h1), (day_m5, sess_m5, fact_m5) = got
    assert day_h1 - day_m5 == pytest.approx(PREMIUM, rel=1e-9)
    assert sess_h1 - sess_m5 == pytest.approx(PREMIUM, rel=1e-9)
    assert fact_h1 - fact_m5 == pytest.approx(PREMIUM, abs=0.011)   # the fact is rounded to cents


def test_session_cost_distribution_includes_the_1h_premium(tmp_path: Path) -> None:
    scd = _load("scd_analyze", "artifacts/session/session_cost_distribution/analyze.py")
    totals = []
    for con in _pair(tmp_path):
        units, meta = scd.collect(con)
        assert meta["unpriced"] == 0
        totals.append(sum(u["app_c"] for u in units["merged"]["step"]))
    assert totals[0] - totals[1] == pytest.approx(PREMIUM, rel=1e-9)


def test_idle_cache_counterfactual_includes_the_1h_premium(tmp_path: Path) -> None:
    cf = _load("cf_analyze", "artifacts/prefix_cache/human_idle_cache_counterfactual/analyze.py")
    observed = [cf.collect(con)["merged"].observed_input_cost for con in _pair(tmp_path)]
    assert observed[0] - observed[1] == pytest.approx(PREMIUM, rel=1e-9)


def _idle_trace(tmp_path: Path, one_hour: bool, with_split: bool = True):
    """Two steps 90 minutes apart; every cache write is 1-hour (or 5-minute) TTL. ``with_split=False``
    leaves out usage.cache_creation, as in older records (and in v0.0.2)."""
    def usage(write: int) -> dict:
        split = {"ephemeral_5m_input_tokens": 0 if one_hour else write,
                 "ephemeral_1h_input_tokens": write if one_hour else 0}
        u = {"input_tokens": 10, "cache_creation_input_tokens": write, "cache_read_input_tokens": 0,
             "output_tokens": 50}
        return {**u, "cache_creation": split} if with_split else u

    def user(ts: str) -> dict:
        return {"type": "user", "timestamp": ts, "sessionId": SESSION, "message": {"role": "user", "content": "go"}}

    records = [user("2026-01-01T00:00:00.000Z"),
               {**_assistant("msg_01", usage(100_000)), "timestamp": "2026-01-01T00:00:01.000Z"},
               user("2026-01-01T01:30:00.000Z"),   # idle gap: the whole prefix is written again
               {**_assistant("msg_02", usage(120_000)), "timestamp": "2026-01-01T01:30:01.000Z"}]
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    rows = extract_session_with_key(path, "demo", SESSION)
    for r in rows:
        r["trace_key"] = round_key(r)
    return _db(tmp_path, rows)


def test_idle_cache_counterfactual_bills_the_writes_it_keeps_at_the_1h_rate(tmp_path: Path) -> None:
    cf = _load("cf_analyze_idle", "artifacts/prefix_cache/human_idle_cache_counterfactual/analyze.py")
    acc = {}
    for name, one_hour in (("h1", True), ("m5", False)):
        (tmp_path / name).mkdir()
        acc[name] = cf.collect(_idle_trace(tmp_path / name, one_hour))["merged"]
    # step 1 (nothing shifted) keeps its 100,000 writes; step 2 (total input 120,010, context growth 20,000):
    # 100,010 tokens move to cache reads and 120,000 - 100,010 = 19,990 writes are kept
    kept_premium = (100_000 + 19_990) * (10 - 6.25) / 1e6
    diff = acc["h1"].counterfactual_input_cost - acc["m5"].counterfactual_input_cost
    assert diff == pytest.approx(kept_premium, rel=1e-9)


@pytest.mark.parametrize("with_split", [True, False])
def test_table13_caption_names_the_1h_rate_only_when_the_trace_has_the_split(tmp_path: Path, with_split) -> None:
    cf = _load(f"cf_caption_{with_split}", "artifacts/prefix_cache/human_idle_cache_counterfactual/analyze.py")
    con = _idle_trace(tmp_path, one_hour=True, with_split=with_split)
    tex = tmp_path / "table13.tex"
    cf.write_latex_table(tex, cf.collect(con), cf.has_1h_split(con))
    caption = tex.read_text(encoding="utf-8").split("\\label")[0]
    assert ("1-hour" in caption) is with_split
    assert "5-minute" in caption
