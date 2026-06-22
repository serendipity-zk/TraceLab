---
name: tracelab-public-release
description: Publish TraceLab snapshots from the internal repo to the public uw-syfi/TraceLab GitHub repo as clean, mergeable, incremental pull requests from a persistent public mirror branch. Use when refreshing the public site/artifacts, drafting a public release PR, reconciling a diverged public branch, or creating GitHub releases with syfi_coding_trace.jsonl.gz and syfi_coding_trace.duckdb assets after the PR is merged.
---

# TraceLab Public Release

## Purpose

Publish curated TraceLab snapshots from the internal repository to the public repository:

```text
https://github.com/uw-syfi/TraceLab.git
```

as **incremental, mergeable pull requests** that preserve git ancestry, so every release shows
only its true net diff and can be merged without conflicts. Do not push snapshots directly to the
public `main`; open a (draft) PR for review.

## Mental model — why ancestry matters

The public repo must always be reachable as an **ancestor** of the branch you open the PR from.
When that holds, GitHub's PR diff (merge-base...head) equals the real net change and the PR merges
cleanly. When it does not, git falls back to an ancient common ancestor and reports **phantom
conflicts** — add/add on every new file and content conflicts on every edited file — even though
the branch is a clean superset of what the public repo already has.

This is exactly what the old "export to a temp dir, wipe a clean clone of public, copy the export,
commit one snapshot" method caused: each release landed in public as a **detached snapshot commit**
with no shared history with the source branch. The next fork→public PR then diffed against the last
truly shared commit (e.g. an early `Merge PR #1`), surfacing hundreds of files that were never
actually in conflict. **Do not publish by wipe-and-snapshot anymore.** Publish from a persistent
mirror whose history stays linked to public.

## Remotes

| Remote   | URL                                            | Role                                                        |
| -------- | ---------------------------------------------- | ----------------------------------------------------------- |
| `origin` | internal source of truth (private)             | Internal development. Never the PR target.                  |
| `public` | `https://github.com/uw-syfi/TraceLab.git`      | PR target. **Never push to its `main` directly.**           |
| `fork`   | `https://github.com/serendipity-zk/TraceLab.git` | The **public mirror**. Push the release branch here; open the PR from it. |

