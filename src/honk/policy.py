"""Team policy, the kill switch, and where Honk keeps its state.

Every rule in the policy takes power away, with one exception: `autonomy`
names actions that may run without a human, and even those only run alone in
cells whose own verified record clears the bar.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

import yaml

from honk.actions import CATALOG, ActionType, Limits, Verifiable
from honk.gate import AutonomyRule

SWITCH_ON: Final[str] = "on"

# The least evidence a team may configure before a cell acts alone. Below this
# "earned autonomy" would mean a coin flip with a record attached.
FLOOR: Final[AutonomyRule] = AutonomyRule(
    min_events=3, min_score=0.6, max_unclear_rate=0.5, min_age_days=1.0, min_distinct_days=2,
)

DEFAULT_POLICY: Final[str] = """\
# Honk policy. Every rule here takes power away, except `autonomy`.

# Where a human approves: `client` shows a prompt inside the agent's app when it
# supports one (MCP elicitation), `cli` always requires `honk approve <id>` in a
# terminal. Use `cli` if your agent harness might answer prompts on its own.
approvals: client

deny:
  namespaces: [kube-system]   # Honk refuses any action in these
  actions: []                 # e.g. [cordon_node]

limits:
  max_replicas: 20
  max_scale_step: 5           # largest replica change in one action
  max_resource_factor: 2.0    # largest cpu/memory change in one action, either way

throttle:
  per_target_per_hour: 3      # the same action on the same target

# Actions listed here may run without asking, but only in a cell (kind of
# problem x action) whose own verified record clears every bar below.
# Empty means Honk always asks. Uncomment to opt in:
autonomy: {}
#   restart_pod:
#     min_events: 8
#     min_score: 0.80
#     max_unclear_rate: 0.30
#     min_age_days: 14
#     min_distinct_days: 5
"""


def home() -> Path:
    return Path(os.environ.get("HONK_HOME") or Path.home() / ".honk")


@dataclass(frozen=True, slots=True)
class Policy:
    deny_namespaces: frozenset[str] = frozenset({"kube-system"})
    deny_actions: frozenset[ActionType] = frozenset()
    limits: Limits = field(default_factory=Limits)
    throttle_per_hour: int = 3
    autonomy: Mapping[ActionType, AutonomyRule] = field(default_factory=dict)
    approvals_in_client: bool = True


class PolicyError(ValueError):
    pass


def _rule(action: ActionType, raw: Any) -> AutonomyRule:
    if CATALOG[action].verifiable is not Verifiable.OUTCOME:
        raise PolicyError(f"{action} cannot earn autonomy: Honk can only confirm it took "
                          "effect, not that it helped")
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise PolicyError(f"autonomy.{action} must be a mapping")
    known = {f.name for f in fields(AutonomyRule)}
    unknown = set(raw) - known
    if unknown:
        raise PolicyError(f"autonomy.{action}: unknown keys {sorted(unknown)}")
    rule = AutonomyRule(**{k: type(getattr(AutonomyRule(), k))(v) for k, v in raw.items()})
    for name in ("min_events", "min_score", "min_age_days", "min_distinct_days"):
        if getattr(rule, name) < getattr(FLOOR, name):
            raise PolicyError(f"autonomy.{action}.{name} is below Honk's floor of "
                              f"{getattr(FLOOR, name)}")
    if rule.max_unclear_rate > FLOOR.max_unclear_rate:
        raise PolicyError(f"autonomy.{action}.max_unclear_rate is above Honk's ceiling of "
                          f"{FLOOR.max_unclear_rate}")
    return rule


def _action(name: Any, where: str) -> ActionType:
    try:
        return ActionType(name)
    except ValueError:
        raise PolicyError(f"{where}: unknown action {name!r}") from None


def parse(text: str) -> Policy:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, Mapping):
        raise PolicyError("policy must be a mapping")
    deny = raw.get("deny") or {}
    limits = raw.get("limits") or {}
    throttle = raw.get("throttle") or {}
    autonomy = raw.get("autonomy") or {}
    d = Limits()
    approvals = raw.get("approvals", "client")
    if approvals not in ("client", "cli"):
        raise PolicyError("approvals must be `client` or `cli`")
    return Policy(
        approvals_in_client=approvals == "client",
        deny_namespaces=frozenset(str(n) for n in deny.get("namespaces") or ()),
        deny_actions=frozenset(_action(a, "deny.actions") for a in deny.get("actions") or ()),
        limits=Limits(
            max_replicas=int(limits.get("max_replicas", d.max_replicas)),
            max_scale_step=int(limits.get("max_scale_step", d.max_scale_step)),
            max_resource_factor=float(
                limits.get("max_resource_factor", d.max_resource_factor)),
        ),
        throttle_per_hour=int(throttle.get("per_target_per_hour", 3)),
        autonomy={_action(a, "autonomy"): _rule(_action(a, "autonomy"), r)
                  for a, r in autonomy.items()},
    )


def load(path: Path | None = None) -> Policy:
    target = path or home() / "policy.yaml"
    if not target.exists():
        return parse(DEFAULT_POLICY)
    return parse(target.read_text(encoding="utf-8"))


def switch_path() -> Path:
    return home() / "switch"


def execution_enabled() -> tuple[bool, str]:
    """Fails closed: a missing or unreadable switch means nothing executes."""
    path = switch_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False, "Honk is paused (no switch file; run `honk init` or `honk resume`)"
    if value != SWITCH_ON:
        return False, "Honk is paused (`honk resume` to allow changes again)"
    return True, "on"


def set_switch(on: bool) -> None:
    path = switch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SWITCH_ON if on else "off", encoding="utf-8")
