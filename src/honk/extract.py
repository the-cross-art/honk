"""Raw Kubernetes objects (camelCase dicts) to closed-vocabulary facts.

Every field read here is either server-assigned or a Kubernetes enum. Names,
labels, annotations, messages and logs are never read on this path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from honk.ownership import OwnerRef
from honk.signature import PodFacts


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def owner_refs(obj: Mapping[str, Any]) -> tuple[OwnerRef, ...]:
    refs: list[OwnerRef] = []
    for raw in (obj.get("metadata") or {}).get("ownerReferences") or ():
        if not isinstance(raw, Mapping):
            continue
        kind, name, uid = raw.get("kind"), raw.get("name"), raw.get("uid")
        if isinstance(kind, str) and isinstance(name, str) and isinstance(uid, str):
            refs.append(OwnerRef(
                kind=kind, name=name, uid=uid,
                api_version=str(raw.get("apiVersion", "v1")),
                controller=bool(raw.get("controller", False)),
            ))
    return tuple(refs)


def failing_conditions(obj: Mapping[str, Any]) -> tuple[str, ...]:
    """Condition types that signal trouble.

    For a node, `Ready` is healthy when True and every pressure condition is
    healthy when False, so the two are read with opposite polarity.
    """
    out: list[str] = []
    for cond in (obj.get("status") or {}).get("conditions") or ():
        if not isinstance(cond, Mapping):
            continue
        ctype, status = cond.get("type"), cond.get("status")
        if not isinstance(ctype, str):
            continue
        if ctype == "Ready":
            if status != "True":
                out.append(ctype)
        elif status == "True":
            out.append(ctype)
    return tuple(sorted(out))


def _speaking_container(statuses: Sequence[Any]) -> Mapping[str, Any] | None:
    """The first not-ready container by name, so the result is order-independent."""
    valid = sorted(
        (cs for cs in statuses or () if isinstance(cs, Mapping)),
        key=lambda cs: str(cs.get("name", "")),
    )
    for cs in valid:
        if not cs.get("ready", False):
            return cs
    return valid[0] if valid else None


def _failing_init_container(statuses: Sequence[Any]) -> Mapping[str, Any] | None:
    for cs in sorted(
        (cs for cs in statuses or () if isinstance(cs, Mapping)),
        key=lambda cs: str(cs.get("name", "")),
    ):
        state = cs.get("state") or {}
        terminated = state.get("terminated") or {}
        if terminated.get("reason") == "Completed" or cs.get("ready"):
            continue
        if state.get("waiting") or terminated or int(cs.get("restartCount") or 0) > 0:
            return cs
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def pod_facts(pod: Mapping[str, Any], *, now: datetime) -> PodFacts:
    status = pod.get("status") or {}
    conditions = {
        c.get("type"): c for c in status.get("conditions") or () if isinstance(c, Mapping)
    }
    scheduled = conditions.get("PodScheduled") or {}
    ready = (conditions.get("Ready") or {}).get("status") == "True"

    container = _failing_init_container(status.get("initContainerStatuses") or ())
    if container is None:
        container = _speaking_container(status.get("containerStatuses") or ())
    container = container or {}

    state = container.get("state") or {}
    last = (container.get("lastState") or {}).get("terminated") or {}
    waiting = state.get("waiting") or {}
    terminated = state.get("terminated") or {}
    started = parse_ts((state.get("running") or {}).get("startedAt"))

    return PodFacts(
        phase=status.get("phase"),
        status_reason=status.get("reason") if status.get("reason") == "Evicted" else None,
        unschedulable=scheduled.get("status") == "False",
        ready=ready,
        waiting_reason=waiting.get("reason") if waiting else None,
        terminated_reason=terminated.get("reason") if terminated else None,
        terminated_exit=_int(terminated.get("exitCode")),
        last_terminated_reason=last.get("reason") if last else None,
        last_exit=_int(last.get("exitCode")),
        restart_count=_int(container.get("restartCount")) or 0,
        running_seconds=(now - started).total_seconds() if started else None,
    )


def restart_total(pod: Mapping[str, Any]) -> int:
    status = pod.get("status") or {}
    return sum(
        _int(cs.get("restartCount")) or 0
        for key in ("initContainerStatuses", "containerStatuses")
        for cs in status.get(key) or ()
        if isinstance(cs, Mapping)
    )


def last_restart_at(pod: Mapping[str, Any]) -> datetime | None:
    """When any container in the pod last terminated, if it ever did."""
    latest: datetime | None = None
    status = pod.get("status") or {}
    for key in ("initContainerStatuses", "containerStatuses"):
        for cs in status.get(key) or ():
            if not isinstance(cs, Mapping):
                continue
            finished = parse_ts(((cs.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"))
            if finished and (latest is None or finished > latest):
                latest = finished
    return latest
