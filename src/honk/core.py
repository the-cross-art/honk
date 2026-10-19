"""Honk's decision path: propose -> (approve) -> execute -> check.

    propose   resolve and sign the target from the live cluster, apply policy,
              dry-run on the apiserver, look up the cell's record, decide
              REFUSE / ASK / AUTO, and write it all down.
    approve   a human's verdict. Execution re-reads the target first and
              refuses if its signature moved since the human looked.
    checks    label each executed action at t+3m, t+1h and t+24h.

Nothing the agent says reaches a decision. The agent picks an action type, a
target and a bounded parameter; the signature, the record and the verdict are
all computed by Honk from the cluster and the ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from honk import gate
from honk.actions import (
    CATALOG,
    WORKLOAD_KINDS,
    ActionType,
    Proposal,
    Request,
    build_request,
    check_resources,
    check_scale,
    check_target_kind,
    container_resources,
    recheck,
)
from honk.extract import failing_conditions, last_restart_at, owner_refs, parse_ts, pod_facts
from honk.extract import restart_total as pod_restarts
from honk.gate import Mode, TrackRecord
from honk.kube import Cluster
from honk.ledger import DueCheck, Ledger
from honk.outcomes import (
    Checkpoint,
    Observation,
    PodObs,
    Result,
    evaluate_recurrence,
    evaluate_window,
)
from honk.ownership import ChainStatus, OwnerResolver, resolve_chain
from honk.policy import Policy, execution_enabled
from honk.signature import OK_STATES, FailureState, Signature, failure_state, sign, sign_node, worst

RESTARTABLE_OWNERS: Final[frozenset[str]] = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}
)
MAX_REASON_CHARS: Final[int] = 500


@dataclass(frozen=True, slots=True)
class Subject:
    """What Honk acts on and what it judges the outcome by."""

    obj: Mapping[str, Any]
    kind: str
    name: str
    uid: str
    signature: Signature
    pods: int = 0


@dataclass(frozen=True, slots=True)
class Assessment:
    proposal_id: str | None
    mode: Mode
    summary: str
    problem: str | None = None
    record: TrackRecord | None = None
    warning: str | None = None
    executed: bool = False
    exec_detail: str = ""
    lines: Sequence[str] = field(default_factory=tuple)

    def render(self) -> str:
        out = [self.summary]
        if self.problem:
            out.append(f"Problem Honk sees: {self.problem}")
        if self.record is not None:
            out.append(f"Record for this action on this problem here: {self.record.sentence()}")
        if self.warning:
            out.append(f"Warning: {self.warning}")
        out.extend(self.lines)
        return "\n".join(out)


class Refusal(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Evaluation:
    proposal: Proposal
    subject: Subject | None
    request: Request | None
    decision: gate.Decision | None
    record: TrackRecord | None
    warning: str | None
    refusal: str | None


def _meta(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    return obj.get("metadata") or {}


def _clean_reason(text: str) -> str:
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat[:MAX_REASON_CHARS]


class Honk:
    def __init__(
        self, *, cluster: Cluster, ledger: Ledger, policy: Policy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        switch: Callable[[], tuple[bool, str]] = execution_enabled,
    ) -> None:
        self.cluster = cluster
        self.ledger = ledger
        self.policy = policy
        self.clock = clock
        self.switch = switch

    # --- reading the target -----------------------------------------------------

    def _node_sick(self, node_name: Any, cache: dict[str, bool]) -> bool:
        if not isinstance(node_name, str) or not node_name:
            return False
        if node_name not in cache:
            node = self.cluster.read("Node", node_name, None)
            cache[node_name] = bool(node is not None and failing_conditions(node))
        return cache[node_name]

    def _workload_pods(
        self, namespace: str, uid: str, resolver: OwnerResolver
    ) -> list[Mapping[str, Any]]:
        pods = []
        for pod in self.cluster.list_pods(namespace):
            chain = resolve_chain(
                pod_owner_refs=owner_refs(pod), namespace=namespace, resolver=resolver)
            target = chain.act_target
            if target is not None and target.uid == uid:
                pods.append(pod)
        return pods

    def resolve(self, p: Proposal) -> Subject:
        now = self.clock()
        resolver = self.cluster.resolver()
        nodes: dict[str, bool] = {}
        obj = self.cluster.read(p.kind, p.name, p.namespace)
        if obj is None:
            raise Refusal(f"{p.kind} {p.namespace + '/' if p.namespace else ''}{p.name} "
                          "was not found (or Honk is not allowed to read it)")
        uid = str(_meta(obj).get("uid") or "")

        if p.kind == "Node":
            return Subject(obj, "Node", p.name, uid, sign_node(failing_conditions(obj)))

        assert p.namespace is not None
        if p.kind == "Pod":
            chain = resolve_chain(
                pod_owner_refs=owner_refs(obj), namespace=p.namespace, resolver=resolver)
            if chain.status is ChainStatus.BARE:
                raise Refusal("this pod has no controller, so evicting it would delete it "
                              "with nothing to recreate it")
            target = chain.act_target
            if target is None:
                raise Refusal(f"this pod's owner could not be verified ({chain.reason}); "
                              "Honk will not act on an unverified ownership claim")
            if target.kind not in RESTARTABLE_OWNERS:
                raise Refusal(f"this pod belongs to a {target.kind}; restart_pod only "
                              "handles pods of Deployments, StatefulSets and DaemonSets")
            state = failure_state(pod_facts(obj, now=now))
            sig = sign(owner_kind=target.kind, state=state,
                       node_unhealthy=self._node_sick((obj.get("spec") or {}).get("nodeName"),
                                                      nodes))
            owner = self.cluster.read(target.kind, target.name, p.namespace) or {}
            return Subject(owner, target.kind, target.name, target.uid, sig, 1)

        pods = self._workload_pods(p.namespace, uid, resolver)
        states = [failure_state(pod_facts(pod, now=now)) for pod in pods]
        sick = any(self._node_sick((pod.get("spec") or {}).get("nodeName"), nodes)
                   for pod in pods)
        sig = sign(owner_kind=p.kind, state=worst(states), node_unhealthy=sick)
        return Subject(obj, p.kind, p.name, uid, sig, len(pods))

    def _check_params(self, p: Proposal, subject: Subject) -> None:
        limits = self.policy.limits
        if p.action is ActionType.SCALE:
            current = int((subject.obj.get("spec") or {}).get("replicas") or 0)
            if (why := check_scale(p.params.get("replicas"), current, limits)) is not None:
                raise Refusal(why)
        elif p.action is ActionType.SET_RESOURCES:
            container = p.params.get("container")
            resources = container_resources(subject.obj, str(container))
            if resources is None:
                raise Refusal(f"{p.kind} {p.name} has no container named {container!r}")
            if (why := check_resources(p.params.get("resources"), resources, limits)) is not None:
                raise Refusal(why)
        elif p.action in (ActionType.CORDON_NODE, ActionType.UNCORDON_NODE):
            unschedulable = bool((subject.obj.get("spec") or {}).get("unschedulable"))
            if unschedulable == (p.action is ActionType.CORDON_NODE):
                state = "already cordoned" if unschedulable else "not cordoned"
                raise Refusal(f"node {p.name} is {state}")

    def _request(self, p: Proposal) -> tuple[Subject, Request]:
        """Resolve, sign and build the exact call. Raises Refusal."""
        if (why := check_target_kind(p)) is not None:
            raise Refusal(why)
        if p.namespace in self.policy.deny_namespaces:
            raise Refusal(f"policy denies all actions in namespace {p.namespace}")
        if p.action in self.policy.deny_actions:
            raise Refusal(f"policy denies {p.action}")
        subject = self.resolve(p)
        self._check_params(p, subject)
        target = subject.obj if p.kind != "Pod" else (self.cluster.read(
            "Pod", p.name, p.namespace) or {})
        request = build_request(
            p, target_uid=str(_meta(target).get("uid") or ""),
            resource_version=str(_meta(target).get("resourceVersion") or ""), now=self.clock())
        if (why := recheck(p.action, request)) is not None:
            raise Refusal(why)
        return subject, request

    # --- propose ------------------------------------------------------------------

    def _evaluate(self, p: Proposal) -> _Evaluation:
        """Everything a decision needs, with no ledger writes (a dry run is not a write)."""
        now = self.clock()
        p = Proposal(p.action, p.kind, p.name, p.namespace, dict(p.params),
                     _clean_reason(p.reason), p.client)
        subject: Subject | None = None
        try:
            subject, request = self._request(p)
            enabled, why = self.switch()
            if not enabled:
                raise Refusal(why)
            recent = self.ledger.recent_executions(
                subject.uid, p.action, since=now - timedelta(hours=1))
            if recent >= self.policy.throttle_per_hour:
                raise Refusal(
                    f"{p.action} already ran {recent} times on {subject.kind} {subject.name} in "
                    "the last hour; repeating it is unlikely to help, so a human should look")
            ok, detail = self.cluster.send(request, persist=False)
            if not ok:
                raise Refusal(f"the apiserver rejected a dry run: {detail}")
        except Refusal as refusal:
            return _Evaluation(p, subject, None, None, None, None, str(refusal))

        sig = subject.signature
        record = self.ledger.track_record(sig.digest, p.action, now=now)
        decision = gate.decide(record, rule=self.policy.autonomy.get(p.action),
                               breaker_open=self.ledger.breaker_open(now=now))
        warn = gate.warning(record)
        if FailureState(sig.failure_state) in OK_STATES:
            warn = "Honk sees nothing wrong with this target right now"
        return _Evaluation(p, subject, request, decision, record, warn, None)

    def preview(self, p: Proposal) -> Assessment:
        """What `propose` would decide, recording nothing. Deterministic for unchanged
        inputs, so the same question rendered twice is the same text."""
        ev = self._evaluate(p)
        if ev.refusal is not None or ev.decision is None:
            return Assessment(None, Mode.REFUSE, f"Honk would refuse {p.action}: {ev.refusal}")
        return Assessment(
            None, ev.decision.mode,
            f"An agent wants to run {p.action} on {p.kind} "
            f"{p.namespace + '/' if p.namespace else ''}{p.name}.",
            problem=ev.subject.signature.label if ev.subject else None, record=ev.record,
            warning=ev.warning,
            lines=(f"Agent's reason (unverified): {ev.proposal.reason or '-'}",
                   f"Why Honk is asking: {ev.decision.detail}."))

    def propose(self, p: Proposal) -> Assessment:
        ev = self._evaluate(p)
        p, subject = ev.proposal, ev.subject
        if ev.refusal is not None or ev.decision is None or subject is None:
            pid = self._record(p, subject, None, Mode.REFUSE, "refused", str(ev.refusal), None)
            return Assessment(pid, Mode.REFUSE, f"Honk refused {p.action}: {ev.refusal}",
                              problem=subject.signature.label if subject else None)

        sig, decision, record, warn = subject.signature, ev.decision, ev.record, ev.warning
        pid = self._record(p, subject, ev.request, decision.mode, decision.reason,
                           decision.detail, record)
        if decision.mode is Mode.AUTO:
            executed, detail = self._execute(pid, mode="auto")
            return Assessment(
                pid, Mode.AUTO,
                (f"Honk ran {p.action} on {p.kind} {p.name} without asking ({decision.detail})."
                 if executed else f"Honk did not run {p.action}: {detail}"),
                problem=sig.label, record=record, warning=warn, executed=executed,
                exec_detail=detail, lines=self._followup(executed))
        return Assessment(
            pid, Mode.ASK,
            f"Honk needs a human to approve {p.action} on {p.kind} {p.name} "
            f"(proposal {pid}).",
            problem=sig.label, record=record, warning=warn,
            lines=(f"Why it is asking: {decision.detail}.",
                   f"To approve: `honk approve {pid}`   To deny: `honk deny {pid}`"))

    def _record(self, p: Proposal, subject: Subject | None, request: Request | None, mode: Mode,
                reason: str, detail: str, record: TrackRecord | None) -> str:
        sig = subject.signature if subject else None
        return self.ledger.record_proposal(
            now=self.clock(), client=p.client, action=p.action, kind=p.kind,
            namespace=p.namespace, name=p.name, target_uid=subject.uid if subject else None,
            subject_kind=subject.kind if subject else None,
            subject_name=subject.name if subject else None, params=p.params, reason=p.reason,
            signature=sig.fields() if sig else None, sig_digest=sig.digest if sig else None,
            sig_label=sig.label if sig else None, mode=mode.value, decision_reason=reason,
            decision_detail=detail,
            record_shown=_record_dict(record) if record else None,
            request={"verb": request.verb, "kind": request.kind, "name": request.name,
                     "namespace": request.namespace, "subresource": request.subresource,
                     "body": dict(request.body)} if request else None)

    @staticmethod
    def _followup(executed: bool) -> tuple[str, ...]:
        if not executed:
            return ()
        return ("Honk will check whether it worked at +3 minutes, +1 hour and +24 hours.",)

    # --- approve / deny / execute ---------------------------------------------------

    def approve(self, pid: str, *, approver: str, note: str = "") -> Assessment:
        p = self._pending(pid)
        self.ledger.record_approval(pid, now=self.clock(), verdict="approved",
                                    approver=approver, note=note)
        executed, detail = self._execute(pid, mode="approved")
        summary = (f"Approved and ran {p.action} on {p.kind} {p.name}." if executed
                   else f"Approved, but {p.action} did not run: {detail}")
        return Assessment(pid, Mode.ASK, summary, executed=executed, exec_detail=detail,
                          lines=self._followup(executed))

    def deny(self, pid: str, *, approver: str, note: str = "") -> Assessment:
        p = self._pending(pid)
        self.ledger.record_approval(pid, now=self.clock(), verdict="denied", approver=approver,
                                    note=note)
        return Assessment(pid, Mode.ASK, f"Denied {p.action} on {p.kind} {p.name}.")

    def _pending(self, pid: str) -> Any:
        p = self.ledger.proposal(pid)
        if p is None:
            raise Refusal(f"no proposal {pid}")
        if p.mode != Mode.ASK.value or p.request is None:
            raise Refusal(f"proposal {pid} is not waiting for approval")
        if self.ledger.approval(pid) is not None or self.ledger.executed(pid):
            raise Refusal(f"proposal {pid} was already decided")
        return p

    def _execute(self, pid: str, *, mode: str) -> tuple[bool, str]:
        """Re-read and re-sign the target; act only if it is still what was decided on."""
        row = self.ledger.proposal(pid)
        assert row is not None
        p = Proposal(row.action, row.kind, row.name, row.namespace, row.params, row.reason,
                     row.client)
        now = self.clock()
        try:
            enabled, why = self.switch()
            if not enabled:
                raise Refusal(why)
            subject, request = self._request(p)
            if subject.signature.digest != row.sig_digest:
                raise Refusal(f"the target changed since this was proposed (it was "
                              f"{row.sig_label}, now {subject.signature.label}); propose again")
            if row.kind == "Pod" and request.body.get("deleteOptions", {}).get(
                    "preconditions", {}).get("uid") != (row.request or {}).get("body", {}).get(
                    "deleteOptions", {}).get("preconditions", {}).get("uid"):
                raise Refusal("the pod was already replaced; propose again for the new pod")
            ok, detail = self.cluster.send(request, persist=True)
        except Refusal as refusal:
            ok, detail = False, str(refusal)
        generation = None
        if ok and row.subject_kind and row.subject_kind != "Node":
            after = self.cluster.read(row.subject_kind, row.subject_name or "", row.namespace)
            gen = _meta(after or {}).get("generation")
            generation = gen if isinstance(gen, int) else None
        self.ledger.record_execution(pid, now=now, mode=mode, ok=ok, detail=detail,
                                     generation=generation)
        return ok, detail

    # --- checks ---------------------------------------------------------------------

    def observe(self, due: DueCheck) -> Observation:
        p = due.proposal
        kind, name = p.subject_kind or p.kind, p.subject_name or p.name
        obj = self.cluster.read(kind, name, p.namespace)
        if obj is None or str(_meta(obj).get("uid")) != p.target_uid:
            return Observation(target_exists=False)
        if kind == "Node":
            cordoned = bool((obj.get("spec") or {}).get("unschedulable"))
            return Observation(True, effect_ok=cordoned == (p.action is ActionType.CORDON_NODE))
        gen = _meta(obj).get("generation")
        changed = (isinstance(gen, int) and due.generation is not None
                   and gen > due.generation)
        assert p.namespace is not None and p.target_uid is not None
        now = self.clock()
        pods = [
            PodObs(state=failure_state(pod_facts(pod, now=now))[0],
                   created_at=parse_ts(_meta(pod).get("creationTimestamp")),
                   restarts=pod_restarts(pod), last_restart_at=last_restart_at(pod))
            for pod in self._workload_pods(p.namespace, p.target_uid, self.cluster.resolver())
        ]
        return Observation(True, pods=pods, changed_by_others=changed)

    def run_checks(self) -> list[tuple[str, Checkpoint, Result | None, str]]:
        """Run every due check. A check that cannot run is reported and retried next
        time, never dropped and never allowed to stop the others."""
        done: list[tuple[str, Checkpoint, Result | None, str]] = []
        now = self.clock()
        for due in self.ledger.due_checks(now=now):
            try:
                obs = self.observe(due)
            except Exception as exc:
                done.append((due.proposal.id, due.checkpoint, None, f"could not read: {exc!r}"))
                continue
            verifiable = CATALOG[due.proposal.action].verifiable
            if due.checkpoint is Checkpoint.T3M:
                result, detail = evaluate_window(
                    obs, verifiable=verifiable, executed_at=due.executed_at, now=now)
            else:
                result, detail = evaluate_recurrence(
                    obs, checkpoint=due.checkpoint, executed_at=due.executed_at, now=now)
            self.ledger.record_check(due.proposal.id, now=now, checkpoint=due.checkpoint,
                                     result=result, detail=detail)
            done.append((due.proposal.id, due.checkpoint, result, detail))
        return done

    # --- read-only advice -----------------------------------------------------------

    def advise(self, kind: str, name: str, namespace: str | None) -> str:
        """What Honk sees on a target and what has worked on that problem before."""
        actions = [a for a, spec in CATALOG.items() if kind in spec.target_kinds]
        if kind in WORKLOAD_KINDS:
            # A workload's cell is its worst pod's, so restarting that pod is the same cell.
            actions.insert(0, ActionType.RESTART_POD)
        if not actions:
            return f"Honk has no actions for {kind}."
        probe = Proposal(next(a for a in actions if kind in CATALOG[a].target_kinds), kind,
                         name, namespace)
        try:
            subject = self.resolve(probe)
        except Refusal as refusal:
            return f"Honk cannot assess this target: {refusal}"
        now = self.clock()
        lines = [f"Problem Honk sees: {subject.signature.label}"]
        if subject.signature.failure_state == FailureState.HEALTHY.value:
            lines.append("It looks healthy, so Honk would expect no action to be needed.")
        for action in actions:
            record = self.ledger.track_record(subject.signature.digest, action, now=now)
            note = gate.warning(record)
            lines.append(f"  {action}: {record.sentence()}" + (f"  (warning: {note})"
                                                               if note else ""))
        return "\n".join(lines)


def _record_dict(record: TrackRecord) -> Mapping[str, Any]:
    return asdict(record)
