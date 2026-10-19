# STATUS

**2026-10-19 · v0.1 alpha · `make check` green: 66 tests, ruff, strict mypy · ran live on kind · not yet committed, not pushed**

## What works, and how it was verified

| Thing | Verified by |
|---|---|
| Problem signature, stable across a crash-loop cycle | `test_signature.py`: waiting / terminated / just-restarted reads give one cell; 1000 forged reasons collapse to one |
| Owner chain verified by UID | ported from the research repo; forged owner refused in `test_core.py` |
| Six actions, fixed request shapes, preconditions on every body | `test_actions_ledger.py`: every built request passes its own recheck; smuggled fields refused |
| Append-only ledger | every table refuses UPDATE and DELETE (parametrised test) |
| Outcome labels, retraction | fold sweep: `fixed` only for healthy at +3m and held at +1h and +24h |
| Ask / auto gate | 576-combination sweep: AUTO only when opted in, breaker closed, every bar clears |
| Propose, approve, execute, check | `test_core.py` against a fake cluster: refusals never mutate, stale approval refused, throttle stops loops, autonomy earned per cell |
| **Live, on a kind cluster** | agent over real stdio MCP, human approving in a terminal: 5 dry runs accepted by the apiserver; refusals for kube-system and a 64x memory jump; 3-minute labels correct for all 4 (`checkout`/`cart`/`images` failing, `search` healthy); `set_resources` 128Mi fixed `cart` (0 restarts). The +1h retraction for `search` was pending at session end |
| MCP server | `test_server.py` over the real protocol: no tool can approve; in-client approve, deny and dismiss on both protocol eras (legacy mid-call request, 2026-07-28 input-required round trip) with exactly one proposal row; terminal fallback for clients that cannot prompt; refused changes never prompt |

## What does not exist yet

- Mined real-world scenarios (only 4 demo ones in `scenarios/demo/`).
- Slack approvals, Helm chart, admission policy pinning patch shapes, `rollout_undo`.

## Next action

1. Check the smoke run's +1h check: `search` restart should read `retracted`
   (`HONK_HOME=<scratch>/honk-smoke honk log`). If it says `unclear`, find out why.
2. The author's own run in Claude Code, namespace `store` (`make demo-apply NS=store`).
3. Ask Claude Code to fix namespace `store`. Expect: `checkout` restart fails, `cart`
   restart fails and `set_resources` fixes it, `search` restart retracted at +1h,
   `images` nothing helps. Fix whatever breaks; record surprises below.
4. Mine about 20 real scenarios into `scenarios/` (format in `scenarios/README.md`).

## Roadmap ideas (not started)

- **The fixes agents actually need are often spec changes.** In the author's first
  run, Claude Code diagnosed `checkout` (missing env var) and `images` (bad tag)
  correctly and found no Honk action for either. Options: `rollout_undo`, a
  bounded `set_image` / `set_env`, or "propose a PR" for GitOps clusters. Decide
  with design partners; this is the biggest coverage gap.

- **Prediction scoring.** Let the agent, or a small decision model such as Laya,
  attach "chance this fixes it" to a proposal. Show it next to the record and
  score it against the verified outcome (calibration per model). Advisory only:
  it never feeds the gate. This makes Honk the place that measures whether an
  agent's confidence can be trusted.
- **Positioning:** Honk is a harness component (permissions + feedback loop) for
  ops agents. Say "harness" in the README and launch post.

## Open decisions

| # | Decision | Status |
|---|---|---|
| 1 | Default approval channel: in-client prompt or terminal only | `client` for now. A harness that auto-answers prompts would make approvals non-human; the approver column records the channel, and `approvals: cli` exists |
| 2 | Publish: GitHub repo, PyPI name `honkctl` | waiting on the author |
| 3 | Autonomy floor values (`policy.FLOOR`) | chosen, not derived; revisit with real data |

## Session log

**2026-10-19, session 1.** Pivoted from the YC-C research repo to Honk, a trust
layer between any agent and the cluster. Ported ownership, the closed vocabulary,
the scoring maths, the kill switch, throttle and preconditions; rewrote the
signature for point reads (collapsing waiting/terminated, the research repo's open
decision #34); SQLite ledger with triggers.

What went wrong: a `slots=True` dataclass default read as a descriptor (15 tests
failed); a stale uv build cache hid the package. The test suite caught a real
hole in my request recheck: an empty `taints: []` produced no path and passed
the allowlist, and in a node patch it would delete every taint. MCP SDK 2.x's
default connection has no back-channel for prompts, so approval falls back to the
terminal. The kube client defaults patches to JSON-patch, so every patch now
forces strategic-merge. Live run found one more: an empty pod name reached the
apiserver as a LIST, and Honk read the PodList as an ownerless pod. Names are now
validated against Kubernetes' grammar and a read must return the object named.
The author's first real setup failed: `honk init` printed `claude mcp add honk --
honk serve`, and `honk` is not on PATH. The hint now prints the absolute path.
The agent in that session did the right thing: diagnosed all four services
read-only and refused to work around the missing tools with raw kubectl.

In-client approvals did not appear in Claude Code because it negotiated protocol
`2026-07-28` (seen in its MCP log), where a server cannot push a prompt mid-call.
Rebuilt the approval on the SDK's resolver pattern: a read-only `preview` builds
the question, and the SDK reuses an answer only for the identical question, so an
approval binds to exactly what the human saw. Not yet confirmed against Claude
Code itself: it depends on whether it declares the elicitation capability.
