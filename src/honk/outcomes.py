"""Did the action work? Three answers, and "can't tell" is one of them.

After an action runs, Honk looks again at fixed checkpoints:

    t+3m   did the target come back healthy?        healthy / failing / unclear
    t+1h   did the problem come back?               held / recurred / missed
    t+24h  did the problem come back?               held / recurred / missed

and folds them into one status:

    pending      the 3-minute check has not run yet
    provisional  healthy at 3 minutes, later checks still to come
    fixed        healthy at 3 minutes and held at every later check
    retracted    healthy at 3 minutes, then the problem came back
    failed       still broken at 3 minutes
    unclear      Honk could not tell (target vanished, someone else changed it,
                 or a checkpoint was missed)

Only `fixed` counts as a success. `unclear` and `retracted` count against an
action exactly as much as `failed`: if "I could not tell" were cheaper than
"it did not work", the system would be rewarded for being hard to verify.

A checkpoint that runs late still uses evidence that cannot have been lost:
restart counts only grow while a pod lives, so a late check can still prove a
recurrence, or prove the pods lived through the window untouched.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from honk.actions import Verifiable
from honk.signature import OK_STATES, FailureState


class Checkpoint(enum.StrEnum):
    T3M = "t3m"
    T1H = "t1h"
    T24H = "t24h"


OFFSETS: Final[Mapping[Checkpoint, timedelta]] = {
    Checkpoint.T3M: timedelta(minutes=3),
    Checkpoint.T1H: timedelta(hours=1),
    Checkpoint.T24H: timedelta(hours=24),
}


def checkpoints_for(verifiable: Verifiable) -> tuple[Checkpoint, ...]:
    if verifiable is Verifiable.EFFECT_ONLY:
        return (Checkpoint.T3M,)
    return (Checkpoint.T3M, Checkpoint.T1H, Checkpoint.T24H)


def grace(checkpoint: Checkpoint) -> timedelta:
    """How late a check may run and still count as on time: 10%, at least a minute."""
    return max(timedelta(minutes=1), OFFSETS[checkpoint] / 10)


class Result(enum.StrEnum):
    HEALTHY = "healthy"
    FAILING = "failing"
    UNCLEAR = "unclear"
    HELD = "held"
    RECURRED = "recurred"
    MISSED = "missed"


class Status(enum.StrEnum):
    PENDING = "pending"
    PROVISIONAL = "provisional"
    FIXED = "fixed"
    RETRACTED = "retracted"
    FAILED = "failed"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class PodObs:
    state: FailureState
    created_at: datetime | None
    restarts: int
    last_restart_at: datetime | None


@dataclass(frozen=True, slots=True)
class Observation:
    """What Honk can see of the target when a checkpoint runs."""

    target_exists: bool
    pods: Sequence[PodObs] = ()
    changed_by_others: bool = False
    effect_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class Check:
    checkpoint: Checkpoint
    result: Result
    detail: str


def _failing(pods: Sequence[PodObs]) -> list[PodObs]:
    return [p for p in pods if p.state not in OK_STATES and p.state is not FailureState.STARTING]


def evaluate_window(
    obs: Observation, *, verifiable: Verifiable, executed_at: datetime, now: datetime
) -> tuple[Result, str]:
    """The 3-minute check."""
    due = executed_at + OFFSETS[Checkpoint.T3M]
    late = now > due + grace(Checkpoint.T3M)

    if not obs.target_exists:
        return Result.UNCLEAR, "the target no longer exists"
    if obs.changed_by_others:
        return Result.UNCLEAR, "someone else changed the target during the window"
    if verifiable is Verifiable.EFFECT_ONLY:
        if obs.effect_ok:
            return Result.HEALTHY, "the change took effect (this action's benefit is not measured)"
        return Result.FAILING, "the change is not in effect"
    if not obs.pods:
        return Result.UNCLEAR, "the target has no pods to judge"

    failing = _failing(obs.pods)
    if failing:
        states = sorted({p.state.value for p in failing})
        return Result.FAILING, f"{len(failing)} of {len(obs.pods)} pods still {', '.join(states)}"
    if any(p.state is FailureState.STARTING for p in obs.pods):
        return Result.UNCLEAR, "pods were still starting when the window closed"
    if late:
        survived = all(
            p.restarts == 0 and p.created_at is not None and p.created_at <= due
            for p in obs.pods
        )
        if not survived:
            return Result.UNCLEAR, "checked late, and the pods' history does not cover the window"
        return Result.HEALTHY, "checked late; every pod has run since before the window closed"
    return Result.HEALTHY, f"all {len(obs.pods)} pods healthy"


def evaluate_recurrence(
    obs: Observation, *, checkpoint: Checkpoint, executed_at: datetime, now: datetime
) -> tuple[Result, str]:
    """A later check: did the problem come back since the action?"""
    due = executed_at + OFFSETS[checkpoint]
    late = now > due + grace(checkpoint)

    if obs.target_exists and obs.pods:
        failing = _failing(obs.pods)
        if failing:
            states = sorted({p.state.value for p in failing})
            return Result.RECURRED, f"{len(failing)} pods {', '.join(states)} again"
        restarted = [
            p for p in obs.pods
            if p.restarts > 0 and p.last_restart_at is not None and p.last_restart_at > executed_at
        ]
        if restarted:
            return Result.RECURRED, f"{len(restarted)} pods restarted after the action"

    if not obs.target_exists or not obs.pods:
        return Result.MISSED, "the target is gone, so recurrence cannot be judged"
    if obs.changed_by_others:
        return Result.MISSED, "someone else changed the target since, so a hold proves nothing"
    if late and not all(p.created_at is not None and p.created_at <= due for p in obs.pods):
        return Result.MISSED, "checked late, and pods were replaced after the checkpoint"
    return Result.HELD, "no recurrence"


def fold(checks: Sequence[Check], *, verifiable: Verifiable) -> Status:
    by_point = {c.checkpoint: c.result for c in checks}
    first = by_point.get(Checkpoint.T3M)
    if first is None:
        return Status.PENDING
    if first is Result.FAILING:
        return Status.FAILED
    if first is Result.UNCLEAR:
        return Status.UNCLEAR
    later = [by_point.get(cp) for cp in checkpoints_for(verifiable)[1:]]
    if Result.RECURRED in later:
        return Status.RETRACTED
    if Result.MISSED in later:
        return Status.UNCLEAR
    if any(r is None for r in later):
        return Status.PROVISIONAL
    return Status.FIXED
