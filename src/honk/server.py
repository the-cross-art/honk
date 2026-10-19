"""Honk as an MCP server: the write path an agent uses instead of raw kubectl.

Approval happens outside the agent. When the client supports elicitation, the
human answers a prompt in the client UI, on either protocol era (mid-call request
before 2026-07-28, input-required round trip after); otherwise, or when the policy
says `approvals: cli`, the agent is told to ask the human to run `honk approve`.
No tool lets the agent approve its own proposal.
"""

# No `from __future__ import annotations`: tool signatures reference resolvers
# defined inside `build`, which the SDK must resolve at registration time.

import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

import anyio
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from pydantic import BaseModel, Field

from honk.actions import ActionType, Proposal
from honk.core import Honk, Refusal
from honk.gate import Mode

CHECK_INTERVAL_SECONDS = 30.0
T = TypeVar("T")

INSTRUCTIONS = """\
Honk guards changes to this Kubernetes cluster. Use Honk's tools for every change
instead of kubectl. Honk dry-runs each change, shows a human what this action has
done for this kind of problem before, and records whether it actually worked.
Call `honk_advise` before acting to see what has and has not worked here. If Honk
refuses or warns that an action has not helped before, do not retry it: tell the
human what you found."""


class Approval(BaseModel):
    approve: bool = Field(description="Run this change now?")
    note: str = Field(default="", description="Optional note for the record")


class NoPrompt(BaseModel):
    """A resolver's "do not ask in the client" outcome."""

    why: str


Answer = ElicitationResult[Approval]


