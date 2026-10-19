"""The only module that talks to the Kubernetes API.

Reads are restricted to an allowlist of kinds. Writes are exactly the request
shapes `actions.build_request` produces; anything else is refused rather than
sent through a generic client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol, cast

from honk.actions import Request
from honk.extract import owner_refs
from honk.ownership import OwnerRef, OwnerResolver

STRATEGIC_MERGE: Final[str] = "application/strategic-merge-patch+json"

READABLE_KINDS: Final[frozenset[str]] = frozenset({
    "Pod", "Node", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet",
    "Job", "CronJob", "ReplicationController",
})


class Cluster(Protocol):
    def read(self, kind: str, name: str, namespace: str | None) -> Mapping[str, Any] | None: ...
    def list_pods(self, namespace: str) -> Sequence[Mapping[str, Any]]: ...
    def resolver(self) -> OwnerResolver: ...
    def send(self, request: Request, *, persist: bool) -> tuple[bool, str]: ...


class CachingResolver:
    """An OwnerResolver over any Cluster, cached for the life of one assessment."""

    def __init__(self, cluster: Cluster) -> None:
        self._cluster = cluster
        self._cache: dict[tuple[str, str, str | None], Any] = {}

    def lookup(
        self, *, kind: str, name: str, namespace: str | None
    ) -> tuple[str, tuple[OwnerRef, ...]] | None:
        key = (kind, name, namespace)
        if key not in self._cache:
            obj = self._cluster.read(kind, name, None if kind == "Node" else namespace)
            if obj is None:
                self._cache[key] = None
            else:
                uid = (obj.get("metadata") or {}).get("uid")
                self._cache[key] = (str(uid), owner_refs(obj)) if uid else None
        result: tuple[str, tuple[OwnerRef, ...]] | None = self._cache[key]
        return result


class KubeCluster:
    """A Cluster backed by the real apiserver, via the current kubeconfig."""

    def __init__(self, *, context: str | None = None) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(context=context)
        self._client = client
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._batch = client.BatchV1Api()
        self._sanitize = client.ApiClient().sanitize_for_serialization

    def _dict(self, obj: Any) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self._sanitize(obj))

    def read(self, kind: str, name: str, namespace: str | None) -> Mapping[str, Any] | None:
        if kind not in READABLE_KINDS or not name:
            return None
        if kind != "Node" and not namespace:
            return None
        ns = namespace or ""
        readers: Mapping[str, Any] = {
            "Pod": lambda: self._core.read_namespaced_pod(name, ns),
            "Node": lambda: self._core.read_node(name),
            "Deployment": lambda: self._apps.read_namespaced_deployment(name, ns),
            "StatefulSet": lambda: self._apps.read_namespaced_stateful_set(name, ns),
            "DaemonSet": lambda: self._apps.read_namespaced_daemon_set(name, ns),
            "ReplicaSet": lambda: self._apps.read_namespaced_replica_set(name, ns),
            "Job": lambda: self._batch.read_namespaced_job(name, ns),
            "CronJob": lambda: self._batch.read_namespaced_cron_job(name, ns),
            "ReplicationController":
                lambda: self._core.read_namespaced_replication_controller(name, ns),
        }
        try:
            obj = self._dict(readers[kind]())
        except self._client.ApiException as exc:
            if exc.status in (403, 404):
                return None
            raise
        return obj if (obj.get("metadata") or {}).get("name") == name else None

    def list_pods(self, namespace: str) -> Sequence[Mapping[str, Any]]:
        listing = self._core.list_namespaced_pod(namespace)
        return [self._dict(p) for p in listing.items]

    def resolver(self) -> OwnerResolver:
        return CachingResolver(self)

    def send(self, request: Request, *, persist: bool) -> tuple[bool, str]:
        """Send one Honk request. `persist=False` is a server-side dry run (dryRun=All)."""
        dry = None if persist else "All"
        r = request
        ns = r.namespace or ""
        body: Any = dict(r.body)
        try:
            if r.verb == "create" and r.kind == "Pod" and r.subresource == "eviction":
                self._core.create_namespaced_pod_eviction(r.name, ns, body, dry_run=dry)
            elif r.verb == "patch" and r.subresource is None and r.kind in (
                    "Deployment", "StatefulSet", "DaemonSet", "Node"):
                patchers: Mapping[str, Any] = {
                    "Deployment": lambda: self._apps.patch_namespaced_deployment(
                        r.name, ns, body, dry_run=dry, _content_type=STRATEGIC_MERGE),
                    "StatefulSet": lambda: self._apps.patch_namespaced_stateful_set(
                        r.name, ns, body, dry_run=dry, _content_type=STRATEGIC_MERGE),
                    "DaemonSet": lambda: self._apps.patch_namespaced_daemon_set(
                        r.name, ns, body, dry_run=dry, _content_type=STRATEGIC_MERGE),
                    "Node": lambda: self._core.patch_node(
                        r.name, body, dry_run=dry, _content_type=STRATEGIC_MERGE),
                }
                patchers[r.kind]()
            else:
                return False, f"Honk has no request path for {r.verb} {r.kind}/{r.subresource}"
        except self._client.ApiException as exc:
            return False, f"{exc.status} {(exc.reason or '').strip()}: {_message(exc)}".strip()
        return True, "dry run accepted by the apiserver" if not persist else "applied"


def _message(exc: Any) -> str:
    try:
        parsed = json.loads(exc.body)
    except (TypeError, ValueError):
        return ""
    return str(parsed.get("message") or "") if isinstance(parsed, Mapping) else ""
