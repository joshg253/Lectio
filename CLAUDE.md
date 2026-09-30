# Lectio

Self-hosted feed reader, triage, and workflow app. Auth is always on; per-user isolation runs through a storage-layer tenancy resolver (see `docs/architecture/tenancy.md`).

## Core rules
- Ask before building only when a wrong guess would be expensive to undo; otherwise proceed and state the assumption.
- Be concise and stay on task: short, plain answers, no headers/bullets for a single fact, no restating file contents, at most one
  sentence of "here's what I'm about to do", and no end-of-turn recap unless the change is multi-file or non-obvious.
- For multi-file or behavior-changing work, present a short plan before editing.
- When work surfaces adjacent bugs, cleanup opportunities, or ideas beyond the task: fix true
  blockers (needed for the task to work correctly/safely) and small opportunistic fixes (same
  code path, low-risk, independently understandable, ≤~15 min); everything else is a follow-up —
  note it in `Plan.md` rather than folding it into the current change. At the end of the task,
  report what was requested, what was additionally fixed, and what was deferred.
- Prefer existing `reader` capabilities over custom code; never duplicate behavior the library already provides.
- Preserve the architecture split:
  - UI/API: routes, handlers, presentation state.
  - Services: feed operations, tagging, filtering, refresh, readability, integrations.
  - Storage: `reader` DB, app-data/settings, and tenancy-aware persistence.
- Keep runtime config env-driven; keep mutable state in app-data paths.
- Keep remembered preferences, per-user preferences, session overrides, and transient navigation state separate.
- Keep tenancy concerns behind the storage/resolver layer; do not leak tenancy-mode branching into UI/routes unless truly necessary.
- Favor low-friction triage: bulk actions, keyboard-first flows, predictable refresh.
- Prefer plugin/adapter-style extensions over hardwired branching when adding non-native behavior.
- Use `uv` for scripts, tests, and tooling.
- Line wrapping: commit messages get no line breaks (one long line per paragraph). Everything else wraps at 140 columns; `ruff format`
  handles code (see `scripts/lint_changed.py`), comments/docstrings/prose are wrapped by hand. Only wrap new or edited paragraphs.

## Model guidance
- The main session is Sonnet at medium effort (pinned in `.claude/settings.json`). Use it directly for normal implementation, refactors,
  tests, docs, and routine debugging — don't ask the user to switch models for a single hard step in an otherwise normal task.
- Switching the main session's model via `/model` resends the whole conversation history and invalidates the prompt cache — expensive on
  a long thread. Reserve it for a durable shift in what the *rest of the session* needs; state the recommendation in one line and wait.
- Subagents start cold, so delegating a small task costs more than doing it inline. Delegate only when it keeps bulk reading out of the
  main context (broad searches, log trawls, bulk mechanical edits) or the sub-task needs more reasoning than the main thread has.
- Always pass an explicit `model` to the Agent tool:
  - `haiku` — search and mechanical, well-scoped work: simple edits, boilerplate, straightforward tests, formatting.
  - `sonnet` — implementation-sized sub-tasks that don't need the main thread's full context.
  - `opus` — anything gnarly: architecture, ambiguous requirements, deep debugging, design tradeoffs. Have it return a plan or diagnosis;
    execute on the main thread or hand it to a Sonnet/Haiku subagent.
  - Never `fable` unless the user asks for it.
- Brief a subagent like a colleague with no memory of this conversation — its context is only the prompt you write.

## Docs
- Update `README.md` for user-visible behavior changes or feature changes.
- Update the architecture docs for design rationale, layering, tenancy, or state-model
  changes: `ARCHITECTURE.md` is the index and holds only the cross-cutting notes;
  the rationale itself lives in `docs/architecture/` (tenancy, feeds, views, images,
  reading, saved, apis). Add to the file for the area you changed.
- Update `Plan.md` for future work, deferred work, or intentional follow-ups.
- When changing `.env`, mirror the same keys, comments, and safe defaults in `.env.example`.
