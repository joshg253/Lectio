"""Fail only on ruff/ty findings that sit on lines this change actually touched.

Whole-repo linting is not adoptable here: main.py alone carries ~120 existing
findings and is touched by nearly every change, so a "changed files" gate would
be red forever and get ignored — which is how a duplicate method definition sat
in services/lead_images.py unnoticed. Gating the *lines* you wrote is green on
day one by construction, and still means nothing new joins the pile. The pile
shrinks whenever someone cleans a file they are already editing.

Both repo-wide backlogs happen to be at zero as of 2026-08-24, but that is not
why this stays line-scoped — ty is a preview tool, and a version bump already
changed its diagnostic count once with no code change (see Plan.md "Code
health"). A whole-repo gate would go red on an unrelated PR the next time that
happens; the whole-repo `ty check .` / `ruff check .` CI steps are informational
for exactly that reason.

Used by both .githooks/pre-commit (staged changes) and the CI lint job (the
whole PR), so a commit that passed locally cannot fail in CI.

Usage:
    uv run scripts/lint_changed.py              # staged changes (the hook)
    uv run scripts/lint_changed.py --base <sha> # everything since <sha> (CI)

`ruff format` is deliberately not run: this repo has never been formatted, so
enabling it would mean reformatting every file at once. Nothing here prevents
adopting it later, file by file.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def changed_lines(base: str | None) -> dict[str, set[int]]:
    """{path: {line numbers added or modified}} for Python files."""
    diff_args = ["git", "diff", "-U0", "--diff-filter=ACM"]
    diff_args += [f"{base}...HEAD"] if base else ["--cached"]
    diff_args += ["--", "*.py"]

    out: dict[str, set[int]] = defaultdict(set)
    path: str | None = None
    for line in _run(diff_args).splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            out[path].update(range(start, start + count))
    return dict(out)


def _ruff_hits(touched: dict[str, set[int]]) -> list[str]:
    raw = _run(["ruff", "check", "--output-format=json", "--", *sorted(touched)])
    try:
        findings = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        print("lint: could not parse ruff output; treating as pass", file=sys.stderr)
        return []

    # ruff reports absolute paths while git reports repo-relative ones; without
    # normalising, nothing ever matches and the gate silently passes everything
    # — a check that cannot fail is worse than no check.
    root = _run(["git", "rev-parse", "--show-toplevel"]).strip()
    hits = []
    for f in findings:
        name = f.get("filename", "")
        try:
            rel = str(Path(name).resolve().relative_to(Path(root).resolve()))
        except (ValueError, OSError):
            rel = name.removeprefix("./")
        row = f.get("location", {}).get("row")
        if row in touched.get(rel, set()):
            loc = f.get("location", {})
            hits.append(f"{f.get('filename')}:{loc.get('row')}:{loc.get('column')}: "
                        f"[ruff] {f.get('code')} {f.get('message')}")
    return hits


def _ty_hits(touched: dict[str, set[int]]) -> list[str]:
    # gitlab format gives repo-relative paths (ty run from repo root against
    # repo-relative args) and real line numbers in one JSON shot — unlike
    # ruff, ty needs no path normalising here.
    raw = _run(["ty", "check", "--output-format=gitlab", "--", *sorted(touched)])
    try:
        findings = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        print("lint: could not parse ty output; treating as pass", file=sys.stderr)
        return []

    hits = []
    for f in findings:
        loc = f.get("location", {})
        rel = loc.get("path", "")
        row = loc.get("positions", {}).get("begin", {}).get("line")
        col = loc.get("positions", {}).get("begin", {}).get("column")
        if row in touched.get(rel, set()):
            # description already leads with "<check_name>: ", so print it alone.
            hits.append(f"{rel}:{row}:{col}: [ty] {f.get('description')}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint only the lines this change touches.")
    ap.add_argument("--base", default=None, help="base ref to diff against (default: staged changes)")
    args = ap.parse_args()

    touched = changed_lines(args.base)
    if not touched:
        print("lint: no Python lines changed")
        return 0

    hits = _ruff_hits(touched) + _ty_hits(touched)
    if not hits:
        print(f"lint: {len(touched)} file(s) checked, nothing new on the lines you changed")
        return 0

    for h in hits:
        print(h)
    print(f"\nlint: {len(hits)} finding(s) on lines this change touches.")
    print("  fix (ruff): uv run ruff check --fix <file>")
    print("  fix (ty):   uv run ty check <file>  # then edit by hand")
    print("  bypass:     git commit --no-verify")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