The mirror branch (the fork's `main`; commonly checked out locally as `fork-main`) **is** the
public-safe history. Keep it linked to `public/main`.

## Guardrails

1. Treat `origin` as the internal/source-of-truth remote unless the user says otherwise.
2. Never push to the public `main`. Push only to the `fork` mirror (its `main` or a release branch)
   and open a PR into `public:main`.
3. Keep `public/main` an **ancestor** of the branch you open the PR from. Reconcile first (below)
   whenever it is not — never open a PR that GitHub reports as conflicting.
4. Do not commit trace data to Git. Release data files belong only on GitHub Releases. Keep
   ignored/local files such as `trace/*.jsonl*`, `trace/*.duckdb`, `trace.tar.gz`, generated
   artifact outputs, and server runtime data out of the public tree.
5. When a reconciling merge conflicts, resolve in favor of the **mirror** for content the mirror has
   advanced — but first confirm no genuine public-only commit (community PR, hotfix) or public-only
   file is being dropped.
6. Preserve the web product title `SyFI Trace Atlas` unless the user explicitly asks to rename it.
7. Do not put dates in PR titles or branch names. Name the branch after the actual release theme.

## Release workflow

### 1. Sync and inspect

```bash
git fetch public
git fetch fork
git status --short --branch
git remote -v
```

Add the public/fork remotes if missing:

```bash
git remote add public https://github.com/uw-syfi/TraceLab.git
git remote add fork   https://github.com/serendipity-zk/TraceLab.git
```

### 2. Land intentional changes on the mirror

Check out the mirror branch (e.g. `fork-main`) and make sure every change you intend to publish is
committed there. Do not try to snapshot a dirty worktree — commit the intentional source/doc changes
first, and leave ignored trace data and generated outputs uncommitted.

### 3. Re-link ancestry (the key step)

Make `public/main` an ancestor of the mirror by merging it in:

```bash
git merge --no-ff public/main
```

- If `public/main` is already an ancestor, this is a no-op or fast-forward — skip to step 4.
- If it conflicts, the conflicts are almost certainly **phantom** (public holds an older copy of the
  same lineage from a prior snapshot release). Confirm the mirror is the newer superset and resolve
  every conflict to the mirror, then verify nothing real was dropped:

  ```bash
  # see why each side diverged — public's version should be a subset of the mirror's
  git diff <merge-base> public/main -- <file>     # what the public snapshot changed
  git diff <merge-base> fork-main  -- <file>      # what the mirror changed (should subsume it)

  # resolve all conflicts to the mirror
  for f in $(git diff --name-only --diff-filter=U); do git checkout --ours -- "$f" && git add -- "$f"; done

  # SAFETY: no files exist only in public, and no non-export public commits are being skipped
  comm -23 <(git ls-tree -r --name-only public/main | sort) <(git ls-tree -r --name-only fork-main | sort)
  git log --oneline <merge-base>..public/main      # every entry should be an export/snapshot, not a community PR
  ```

  If a real public-only file or community commit exists, stop and merge it into the mirror properly
  instead of discarding it. Otherwise commit the reconciling merge:

  ```bash
  git commit --no-edit
  ```

Verify ancestry is now linked and the PR will be clean:

```bash
git merge-base --is-ancestor public/main fork-main && echo linked
git diff --name-only public/main...fork-main | wc -l   # three-dot (what GitHub shows)
git diff --name-only public/main    fork-main | wc -l   # two-dot (true delta) — must match
```

### 4. Review the real net diff

Review the entire net diff before naming the branch/PR — this is what you are publishing. Do not
summarize only your own recent edits.

```bash
git diff --name-status public/main fork-main
git diff --stat        public/main fork-main
```

Read enough changed files to understand the release scope, by area:

- top-level docs and config (`README.md`, `artifacts/README.md`, `config/services.json`, `.gitignore`)
- new/changed artifact READMEs and analysis scripts, grouped by category
- renamed or moved tools (e.g. `replay/`, web/AI infrastructure)
- web UI changes under `web/app/src` and helper scripts under `web/scripts` / `web/tools`
- deleted files, renames, and generated-file removals

### 5. Public-safety checks

```bash
find trace -maxdepth 2 -type f 2>/dev/null | sort
find . -maxdepth 4 \( -name '*.duckdb' -o -name '*.jsonl.gz' -o -name '*.jsonl' \)
rg -n '/m-coriander|coding_trace_refactor|serendipity-zk|coding-trace-collect|API_KEY=' . -g '!web/app/dist/**' -g '!.codex/**'
git diff --check
```

Benign matches (documentation naming environment variables, this skill referencing the scan
patterns) are fine. Internal absolute paths, private remotes, secrets, trace data, or generated
runtime data must be removed or explicitly justified before continuing. Scan the **net diff added
lines** specifically:

```bash
git diff public/main fork-main | grep -E '^\+' | rg '/m-coriander|coding_trace_refactor|serendipity-zk|API_KEY='
```

### 6. Push the mirror and open a draft PR

Push the mirror branch to the fork (its `main`, or a dedicated release branch if you prefer not to
move the mirror's `main` until merge):

```bash
git push fork fork-main:main
# or a topic branch off the mirror:
# git push fork fork-main:refresh-meaningful-topic
```

Choose a descriptive topic from the actual diff (not "public-snapshot", no dates). Open a **draft**
PR into the public `main`:

```bash
gh pr create \
  --repo uw-syfi/TraceLab \
  --base main \
  --head serendipity-zk:main \
  --draft \
  --title "Refresh artifact analyses, detail UI, and Chinese docs" \
  --body-file /tmp/tracelab-public-pr-body.md
```

Suggested PR body structure:

```markdown
## What this changes
One paragraph describing the actual release theme.

## Main updates
- Artifacts / analyses: ...
- Web UI: ...
- Tooling / docs: ...

## Public-release safety
- public/main is an ancestor of this branch; the diff is the true net change and merges cleanly.
- No trace release data files are committed to Git.
- Internal absolute paths, private remotes, and secrets were scanned.
- Release assets will be uploaded only after review and merge.
```

### 7. Keep ancestry intact on merge

How the public PR is merged decides whether the *next* release is clean:

- Prefer **"Create a merge commit"** or **"Rebase and merge"** — these keep `public/main` reachable
  from the mirror, so the next release needs no reconciliation.
- A **squash merge** re-detaches history (it creates a new commit not in the mirror). That is
  allowed, but the next release must redo the step-3 reconciling merge to re-link ancestry.

After merge, fast-forward the mirror so it tracks the merged public state:

```bash
git fetch public
git merge --ff-only public/main   # or merge --no-ff if it cannot fast-forward
git push fork fork-main:main
```

## Standard release assets

Upload both assets when creating a public data release (only after the PR is merged):

```text
trace/syfi_coding_trace.jsonl.gz
trace/syfi_coding_trace.duckdb
```

Before upload, verify:

```bash
gzip -t trace/syfi_coding_trace.jsonl.gz
sha256sum trace/syfi_coding_trace.jsonl.gz trace/syfi_coding_trace.duckdb
```

Create the release:

```bash
gh release create vYYYY-MM-DD-syfi-trace \
  trace/syfi_coding_trace.jsonl.gz \
  trace/syfi_coding_trace.duckdb \
  --repo uw-syfi/TraceLab \
  --target main \
  --title "TraceLab public trace snapshot YYYY-MM-DD" \
  --notes "Public sanitized trace release. Assets include the compressed JSONL rows and a DuckDB database."
```

If the release already exists, use `gh release upload --clobber`.

## Verification

After drafting the PR:

```bash
gh pr view --repo uw-syfi/TraceLab --web
gh pr view --repo uw-syfi/TraceLab --json mergeable,mergeStateStatus   # expect MERGEABLE / clean
git ls-remote fork refs/heads/main
```

After merge and release:

```bash
gh release view vYYYY-MM-DD-syfi-trace --repo uw-syfi/TraceLab
```

Check that release download URLs in `README.md` point to `uw-syfi/TraceLab`, and that the public
commit carries no internal remote URLs except intentional historical references.
