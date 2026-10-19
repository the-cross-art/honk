"""For a Kubernetes practitioner: review the scenarios one at a time, answer y/n.

    python scripts/review.py           # review everything not yet reviewed
    python scripts/review.py --all     # go through all of them again

Your answers are saved into each scenario file as you go, so you can stop
(Ctrl-C) and continue later.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "scenarios" / "real"

HELPS = {
    True: "it DOES fix the problem",
    False: "it does NOT fix the problem",
    "temporarily": "it helps only for a while, then the problem comes back",
    "partly": "it helps a little, but is not the real fix",
}


def ask(question: str, choices: str) -> str:
    while True:
        answer = input(f"{question} [{choices}] ").strip().lower()
        if answer in choices.split("/"):
            return answer
        print(f"  please type one of: {choices}")


def review(path: Path, reviewer: str, n: int, total: int) -> None:
    s = yaml.safe_load(path.read_text())
    src = s["source"]
    print("\n" + "=" * 72)
    print(f"Scenario {n} of {total}: {s['title']}")
    print("=" * 72)
    print(f"\nSource ({src['kind']}): {src['url']}")
    if src.get("also"):
        print(f"Also see:          {src['also']}")
    print(f"\nWhat an AI agent would probably try:  {s['tempting_action']}")
    print(f"What we wrote down:                   {HELPS[s['helps']]}")
    print(f"The real fix, we think:               {s['real_fix']}")
    if check := s.get("source_check"):
        print(f"Already checked by {check['by']} ({check['method']}): {check['verdict']}")
    print()

    if ask("Open the source in your browser?", "y/n") == "y":
        subprocess.run(["open", src["url"]], check=False)
        input("Read it, then press Enter to answer... ")

    helps_ok = ask(f"Is it right that {s['tempting_action']}: {HELPS[s['helps']]}?",
                   "y/n/unsure")
    fix_ok = ask("Is the real fix right (roughly what the source says)?", "y/n/unsure")
    note = ""
    if "n" in (helps_ok, fix_ok) or "unsure" in (helps_ok, fix_ok):
        note = input("What's wrong or unclear? (one line, Enter to skip) ").strip()

    s["reviewed_by"] = reviewer
    s["review"] = {"helps_ok": helps_ok, "fix_ok": fix_ok, "note": note,
                   "date": date.today().isoformat()}
    path.write_text(yaml.safe_dump(s, sort_keys=False, width=100))
    print("  saved.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="include already-reviewed ones")
    args = parser.parse_args()
    files = sorted(ROOT.glob("*.yaml"))
    todo = [f for f in files if args.all or not yaml.safe_load(f.read_text())["reviewed_by"]]
    if not todo:
        print("Everything is reviewed. Use --all to go through them again.")
        return
    reviewer = input(f"Your name for the record [{getpass.getuser()}]: ").strip() or \
        getpass.getuser()
    print(f"\n{len(todo)} to review. Ctrl-C any time; your answers so far are saved.")
    try:
        for i, path in enumerate(todo, 1):
            review(path, reviewer, i, len(todo))
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Run it again to continue where you left off.")
        sys.exit(0)
    print("\nAll done. Tell Claude, and it will read your answers and fix any 'n'.")


if __name__ == "__main__":
    main()
