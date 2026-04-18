# Lectio Copilot Instructions

## Priorities
- Local-first single-user RSS reader
- Deployment-aware for VPS later  
- 3-layer split: UI/API → Services → Storage

## Docs
- `README.md`: user docs/setup/usage
- `Plan.md`: backlog/future features  
- `ARCHITECTURE.md`: design rationale
- "add to list later" → `Plan.md`
- "document design" → `ARCHITECTURE.md`

## Rules
**Reader-first**: `reader` API > plugin > custom (justify custom)  
**Plugin-first**: extensions over hardwired logic  
**Tooling**: `uv` + `ty` (justify alternatives)  
**Data**: config/env-driven, app-data path  
**View state**: persist preferences, temporary contextual overrides, separate transient nav

## Future
- No-login now → basic auth before VPS
- Reverse-proxy ready
- Keep simple, avoid auth rewrites later

## Gates
- User-visible → update `README.md` same commit

## UX
- Prioritize 3-pane desktop and 1-pane mobile development in near tandem
- Treat 2-pane medium-width as later refinement
- Do not regress mobile into stacked/squished multi-pane layouts
- Test: Win11(Vivaldi/VSCode), Surface Pro6(Firefox touch), S21+
- Iconography consistency: prefer Material Symbols Rounded for UI icons/glyph actions when practical; avoid mixing disparate icon styles unless there is a clear UX reason

## Change summaries note:
layer(s), reader/custom, state type, docs updated
