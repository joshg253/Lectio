# Lectio Claude Instructions

- Be concise. Avoid filler. Ask before expanding beyond the task.
- Local-first, single-user RSS reader.
- Reader-first: prefer `reader` API/plugin behavior before custom code.
- Preserve the 3-layer split:
  - UI/API: routes, handlers, presentation state.
  - Services: feed ops, tagging, filtering, refresh, readability.
  - Storage: reader DB, app-data/settings.
- Use `uv` for scripts, tests, and tooling.
- Keep runtime settings env-driven; keep mutable app state in app-data paths.
- Treat remembered preferences, session overrides, and transient navigation state separately.
- Don't duplicate capabilities the `reader` library already provides.
- If a change affects user-visible behavior, update `README.md`.
- If it changes design rationale, update `ARCHITECTURE.md`.
- If it's future work, update `Plan.md`.
- When adding or changing env vars in `.env`, always mirror the change in `.env.example` (same keys, blank or safe default values, same comments).
