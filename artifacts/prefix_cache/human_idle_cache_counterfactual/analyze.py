#!/usr/bin/env python3
"""Cost of human thinking time under prefix-cache misses.

This analyzes prefix caching from a consumer perspective: how much fresh-input prefill and API cost
does a user pay because "thinking" time can expire the prefix cache? It reports an upper bound on
savings from eliminating user-thinking-induced prefix-cache misses. For every user-initiated step
``S`` with a predecessor ``P`` in the same session, this experiment caps retained-cache append at
the step's net context growth:

  * ``total_input(S)``      = ``prefix_tokens(S) + newly_append_tokens(S)``
  * ``context_growth(S)``   = ``max(0, total_input(S) - total_input(P))``
  * ``append_after_retained_cache(S) = min(newly_append_tokens(S), context_growth(S))``

All other steps keep their observed ``prefix_tokens`` / ``newly_append_tokens`` split. The total
input length of each step is unchanged, so any append-token reduction is moved into prefix tokens
and billed at the cache-read price. Remaining Claude cache-creation tokens are billed at the
5-minute cache-write rate. Output tokens are unchanged. This is an upper-bound estimate because it
assumes all shifted tokens can be served from cache at the cache-read rate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "web_analytics"))

import md_table  # noqa: E402
import pricing  # noqa: E402
import trace_db  # noqa: E402

DEFAULT_OUTPUT_DIR = SCRIPT_DIR
SCOPES = ("merged", "claude", "codex")
TABLE_SCOPES = (("claude", "Claude"), ("codex", "Codex"), ("merged", "Total"))


@dataclass
class ScopeAccum:
    rounds: int = 0
    user_rounds: int = 0
    user_rounds_with_predecessor: int = 0
    user_rounds_reduced: int = 0
    user_rounds_increased: int = 0
    observed_prefix_tokens: int = 0
    observed_append_tokens: int = 0
    counterfactual_prefix_tokens: int = 0
    counterfactual_append_tokens: int = 0
    observed_user_append_tokens: int = 0
    counterfactual_user_append_tokens: int = 0
    output_tokens: int = 0
    priced_rounds: int = 0
    unpriced_rounds: int = 0
    observed_input_cost: float = 0.0
    observed_cached_cost: float = 0.0
    observed_output_cost: float = 0.0
    counterfactual_input_cost: float = 0.0
    counterfactual_cached_cost: float = 0.0
    counterfactual_output_cost: float = 0.0
    reduced_cost_savings: list[float] = field(default_factory=list)
    reduced_idle_gaps_seconds: list[float] = field(default_factory=list)

    @property
    def append_reduction_tokens(self) -> int:
        return self.observed_append_tokens - self.counterfactual_append_tokens

    @property
    def user_append_reduction_tokens(self) -> int:
        return self.observed_user_append_tokens - self.counterfactual_user_append_tokens

    @property
    def observed_total_cost(self) -> float:
        return self.observed_input_cost + self.observed_cached_cost + self.observed_output_cost

    @property
    def counterfactual_total_cost(self) -> float:
        return (
            self.counterfactual_input_cost
            + self.counterfactual_cached_cost
            + self.counterfactual_output_cost
        )

    @property
    def cost_reduction(self) -> float:
        return self.observed_total_cost - self.counterfactual_total_cost

    @property
    def input_cost_reduction(self) -> float:
        return self.observed_input_cost - self.counterfactual_input_cost


def _int_or_zero(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _has_round_column(con, column: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT count(*) > 0
            FROM information_schema.columns
            WHERE table_name = 'rounds' AND column_name = ?
            """,
            [column],
        ).fetchone()[0]
    )


def _pct(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    upper_weight = index - lower
    lower_weight = 1.0 - upper_weight
    return ordered[lower] * lower_weight + ordered[upper] * upper_weight


def _dist(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "avg": None, "p50": None, "p90": None}
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
    }


def _money(value: float, *, tex: bool = False) -> str:
    sign = "\\$" if tex else "$"
    if abs(value) >= 1000:
        return f"{sign}{value:,.0f}"
    if abs(value) >= 100:
        return f"{sign}{value:,.1f}"
    return f"{sign}{value:,.2f}"


def _money_small(value: float | None, *, tex: bool = False) -> str:
    if value is None:
        return "--"
    sign = "\\$" if tex else "$"
    mag = abs(value)
    if mag >= 1:
        body = f"{value:,.2f}"
    elif mag >= 0.01:
        body = f"{value:,.3f}"
    elif mag >= 0.001:
        body = f"{value:,.4f}"
    else:
        body = f"{value:,.6f}"
    return f"{sign}{body}"


