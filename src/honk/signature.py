"""The trust boundary: what kind of trouble is this target in?

A signature is computed by Honk from the live cluster, never from anything the
agent says. Every input is checked against a closed vocabulary of Kubernetes
enum values; an unrecognised value becomes UNKNOWN and is never hashed raw, so a
workload cannot mint new cells or grind its way into a cell that has earned
autonomy. Stdlib only: this module parses attacker-influenceable input.

Unlike the research prototype this was ported from, Honk reads a target at the
moment an agent calls, with no settling window. A crash-looping container
alternates between `waiting=CrashLoopBackOff` and `terminated=Error` on every
restart, so the raw reasons are collapsed into one `FailureState` that holds
still across the cycle.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final

SIG_VERSION: Final[str] = "honk_sig_v1"
UNKNOWN: Final[str] = "_UNKNOWN_"
NA: Final[str] = "_NA_"
BARE_POD: Final[str] = "_BARE_POD_"

# A container that restarted less than this long ago is still mid-crash-loop,
# even though a point read shows it running. Wider than the 300s backoff cap so
# a loop at the cap is still recognised between restarts.
RECENT_RESTART_SECONDS: Final[float] = 600.0

POD_PHASES: Final[frozenset[str]] = frozenset(
    {"Failed", "Pending", "Running", "Succeeded", "Unknown"}
)
IMAGE_REASONS: Final[frozenset[str]] = frozenset({
    "ErrImageNeverPull", "ErrImagePull", "ImageInspectError", "ImagePullBackOff",
    "InvalidImageName", "RegistryUnavailable", "SignatureValidationFailed",
})
CONFIG_REASONS: Final[frozenset[str]] = frozenset(
    {"CreateContainerConfigError", "CreateContainerError"}
)
START_FAILURE_REASONS: Final[frozenset[str]] = frozenset({"RunContainerError", "StartError"})
STARTING_REASONS: Final[frozenset[str]] = frozenset({"ContainerCreating", "PodInitializing"})
WAITING_REASONS: Final[frozenset[str]] = (
    IMAGE_REASONS | CONFIG_REASONS | START_FAILURE_REASONS | STARTING_REASONS
    | {"CrashLoopBackOff"}
)
TERMINATED_REASONS: Final[frozenset[str]] = frozenset({
    "Completed", "ContainerCannotRun", "ContainerStatusUnknown", "DeadlineExceeded",
    "Error", "Evicted", "OOMKilled", "StartError",
})
OWNER_KINDS: Final[frozenset[str]] = frozenset({
    "CronJob", "DaemonSet", "Deployment", "Job", "ReplicaSet",
    "ReplicationController", "StatefulSet",
})
NODE_PRESSURE_CONDITIONS: Final[frozenset[str]] = frozenset(
    {"DiskPressure", "MemoryPressure", "NetworkUnavailable", "PIDPressure"}
)


class FailureState(enum.StrEnum):
    HEALTHY = "healthy"
    STARTING = "starting"
    COMPLETED = "completed"
    CRASH_LOOP = "crash_loop"
    OOM_KILLED = "oom_killed"
    IMAGE_PULL = "image_pull"
    CONFIG_ERROR = "config_error"
    UNSCHEDULABLE = "unschedulable"
    NOT_READY = "not_ready"
    EVICTED = "evicted"
    FAILED = "failed"
    NO_PODS = "no_pods"
    NODE_HEALTHY = "node_healthy"
    NODE_NOT_READY = "node_not_ready"
    NODE_PRESSURE = "node_pressure"
    UNKNOWN = "unknown"


OK_STATES: Final[frozenset[FailureState]] = frozenset(
    {FailureState.HEALTHY, FailureState.COMPLETED, FailureState.NODE_HEALTHY}
)

# Worst first. A workload's signature is its worst pod's, so one ordering decides it.
SEVERITY: Final[tuple[FailureState, ...]] = (
    FailureState.OOM_KILLED, FailureState.CRASH_LOOP, FailureState.IMAGE_PULL,
    FailureState.CONFIG_ERROR, FailureState.UNSCHEDULABLE, FailureState.EVICTED,
    FailureState.FAILED, FailureState.UNKNOWN, FailureState.NOT_READY,
    FailureState.STARTING, FailureState.NO_PODS, FailureState.COMPLETED,
    FailureState.HEALTHY,
)

EXIT_CLASSES: Final[Mapping[int, str]] = {
    0: "clean", 128: "start_failure", 134: "sigabrt", 137: "sigkill",
    139: "sigsegv", 143: "sigterm",
}


def classify_exit(exit_code: int | None) -> str:
    if exit_code is None:
        return NA
    if (named := EXIT_CLASSES.get(exit_code)) is not None:
        return named
    if 125 <= exit_code <= 127:
        return "exec_error"
    if 128 < exit_code <= 255:
        return "signal_other"
    if exit_code > 0:
        return "app_error"
    return NA


def _closed(value: Any, vocabulary: frozenset[str]) -> str | None:
    """`None` stays `None` (absent is a fact); anything off-vocabulary is UNKNOWN."""
    if value is None:
        return None
    if isinstance(value, str) and value in vocabulary:
        return value
    return UNKNOWN


@dataclass(frozen=True, slots=True)
class PodFacts:
    """Closed-vocabulary facts about one pod, as `extract.pod_facts` reads them."""

    phase: str | None
    status_reason: str | None
    unschedulable: bool
    ready: bool
    waiting_reason: str | None
    terminated_reason: str | None
    terminated_exit: int | None
    last_terminated_reason: str | None
    last_exit: int | None
    restart_count: int
    running_seconds: float | None


def failure_state(f: PodFacts) -> tuple[FailureState, str]:
    """The pod's failure state and the exit class that refines it."""
    phase = _closed(f.phase, POD_PHASES)
    waiting = _closed(f.waiting_reason, WAITING_REASONS)
    terminated = _closed(f.terminated_reason, TERMINATED_REASONS)
    last_terminated = _closed(f.last_terminated_reason, TERMINATED_REASONS)

    if phase == "Succeeded":
        return FailureState.COMPLETED, NA
    if f.status_reason == "Evicted":
        return FailureState.EVICTED, NA
    if phase == "Pending" and f.unschedulable:
        return FailureState.UNSCHEDULABLE, NA
    if waiting in IMAGE_REASONS:
        return FailureState.IMAGE_PULL, NA
    if waiting in CONFIG_REASONS:
        return FailureState.CONFIG_ERROR, NA

    recently_restarted = (
        f.restart_count > 0
        and f.running_seconds is not None
        and f.running_seconds < RECENT_RESTART_SECONDS
    )
    looping = (
        waiting == "CrashLoopBackOff"
        or waiting in START_FAILURE_REASONS
        or (terminated is not None and terminated != "Completed")
        or (recently_restarted and last_terminated not in (None, "Completed"))
    )
    if looping:
        if "OOMKilled" in (terminated, last_terminated):
            return FailureState.OOM_KILLED, NA
        exit_code = f.terminated_exit if f.terminated_exit is not None else f.last_exit
        return FailureState.CRASH_LOOP, classify_exit(exit_code)

    if waiting in STARTING_REASONS:
        return FailureState.STARTING, NA
    if waiting == UNKNOWN or phase == UNKNOWN:
        return FailureState.UNKNOWN, NA
    if phase == "Failed":
        return FailureState.FAILED, NA
    if phase == "Pending":
        return FailureState.STARTING, NA
    if phase == "Running":
        return (FailureState.HEALTHY if f.ready else FailureState.NOT_READY), NA
    return FailureState.UNKNOWN, NA


