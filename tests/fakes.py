"""An in-memory cluster, so the decision path is testable without one."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from honk.actions import Request
from honk.kube import CachingResolver
from honk.ownership import OwnerResolver

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def deployment(name: str = "web", uid: str = "dep-1", ns: str = "shop", replicas: int = 1,
               generation: int = 1) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "uid": uid, "resourceVersion": "100",
                     "generation": generation},
        "spec": {"replicas": replicas, "template": {"spec": {"containers": [
            {"name": "app", "resources": {"limits": {"memory": "256Mi", "cpu": "500m"},
                                          "requests": {"memory": "128Mi"}}}]}}},
    }


def replicaset(name: str = "web-rs", uid: str = "rs-1", ns: str = "shop",
               owner: tuple[str, str] = ("web", "dep-1")) -> dict[str, Any]:
    return {
        "kind": "ReplicaSet",
        "metadata": {"name": name, "namespace": ns, "uid": uid, "ownerReferences": [
            {"kind": "Deployment", "name": owner[0], "uid": owner[1], "controller": True}]},
    }


def node(name: str = "node-1", uid: str = "node-uid", ready: bool = True,
         unschedulable: bool = False) -> dict[str, Any]:
    return {
        "kind": "Node",
        "metadata": {"name": name, "uid": uid, "resourceVersion": "7"},
        "spec": {"unschedulable": unschedulable},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"},
                                  {"type": "MemoryPressure", "status": "False"}]},
    }


def pod(name: str = "web-abc", uid: str = "pod-1", ns: str = "shop",
        owner: tuple[str, str, str] | None = ("ReplicaSet", "web-rs", "rs-1"),
        state: str = "crashloop", now: datetime = T0, restarts: int = 5,
        created: datetime | None = None, node_name: str = "node-1",
        exit_code: int = 1) -> dict[str, Any]:
    refs = ([{"kind": owner[0], "name": owner[1], "uid": owner[2], "controller": True}]
            if owner else [])
    if state == "crashloop":
        cs = {"name": "app", "ready": False, "restartCount": restarts,
              "state": {"waiting": {"reason": "CrashLoopBackOff"}},
              "lastState": {"terminated": {"reason": "Error", "exitCode": exit_code,
                                           "finishedAt": iso(now - timedelta(seconds=20))}}}
        phase, ready = "Running", "False"
    elif state == "oom":
        cs = {"name": "app", "ready": False, "restartCount": restarts,
              "state": {"waiting": {"reason": "CrashLoopBackOff"}},
              "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137,
                                           "finishedAt": iso(now - timedelta(seconds=20))}}}
        phase, ready = "Running", "False"
    elif state == "healthy":
        cs = {"name": "app", "ready": True, "restartCount": restarts,
              "state": {"running": {"startedAt": iso(now - timedelta(hours=2))}}}
        phase, ready = "Running", "True"
    elif state == "imagepull":
        cs = {"name": "app", "ready": False, "restartCount": 0,
              "state": {"waiting": {"reason": "ImagePullBackOff"}}}
        phase, ready = "Pending", "False"
    else:
        raise ValueError(state)
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": ns, "uid": uid, "resourceVersion": "555",
                     "ownerReferences": refs,
                     "creationTimestamp": iso(created or now - timedelta(hours=3))},
        "spec": {"nodeName": node_name},
        "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready},
                                                  {"type": "PodScheduled", "status": "True"}],
                   "containerStatuses": [cs]},
    }


class FakeCluster:
    def __init__(self, objects: Sequence[Mapping[str, Any]] = ()) -> None:
        self.objects: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        for obj in objects:
            self.put(obj)
        self.sent: list[tuple[Request, bool]] = []
        self.reject_dry_run: str | None = None
        self.on_persist: Callable[[Request], None] | None = None

    def put(self, obj: Mapping[str, Any]) -> None:
        meta = obj["metadata"]
        ns = None if obj["kind"] == "Node" else meta.get("namespace")
        self.objects[(obj["kind"], ns, meta["name"])] = copy.deepcopy(dict(obj))

    def remove(self, kind: str, name: str, ns: str | None) -> None:
        self.objects.pop((kind, ns, name), None)

    def read(self, kind: str, name: str, namespace: str | None) -> Mapping[str, Any] | None:
        ns = None if kind == "Node" else namespace
        obj = self.objects.get((kind, ns, name))
        return copy.deepcopy(obj) if obj else None

    def list_pods(self, namespace: str) -> Sequence[Mapping[str, Any]]:
        return [copy.deepcopy(o) for (k, ns, _), o in self.objects.items()
                if k == "Pod" and ns == namespace]

    def resolver(self) -> OwnerResolver:
        return CachingResolver(self)

    def send(self, request: Request, *, persist: bool) -> tuple[bool, str]:
        self.sent.append((request, persist))
        if not persist:
            if self.reject_dry_run:
                return False, self.reject_dry_run
            return True, "dry run accepted by the apiserver"
        if self.on_persist:
            self.on_persist(request)
        return True, "applied"
