# Normalized trace format v3: agent structure

Status: **proposal** (2026-10-16). Builds on PR #30 (subagent hierarchy) and PR #37 (session
relationships). Field names follow #30 where it already defines them.

v3 keeps the v2 shape, one JSONL row per LLM round, and adds fields. Nothing in v2 changes meaning
except one correction: `user_message` events now come only from text a person wrote (see
[Who wrote the input](#who-wrote-the-input)). A missing field or `null` means **unknown**, never
"main session" or "human".

## Why

Measured on the raw archives of all four hosts (2026-10-16, 279.2B input tokens after dedup):

| sessions started by an agent | input tokens | share |
|---|---|---|
| Claude Code subagents (incl. workflows 1.3%, agent teams 1.75%) | 36.1B | 12.9% |
| Codex subagents (`spawn_agent`) | 16.2B | 5.8% |
| Codex automatic review (`guardian`) | 5.7B | 2.0% |
| **total** | **58.0B** | **20.8%** |

Another 38.3B (13.7%) comes from `codex exec` sessions, mostly started by scripts or agent systems.
In v2 all of these look like independent sessions opened by a person: a delegated task is split
across unrelated sessions, concurrency between subagents is invisible, and agent-written prompts
count as human input.

## Session fields

Every round of a session carries the same values, so each row stands on its own.

| field | values | meaning |
|---|---|---|
| `thread_kind` | `main` · `subagent` · `guardian` · `internal` | who started the session. `guardian` = Codex automatic review; `internal` = Codex's own `review` / `compact` sub-sessions |
| `parent_session_id` | session id | the session that started this one (the **direct** parent). For `guardian`, the session it reviewed |
| `root_session_id` | session id | the top `main` session of the tree; equals `session_id` for a main session |
| `spawn_depth` | 0, 1, 2, … | 0 = main; 1 = started by the main session; 2 = started by a depth-1 subagent; … |
| `spawn_tool_call_id` | tool call id | the tool call in the parent that started this session (joins to `tools[].tool_call_id` of the parent's rounds) |
| `spawn_mode` | `foreground` · `background` · `teammate` | whether the parent waited for it, ran it concurrently, or it is a peer in a Claude Code agent team |
| `is_fork` | bool | the session starts with a copy of the parent's conversation |
| `agent_role` | role | built-in roles kept (`general-purpose`, `Explore`, `Plan`, `workflow-subagent`, `fork`, `claude-code-guide`, `statusline-setup`, `guardian`, `review`, `compact`); others become `custom_role_N` |
| `workflow_id` | opaque id | Claude Code workflow run that started it |
| `launch_source` | `interactive` · `ide` · `desktop` · `sdk` · `exec` · `agent` | how the session was started |
| `trace_schema_version` | `3` | |

Why each field earns its place:

- **Direct parent and root, not one or the other.** Nesting is real: 256 Claude subagents have
  `spawnDepth` 2–3 and their direct parent is another subagent (`parentAgentId`); Codex has 25
  sessions at depth 2–3. Pointing them at the main session (as #30 does) is wrong; leaving the root
  unknown (as #37 does for Codex) loses the tree.
- **`spawn_tool_call_id`** places a child on its parent's timeline: which round started it, and
  what the parent did while it ran. It is recorded for 2,198 Claude subagents and every Codex
  `spawn_agent`.
- **`spawn_mode`** separates concurrency from blocking delegation. Most recent Claude subagents run
  in the background (`requestShape: background`, `isAsync: true`); all Codex `spawn_agent` children
  do. 154 of 158 Claude agent-team members have `spawnDepth` 0: they are peers of the session that
  started the team, not its children, hence `teammate`.
- **`workflow_id`**: 49 workflow runs, 1–265 subagents each, 3.6B tokens. The parent session alone
  cannot group them: 8 of 14 parents ran several workflows (up to 15) and 140 other subagents share
  those parents.
- **`launch_source`** separates people from automation among `main` sessions; `exec` is the
  largest Codex source (32% of Codex tokens).

Left out on purpose: an agent-team id (18 teams from 3 users; the parent session already groups
them in all but 2 cases), `workflowPhase` (33 author-written names, no meaning once anonymized), and
anything the user wrote about the task: `description`, `prompt`, `name`, `teamName`, Codex
`agent_path` and `agent_nickname`. These are read only to link sessions, never written out.

## Where each field comes from

### Claude Code

Subagent transcripts sit flat under the parent session's directory, whatever their depth:
`<session>/subagents/agent-<id>.jsonl`, or `<session>/subagents/workflows/<wf>/agent-<id>.jsonl`.
Each has a sidecar `agent-<id>.meta.json` (4,492 found).

| field | source |
|---|---|
| `thread_kind` | `subagent` for files under `subagents/`; inline sidechain records in an old main transcript (`isSidechain: true`) as in #30 |
| `parent_session_id` | `meta.parentAgentId` → `claude:<session>/subagents/agent-<parentAgentId>`; else the session directory's main transcript |
| `root_session_id` | the session directory's main transcript |
| `spawn_depth` | `meta.spawnDepth`; else 1 (0 for teammates) |
| `spawn_tool_call_id` | `meta.toolUseId` |
| `spawn_mode` | `taskKind: in_process_teammate` → `teammate`; `requestShape: background` → `background`; else the parent's tool result (`toolUseResult.isAsync`, `status: async_launched`); else `foreground` |
| `is_fork` | `meta.isFork` or `agentType: fork` |
| `agent_role` | `meta.agentType`, else the records' `attributionAgent` |
| `workflow_id` | the `workflows/<wf>` directory |
| `launch_source` | main sessions: records' `entrypoint` (`cli` → `interactive`, `claude-vscode` → `ide`, `claude-desktop` → `desktop`, `sdk-*` → `sdk`); subagents: `agent` |

### Codex

The first `session_meta` of a rollout describes the file's own session.

| field | source |
|---|---|
| `thread_kind` | `source.subagent.thread_spawn` → `subagent`; `source.subagent.other == "guardian"` (or `thread_source: guardian_review`) → `guardian`; `source.subagent` `review` / `compact` → `internal`; else `main` |
| `parent_session_id` | `thread_spawn.parent_thread_id`; for `guardian`, `session_meta.parent_thread_id` |
| `root_session_id` | follow `parent_session_id` up to a `main` session (needs the other rollouts, see [Processing](#processing)) |
| `spawn_depth` | `thread_spawn.depth` |
| `spawn_tool_call_id` | the parent's `spawn_agent` call whose output `task_name` equals the child's `thread_spawn.agent_path` |
| `spawn_mode` | `background` |
| `is_fork` | the parent's `spawn_agent` arguments carry `fork_turns` other than `none` |
| `agent_role` | `thread_spawn.agent_role`; `guardian`; `review` / `compact` |
| `workflow_id` | null |
| `launch_source` | `source`: `cli` → `interactive`, `vscode` → `ide`, `exec` → `exec`; subagents and reviews → `agent` |

A forked child replays the parent's conversation before its own turns. Replayed rounds keep the
parent's identity and are dropped in favour of the parent's own copy (see
[Merging](#merging)); only the child's live rounds get the child's session fields.

## Who wrote the input

v2 counts every user record that is not a tool result as `user_message`. Claude Code also writes
system reminders, background-task notifications and messages from other agents as user records:

| Claude user records (not tool results), all hosts | count |
|---|---|
| `origin.kind: human` (typed, queued, SDK, accepted suggestion) | 24,000 |
| `isMeta: true` (system reminders, command caveats, …) | 39,000 |
| `origin.kind: task-notification` (a background task finished) | 12,400 |
| `origin.kind: peer` / `coordinator` (agent-team messages) | 1,060 |
| no `origin` marker (mostly older clients) | 70,000 |

Codex has the same problem with `response_item/agent_message` (29,209 messages between agents, with
`author` and `recipient`): v2 does not read them, so the rounds they start have no trigger.

v3 splits the input events:

| `event_type` | from |
|---|---|
| `user_message` | text a person wrote: Claude `origin.kind: human`, or no `origin` and not `isMeta`; Codex `user_message` events in `main` sessions |
| `agent_message` | Claude `origin.kind` `task-notification` / `peer` / `coordinator`; Codex `response_item/agent_message`; the first prompt of a subagent (written by its parent) |
| `system_message` | Claude `isMeta: true`, `origin.kind: auto-continuation` |
| `tool_result` | unchanged |

`first_input_event_type`, the `current_*` counts and `current_input_chars` follow the same split.
Old Claude records without `origin` are treated as human unless `isMeta` is set, which is what v2
did, so for them nothing changes.

## Sanitization

Extends #30's rules:

- `session_id`, `parent_session_id`, `root_session_id` share one map, so links survive.
- `spawn_tool_call_id` shares the `tool_call_id` map, so it still joins to the parent's tool call.
- `workflow_id` → opaque `wf_N`; `agent_role` → built-in or `custom_role_N`.
- `thread_kind`, `spawn_mode`, `launch_source` are closed vocabularies; any other value becomes null.
- The user-written fields listed above never reach a row.

## Processing

Two fields need other files: `root_session_id` for Codex (the parent's own parent) and
`spawn_tool_call_id` for Codex (the parent's `spawn_agent` output). Per-file extraction emits the
direct facts and records `_spawn_agent_path` and `_parent_spawn_calls` as private hints; a
collection-level pass over all extracted rows then

1. resolves `root_session_id` by following `parent_session_id` (cycle-safe; null if a parent is
   missing),
2. matches each Codex child to its parent's `spawn_agent` call,
3. drops the private hints.

The collector now reads the raw archives (`tracelab-raw/<host>/<run>/<user>.tar.zst`), which hold
every store on every host, so both lookups see the parent. A session whose parent is not in the
collection keeps `parent_session_id` and gets `root_session_id: null`.

## Merging

`merge_round_traces.py` keeps the last copy of a round identity today, so a replayed copy in a
child rollout can replace the parent's original. v3 prefers, in order: a row with a trigger event
over one without; a row whose `session_id` owns the file over a replayed copy; then the newest.

## Compatibility

- v2 rows have no v3 fields; readers treat them as unknown. Analyze already hides the hierarchy UI
  when the columns are absent (#30).
- `trace_db` pins the new string columns to VARCHAR, `spawn_depth` to BIGINT and `is_fork` to
  BOOLEAN, so all-null columns do not infer as JSON.
- Re-extracting v2-era sessions from raw changes `user_message` counts where Claude records carry
  `origin` / `isMeta`; the release notes must say so.

## Plan

1. Rebase #30 on main, fold in #37's `parentAgentId` / `root_session_id` logic, close #37 with
   credit.
2. Add `spawn_depth`, `spawn_tool_call_id`, `spawn_mode`, `is_fork`, `launch_source`,
   `trace_schema_version`, and the collection-level pass.
3. Split input events (`agent_message`, `system_message`); add `tool_search_output` as a tool
   result (Codex).
4. Merge preference for originals over replayed copies.
5. Collector reads raw archives; re-extract everything and compare totals with the 2026-10-16
   accounting (1,547,073 rounds, 279.2B input).
