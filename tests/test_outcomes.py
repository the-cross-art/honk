import itertools
from datetime import timedelta

from honk.actions import Verifiable
from honk.outcomes import (
    Check,
    Checkpoint,
    Observation,
    PodObs,
    Result,
    Status,
    evaluate_recurrence,
    evaluate_window,
    fold,
)
from honk.signature import FailureState
from tests.fakes import T0

EXEC = T0
HEALTHY = PodObs(FailureState.HEALTHY, EXEC + timedelta(seconds=10), 0, None)
LOOPING = PodObs(FailureState.CRASH_LOOP, EXEC + timedelta(seconds=10), 3,
                 EXEC + timedelta(minutes=2))


def window(obs: Observation, minutes: float = 3) -> Result:
    return evaluate_window(obs, verifiable=Verifiable.OUTCOME, executed_at=EXEC,
                           now=EXEC + timedelta(minutes=minutes))[0]


def test_window_basics() -> None:
    assert window(Observation(True, [HEALTHY])) is Result.HEALTHY
    assert window(Observation(True, [HEALTHY, LOOPING])) is Result.FAILING
    assert window(Observation(False)) is Result.UNCLEAR
    assert window(Observation(True, [])) is Result.UNCLEAR
    assert window(Observation(True, [HEALTHY], changed_by_others=True)) is Result.UNCLEAR
    starting = PodObs(FailureState.STARTING, EXEC, 0, None)
    assert window(Observation(True, [starting])) is Result.UNCLEAR


def test_late_window_only_credits_pods_that_lived_through_it() -> None:
    assert window(Observation(True, [HEALTHY]), minutes=60) is Result.HEALTHY
    replaced = PodObs(FailureState.HEALTHY, EXEC + timedelta(minutes=30), 0, None)
    assert window(Observation(True, [replaced]), minutes=60) is Result.UNCLEAR
    restarted = PodObs(FailureState.HEALTHY, EXEC, 1, EXEC + timedelta(minutes=1))
    assert window(Observation(True, [restarted]), minutes=60) is Result.UNCLEAR


def test_recurrence_is_seen_even_when_checked_late() -> None:
    later = PodObs(FailureState.HEALTHY, EXEC, 1, EXEC + timedelta(minutes=40))
    for minutes in (60, 600):
        r, _ = evaluate_recurrence(Observation(True, [later]), checkpoint=Checkpoint.T1H,
                                   executed_at=EXEC, now=EXEC + timedelta(minutes=minutes))
        assert r is Result.RECURRED


def test_recurrence_held_and_missed() -> None:
    r, _ = evaluate_recurrence(Observation(True, [HEALTHY]), checkpoint=Checkpoint.T1H,
                               executed_at=EXEC, now=EXEC + timedelta(hours=1))
    assert r is Result.HELD
    r, _ = evaluate_recurrence(Observation(False), checkpoint=Checkpoint.T1H,
                               executed_at=EXEC, now=EXEC + timedelta(hours=1))
    assert r is Result.MISSED


def test_nothing_but_a_clean_run_is_ever_fixed() -> None:
    """Property: FIXED needs healthy at 3m AND held at every later checkpoint."""
    later_results = [None, Result.HELD, Result.RECURRED, Result.MISSED]
    for first in [None, Result.HEALTHY, Result.FAILING, Result.UNCLEAR]:
        for t1h, t24h in itertools.product(later_results, repeat=2):
            checks = [Check(cp, r, "") for cp, r in
                      ((Checkpoint.T3M, first), (Checkpoint.T1H, t1h), (Checkpoint.T24H, t24h))
                      if r is not None]
            status = fold(checks, verifiable=Verifiable.OUTCOME)
            clean = first is Result.HEALTHY and t1h is Result.HELD and t24h is Result.HELD
            assert (status is Status.FIXED) == clean, (first, t1h, t24h, status)
            if first is Result.HEALTHY and Result.RECURRED in (t1h, t24h):
                assert status is Status.RETRACTED


def test_effect_only_actions_fold_on_one_check() -> None:
    assert fold([Check(Checkpoint.T3M, Result.HEALTHY, "")],
                verifiable=Verifiable.EFFECT_ONLY) is Status.FIXED