def _duration(value_seconds: float | None) -> str:
    if value_seconds is None:
        return "--"
    if value_seconds < 1:
        return f"{value_seconds * 1000:.0f}ms"
    if value_seconds < 10:
        return f"{value_seconds:.2f}s"
    if value_seconds < 60:
        return f"{value_seconds:.1f}s"
    minutes = value_seconds / 60
    if minutes < 10:
        return f"{minutes:.2f}m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    if hours < 10:
        return f"{hours:.2f}h"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _tokens(value: int | float) -> str:
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        return f"{sign}{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{sign}{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{sign}{value / 1e3:.1f}K"
    return f"{sign}{value:.0f}"


def _tokens_tex(value: int | float) -> str:
    return _tokens(value).replace("B", "\\,B").replace("M", "\\,M").replace("K", "\\,K")


def _pct_str(value: float | None, *, tex: bool = False) -> str:
    if value is None:
        return "--"
    suffix = "\\%" if tex else "%"
    return f"{value * 100:.1f}{suffix}"


def _add_round(
    acc: ScopeAccum,
    *,
    event_type: str | None,
    has_predecessor: bool,
    pre: int,
    app: int,
    out: int,
    cf_pre: int,
    cf_app: int,
    observed_cost: pricing.RoundCost | None,
    counterfactual_cost: pricing.RoundCost | None,
    cost_reduction_sample: float | None,
    idle_gap_seconds: float | None,
) -> None:
    acc.rounds += 1
    acc.observed_prefix_tokens += pre
    acc.observed_append_tokens += app
    acc.counterfactual_prefix_tokens += cf_pre
    acc.counterfactual_append_tokens += cf_app
    acc.output_tokens += out

    if event_type == "user_message":
        acc.user_rounds += 1
        if has_predecessor:
            acc.user_rounds_with_predecessor += 1
            acc.observed_user_append_tokens += app
            acc.counterfactual_user_append_tokens += cf_app
            if app > cf_app:
                acc.user_rounds_reduced += 1
                if cost_reduction_sample is not None and cost_reduction_sample > 0:
                    acc.reduced_cost_savings.append(cost_reduction_sample)
                if idle_gap_seconds is not None:
                    acc.reduced_idle_gaps_seconds.append(idle_gap_seconds)
            elif cf_app > app:
                acc.user_rounds_increased += 1

    if observed_cost is None or counterfactual_cost is None:
        acc.unpriced_rounds += 1
        return
    acc.priced_rounds += 1
    acc.observed_input_cost += observed_cost["inputCost"]
    acc.observed_cached_cost += observed_cost["cachedCost"]
    acc.observed_output_cost += observed_cost["outputCost"]
    acc.counterfactual_input_cost += counterfactual_cost["inputCost"]
    acc.counterfactual_cached_cost += counterfactual_cost["cachedCost"]
    acc.counterfactual_output_cost += counterfactual_cost["outputCost"]


