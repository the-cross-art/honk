"""The closed action vocabulary: the only changes Honk will make to a cluster.

An agent can ask for anything. Honk maps a request onto one of these types or
refuses it. Each type has a fixed request shape, so the agent controls which
object and a bounded parameter, never the body that reaches the apiserver.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final


class ActionType(enum.StrEnum):
    RESTART_POD = "restart_pod"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE = "scale"
    SET_RESOURCES = "set_resources"
    CORDON_NODE = "cordon_node"
    UNCORDON_NODE = "uncordon_node"


class Verifiable(enum.StrEnum):
    OUTCOME = "outcome"
    EFFECT_ONLY = "effect_only"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    target_kinds: frozenset[str]
    blast_radius: str
    verifiable: Verifiable
    summary: str


WORKLOAD_KINDS: Final[frozenset[str]] = frozenset({"Deployment", "StatefulSet", "DaemonSet"})
SCALABLE_KINDS: Final[frozenset[str]] = frozenset({"Deployment", "StatefulSet"})

# Node actions are EFFECT_ONLY: Honk can confirm the node was cordoned, not that
# cordoning helped, so they can never earn autonomy.
CATALOG: Final[Mapping[ActionType, ActionSpec]] = {
    ActionType.RESTART_POD: ActionSpec(
        frozenset({"Pod"}), "pod", Verifiable.OUTCOME,
        "evict one pod so its controller recreates it (respects PodDisruptionBudgets)"),
    ActionType.ROLLOUT_RESTART: ActionSpec(
        WORKLOAD_KINDS, "workload", Verifiable.OUTCOME,
        "rolling restart of every pod in a workload"),
    ActionType.SCALE: ActionSpec(
        SCALABLE_KINDS, "workload", Verifiable.OUTCOME,
        "change a workload's replica count"),
    ActionType.SET_RESOURCES: ActionSpec(
        frozenset({"Deployment", "StatefulSet"}), "workload", Verifiable.OUTCOME,
        "change one container's cpu/memory requests or limits (triggers a rollout)"),
    ActionType.CORDON_NODE: ActionSpec(
        frozenset({"Node"}), "node", Verifiable.EFFECT_ONLY,
        "mark a node unschedulable"),
    ActionType.UNCORDON_NODE: ActionSpec(
        frozenset({"Node"}), "node", Verifiable.EFFECT_ONLY,
        "mark a node schedulable again"),
}

RESOURCE_KEYS: Final[frozenset[str]] = frozenset(
    {"limits.cpu", "limits.memory", "requests.cpu", "requests.memory"}
)


@dataclass(frozen=True, slots=True)
class Proposal:
    """What an agent asked for. `reason` is untrusted text: stored and shown, never decided on."""

    action: ActionType
    kind: str
    name: str
    namespace: str | None
    params: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    client: str = "unknown"


@dataclass(frozen=True, slots=True)
class Limits:
    max_replicas: int = 20
    max_scale_step: int = 5
    max_resource_factor: float = 2.0


@dataclass(frozen=True, slots=True)
class Request:
    """One apiserver call. Built only here; the executor sends nothing else."""

    verb: str
    kind: str
    name: str
    namespace: str | None
    subresource: str | None
    body: Mapping[str, Any]


_CPU_SUFFIX: Final[Mapping[str, float]] = {"m": 0.001, "": 1.0}
_MEM_SUFFIX: Final[Mapping[str, int]] = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "": 1,
}


def parse_quantity(value: Any, resource: str) -> float | None:
    """A cpu (cores) or memory (bytes) quantity, or None if it is not one we accept."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    table: Mapping[str, float] = (
        _CPU_SUFFIX if resource == "cpu" else {k: float(v) for k, v in _MEM_SUFFIX.items()}
    )
    for suffix in sorted(table, key=len, reverse=True):
        if suffix and not text.endswith(suffix):
            continue
        digits = text[: len(text) - len(suffix)] if suffix else text
        try:
            number = float(digits)
        except ValueError:
            return None
        return number * table[suffix] if number > 0 else None
    return None


_NAME: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_NAMESPACE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def check_target_kind(proposal: Proposal) -> str | None:
    """Kind, name and namespace, checked before anything reaches the apiserver.

    An empty name is not "not found": the client turns a GET on name "" into a
    LIST, so names are validated against Kubernetes' own grammar here.
    """
    spec = CATALOG[proposal.action]
    if proposal.kind not in spec.target_kinds:
        kinds = ", ".join(sorted(spec.target_kinds))
        return f"{proposal.action} applies to {kinds}, not {proposal.kind}"
    if not _NAME.fullmatch(proposal.name or ""):
        return f"{proposal.name!r} is not a valid Kubernetes object name"
    if proposal.kind == "Node":
        return None if proposal.namespace is None else "nodes are not namespaced"
    if not proposal.namespace:
        return f"{proposal.kind} needs a namespace"
    if not _NAMESPACE.fullmatch(proposal.namespace):
        return f"{proposal.namespace!r} is not a valid namespace"
    return None


def check_scale(replicas: Any, current: int, limits: Limits) -> str | None:
    if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0:
        return "replicas must be a non-negative integer"
    if replicas > limits.max_replicas:
        return f"replicas {replicas} exceeds the policy maximum of {limits.max_replicas}"
    if abs(replicas - current) > limits.max_scale_step:
        return (f"changing replicas from {current} to {replicas} exceeds the policy step of "
                f"{limits.max_scale_step}")
    if replicas == current:
        return f"the workload already has {current} replicas"
    return None


