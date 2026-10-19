from datetime import timedelta
from typing import Any

import pytest

from honk.actions import ActionType, Proposal, Request
from honk.core import Honk, Refusal
from honk.gate import Mode
from honk.ledger import Ledger
from honk.outcomes import Result, Status
from honk.policy import parse
from tests.fakes import Clock, FakeCluster, deployment, node, pod, replicaset

ON = lambda: (True, "on")  # noqa: E731


def world(state: str = "crashloop", **pod_kw: Any) -> FakeCluster:
    return FakeCluster([deployment(), replicaset(), node(), pod(state=state, **pod_kw)])


def make(cluster: FakeCluster, policy: str = "", clock: Clock | None = None,
         switch: Any = ON) -> tuple[Honk, Clock]:
    clock = clock or Clock()
    return Honk(cluster=cluster, ledger=Ledger(":memory:"), policy=parse(policy), clock=clock,
                switch=switch), clock


def restart(name: str = "web-abc") -> Proposal:
    return Proposal(ActionType.RESTART_POD, "Pod", name, "shop", reason="it is crashing",
                    client="test")


def replace_pod_on_eviction(cluster: FakeCluster, clock: Clock, new_state: str,
                            counter: list[int]) -> None:
    def handler(request: Request) -> None:
        cluster.remove("Pod", request.name, "shop")
        counter[0] += 1
        cluster.put(pod(name=f"web-new{counter[0]}", uid=f"pod-new{counter[0]}",
                        state=new_state, now=clock.now, restarts=0, created=clock.now))
    cluster.on_persist = handler


def test_a_new_cell_always_asks_and_nothing_mutates_before_approval() -> None:
    cluster = world()
    honk, _ = make(cluster)
    a = honk.propose(restart())
    assert a.mode is Mode.ASK
    assert a.problem == "crash_loop / app_error / deployment"
    assert all(not persist for _, persist in cluster.sent)
    assert [p.id for p in honk.ledger.pending_approvals()] == [a.proposal_id]


def test_approve_executes_then_checks_label_the_outcome() -> None:
    cluster = world()
    honk, clock = make(cluster)
    replace_pod_on_eviction(cluster, clock, "crashloop", [0])
    a = honk.propose(restart())
    assert a.proposal_id is not None
    done = honk.approve(a.proposal_id, approver="imran")
    assert done.executed
    eviction = next(r for r, persist in cluster.sent if persist)
    assert eviction.body["deleteOptions"]["preconditions"]["uid"] == "pod-1"

    clock.advance(minutes=3)
    results = honk.run_checks()
    assert [r for _, _, r, _ in results] == [Result.FAILING]
    assert honk.ledger.status(a.proposal_id) is Status.FAILED
    assert honk.run_checks() == []


def test_the_record_follows_the_problem_not_the_pod() -> None:
    """Replacement pods are new objects with new UIDs; the record is per cell."""
    cluster = world()
    honk, clock = make(cluster)
    counter = [0]
    replace_pod_on_eviction(cluster, clock, "crashloop", counter)
    name = "web-abc"
    for _ in range(2):
        a = honk.propose(restart(name))
        assert a.proposal_id is not None
        honk.approve(a.proposal_id, approver="imran")
        clock.advance(minutes=3)
        honk.run_checks()
        name = f"web-new{counter[0]}"
        cluster.put(pod(name=name, uid=f"pod-new{counter[0]}", state="crashloop",
                        now=clock.now))
        clock.advance(minutes=30)
    third = honk.propose(restart(name))
    assert third.record is not None and third.record.failed == 2
    assert third.warning and "has not fixed" in third.warning