def worst(states: Iterable[tuple[FailureState, str]]) -> tuple[FailureState, str]:
    """The most severe state, ties broken by exit class so the result is deterministic."""
    ranked = sorted(states, key=lambda s: (SEVERITY.index(s[0]), s[1]))
    return ranked[0] if ranked else (FailureState.NO_PODS, NA)


@dataclass(frozen=True, slots=True)
class Signature:
    """A cell key. Coarse on purpose: every extra field multiplies cells and slows learning."""

    version: str
    owner_kind: str
    failure_state: str
    exit_class: str
    node_unhealthy: bool

    @property
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    @property
    def label(self) -> str:
        parts = [self.failure_state]
        if self.exit_class != NA:
            parts.append(self.exit_class)
        parts.append(self.owner_kind.lstrip("_").lower())
        if self.node_unhealthy:
            parts.append("sick-node")
        return " / ".join(parts)

    def fields(self) -> dict[str, Any]:
        return asdict(self)


def sign(
    *, owner_kind: str | None, state: tuple[FailureState, str], node_unhealthy: bool
) -> Signature:
    kind = BARE_POD if owner_kind is None else (_closed(owner_kind, OWNER_KINDS) or UNKNOWN)
    return Signature(
        version=SIG_VERSION, owner_kind=kind, failure_state=state[0].value,
        exit_class=state[1], node_unhealthy=node_unhealthy,
    )


def node_state(failing_conditions: Iterable[str]) -> FailureState:
    conditions = set(failing_conditions)
    if "Ready" in conditions:
        return FailureState.NODE_NOT_READY
    if conditions & NODE_PRESSURE_CONDITIONS:
        return FailureState.NODE_PRESSURE
    return FailureState.NODE_HEALTHY


def sign_node(failing_conditions: Iterable[str]) -> Signature:
    state = node_state(failing_conditions)
    return Signature(
        version=SIG_VERSION, owner_kind="Node", failure_state=state.value,
        exit_class=NA, node_unhealthy=state is not FailureState.NODE_HEALTHY,
    )
