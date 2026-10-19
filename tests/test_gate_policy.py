import itertools

import pytest

from honk.actions import ActionType
from honk.gate import AutonomyRule, Mode, TrackRecord, decide, warning
from honk.policy import DEFAULT_POLICY, PolicyError, parse


def test_score_derivation_written_in_gate_docstring() -> None:
    assert TrackRecord().score == pytest.approx(1 / 3)
    assert TrackRecord(fixed=8).score == pytest.approx(9 / 11)
    assert TrackRecord(fixed=7, failed=1).score == pytest.approx(8 / 11)
    rule = AutonomyRule()
    assert TrackRecord(fixed=8).score >= rule.min_score > TrackRecord(fixed=7, failed=1).score


def test_unclear_costs_exactly_what_failure_costs() -> None:
    for n in range(10):
        assert TrackRecord(fixed=n, failed=1).score == TrackRecord(fixed=n, unclear=1).score
        assert TrackRecord(fixed=n, failed=1).score == TrackRecord(fixed=n, retracted=1).score


def test_provisional_outcomes_are_not_evidence() -> None:
    assert TrackRecord(provisional=50).evidence == 0


def test_auto_requires_every_bar() -> None:
    """Swept, not enumerated: AUTO only when the policy opts in and every bar clears."""
    rule = AutonomyRule()
    for fixed, failed, unclear, age, days, breaker, opted in itertools.product(
            [0, 7, 8, 20], [0, 1, 3], [0, 4], [0.0, 13.9, 14.0], [4, 5], [False, True],
            [False, True]):
        record = TrackRecord(fixed=fixed, failed=failed, unclear=unclear, age_days=age,
                             distinct_days=days)
        mode = decide(record, rule=rule if opted else None, breaker_open=breaker).mode
        earned = (opted and not breaker and record.evidence >= 8 and age >= 14
                  and days >= 5 and record.unclear_rate <= 0.3 and record.score >= 0.8)
        assert (mode is Mode.AUTO) == earned
        assert mode is not Mode.REFUSE


def test_warning_when_an_action_never_helps() -> None:
    assert warning(TrackRecord(failed=4)) is not None
    assert warning(TrackRecord(fixed=3, failed=1)) is None
    assert warning(TrackRecord()) is None


def test_default_policy_asks_for_everything() -> None:
    policy = parse(DEFAULT_POLICY)
    assert policy.autonomy == {}
    assert "kube-system" in policy.deny_namespaces


def test_policy_cannot_grant_autonomy_to_unmeasurable_actions() -> None:
    with pytest.raises(PolicyError, match="cannot earn autonomy"):
        parse("autonomy:\n  cordon_node: {}\n")


def test_policy_cannot_set_the_bar_below_the_floor() -> None:
    with pytest.raises(PolicyError, match="floor"):
        parse("autonomy:\n  restart_pod:\n    min_events: 1\n")
    with pytest.raises(PolicyError, match="ceiling"):
        parse("autonomy:\n  restart_pod:\n    max_unclear_rate: 0.9\n")
    with pytest.raises(PolicyError, match="unknown"):
        parse("autonomy:\n  restart_pod:\n    trust_me: true\n")
    with pytest.raises(PolicyError, match="unknown action"):
        parse("deny:\n  actions: [rm_rf]\n")


def test_opt_in_uses_defaults() -> None:
    policy = parse("autonomy:\n  restart_pod:\n")
    assert policy.autonomy[ActionType.RESTART_POD] == AutonomyRule()