def test_a_fix_that_holds_becomes_fixed_and_one_that_returns_is_retracted() -> None:
    for comes_back in (False, True):
        cluster = world()
        honk, clock = make(cluster)
        replace_pod_on_eviction(cluster, clock, "healthy", [0])
        a = honk.propose(restart())
        assert a.proposal_id is not None
        honk.approve(a.proposal_id, approver="imran")
        clock.advance(minutes=3)
        honk.run_checks()
        assert honk.ledger.status(a.proposal_id) is Status.PROVISIONAL
        if comes_back:
            cluster.put(pod(name="web-new1", uid="pod-new1", state="crashloop", now=clock.now,
                            created=clock.now - timedelta(minutes=3)))
        clock.advance(hours=1)
        honk.run_checks()
        clock.advance(hours=23)
        honk.run_checks()
        expected = Status.RETRACTED if comes_back else Status.FIXED
        assert honk.ledger.status(a.proposal_id) is expected


def test_refusals_are_recorded_and_never_mutate() -> None:
    cases: list[tuple[FakeCluster, Proposal, str, str]] = []
    bare = FakeCluster([node(), pod(owner=None)])
    cases.append((bare, restart(), "", "no controller"))
    forged = FakeCluster([deployment(), replicaset(), node(),
                          pod(owner=("ReplicaSet", "web-rs", "WRONG-UID"))])
    cases.append((forged, restart(), "", "could not be verified"))
    cases.append((world(), Proposal(ActionType.RESTART_POD, "Pod", "nope", "shop"), "",
                  "not found"))
    for bad_name in ("", "Web_ABC", "a/../b", "x" * 300):
        cases.append((world(), Proposal(ActionType.RESTART_POD, "Pod", bad_name, "shop"), "",
                      "not a valid Kubernetes object name"))
    cases.append((world(), Proposal(ActionType.RESTART_POD, "Pod", "web-abc", "Shop!"), "",
                  "not a valid namespace"))
    cases.append((world(), Proposal(ActionType.SCALE, "Deployment", "web", "shop",
                                    {"replicas": 50}), "", "policy maximum"))
    cases.append((world(), restart(), "deny:\n  actions: [restart_pod]\n", "policy denies"))
    cases.append((world(), Proposal(ActionType.SCALE, "Pod", "web-abc", "shop",
                                    {"replicas": 2}), "", "applies to"))
    rejecting = world()
    rejecting.reject_dry_run = "403 Forbidden: Cannot evict pod as it would violate the PDB"
    cases.append((rejecting, restart(), "", "rejected a dry run"))
    for cluster, proposal, policy, expected in cases:
        honk, _ = make(cluster, policy)
        a = honk.propose(proposal)
        assert a.mode is Mode.REFUSE, expected
        assert expected in a.summary
        assert not any(persist for _, persist in cluster.sent)
        assert honk.ledger.proposal(a.proposal_id or "") is not None


def test_paused_honk_refuses_and_approval_after_pause_does_not_run() -> None:
    honk, _ = make(world(), switch=lambda: (False, "Honk is paused"))
    assert honk.propose(restart()).mode is Mode.REFUSE

    state = {"on": True}
    cluster = world()
    honk, _ = make(cluster, switch=lambda: (state["on"], "paused"))
    a = honk.propose(restart())
    state["on"] = False
    assert a.proposal_id is not None
    assert not honk.approve(a.proposal_id, approver="imran").executed
    assert not any(persist for _, persist in cluster.sent)


def test_execution_refuses_when_the_problem_changed_since_approval_was_asked() -> None:
    cluster = world()
    honk, _ = make(cluster)
    a = honk.propose(restart())
    cluster.put(pod(state="oom"))
    assert a.proposal_id is not None
    result = honk.approve(a.proposal_id, approver="imran")
    assert not result.executed and "changed since" in result.exec_detail
    assert not any(persist for _, persist in cluster.sent)


def test_an_approval_cannot_be_replayed() -> None:
    honk, _ = make(world())
    a = honk.propose(restart())
    assert a.proposal_id is not None
    honk.approve(a.proposal_id, approver="imran")
    with pytest.raises(Refusal):
        honk.approve(a.proposal_id, approver="imran")
    with pytest.raises(Refusal):
        honk.deny(a.proposal_id, approver="imran")


