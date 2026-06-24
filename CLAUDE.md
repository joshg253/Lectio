# Lectio

Self-hosted feed reader, triage, and workflow app with optional multi-user support.

## Core rules
- Do not change code until you are at least 95% confident about what should be built; ask follow-up questions until then.
- Be concise. Avoid filler. Do not expand beyond the task unless asked.
- For multi-file or behavior-changing work, present a short plan before editing.
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
- Prefer Haiku for mechanical, well-scoped tasks: search, simple edits, boilerplate, straightforward tests, formatting, docs, and other low-judgment work.
- Prefer Sonnet for normal implementation, refactors, tests, docs, routine debugging, and most execution after the plan is clear.
- Prefer Opus for architecture, ambiguous requirements, deep debugging, design tradeoffs, and planning multi-step changes.
- Use lower effort for mechanical or well-scoped execution; use higher effort only when the task still requires real reasoning.
- If a task starts in Haiku and needs broader codebase reasoning, switch to Sonnet.
- If a task starts in Sonnet and becomes ambiguous, architectural, or cross-cutting, stop and recommend switching to Opus for planning before continuing.
- After Opus produces a clear plan, Sonnet should usually execute it; Haiku can handle narrow follow-up edits.

## Docs
- Update `README.md` for user-visible behavior changes or feature changes.
- Update `ARCHITECTURE.md` for design rationale, layering, tenancy, or state-model changes.
- Update `Plan.md` for future work, deferred work, or intentional follow-ups.
- When changing `.env`, mirror the same keys, comments, and safe defaults in `.env.example`.
