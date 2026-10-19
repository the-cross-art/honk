# STATUS

**2026-10-20 · v0.1 alpha · `make check` green: 97 tests, ruff, strict mypy · ran live on kind · private repo the-cross-art/honk**

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
| Agent study, first run | `scripts/agent_study.py`: 18 runs (9 scenarios × bare/honk, sonnet), 0 contaminated; results and findings in `study/REPORT.md` |
| MCP server | `test_server.py` over the real protocol: no tool can approve; in-client approve, deny and dismiss on both protocol eras (legacy mid-call request, 2026-07-28 input-required round trip) with exactly one proposal row; terminal fallback for clients that cannot prompt; refused changes never prompt |

## What does not exist yet

- Reviewer sign-off on the 23 real scenarios (`reviewed_by` is empty on all).
- An agent study on more than one model, more than one run per cell, or real (non-busybox) apps.
- Slack approvals, Helm chart, admission policy pinning patch shapes, `rollout_undo`.

## Next action

1. **Decide what the study means for the product** (open decision 4). The bare
   agent never restarted anything. Its risky moves were green-but-wrong spec
   changes (probe disabled, empty ConfigMap, PVC deleted and recreated), which a
   3-minute health check labels `fixed`.
2. Re-run slow-leak with a break window long enough to OOM (it read `healthy` at 90s).
3. Repeat the study with a second model (`--model opus`, `haiku`) and 3 runs per
   cell before quoting anything.
4. A Kubernetes practitioner (from outreach) runs `scripts/review.py` on the 23
   scenarios. Until then, publish no numbers.

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
| 1 | Default approval channel: in-client prompt or terminal only | **RESOLVED 2026-10-19: `client`.** Verified in Claude Code on protocol 2026-07-28: the prompt showed the record, the author clicked decline 9s later, and Honk recorded `denied` by `claude-code (in-client prompt)`. `approvals: cli` stays for harnesses that might answer prompts themselves |
| 2 | Publish: GitHub repo, PyPI name `honkctl` | waiting on the author |
| 3 | Autonomy floor values (`policy.FLOOR`) | chosen, not derived; revisit with real data |
| 4 | Product direction after the study: keep the six-action guard, or move to reviewing and labelling *spec changes* (diff + semantic check: probe weakened, empty config, destructive delete) | open, 2026-10-20 |
| 5 | `max_resource_factor` blocked a correct typo fix (64 CPUs → 250m). Exempt request cuts that make a Pending pod schedulable, or ask instead of refusing? | open, 2026-10-20 |

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
approval binds to exactly what the human saw. Confirmed against Claude Code after a
`/mcp` reconnect (the first test silently ran the old server process).

The record changed agent behaviour: shown "restart_pod: tried 1, fixed 0,
failed 1", the agent declined to retry and named the real fix. Launch line.

**Session 1, continued: real scenarios.** Stack Overflow is blocked for the web
tools, so `scripts/stackexchange.py` uses the official Stack Exchange API
(rate-limited, cached, not committed). 23 real scenarios in `scenarios/real/`
from Stack Overflow, GitHub issues and k8s.af postmortems; 9 reproduce on kind.

Live reads found a signature bug the unit tests had missed: a container exiting
0 under restartPolicy Always read as `not_ready` mid-cycle and `crash_loop` in
back-off, so one incident split across two cells (the research repo's #41 shape
again). Fixed as `honk_sig_v2`; all 9 now read identically on 5 of 5 samples.
Lesson: every scenario's `signature_observed` must come from a live read.

Source check: Claude re-read all 23 sources in full (Stack Overflow via the API,
others by page). 5 answer keys were wrong when written from search snippets,
including Grafana (adding capacity *did* restore service) and the resize bug (the
fix is a kubelet upgrade, not recreating the pod). Snippet-grade sources are a
hypothesis, same lesson as the research repo's citations.

**2026-10-20, session 2: the agent study.** 18 headless Claude Code runs (sonnet),
9 lab scenarios, with and without Honk, all valid. Findings are in `study/REPORT.md`.
The headline cuts against the pitch: the bare agent never reached for the tempting
restart. It fixed 4 of 9 correctly, stopped correctly on 1, and made 3 green-but-wrong
spec changes, all disclosed: a probe replaced by `true`, an empty ConfigMap, and a
PVC deleted and recreated. With Honk the agent never proposed a useless action and
escalated with the right diagnosis, but Honk's 2x cap refused the correct CPU fix.

What went wrong: the pilot leaked the answer keys through the author's
`additionalDirectories` (fixed with `--setting-sources project`, disallowed file
tools, and a contamination flag). Honk MCP first inherited the agent's read-only
KUBECONFIG. The verb parser read `-n <ns>` as the verb. slow-leak's 90-second break
window is shorter than its time to OOM, so it tested nothing. The refusal message
printed a 1/256 cut as "0.00x", and the agent got that text verbatim. Now fixed.
