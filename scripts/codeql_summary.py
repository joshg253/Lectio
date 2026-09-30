"""Print a per-rule count of a CodeQL SARIF file; with RULE arguments, also list each result's file:line.

Usage: uv run scripts/codeql_summary.py .tools/codeql.sarif [RULE_ID ...]
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def main(argv: list[str]) -> int:
    with open(argv[1]) as fh:
        runs = json.load(fh)["runs"]
    results = [r for run in runs for r in run["results"]]
    counts = Counter(r["ruleId"] for r in results)
    print(f"{sum(counts.values())} result(s)")
    for rule, n in counts.most_common():
        print(f"{n:4d}  {rule}")
    for rule in argv[2:]:
        print(f"\n{rule}:")
        for r in results:
            if r["ruleId"] == rule:
                loc = r["locations"][0]["physicalLocation"]
                print(f"  {loc['artifactLocation']['uri']}:{loc['region']['startLine']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
