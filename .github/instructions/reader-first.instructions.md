---
applyTo: "**/{main.py,README.md}"
description: "Use when changing backend behavior or docs; enforce reader-first and plugin-first implementation choices."
---

# Reader-First and Plugin-First File Rules

## Decision Order
1. Use `reader` APIs if available.
2. If API support is missing, use a `reader` plugin or plugin-style extension point.
3. Add custom core app logic only as a last resort.

## Required Change Notes
- When step 3 is chosen, include a brief reason in the change summary describing why `reader` API/plugin paths were insufficient.

## Migration Safety Guidance
- During refactors toward `reader` capabilities, preserve user-visible behavior unless explicitly changing requirements.
- In this pre-production phase, schema/data reset is acceptable when it simplifies implementation.

## Documentation Requirement
- Reflect user-visible behavior changes in `README.md` in the same PR/change set.
