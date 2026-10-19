from datetime import timedelta

import pytest

from honk.extract import pod_facts
from honk.signature import (
    NA,
    UNKNOWN,
    FailureState,
    PodFacts,
    failure_state,
    sign,
    sign_node,
    worst,
)
from tests.fakes import T0, iso, pod


def facts(**kw: object) -> PodFacts:
    base: dict[str, object] = dict(
        phase="Running", status_reason=None, unschedulable=False, ready=False,
        waiting_reason=None, terminated_reason=None, terminated_exit=None,
        last_terminated_reason=None, last_exit=None, restart_count=0, running_seconds=None)
    base.update(kw)
    return PodFacts(**base)  # type: ignore[arg-type]


def test_crash_loop_is_one_state_across_the_restart_cycle() -> None:
    """A point read lands in any phase of the cycle; all must give the same cell."""
    waiting = facts(waiting_reason="CrashLoopBackOff", last_terminated_reason="Error",
                    last_exit=1, restart_count=4)
    terminated = facts(terminated_reason="Error", terminated_exit=1, restart_count=4)
    just_restarted = facts(running_seconds=5.0, last_terminated_reason="Error", last_exit=1,
                           restart_count=4)
    states = {failure_state(f) for f in (waiting, terminated, just_restarted)}
    assert states == {(FailureState.CRASH_LOOP, "app_error")}


def test_oom_is_separate_from_crash_loop_in_every_phase() -> None:
    waiting = facts(waiting_reason="CrashLoopBackOff", last_terminated_reason="OOMKilled",
                    last_exit=137, restart_count=2)
    terminated = facts(terminated_reason="OOMKilled", terminated_exit=137, restart_count=2)
    assert failure_state(waiting) == failure_state(terminated) == (FailureState.OOM_KILLED, NA)


def test_sigkill_without_oom_reason_is_not_claimed_as_oom() -> None:
    f = facts(waiting_reason="CrashLoopBackOff", last_terminated_reason="Error", last_exit=137,
              restart_count=3)
    assert failure_state(f) == (FailureState.CRASH_LOOP, "sigkill")


def test_a_long_running_pod_with_old_restarts_is_healthy() -> None:
    f = facts(ready=True, running_seconds=7200.0, last_terminated_reason="Error", last_exit=1,
              restart_count=3)
    assert failure_state(f) == (FailureState.HEALTHY, NA)


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName"])
def test_image_problems(reason: str) -> None:
    assert failure_state(facts(phase="Pending", waiting_reason=reason))[0] is \
        FailureState.IMAGE_PULL


def test_unrecognised_reasons_never_reach_the_hash_raw() -> None:
    """1000 attacker-chosen reasons must collapse into one cell."""
    digests = {
        sign(owner_kind="Deployment",
             state=failure_state(facts(waiting_reason=f"Evil{i}")),
             node_unhealthy=False).digest
        for i in range(1000)
    }
    assert len(digests) == 1
    assert failure_state(facts(waiting_reason="Evil"))[0] is FailureState.UNKNOWN


def test_forged_owner_kind_becomes_unknown() -> None:
    assert sign(owner_kind="Totally-Legit", state=(FailureState.HEALTHY, NA),
                node_unhealthy=False).owner_kind == UNKNOWN


def test_extract_reads_a_real_shaped_crash_looping_pod() -> None:
    assert failure_state(pod_facts(pod(state="crashloop"), now=T0)) == (
        FailureState.CRASH_LOOP, "app_error")
    assert failure_state(pod_facts(pod(state="oom"), now=T0))[0] is FailureState.OOM_KILLED
    assert failure_state(pod_facts(pod(state="healthy"), now=T0))[0] is FailureState.HEALTHY


def test_failing_init_container_speaks_for_the_pod() -> None:
    p = pod(state="healthy")
    p["status"]["phase"] = "Pending"
    p["status"]["initContainerStatuses"] = [{
        "name": "init", "ready": False, "restartCount": 3,
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "lastState": {"terminated": {"reason": "Error", "exitCode": 2,
                                     "finishedAt": iso(T0 - timedelta(seconds=5))}}}]
    assert failure_state(pod_facts(p, now=T0)) == (FailureState.CRASH_LOOP, "app_error")


def test_worst_is_order_independent() -> None:
    states = [(FailureState.HEALTHY, NA), (FailureState.CRASH_LOOP, "app_error"),
              (FailureState.STARTING, NA)]
    assert worst(states) == worst(list(reversed(states))) == (FailureState.CRASH_LOOP,
                                                              "app_error")
    assert worst([]) == (FailureState.NO_PODS, NA)


def test_node_signatures() -> None:
    assert sign_node(["Ready"]).failure_state == "node_not_ready"
    assert sign_node(["MemoryPressure"]).failure_state == "node_pressure"
    assert sign_node([]).failure_state == "node_healthy"
    assert not sign_node([]).node_unhealthy
