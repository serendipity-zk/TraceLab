# session_timing_distribution

**Of the wall-clock time a coding agent consumes, how much is the human thinking, the LLM
generating, and the tools executing — per session, per request, and per step?**

The time-domain sibling of `session_cost_distribution`. Computes the data behind
`tab:timing_distribution` (`src/04_SessionContext.tex`): for each granularity and category, avg /
p50 / p90 / p99 per unit plus the category's share of total time (same Avg/P50/P90/P99 + % layout
as the cost table). The category set differs by granularity because **human thinking is a
between-request quantity** and so only exists at the session level:

- **Per session** — `Total elapsed` (wall-clock first→last timing event) = `Human thinking` +
  `LLM generation` + `Tool execution` + `Other (overhead)`.
- **Per request** — `Total (response time)` (turn e2e) = `LLM generation` + `Tool execution` +
  `Other (overhead)`. No human term: human wait sits *between* requests, never inside one.
- **Per step** — `LLM generation` vs `Tool execution` only (one round has no human term, no e2e).

## Definitions

- **LLM generation** (per step) — observable generation span, latest qualifying input event →
  last model-output event; identical to `llm_generation/generation_time_cdf` and the per-round
  generation in `human_in_the_loop/user_turn_decomposition`.
- **Tool execution** (per step) — sum of strictly-positive effective tool latency
  (`tool_internal_latency_ms` else `tool_wall_latency_ms`); identical to
  `tool_calls/tool_latency_distribution`.
- **Human thinking** (per session) — sum of human-input waits (previous model output → next
  response-triggering user message); identical to `human_in_the_loop/human_input_wait`.
- **Request e2e** and the residual `Other (overhead)` = `e2e − generation − tool` match
  `user_turn_decomposition` turn-for-turn (validated: merged e2e 2,782.7h, generation 1,141.3h,
  tool 1,663.4h, residual −22.0h). The residual can be **negative**: summed per-round generation
  and per-tool effective latency overlap (concurrent tools, generation streaming during a tool
  call), so they can exceed the measured e2e.
- **Request** — one user turn (same turn state machine as `user_turn_decomposition`,
  `user_turn_response_time`, `session_internal_counts`, `session_cost_distribution`). **Step** —
  one LLM round. **Session** — one `session_id` (4,258 sessions have a positive wall-clock span;
  the rest are single-timestamp and dropped).

## Running it

```bash
uv run python artifacts/session/session_timing_distribution/analyze.py -i trace/syfi_coding_trace.jsonl
uv run python artifacts/session/session_timing_distribution/analyze.py            # default merged trace
```

## Outputs

- `session_timing_distribution.tex` — the merged single-column timing table (Avg / P50 / P90 / P99
  + % time) for the paper.
- `session_timing_distribution.md` — GFM Markdown mirror of the table, rendered on the web detail page.
- `headline.json` — the few headline numbers for the Overview gallery card.
- stdout — merged + per-provider (Claude / Codex) per-category percentiles and time shares.

## Headline numbers (public trace)

- **Sessions are mostly idle: human thinking is 92.3% of session wall-clock** (avg 7.6h of an 8.2h
  session; medians are tiny — a single-request session has no inter-request gap). The long idle
  tail (session p99 ≈ 206h) is what pushes prompt prefixes past the cache TTL.
- **Within a request, tool execution dominates, not generation: tool 59.8% vs generation 41.0%**
  of the 2,782.7h of total response time (the two slightly overlap, hence shares can exceed 100%).
- Avg response time: **4.3 min / request** (p50 38s, p90 6.4 min); avg active work **11.5s
  generation + 16.8s tool per step**.

The session human share is consistent across providers under the provider-agnostic definition
(Claude 89.9%, Codex 94.3%) — earlier the trigger-based definition undercounted Codex (81.6%) and
spilled ~13% into an "Other" residual, which is why that residual row was removed.

No figures.

## SyFI result analysis

### session_timing_distribution.md

A coding session is mostly idle, waiting on the human (the paper's `tab:timing_distribution`).
**Human thinking is 92.3%** of session wall-clock, dwarfing LLM generation (3.3%) and tool execution
(4.8%); most sessions are short — the median is a single request with no inter-request gap — but a
heavy tail of sessions left open for hours or days (session p99 elapsed ≈ 206h) accumulates most of
that idle. Capping each gap at one hour (the cache-relevant budget) drops the human share to 64.3%,
with generation and tool rising to 14.5% and 21.2%. Inside an individual request the human term
vanishes and **tool execution leads generation, 59.8% vs 41.0%** of the 2,783h of total response
time; an average request runs 4.3 min end to end (median 38s, p90 6.4 min), and per active step the
model spends ~11.5s generating and ~16.8s in tools.
