"""Summarise an agent-study run as a markdown table (latest run by default).

    python scripts/study_report.py [results/<stamp>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "study" / "results"


def main() -> None:
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else sorted(RESULTS.iterdir())[-1]
    rows = [json.loads(p.read_text()) for p in sorted(run.glob("*--*.json"))
            if not p.name.endswith(".events.jsonl")]
    by = {(r["scenario"], r["arm"]): r for r in rows}
    print(f"# Agent study {run.name}\n")
    print("| scenario | Honk sees | bare: what the agent changed | bare: after 3 min | "
          "with Honk: what it called | answer key: does the tempting fix help? |")
    print("|---|---|---|---|---|---|")
    for scenario in sorted({r["scenario"] for r in rows}):
        bare, honk = by.get((scenario, "bare")), by.get((scenario, "honk"))
        key = yaml.safe_load((REPO / "scenarios" / "real" / f"{scenario}.yaml").read_text())
        def cell(r: dict | None, field: str) -> str:
            if r is None:
                return "-"
            if not r["valid"]:
                return "INVALID"
            value = r[field]
            if isinstance(value, list):
                if field == "honk_calls":
                    value = [c["tool"] for c in value]
                return ", ".join(value) or "nothing"
            return str(value or "-")
        before = (bare or honk or {}).get("problem_before", "-")
        print(f"| {scenario} | {before} | {cell(bare, 'kubectl_mutations')} | "
              f"{cell(bare, 'problem_after')} | {cell(honk, 'honk_calls')} | "
              f"{key['tempting_action']}: {key['helps']} |")
    invalid = [f"{r['scenario']}/{r['arm']}" for r in rows if not r["valid"]]
    names = ": " + ", ".join(invalid) if invalid else ""
    print(f"\n{len(rows)} runs, {len(invalid)} invalid{names}")


if __name__ == "__main__":
    main()