def collect(con) -> dict[str, ScopeAccum]:
    """Collect observed and counterfactual token/cost totals by provider and merged scope."""
    cache_write_expr = (
        "r.claude_cache_creation_input_tokens"
        if _has_round_column(con, "claude_cache_creation_input_tokens")
        else "CAST(NULL AS BIGINT) AS claude_cache_creation_input_tokens"
    )
    rows = con.execute(
        """
        WITH first_ev AS (
            SELECT round_pk,
                   event_type,
                   CAST(epoch_us(timestamp) AS BIGINT) AS first_event_us
            FROM timing_events
            WHERE event_index = 1
        ),
        activity AS (
            SELECT round_pk, CAST(epoch_us(timestamp) AS BIGINT) AS activity_us
            FROM timing_events
            WHERE timestamp IS NOT NULL
            UNION ALL
            SELECT round_pk, CAST(epoch_us(emitted_at) AS BIGINT) AS activity_us
            FROM tool_calls
            WHERE emitted_at IS NOT NULL
            UNION ALL
            SELECT round_pk, CAST(epoch_us(result_at) AS BIGINT) AS activity_us
            FROM tool_calls
            WHERE result_at IS NOT NULL
        ),
        activity_bounds AS (
            SELECT round_pk,
                   min(activity_us) AS min_activity_us,
                   max(activity_us) AS last_activity_us
            FROM activity
            GROUP BY round_pk
        )
        SELECT r.round_pk,
               r.session_id,
               r.provider,
               r.model,
               r.prefix_tokens,
               r.newly_append_tokens,
               {cache_write_expr},
               r.output_tokens,
               f.event_type,
               f.first_event_us,
               b.min_activity_us,
               b.last_activity_us
        FROM rounds r LEFT JOIN first_ev f USING (round_pk)
                      LEFT JOIN activity_bounds b USING (round_pk)
        ORDER BY r.round_pk
        """.format(cache_write_expr=cache_write_expr)
    ).fetchall()

    accums: dict[str, ScopeAccum] = defaultdict(ScopeAccum)
    last_by_session: dict[str, dict[str, int | None]] = {}
    for (
        _round_pk,
        session_id,
        provider,
        model,
        prefix_tokens,
        append_tokens,
        cache_write_tokens,
        output_tokens,
        event_type,
        first_event_us,
        min_activity_us,
        last_activity_us,
    ) in rows:
        provider = provider if isinstance(provider, str) else "unknown"
        pre = _int_or_zero(prefix_tokens)
        app = _int_or_zero(append_tokens)
        cache_write = min(_int_or_zero(cache_write_tokens), app)
        out = _int_or_zero(output_tokens)
        total_input = pre + app

        has_predecessor = isinstance(session_id, str) and session_id in last_by_session
        first_activity_us = first_event_us if first_event_us is not None else min_activity_us
        idle_gap_seconds = None
        if event_type == "user_message" and has_predecessor:
            previous_last_activity_us = last_by_session[session_id].get("last_activity_us")
            if previous_last_activity_us is not None and first_activity_us is not None:
                delta = (first_activity_us - previous_last_activity_us) / 1e6
                if delta >= 0:
                    idle_gap_seconds = delta
        cf_app = app
        if event_type == "user_message" and has_predecessor:
            previous = last_by_session[session_id]
            context_growth = max(0, total_input - (previous["total_input"] or 0))
            cf_app = min(app, context_growth)
        cf_pre = total_input - cf_app
        shifted_from_append = max(0, app - cf_app)
        cf_cache_write = max(0, cache_write - shifted_from_append)
        cf_cache_write = min(cf_cache_write, cf_app)

        price = pricing.price_for(provider, model)
        observed_cost = counterfactual_cost = None
        cost_reduction_sample = None
        if price is not None:
            observed_cost = pricing.round_cost(
                price, pre, app, out, cache_write_tokens=cache_write
            )
            counterfactual_cost = pricing.round_cost(
                price, cf_pre, cf_app, out, cache_write_tokens=cf_cache_write
            )
            cost_reduction_sample = observed_cost["total"] - counterfactual_cost["total"]

        for scope in ("merged", provider):
            _add_round(
                accums[scope],
                event_type=event_type if isinstance(event_type, str) else None,
                has_predecessor=has_predecessor,
                pre=pre,
                app=app,
                out=out,
                cf_pre=cf_pre,
                cf_app=cf_app,
                observed_cost=observed_cost,
                counterfactual_cost=counterfactual_cost,
                cost_reduction_sample=cost_reduction_sample,
                idle_gap_seconds=idle_gap_seconds,
            )

        if isinstance(session_id, str):
            last_by_session[session_id] = {
                "total_input": total_input,
                "output_tokens": out,
                "last_activity_us": last_activity_us,
            }
    return dict(accums)