def test_throttle_stops_an_agent_loop_on_one_workload() -> None:
    cluster = world()
    honk, clock = make(cluster, "throttle:\n  per_target_per_hour: 2\n")
    counter = [0]
    replace_pod_on_eviction(cluster, clock, "crashloop", counter)
    name = "web-abc"
    for _ in range(2):
        a = honk.propose(restart(name))
        assert a.proposal_id is not None
        honk.approve(a.proposal_id, approver="imran")
        name = f"web-new{counter[0]}"
        clock.advance(minutes=1)
    third = honk.propose(restart(name))
    assert third.mode is Mode.REFUSE and "last hour" in third.summary


def test_autonomy_is_earned_per_cell_and_only_when_the_policy_allows_it() -> None:
    cluster = world()
    policy = ("autonomy:\n  restart_pod:\n    min_events: 3\n    min_score: 0.6\n"
              "    min_age_days: 1\n    min_distinct_days: 2\n")
    honk, clock = make(cluster, policy)
    counter = [0]

    def heal(request: Request) -> None:
        cluster.remove("Pod", request.name, "shop")
        counter[0] += 1
        cluster.put(pod(name=f"web-new{counter[0]}", uid=f"pod-new{counter[0]}",
                        state="healthy", now=clock.now, restarts=0, created=clock.now))
    cluster.on_persist = heal

    modes = []
    name = "web-abc"
    for _ in range(4):
        cluster.put(pod(name=name, uid=f"uid-{name}", state="crashloop", now=clock.now))
        a = honk.propose(restart(name))
        modes.append(a.mode)
        if a.mode is Mode.ASK:
            assert a.proposal_id is not None
            honk.approve(a.proposal_id, approver="imran")
        for hours in (0.05, 1, 23):
            clock.advance(hours=hours)
            honk.run_checks()
        cluster.remove("Pod", f"web-new{counter[0]}", "shop")
        name = f"web-again{counter[0]}"
        clock.advance(hours=1)
    assert modes == [Mode.ASK, Mode.ASK, Mode.ASK, Mode.AUTO]

    other_problem = world(state="oom")
    honk.cluster = other_problem
    assert honk.propose(restart()).mode is Mode.ASK


def test_advice_reports_the_record_without_changing_anything() -> None:
    cluster = world()
    honk, _ = make(cluster)
    text = honk.advise("Pod", "web-abc", "shop")
    assert "crash_loop" in text and "restart_pod" in text
    assert cluster.sent == []


def test_a_check_that_cannot_read_the_cluster_is_retried_not_lost() -> None:
    cluster = world()
    honk, clock = make(cluster)
    replace_pod_on_eviction(cluster, clock, "healthy", [0])
    a = honk.propose(restart())
    assert a.proposal_id is not None
    honk.approve(a.proposal_id, approver="imran")
    clock.advance(minutes=3)
    real_read = cluster.read

    def broken(*_args: Any) -> Any:
        raise ConnectionError("apiserver unreachable")
    cluster.read = broken  # type: ignore[method-assign]
    results = honk.run_checks()
    assert [r for _, _, r, _ in results] == [None]
    assert honk.ledger.status(a.proposal_id) is Status.PENDING
    cluster.read = real_read  # type: ignore[method-assign]
    assert [r for _, _, r, _ in honk.run_checks()] == [Result.HEALTHY]


def test_workload_advice_includes_restarting_its_pods_with_the_same_record() -> None:
    cluster = world()
    honk, clock = make(cluster)
    replace_pod_on_eviction(cluster, clock, "crashloop", [0])
    a = honk.propose(restart())
    assert a.proposal_id is not None
    honk.approve(a.proposal_id, approver="imran")
    clock.advance(minutes=3)
    honk.run_checks()
    text = honk.advise("Deployment", "web", "shop")
    assert "restart_pod: tried 1, fixed 0, failed 1" in text


def test_acting_on_a_healthy_target_is_flagged() -> None:
    honk, _ = make(world(state="healthy"))
    a = honk.propose(restart())
    assert a.mode is Mode.ASK and a.warning and "nothing wrong" in a.warning
