import sqlite3
from dataclasses import replace

import pytest

from honk.actions import (
    ActionType,
    Limits,
    Proposal,
    Request,
    build_request,
    check_resources,
    check_scale,
    recheck,
)
from honk.ledger import TABLES, Ledger
from honk.outcomes import Checkpoint, Result
from tests.fakes import T0


@pytest.mark.parametrize("action,kind,params", [
    (ActionType.RESTART_POD, "Pod", {}),
    (ActionType.ROLLOUT_RESTART, "Deployment", {}),
    (ActionType.SCALE, "Deployment", {"replicas": 3}),
    (ActionType.SET_RESOURCES, "Deployment",
     {"container": "app", "resources": {"limits.memory": "512Mi"}}),
    (ActionType.CORDON_NODE, "Node", {}),
    (ActionType.UNCORDON_NODE, "Node", {}),
])
def test_every_built_request_passes_its_own_recheck(action: ActionType, kind: str,
                                                    params: dict[str, object]) -> None:
    ns = None if kind == "Node" else "shop"
    req = build_request(Proposal(action, kind, "x", ns, params), target_uid="u",
                        resource_version="1", now=T0)
    assert recheck(action, req) is None
    assert req.body["metadata"].get("resourceVersion") == "1" or \
        req.body["deleteOptions"]["preconditions"]["resourceVersion"] == "1"


def test_recheck_refuses_smuggled_fields() -> None:
    req = build_request(Proposal(ActionType.SCALE, "Deployment", "x", "shop", {"replicas": 2}),
                        target_uid="u", resource_version="1", now=T0)
    evil = replace(req, body={**req.body, "spec": {"replicas": 2, "template": {"spec": {
        "containers": [{"name": "app", "image": "evil"}]}}}})
    assert recheck(ActionType.SCALE, evil) is not None
    node = Request("patch", "Node", "n", None, None,
                   {"metadata": {"resourceVersion": "1"}, "spec": {"unschedulable": True,
                                                                  "taints": []}})
    assert recheck(ActionType.CORDON_NODE, node) is not None


def test_scale_limits() -> None:
    limits = Limits(max_replicas=10, max_scale_step=3)
    assert check_scale(4, 2, limits) is None
    assert check_scale(11, 9, limits) is not None
    assert check_scale(9, 2, limits) is not None
    assert check_scale(-1, 2, limits) is not None
    assert check_scale(True, 2, limits) is not None
    assert check_scale(2, 2, limits) is not None


def test_resource_limits() -> None:
    current = {"limits": {"memory": "256Mi", "cpu": "500m"}}
    limits = Limits(max_resource_factor=2.0)
    assert check_resources({"limits.memory": "512Mi"}, current, limits) is None
    assert check_resources({"limits.memory": "1Gi"}, current, limits) is not None
    assert check_resources({"limits.cpu": "200m"}, current, limits) is not None
    assert check_resources({"limits.nvidia.com/gpu": "1"}, current, limits) is not None
    assert check_resources({"limits.memory": "lots"}, current, limits) is not None
    assert check_resources({"requests.memory": "64Mi"}, current, limits) is None


def test_a_large_cut_is_reported_as_a_fraction() -> None:
    """Found in the agent study: 64 CPUs to 250m was reported as '0.00x'."""
    refusal = check_resources({"requests.cpu": "250m"}, {"requests": {"cpu": "64"}},
                              Limits(max_resource_factor=2.0))
    assert refusal is not None and "1/256 of" in refusal
    up = check_resources({"limits.memory": "1Gi"}, {"limits": {"memory": "256Mi"}},
                         Limits(max_resource_factor=2.0))
    assert up is not None and "4.00x" in up


def _seed(ledger: Ledger) -> str:
    pid = ledger.record_proposal(
        now=T0, client="t", action=ActionType.RESTART_POD, kind="Pod", namespace="shop",
        name="p", target_uid="dep-1", subject_kind="Deployment", subject_name="web", params={},
        reason="", signature={"version": "v"}, sig_digest="d", sig_label="l", mode="ask",
        decision_reason="r", decision_detail="d", record_shown={}, request={"verb": "create"})
    ledger.record_approval(pid, now=T0, verdict="approved", approver="me")
    ledger.record_execution(pid, now=T0, mode="approved", ok=True, detail="ok", generation=1)
    ledger.record_check(pid, now=T0, checkpoint=Checkpoint.T3M, result=Result.FAILING,
                        detail="x")
    return pid


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_append_only(table: str) -> None:
    ledger = Ledger(":memory:")
    _seed(ledger)
    ledger.reset_breaker(now=T0, actor="me")
    conn = ledger._conn
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET ts = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")


def test_an_approval_cannot_be_recorded_twice() -> None:
    ledger = Ledger(":memory:")
    pid = _seed(ledger)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_approval(pid, now=T0, verdict="denied", approver="someone-else")