def write_summary_csv(path: Path, accums: dict[str, ScopeAccum]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "scope",
        "rounds",
        "user_rounds",
        "user_rounds_with_predecessor",
        "user_rounds_reduced",
        "user_rounds_increased",
        "observed_append_tokens",
        "counterfactual_append_tokens",
        "append_reduction_tokens",
        "append_reduction_pct_of_all_append",
        "observed_user_append_tokens",
        "counterfactual_user_append_tokens",
        "user_append_reduction_tokens",
        "user_append_reduction_pct",
        "observed_prefix_tokens",
        "counterfactual_prefix_tokens",
        "output_tokens",
        "priced_rounds",
        "unpriced_rounds",
        "observed_input_cost_usd",
        "counterfactual_input_cost_usd",
        "input_cost_reduction_usd",
        "observed_total_cost_usd",
        "counterfactual_total_cost_usd",
        "total_cost_reduction_usd",
        "total_cost_reduction_pct",
        "cost_saved_per_reduced_step_samples",
        "cost_saved_per_reduced_step_avg_usd",
        "cost_saved_per_reduced_step_p50_usd",
        "cost_saved_per_reduced_step_p90_usd",
        "idle_gap_per_reduced_step_samples",
        "idle_gap_per_reduced_step_avg_seconds",
        "idle_gap_per_reduced_step_p50_seconds",
        "idle_gap_per_reduced_step_p90_seconds",
        "pricing_as_of",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            acc = accums.get(scope, ScopeAccum())
            cost_dist = _dist(acc.reduced_cost_savings)
            idle_dist = _dist(acc.reduced_idle_gaps_seconds)
            writer.writerow(
                {
                    "scope": scope,
                    "rounds": acc.rounds,
                    "user_rounds": acc.user_rounds,
                    "user_rounds_with_predecessor": acc.user_rounds_with_predecessor,
                    "user_rounds_reduced": acc.user_rounds_reduced,
                    "user_rounds_increased": acc.user_rounds_increased,
                    "observed_append_tokens": acc.observed_append_tokens,
                    "counterfactual_append_tokens": acc.counterfactual_append_tokens,
                    "append_reduction_tokens": acc.append_reduction_tokens,
                    "append_reduction_pct_of_all_append": _pct(
                        acc.append_reduction_tokens, acc.observed_append_tokens
                    ),
                    "observed_user_append_tokens": acc.observed_user_append_tokens,
                    "counterfactual_user_append_tokens": acc.counterfactual_user_append_tokens,
                    "user_append_reduction_tokens": acc.user_append_reduction_tokens,
                    "user_append_reduction_pct": _pct(
                        acc.user_append_reduction_tokens, acc.observed_user_append_tokens
                    ),
                    "observed_prefix_tokens": acc.observed_prefix_tokens,
                    "counterfactual_prefix_tokens": acc.counterfactual_prefix_tokens,
                    "output_tokens": acc.output_tokens,
                    "priced_rounds": acc.priced_rounds,
                    "unpriced_rounds": acc.unpriced_rounds,
                    "observed_input_cost_usd": acc.observed_input_cost,
                    "counterfactual_input_cost_usd": acc.counterfactual_input_cost,
                    "input_cost_reduction_usd": acc.input_cost_reduction,
                    "observed_total_cost_usd": acc.observed_total_cost,
                    "counterfactual_total_cost_usd": acc.counterfactual_total_cost,
                    "total_cost_reduction_usd": acc.cost_reduction,
                    "total_cost_reduction_pct": _pct(
                        acc.cost_reduction, acc.observed_total_cost
                    ),
                    "cost_saved_per_reduced_step_samples": cost_dist["count"],
                    "cost_saved_per_reduced_step_avg_usd": cost_dist["avg"],
                    "cost_saved_per_reduced_step_p50_usd": cost_dist["p50"],
                    "cost_saved_per_reduced_step_p90_usd": cost_dist["p90"],
                    "idle_gap_per_reduced_step_samples": idle_dist["count"],
                    "idle_gap_per_reduced_step_avg_seconds": idle_dist["avg"],
                    "idle_gap_per_reduced_step_p50_seconds": idle_dist["p50"],
                    "idle_gap_per_reduced_step_p90_seconds": idle_dist["p90"],
                    "pricing_as_of": pricing.PRICING_AS_OF,
                }
            )


def _table_rows(accums: dict[str, ScopeAccum], *, tex: bool = False) -> list[list[str]]:
    def cells(fn) -> list[str]:
        return [fn(accums.get(scope, ScopeAccum())) for scope, _label in TABLE_SCOPES]

    def reduction_cell(a: ScopeAccum) -> str:
        tokens = _tokens_tex(a.append_reduction_tokens) if tex else _tokens(a.append_reduction_tokens)
        pct = _pct_str(_pct(a.append_reduction_tokens, a.observed_append_tokens), tex=tex)
        return f"{tokens} ({pct})"

    def cost_reduction_cell(a: ScopeAccum) -> str:
        cost = _money(a.cost_reduction, tex=tex)
        pct = _pct_str(_pct(a.cost_reduction, a.observed_total_cost), tex=tex)
        return f"{cost} ({pct})"

    return [
        ["User-initiated steps with predecessor", *cells(lambda a: f"{a.user_rounds_with_predecessor:,}")],
        ["Observed append tokens", *cells(lambda a: _tokens_tex(a.observed_append_tokens) if tex else _tokens(a.observed_append_tokens))],
        ["Append after retained cache", *cells(lambda a: _tokens_tex(a.counterfactual_append_tokens) if tex else _tokens(a.counterfactual_append_tokens))],
        ["Append-token reduction", *cells(reduction_cell)],
        ["Observed total cost", *cells(lambda a: _money(a.observed_total_cost, tex=tex))],
        ["Cost after retained cache", *cells(lambda a: _money(a.counterfactual_total_cost, tex=tex))],
        ["Total cost reduction", *cells(cost_reduction_cell)],
        [
            "Cost saved / reduced step avg",
            *cells(lambda a: _money_small(_dist(a.reduced_cost_savings)["avg"], tex=tex)),
        ],
    ]


def render_md(accums: dict[str, ScopeAccum]) -> str:
    headers = ["Metric", "Claude", "Codex", "Total"]
    return md_table.gfm_table(headers, _table_rows(accums), ["l", "r", "r", "r"])


def write_latex_table(path: Path, accums: dict[str, ScopeAccum]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _table_rows(accums, tex=True)
    lines = [
        "% AUTO-GENERATED by artifacts/prefix_cache/human_idle_cache_counterfactual/analyze.py",
        "% do not edit by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Upper-bound append-token and cost savings from eliminating user-thinking-induced",
        "prefix-cache misses. Append after retained cache is capped at context growth for",
        "user-initiated steps; shifted tokens are billed at the cache-read",
        "rate, and remaining Claude cache-created append tokens use the 5-minute",
        "cache-write rate from the provider list prices.}",
        "\\label{tab:human_idle_cache_counterfactual}",
        "\\small",
        "\\setlength{\\tabcolsep}{6pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Claude} & \\textbf{Codex} & \\textbf{Total} \\\\",
        "\\midrule",
    ]
    for metric, claude, codex, total in rows:
        lines.append(f"{metric} & {claude} & {codex} & {total} \\\\")
    lines.extend(["\\bottomrule", "\\end{tabular}", "\\end{table}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def render_headline(accums: dict[str, ScopeAccum]) -> list[dict[str, str]]:
    total = accums.get("merged", ScopeAccum())
    return [
        {"label": "Append reduction", "value": _tokens(total.append_reduction_tokens)},
        {"label": "Cost reduction", "value": _money(total.cost_reduction)},
        {
            "label": "Final cost saved",
            "value": _pct_str(_pct(total.cost_reduction, total.observed_total_cost)),
        },
    ]


def render_stdout(accums: dict[str, ScopeAccum]) -> str:
    lines: list[str] = []
    for scope in SCOPES:
        acc = accums.get(scope, ScopeAccum())
        coverage = _pct(acc.priced_rounds, acc.priced_rounds + acc.unpriced_rounds)
        cost_dist = _dist(acc.reduced_cost_savings)
        idle_dist = _dist(acc.reduced_idle_gaps_seconds)
        lines.append(
            f"[{scope}] user steps with predecessor={acc.user_rounds_with_predecessor:,}, "
            f"reduced={acc.user_rounds_reduced:,}, increased={acc.user_rounds_increased:,}"
        )
        lines.append(
            f"  append: observed {_tokens(acc.observed_append_tokens)} -> "
            f"{_tokens(acc.counterfactual_append_tokens)} "
            f"(reduction {_tokens(acc.append_reduction_tokens)}, "
            f"{_pct_str(_pct(acc.append_reduction_tokens, acc.observed_append_tokens))} of all append)"
        )
        lines.append(
            f"  total cost: {_money(acc.observed_total_cost)} -> "
            f"{_money(acc.counterfactual_total_cost)} "
            f"(reduction {_money(acc.cost_reduction)}, "
            f"{_pct_str(_pct(acc.cost_reduction, acc.observed_total_cost))}); "
            f"priced rounds={_pct_str(coverage)}"
        )
        lines.append(
            f"  cost saved / reduced step: avg {_money_small(cost_dist['avg'])}, "
            f"p50 {_money_small(cost_dist['p50'])}, p90 {_money_small(cost_dist['p90'])}"
        )
        lines.append(
            f"  idle gap / reduced step: avg {_duration(idle_dist['avg'])}, "
            f"p50 {_duration(idle_dist['p50'])}, p90 {_duration(idle_dist['p90'])}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    con = trace_db.open_from_args(args)
    try:
        accums = collect(con)
    finally:
        con.close()

    summary_csv = output_dir / "human_idle_cache_counterfactual_summary.csv"
    md_path = output_dir / "human_idle_cache_counterfactual.md"
    tex_path = output_dir / "human_idle_cache_counterfactual.tex"
    headline_path = output_dir / "headline.json"

    write_summary_csv(summary_csv, accums)
    md_path.write_text(render_md(accums), encoding="utf-8")
    write_latex_table(tex_path, accums)
    headline_path.write_text(json.dumps(render_headline(accums), indent=2) + "\n", encoding="utf-8")

    print(render_stdout(accums))
    print(f"summary_csv={summary_csv}")
    print(f"md_table={md_path}")
    print(f"latex_table={tex_path}")
    print(f"headline={headline_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
