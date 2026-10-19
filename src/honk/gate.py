"""Ask a human, or act alone? Decided per (signature, action) cell from its record.

Every cell starts by asking. A team may opt an action into autonomy in its
policy, and even then a cell acts alone only after its own verified record
clears every bar. The record is recomputed from the ledger on every call, so no
stored counter can drift from the evidence.

Score: Dirichlet(1,1,1) over (fixed, failed, unclear+retracted), i.e.
`(1 + fixed) / (3 + evidence)`. A cell with no history scores 1/3. With the
default bar a perfect 8-for-8 record scores 9/11 = 0.818, so the default 0.80
threshold is reachable by a clean record and rejects 7 fixed + 1 failed
(8/11 = 0.727).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

PRIOR: Final[float] = 1.0


class Mode(enum.StrEnum):
    REFUSE = "refuse"
    ASK = "ask"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class TrackRecord:
    fixed: int = 0
    provisional: int = 0
    retracted: int = 0
    failed: int = 0
    unclear: int = 0
    pending: int = 0
    denied: int = 0
    age_days: float = 0.0
    distinct_days: int = 0

    @property
    def evidence(self) -> int:
        """Finished outcomes. Provisional and pending ones are not evidence yet."""
        return self.fixed + self.failed + self.unclear + self.retracted

    @property
    def tried(self) -> int:
        return self.evidence + self.provisional + self.pending

    @property
    def score(self) -> float:
        return (PRIOR + self.fixed) / (3 * PRIOR + self.evidence)

    @property
    def unclear_rate(self) -> float:
        return (self.unclear + self.retracted) / self.evidence if self.evidence else 0.0

    def sentence(self) -> str:
        if self.tried == 0 and self.denied == 0:
            return "no history: this has never been tried here"
        parts = [f"tried {self.tried}", f"fixed {self.fixed}"]
        for label, n in (("provisional", self.provisional), ("retracted", self.retracted),
                         ("failed", self.failed), ("unclear", self.unclear),
                         ("pending", self.pending)):
            if n:
                parts.append(f"{label} {n}")
        if self.denied:
            parts.append(f"denied by a human {self.denied}")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class AutonomyRule:
    min_events: int = 8
    min_score: float = 0.80
    max_unclear_rate: float = 0.30
    min_age_days: float = 14.0
    min_distinct_days: int = 5


@dataclass(frozen=True, slots=True)
class Decision:
    mode: Mode
    reason: str
    detail: str


def unmet(record: TrackRecord, rule: AutonomyRule) -> list[str]:
    gaps: list[str] = []
    if record.evidence < rule.min_events:
        gaps.append(f"outcomes {record.evidence}/{rule.min_events}")
    if record.age_days < rule.min_age_days:
        gaps.append(f"age {record.age_days:.1f}/{rule.min_age_days:g} days")
    if record.distinct_days < rule.min_distinct_days:
        gaps.append(f"distinct days {record.distinct_days}/{rule.min_distinct_days}")
    if record.unclear_rate > rule.max_unclear_rate:
        gaps.append(f"unclear {record.unclear_rate:.0%} > {rule.max_unclear_rate:.0%}")
    if record.score < rule.min_score:
        gaps.append(f"score {record.score:.2f} < {rule.min_score:.2f}")
    return gaps


def decide(
    record: TrackRecord, *, rule: AutonomyRule | None, breaker_open: bool
) -> Decision:
    if rule is None:
        return Decision(Mode.ASK, "no_autonomy", "policy does not allow this action to run alone")
    if breaker_open:
        return Decision(Mode.ASK, "breaker_open",
                        "recent actions failed, so nothing runs alone until a human resets it")
    gaps = unmet(record, rule)
    if gaps:
        return Decision(Mode.ASK, "not_earned", "has not earned autonomy: " + ", ".join(gaps))
    return Decision(Mode.AUTO, "earned",
                    f"earned autonomy: score {record.score:.2f} over {record.evidence} outcomes "
                    f"on {record.distinct_days} days")


def warning(record: TrackRecord) -> str | None:
    """A plain warning when the record says this is unlikely to help."""
    done = record.evidence
    if done >= 2 and record.fixed == 0:
        return f"this has not fixed this kind of problem in {done} tries here"
    if record.retracted >= 2 and record.retracted >= record.fixed:
        return "this has looked fixed before and then come back more often than it held"
    return None
