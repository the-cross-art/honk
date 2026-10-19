"""UID-verified owner-reference traversal.

An ``ownerReferences`` entry is **workload-writable**. Anyone who can create a
pod can claim any owner they like: kind, name, and a UID they invented. So the
chain cannot be trusted as it arrives -- it has to be *verified*, hop by hop, by
fetching the claimed parent and checking that the UID the child asserts is the
UID the parent actually has.

Two downstream consumers depend
on the chain:

1. **The signature** takes ``owner_kind`` from the root of the chain. A forged
   root lets a workload choose which cell its incidents land in -- and therefore
   which cell's accumulated autonomy applies to it.
2. **Target selection** walks the chain to decide *what to act on*. A forged
   chain is a confused-deputy path: the attacker names a victim and the agent,
   holding real RBAC, acts on it.

An unverifiable chain is not an error and not a gap in the vocabulary -- it is
its own distinct, reportable state. It gets its own cell and earns its own
graduation from zero, like anything else.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

# A pod that genuinely has no owner is a normal, meaningful case. A pod whose
# claimed owner cannot be verified is a different case, and the two must not be
# conflated -- one is a fact about the cluster, the other is a possible attack.
MAX_CHAIN_DEPTH = 4


class ChainStatus(enum.StrEnum):
    VERIFIED = "verified"          # every hop fetched and UID-matched
    BARE = "bare"                  # no ownerReferences at all; legitimate
    UNVERIFIABLE = "unverifiable"  # a hop was missing, mismatched, or cyclic


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """A claimed owner reference, exactly as it arrived. Untrusted."""

    kind: str
    name: str
    uid: str
    api_version: str = "v1"
    controller: bool = False


@dataclass(frozen=True, slots=True)
class Hop:
    """A verified step in the chain."""

    kind: str
    name: str
    uid: str


@dataclass(frozen=True, slots=True)
class OwnerChain:
    status: ChainStatus
    hops: tuple[Hop, ...]
    reason: str | None = None

    @property
    def root_kind(self) -> str | None:
        """The kind the signature should use.

        ``None`` means "no verified owner" -- which the signature layer maps to
        its bare-pod sentinel. An unverifiable chain also yields ``None``,
        because an unverified claim must never be allowed to pick a cell.
        """
        if self.status is not ChainStatus.VERIFIED or not self.hops:
            return None
        return self.hops[-1].kind

    @property
    def act_target(self) -> Hop | None:
        """The object an action should be aimed at, or ``None`` if unsafe.

        Deliberately refuses to answer for anything but a fully verified chain.
        A caller that wants to act on an unverified target has to say so in its
        own code rather than receive one from here by default.
        """
        if self.status is not ChainStatus.VERIFIED or not self.hops:
            return None
        return self.hops[-1]


class OwnerResolver(Protocol):
    """Fetches an object's real UID, or ``None`` if it does not exist.

    A Protocol rather than a concrete client so the traversal is testable
    without a cluster -- which matters, because the cases worth testing are the
    hostile ones that are awkward to stage for real.
    """

    def lookup(
        self, *, kind: str, name: str, namespace: str | None
    ) -> tuple[str, tuple[OwnerRef, ...]] | None:
        """Return ``(uid, owner_refs)`` for the named object, or ``None``."""
        ...


def _controller_ref(refs: tuple[OwnerRef, ...]) -> OwnerRef | None:
    """Pick the controller reference.

    Kubernetes permits many ownerReferences but at most one with
    ``controller: true``. We follow only the controller: the others are
    decorative and following them would multiply the attack surface for nothing.
    If several claim it -- which the API server should prevent, so it signals
    either a bug or a hand-crafted object -- we refuse to guess.
    """
    controllers = [r for r in refs if r.controller]
    if len(controllers) == 1:
        return controllers[0]
    if len(controllers) > 1:
        return None
    return None


def resolve_chain(
    *,
    pod_owner_refs: tuple[OwnerRef, ...],
    namespace: str | None,
    resolver: OwnerResolver,
) -> OwnerChain:
    """Walk and verify the ownership chain from a pod upward."""
    if not pod_owner_refs:
        return OwnerChain(status=ChainStatus.BARE, hops=())

    controller = _controller_ref(pod_owner_refs)
    if controller is None:
        multiple = sum(1 for r in pod_owner_refs if r.controller) > 1
        return OwnerChain(
            status=ChainStatus.UNVERIFIABLE,
            hops=(),
            reason="multiple controller refs" if multiple else "no controller ref",
        )

    hops: list[Hop] = []
    seen_uids: set[str] = set()
    current: OwnerRef | None = controller

    while current is not None:
        if len(hops) >= MAX_CHAIN_DEPTH:
            return OwnerChain(
                status=ChainStatus.UNVERIFIABLE,
                hops=tuple(hops),
                reason=f"chain deeper than {MAX_CHAIN_DEPTH} hops",
            )

        found = resolver.lookup(kind=current.kind, name=current.name, namespace=namespace)
        if found is None:
            return OwnerChain(
                status=ChainStatus.UNVERIFIABLE,
                hops=tuple(hops),
                reason=f"claimed owner {current.kind}/{current.name} does not exist",
            )

        actual_uid, parent_refs = found
        if actual_uid != current.uid:
            # The forgery case: the child names a real object but asserts the
            # wrong UID, which is what a stale or hand-crafted reference looks
            # like. Refusing here is the whole point of the module.
            return OwnerChain(
                status=ChainStatus.UNVERIFIABLE,
                hops=tuple(hops),
                reason=(
                    f"UID mismatch on {current.kind}/{current.name}: "
                    f"claimed {current.uid}, actual {actual_uid}"
                ),
            )

        if actual_uid in seen_uids:
            return OwnerChain(
                status=ChainStatus.UNVERIFIABLE,
                hops=tuple(hops),
                reason="ownership cycle",
            )

        seen_uids.add(actual_uid)
        hops.append(Hop(kind=current.kind, name=current.name, uid=actual_uid))
        current = _controller_ref(parent_refs) if parent_refs else None

    return OwnerChain(status=ChainStatus.VERIFIED, hops=tuple(hops))
