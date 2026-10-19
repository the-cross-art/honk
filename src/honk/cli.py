"""`honk`: the human side. Approve, deny, pause, and see what agents have done."""

from __future__ import annotations

import argparse
import getpass
import shlex
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from honk import policy as policy_mod
from honk.core import Honk, Refusal
from honk.ledger import Ledger

MCP_HINT = """\
Next: connect your agent. For Claude Code:

    claude mcp add honk -- {command} serve

Then give the agent read-only cluster access and let Honk hold the write access,
so every change goes through Honk. See the README for a read-only kubeconfig."""


def _honk_command() -> str:
    """The absolute path of this `honk`, so the hint works when it is not on PATH."""
    on_path = shutil.which("honk")
    here = Path(sys.argv[0]).resolve()
    if on_path and Path(on_path).resolve() == here:
        return "honk"
    return shlex.quote(str(here))


def _ledger() -> Ledger:
    return Ledger(policy_mod.home() / "honk.db")


def _honk(context: str | None = None) -> Honk:
    from honk.kube import KubeCluster

    return Honk(cluster=KubeCluster(context=context), ledger=_ledger(),
                policy=policy_mod.load())


def _who() -> str:
    return f"cli:{getpass.getuser()}"


def cmd_init(_: argparse.Namespace) -> int:
    home = policy_mod.home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "policy.yaml"
    if not path.exists():
        path.write_text(policy_mod.DEFAULT_POLICY, encoding="utf-8")
        print(f"wrote {path}")
    policy_mod.set_switch(True)
    _ledger().close()
    print(f"Honk is ready in {home} and is ON.\n")
    print(MCP_HINT.format(command=_honk_command()))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from honk.server import build

    honk = _honk(args.context)
    build(honk, approvals_in_client=honk.policy.approvals_in_client).run("stdio")
    return 0


def cmd_pending(_: argparse.Namespace) -> int:
    ledger = _ledger()
    rows = ledger.pending_approvals()
    if not rows:
        print("Nothing is waiting for approval.")
    for p in rows:
        where = f"{p.namespace}/" if p.namespace else ""
        print(f"{p.id}  {p.action} {p.kind} {where}{p.name}  [{p.sig_label}]  from {p.client}")
        if p.reason:
            print(f"      agent says: {p.reason}")
        print(f"      {p.decision_detail}")
    return 0


def _decide(args: argparse.Namespace, approve: bool) -> int:
    honk = _honk(args.context)
    try:
        result = (honk.approve if approve else honk.deny)(args.id, approver=_who(),
                                                           note=args.note)
    except Refusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(result.render())
    return 0 if (result.executed or not approve) else 1


def cmd_log(args: argparse.Namespace) -> int:
    for row in _ledger().history(limit=args.n):
        verdict = row["verdict"] or ""
        ran = "" if row["ok"] is None else ("ran" if row["ok"] else "did not run")
        status = row["status"]
        where = f"{row['namespace']}/" if row["namespace"] else ""
        print(f"{row['ts'][:19]}  {row['id']}  {row['mode']:<6} {row['action']:<16} "
              f"{row['kind']} {where}{row['name']}  {verdict} {ran} {status}".rstrip())
        print(f"      problem: {row['sig_label'] or '-'}   {row['decision_detail']}")
    return 0


def cmd_record(_: argparse.Namespace) -> int:
    cells = _ledger().cells(now=datetime.now(UTC))
    if not cells:
        print("No history yet.")
    for _digest, label, action, record in sorted(cells, key=lambda c: (c[1], c[2])):
        print(f"{label:<45} {action:<16} {record.sentence()}  score {record.score:.2f}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    honk = _honk(args.context)
    failures = 0
    while True:
        try:
            for pid, cp, result, detail in honk.run_checks():
                label = result.value if result else "error, will retry"
                print(f"{datetime.now(UTC).isoformat()[:19]}  {pid} {cp}: {label} ({detail})",
                      flush=True)
            if failures:
                print(f"checks recovered after {failures} failed ticks", flush=True)
            failures = 0
        except Exception as exc:
            failures += 1
            print(f"check tick failed ({failures} in a row), will retry: {exc!r}",
                  file=sys.stderr, flush=True)
        if not args.watch:
            return 0 if failures == 0 else 1
        time.sleep(30)


def cmd_switch(on: bool) -> int:
    policy_mod.set_switch(on)
    print("Honk is ON: approved changes will run." if on
          else "Honk is PAUSED: no change will run until `honk resume`.")
    return 0


def cmd_reset_breaker(_: argparse.Namespace) -> int:
    _ledger().reset_breaker(now=datetime.now(UTC), actor=_who())
    print("Breaker reset.")
    return 0


def cmd_advise(args: argparse.Namespace) -> int:
    print(_honk(args.context).advise(args.kind, args.name, args.namespace))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="honk", description=__doc__)
    parser.add_argument("--context", help="kubeconfig context (default: current)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create ~/.honk with a default policy and switch Honk on")
    sub.add_parser("serve", help="run the MCP server on stdio (your agent starts this)")
    sub.add_parser("pending", help="list proposals waiting for a human")
    for name in ("approve", "deny"):
        p = sub.add_parser(name, help=f"{name} a pending proposal")
        p.add_argument("id")
        p.add_argument("--note", default="")
    log = sub.add_parser("log", help="what agents asked for and what happened")
    log.add_argument("-n", type=int, default=20)
    sub.add_parser("record", help="track record per (problem, action)")
    check = sub.add_parser("check", help="run due outcome checks")
    check.add_argument("--watch", action="store_true", help="keep running every 30s")
    sub.add_parser("pause", help="stop all changes immediately")
    sub.add_parser("resume", help="allow approved changes again")
    sub.add_parser("reset-breaker", help="re-allow autonomy after repeated failures")
    advise = sub.add_parser("advise", help="what Honk sees on a target, and its history")
    advise.add_argument("kind")
    advise.add_argument("name")
    advise.add_argument("-n", "--namespace")

    args = parser.parse_args(argv)
    handlers = {
        "init": cmd_init, "serve": cmd_serve, "pending": cmd_pending,
        "approve": lambda a: _decide(a, True), "deny": lambda a: _decide(a, False),
        "log": cmd_log, "record": cmd_record, "check": cmd_check,
        "pause": lambda _: cmd_switch(False), "resume": lambda _: cmd_switch(True),
        "reset-breaker": cmd_reset_breaker, "advise": cmd_advise,
    }
    try:
        return int(handlers[args.cmd](args))
    except policy_mod.PolicyError as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