class Runner:
    """Serialises access to Honk: the kube client and the ledger are not async."""

    def __init__(self, honk: Honk) -> None:
        self.honk = honk
        self._lock = threading.Lock()

    async def run(self, fn: Callable[[], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn()
        return await anyio.to_thread.run_sync(locked)


def _client_name(ctx: Any) -> str:
    try:
        info = ctx.session.client_params.client_info
        return f"{info.name}"
    except AttributeError:
        return "mcp-client"


def _supports_elicitation(ctx: Any) -> bool:
    caps = getattr(ctx, "client_capabilities", None)
    return bool(caps is not None and getattr(caps, "elicitation", None) is not None)


def build(honk: Honk, *, approvals_in_client: bool = True) -> MCPServer:
    runner = Runner(honk)

    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            async def checks() -> None:
                while True:
                    try:
                        for pid, cp, result, detail in await runner.run(honk.run_checks):
                            if result is None:
                                print(f"honk: check {pid} {cp} failed, will retry: {detail}",
                                      file=sys.stderr, flush=True)
                    except Exception as exc:
                        # stdout carries the MCP protocol, so failures go to stderr.
                        print(f"honk: outcome check failed, will retry: {exc!r}",
                              file=sys.stderr, flush=True)
                    await anyio.sleep(CHECK_INTERVAL_SECONDS)
            tg.start_soon(checks)
            yield None
            tg.cancel_scope.cancel()

    server = MCPServer("honk", instructions=INSTRUCTIONS, lifespan=lifespan)

    async def ask(ctx: Context, p: Proposal) -> Elicit[Approval] | NoPrompt:
        """Resolver: build the question from a read-only preview. The SDK reuses an
        answer only for the identical question, so a changed problem or record is
        asked afresh rather than approved on stale information."""
        if not approvals_in_client:
            return NoPrompt(why="policy says approvals: cli")
        if not _supports_elicitation(ctx):
            print(f"honk: approval goes to the terminal: the client did not declare the "
                  f"elicitation capability ({ctx.client_capabilities!r})",
                  file=sys.stderr, flush=True)
            return NoPrompt(why="client cannot show prompts")
        preview = await runner.run(lambda: honk.preview(p))
        if preview.mode is not Mode.ASK:
            return NoPrompt(why=preview.mode.value)
        return Elicit("Honk: an agent wants to make a change.\n\n" + preview.render(), Approval)

    async def submit(ctx: Context, p: Proposal, answer: Any) -> str:
        verdict: bool | None = None
        asked = not (isinstance(answer, AcceptedElicitation) and isinstance(answer.data, NoPrompt))
        if isinstance(answer, AcceptedElicitation) and isinstance(answer.data, Approval):
            verdict = answer.data.approve
        elif isinstance(answer, DeclinedElicitation):
            verdict = False
        note = answer.data.note if verdict is not None and isinstance(
            answer, AcceptedElicitation) else ""

        assessment = await runner.run(lambda: honk.propose(p))
        if assessment.mode is not Mode.ASK or assessment.proposal_id is None:
            return assessment.render()
        pid = assessment.proposal_id
        if not asked:
            return assessment.render() + (
                "\nAsk the human to approve or deny it in a terminal, then call "
                f"`honk_status` with proposal_id={pid}.")
        if verdict is None:
            return assessment.render() + "\nThe prompt was dismissed; it is still pending."
        who = f"{_client_name(ctx)} (in-client prompt)"
        try:
            if verdict:
                return (await runner.run(
                    lambda: honk.approve(pid, approver=who, note=note))).render()
            denied = await runner.run(lambda: honk.deny(pid, approver=who, note=note))
            return denied.render() + "\nThe human said no. Do not retry this action."
        except Refusal as refusal:
            return f"{refusal}. Call `honk_status` with proposal_id={pid} for the outcome."

    def proposal(action: ActionType, kind: str, name: str, namespace: str | None,
                 reason: str, ctx: Context, **params: Any) -> Proposal:
        return Proposal(action, kind, name, namespace, params, reason, _client_name(ctx))

    async def ask_restart_pod(namespace: str, pod: str, reason: str,
                              ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.RESTART_POD, "Pod", pod, namespace, reason,
                                       ctx))

    async def ask_rollout_restart(namespace: str, kind: str, name: str, reason: str,
                                  ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.ROLLOUT_RESTART, kind, name, namespace,
                                       reason, ctx))

    async def ask_scale(namespace: str, kind: str, name: str, replicas: int, reason: str,
                        ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.SCALE, kind, name, namespace, reason, ctx,
                                       replicas=replicas))

    async def ask_set_resources(namespace: str, kind: str, name: str, container: str,
                                resources: dict[str, str], reason: str,
                                ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.SET_RESOURCES, kind, name, namespace, reason,
                                       ctx, container=container, resources=resources))

    async def ask_cordon(node: str, reason: str, ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.CORDON_NODE, "Node", node, None, reason, ctx))

    async def ask_uncordon(node: str, reason: str,
                           ctx: Context) -> Elicit[Approval] | NoPrompt:
        return await ask(ctx, proposal(ActionType.UNCORDON_NODE, "Node", node, None, reason,
                                       ctx))

    @server.tool(description="Evict one pod so its controller recreates it. Respects "
                 "PodDisruptionBudgets. Refused for pods with no controller.")
    async def restart_pod(namespace: str, pod: str, reason: str, ctx: Context,
                          answer: Annotated[Answer, Resolve(ask_restart_pod)]) -> str:
        return await submit(ctx, proposal(ActionType.RESTART_POD, "Pod", pod, namespace,
                                          reason, ctx), answer)

    @server.tool(description="Rolling restart of a Deployment, StatefulSet or DaemonSet.")
    async def rollout_restart(namespace: str, kind: str, name: str, reason: str, ctx: Context,
                              answer: Annotated[Answer, Resolve(ask_rollout_restart)]) -> str:
        return await submit(ctx, proposal(ActionType.ROLLOUT_RESTART, kind, name, namespace,
                                          reason, ctx), answer)

    @server.tool(description="Set the replica count of a Deployment or StatefulSet.")
    async def scale(namespace: str, kind: str, name: str, replicas: int, reason: str,
                    ctx: Context, answer: Annotated[Answer, Resolve(ask_scale)]) -> str:
        return await submit(ctx, proposal(ActionType.SCALE, kind, name, namespace, reason,
                                          ctx, replicas=replicas), answer)

    @server.tool(description="Change one container's cpu/memory requests or limits on a "
                 "Deployment or StatefulSet. Keys: limits.cpu, limits.memory, requests.cpu, "
                 "requests.memory. Example: {\"limits.memory\": \"512Mi\"}.")
    async def set_resources(namespace: str, kind: str, name: str, container: str,
                            resources: dict[str, str], reason: str, ctx: Context,
                            answer: Annotated[Answer, Resolve(ask_set_resources)]) -> str:
        return await submit(ctx, proposal(ActionType.SET_RESOURCES, kind, name, namespace,
                                          reason, ctx, container=container,
                                          resources=resources), answer)

    @server.tool(description="Mark a node unschedulable.")
    async def cordon_node(node: str, reason: str, ctx: Context,
                          answer: Annotated[Answer, Resolve(ask_cordon)]) -> str:
        return await submit(ctx, proposal(ActionType.CORDON_NODE, "Node", node, None, reason,
                                          ctx), answer)

    @server.tool(description="Mark a node schedulable again.")
    async def uncordon_node(node: str, reason: str, ctx: Context,
                            answer: Annotated[Answer, Resolve(ask_uncordon)]) -> str:
        return await submit(ctx, proposal(ActionType.UNCORDON_NODE, "Node", node, None,
                                          reason, ctx), answer)

    @server.tool(description="Read-only. What problem Honk sees on a target (Pod, "
                 "Deployment, StatefulSet, DaemonSet or Node) and what each action has done "
                 "for that problem on this cluster before. Call this before acting.")
    async def honk_advise(kind: str, name: str, namespace: str | None = None) -> str:
        return await runner.run(lambda: honk.advise(kind, name, namespace))

    @server.tool(description="Read-only. Status of a proposal: waiting for approval, "
                 "denied, ran, and whether it worked.")
    async def honk_status(proposal_id: str) -> str:
        return await runner.run(lambda: status_text(honk, proposal_id))

    return server


def status_text(honk: Honk, pid: str) -> str:
    p = honk.ledger.proposal(pid)
    if p is None:
        return f"No proposal {pid}."
    head = f"{p.action} on {p.kind} {p.name}"
    if p.mode == Mode.REFUSE.value:
        return f"{head}: refused ({p.decision_detail})"
    verdict = honk.ledger.approval(pid)
    if not honk.ledger.executed(pid):
        if verdict == "denied":
            return f"{head}: denied by a human. Do not retry it."
        if verdict == "approved":
            return f"{head}: approved but did not run."
        return f"{head}: waiting for a human to approve."
    status = honk.ledger.status(pid)
    if status is None:
        return f"{head}: tried to run and failed."
    checks = "; ".join(f"{c.checkpoint}: {c.result} ({c.detail})"
                       for c in honk.ledger.checks(pid))
    return f"{head}: ran. Outcome so far: {status}." + (f" Checks: {checks}" if checks else "")

