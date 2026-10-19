# Working agreement

Read `STATUS.md` first. It is short on purpose: state, what is next, open
decisions. Keep it under 150 lines; history belongs in git.

## Rules

- `make check` (ruff, strict mypy, pytest) before every commit.
- **Agent text never decides.** The agent chooses an action type, a target and a
  bounded parameter. Signature, record and verdict come from the cluster and the
  ledger. `Proposal.reason` is stored and shown to the human, and must never
  feed a branch, a signature or a gate input.
- **Every rule takes power away** except opt-in `autonomy`, and autonomy is only
  ever granted per cell by its own verified record.
- **Errors go pessimistic.** When unsure, an outcome is `unclear`, never
  `fixed`. A check that cannot run must be visible, never swallowed.
- **Test properties, not cases.** Sweep the input space where it is small
  (`test_auto_requires_every_bar`, the fold sweep). Test the fix, not just the
  bug.
- `signature.py` stays stdlib-only. Bump `SIG_VERSION` on any vocabulary change.
- No absolute paths in the repo. State lives in `$HONK_HOME` (default `~/.honk`).
