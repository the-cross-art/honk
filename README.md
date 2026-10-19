# Honk 🪿

**The guard goose for AI agents on Kubernetes.** A harness component that
gives any ops agent a gated, recorded, outcome-checked way to change a cluster.

AI agents are getting good at diagnosing a broken cluster. Letting them *change*
one is another matter: nobody knows whether "restart the pod" actually fixes
this kind of problem, on this cluster, or just makes it go quiet for an hour.

Honk sits between your agent and the cluster. The agent asks Honk to make a
change; Honk:

1. **Works out what is wrong on its own**, from the live cluster, never from
   what the agent says.
2. **Dry-runs the change on the apiserver** and refuses anything outside six
   fixed action types.
3. **Asks a human, showing the record**: *"restart_pod on crash_loop /
   app_error / deployment: tried 4 times here, fixed 0."*
4. **Checks whether it worked** at +3 minutes, +1 hour and +24 hours, and
   **retracts** a fix that did not hold.
5. **Lets an action run alone only once it has earned it**, per kind of problem,
   and only if your policy opts in.

```
$ # the agent called restart_pod on shop/checkout-7d9f...
Honk needs a human to approve restart_pod on Pod checkout-7d9f-x2k4p (proposal 3f2a91c0d7e4).
Problem Honk sees: crash_loop / app_error / deployment
Record for this action on this problem here: tried 4, fixed 0, failed 4
Warning: this has not fixed this kind of problem in 4 tries here
Why it is asking: policy does not allow this action to run alone.
To approve: `honk approve 3f2a91c0d7e4`   To deny: `honk deny 3f2a91c0d7e4`
```

## Quickstart (5 minutes, local)

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), and a cluster.
[kind](https://kind.sigs.k8s.io/) works.

```sh
git clone <this repo> honk && cd honk
uv sync
uv run honk init                      # creates ~/.honk with a default policy

make demo-cluster demo-apply          # optional: kind cluster with 4 broken services

claude mcp add honk -- "$PWD/.venv/bin/honk" serve   # or any MCP client; `honk init` prints this
```

Then ask your agent: *"Some services in namespace shop are broken. Investigate
and fix them using Honk."* Approve or deny in the client's prompt, or with
`honk pending` / `honk approve <id>` in a terminal. Afterwards:

```sh
honk log        # every proposal, decision, approval and outcome
honk record     # what each action has done for each kind of problem
honk pause      # stop every change, now (honk resume to undo)
```

### Make Honk the only way to change the cluster

Honk only guards changes that go through it. Give the agent **read-only**
cluster access and keep write access for Honk. With the bundled RBAC:

```sh
kubectl apply -f deploy/honk-rbac.yaml
kubectl config set-credentials agent-readonly \
  --token="$(kubectl -n honk-system create token agent-readonly --duration=8h)"
kubectl config set-context agent --cluster="$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')" \
  --user=agent-readonly
# run the agent with `kubectl --context agent`; run `honk serve` with your normal context
```

In Claude Code you can also deny `kubectl` write verbs in permissions so the
agent cannot work around Honk.

## How it works

**The problem signature.** Honk reads the target and reduces it to a few
Kubernetes enum fields: failure state (`crash_loop`, `oom_killed`,
`image_pull`, `unschedulable`, `not_ready`, ...), exit class, owner kind, and
whether its node is sick. Anything outside the known vocabulary becomes
`unknown`, so a workload cannot invent new categories or impersonate one that
has earned trust. Names, labels, logs and messages never reach a decision.

**Six actions, fixed shapes.** `restart_pod` (eviction, respects PDBs),
`rollout_restart`, `scale`, `set_resources` (cpu/memory only, at most 2x per
step by default), `cordon_node`, `uncordon_node`. The agent picks the action, the
target and a bounded parameter, never the request body. Every request carries a
`resourceVersion` precondition and is dry-run on the apiserver first.

**Outcomes, three-valued.** At +3m Honk records `healthy`, `failing` or
`unclear` (the target vanished, someone else changed it, pods still starting).
At +1h and +24h it records whether the problem came back. The result is one of
`fixed`, `provisional`, `retracted`, `failed`, `unclear`. Only `fixed` counts as
a success; `unclear` and `retracted` count against an action exactly as much as
`failed`, so being hard to verify never pays.

**Earned autonomy, opt-in.** By default Honk always asks. A team can list an
action under `autonomy:` in `~/.honk/policy.yaml`. Even then it runs alone only
for a kind of problem where its own record clears the bar (default: 8 finished
outcomes over 14+ days on 5+ distinct days, score >= 0.80, at most 30% unclear).
Node actions can never earn autonomy: Honk can confirm a node was cordoned, not
that cordoning helped.

**Guard rails that only take power away.** Denied namespaces and actions,
replica and resource limits, a per-target throttle (stops agent restart loops),
a circuit breaker that suspends autonomy after repeated failures, and a kill
switch that fails closed. The ledger (`~/.honk/honk.db`) is append-only,
enforced by triggers.

## Status

**v0.1, alpha.** Works end to end against a fake cluster in tests; the live kind
demo is the next milestone. Not yet: Slack approvals, a Helm chart for running
in-cluster, an admission policy pinning Honk's patch shapes, `rollout_undo`.
Outcome checks run while `honk serve` is up, or via `honk check --watch`; a
check that runs late uses only evidence that cannot have been lost, and
otherwise records `unclear`.

## License

Apache-2.0.
