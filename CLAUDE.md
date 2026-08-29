# Lectio

Self-hosted feed reader, triage, and workflow app. Auth is always on; per-user isolation runs through a storage-layer tenancy resolver (see `docs/architecture/tenancy.md`).

## Core rules
- Do not change code until you are at least 95% confident about what should be built; ask follow-up questions until then.
- Be concise. Avoid filler. Do not expand beyond the task unless asked.
- Default to short, plain answers: no headers/bullets for a single fact, no restating a file's contents back, no "summary of what I did" after a small edit — the diff speaks for itself.
- Keep any "here's what I'm about to do" note to one short sentence; skip the end-of-turn recap unless the change is multi-file or non-obvious.
- For multi-file or behavior-changing work, present a short plan before editing.
- When work surfaces adjacent bugs, cleanup opportunities, or ideas beyond the task: fix true
  blockers (needed for the task to work correctly/safely) and small opportunistic fixes (same
  code path, low-risk, independently understandable, ≤~15 min); everything else is a follow-up —
  note it in `Plan.md` rather than folding it into the current change. At the end of the task,
  report what was requested, what was additionally fixed, and what was deferred.
- Prefer existing `reader` capabilities over custom code.
- Do not duplicate behavior the `reader` library already provides.
- Preserve the architecture split:
  - UI/API: routes, handlers, presentation state.
  - Services: feed operations, tagging, filtering, refresh, readability, integrations.
  - Storage: `reader` DB, app-data/settings, and tenancy-aware persistence.
- Keep runtime config env-driven; keep mutable state in app-data paths.
- Keep remembered preferences, per-user preferences, session overrides, and transient navigation state separate.
- Keep tenancy concerns behind the storage/resolver layer; do not leak tenancy-mode branching into UI/routes unless truly necessary.
- Favor workflows that reduce feed triage friction: bulk actions, fast reading flows, reliable tagging/filtering, predictable refresh behavior, and strong keyboard-first interactions.
- Prefer plugin/adapter-style extensions over hardwired branching when adding non-native behavior.
- Use `uv` for scripts, tests, and tooling.

## Model guidance
- The main session defaults to Sonnet at default effort. Talk to it directly for normal implementation, refactors, tests, docs, and routine debugging — don't ask the user to switch models for a single hard step in an otherwise normal task.
- Switching the main session's model via `/model` resends the whole conversation history to the new model and invalidates the prompt cache — expensive on a long thread, especially switching *up*. Reserve it for a durable shift in what the *rest of the session* needs (the conversation is pivoting into real architecture/design work, or a long mechanical stretch is starting fresh). State the recommendation in one line and wait; don't flag it lightly.
- For any self-contained sub-task, delegate to a subagent via the Agent tool and always pass an explicit `model` — it inherits Sonnet from the parent otherwise, which defeats the point:
  - `model: "haiku"`, low effort — mechanical, well-scoped work: search, simple edits, boilerplate, straightforward tests, formatting, docs.
  - `model: "sonnet"` (or omit) — normal implementation-sized sub-tasks that don't need the main thread's full context.
  - `model: "opus"` — architecture, ambiguous requirements, deep debugging, design tradeoffs, multi-step planning. Have it return a plan; execute that plan on the main Sonnet thread or hand it to a Sonnet/Haiku subagent, don't switch the main thread down to it.
- A subagent's context is whatever prompt you write it, not the accumulated thread — that's what makes delegation cheap regardless of the main session's model. Brief it like a colleague with no memory of this conversation (see the Agent tool's own guidance on writing prompts).

## Docs
- Update `README.md` for user-visible behavior changes or feature changes.
- Update the architecture docs for design rationale, layering, tenancy, or state-model
  changes: `ARCHITECTURE.md` is the index and holds only the cross-cutting notes;
  the rationale itself lives in `docs/architecture/` (tenancy, feeds, views, images,
  reading, saved, apis). Add to the file for the area you changed.
- Update `Plan.md` for future work, deferred work, or intentional follow-ups.
- When changing `.env`, mirror the same keys, comments, and safe defaults in `.env.example`.