def container_resources(workload: Mapping[str, Any], container: str) -> Mapping[str, Any] | None:
    for c in ((workload.get("spec") or {}).get("template") or {}).get("spec", {}).get(
            "containers") or ():
        if isinstance(c, Mapping) and c.get("name") == container:
            return c.get("resources") or {}
    return None


def check_resources(
    changes: Any, current: Mapping[str, Any], limits: Limits
) -> str | None:
    """Only cpu/memory, only parseable values, at most `max_resource_factor` up or down."""
    if not isinstance(changes, Mapping) or not changes:
        return "resources must map e.g. 'limits.memory' to a quantity"
    for key, value in changes.items():
        if key not in RESOURCE_KEYS:
            return f"{key!r} is not one of {', '.join(sorted(RESOURCE_KEYS))}"
        section, resource = key.split(".")
        new = parse_quantity(value, resource)
        if new is None:
            return f"{key}={value!r} is not a valid {resource} quantity"
        old = parse_quantity((current.get(section) or {}).get(resource), resource)
        if old is None:
            continue
        factor = new / old
        if factor > limits.max_resource_factor or factor < 1 / limits.max_resource_factor:
            return (f"{key} {value} is {factor:.2f}x the current value; policy allows "
                    f"{limits.max_resource_factor}x either way")
    return None


def build_request(
    proposal: Proposal, *, target_uid: str, resource_version: str, now: datetime
) -> Request:
    """The exact call for a proposal that has already passed its checks.

    Every body carries a precondition, so a target that changed since Honk read
    it is rejected by the apiserver (409) instead of being changed blind.
    """
    meta = {"resourceVersion": resource_version}
    a = proposal.action
    if a is ActionType.RESTART_POD:
        return Request("create", "Pod", proposal.name, proposal.namespace, "eviction", {
            "apiVersion": "policy/v1", "kind": "Eviction",
            "metadata": {"name": proposal.name, "namespace": proposal.namespace},
            "deleteOptions": {"preconditions": {
                "uid": target_uid, "resourceVersion": resource_version}},
        })
    if a is ActionType.ROLLOUT_RESTART:
        return Request("patch", proposal.kind, proposal.name, proposal.namespace, None, {
            "metadata": meta,
            "spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restartedAt": now.isoformat()}}}},
        })
    if a is ActionType.SCALE:
        return Request("patch", proposal.kind, proposal.name, proposal.namespace, None, {
            "metadata": meta, "spec": {"replicas": int(proposal.params["replicas"])},
        })
    if a is ActionType.SET_RESOURCES:
        resources: dict[str, dict[str, str]] = {}
        for key, value in dict(proposal.params["resources"]).items():
            section, resource = key.split(".")
            resources.setdefault(section, {})[resource] = str(value)
        return Request("patch", proposal.kind, proposal.name, proposal.namespace, None, {
            "metadata": meta,
            "spec": {"template": {"spec": {"containers": [
                {"name": str(proposal.params["container"]), "resources": resources}]}}},
        })
    if a in (ActionType.CORDON_NODE, ActionType.UNCORDON_NODE):
        return Request("patch", "Node", proposal.name, None, None, {
            "metadata": meta, "spec": {"unschedulable": a is ActionType.CORDON_NODE},
        })
    raise ValueError(f"no request shape for {a}")


def _paths(body: Any, prefix: str = "") -> set[str]:
    """Every leaf path in a body. An empty dict or list is itself a leaf: in a
    strategic-merge patch `taints: []` deletes every taint, so it must not vanish."""
    if isinstance(body, Mapping) and body:
        out: set[str] = set()
        for k, v in body.items():
            out |= _paths(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    if isinstance(body, list) and body:
        out = set()
        for item in body:
            out |= _paths(item, prefix + "[]")
        return out
    return {prefix}


_ALLOWED_PATHS: Final[Mapping[ActionType, frozenset[str]]] = {
    ActionType.RESTART_POD: frozenset({
        "apiVersion", "kind", "metadata.name", "metadata.namespace",
        "deleteOptions.preconditions.uid", "deleteOptions.preconditions.resourceVersion"}),
    ActionType.ROLLOUT_RESTART: frozenset({
        "metadata.resourceVersion",
        "spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"}),
    ActionType.SCALE: frozenset({"metadata.resourceVersion", "spec.replicas"}),
    ActionType.SET_RESOURCES: frozenset({"metadata.resourceVersion",
        "spec.template.spec.containers[].name"} | {
        f"spec.template.spec.containers[].resources.{k}" for k in RESOURCE_KEYS}),
    ActionType.CORDON_NODE: frozenset({"metadata.resourceVersion", "spec.unschedulable"}),
    ActionType.UNCORDON_NODE: frozenset({"metadata.resourceVersion", "spec.unschedulable"}),
}


def recheck(action: ActionType, request: Request) -> str | None:
    """Refuse a body that touches anything outside the action's fixed shape.

    Catches bugs in `build_request`, not attackers: anyone holding Honk's
    credential can bypass this, which is why the RBAC grant is the real boundary.
    """
    extra = _paths(request.body) - _ALLOWED_PATHS[action]
    if extra:
        return f"request body for {action} touches fields outside its shape: {sorted(extra)}"
    if action is ActionType.SET_RESOURCES and len(request.body["spec"]["template"]["spec"][
            "containers"]) != 1:
        return "set_resources must change exactly one container"
    return None
