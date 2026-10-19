from typing import Any

import pytest
from mcp import types
from mcp.client import Client

from honk.core import Honk
from honk.ledger import Ledger
from honk.policy import parse
from honk.server import build
from tests.fakes import Clock, FakeCluster, deployment, node, pod, replicaset

pytestmark = pytest.mark.anyio

ARGS = {"namespace": "shop", "pod": "web-abc", "reason": "crash looping"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def honk() -> tuple[Honk, FakeCluster]:
    cluster = FakeCluster([deployment(), replicaset(), node(), pod(state="crashloop")])
    h = Honk(cluster=cluster, ledger=Ledger(":memory:"), policy=parse(""), clock=Clock(),
             switch=lambda: (True, "on"))
    return h, cluster


def answering(approve: bool | None) -> Any:
    async def callback(_ctx: Any, params: Any) -> types.ElicitResult:
        answering.seen.append(params.message)
        if approve is None:
            return types.ElicitResult(action="cancel")
        return types.ElicitResult(action="accept", content={"approve": approve, "note": "t"})
    return callback


answering.seen = []  # type: ignore[attr-defined]


def text(result: types.CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def test_no_tool_can_approve_or_unpause() -> None:
    h, _ = honk()
    async with Client(build(h)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"restart_pod", "rollout_restart", "scale", "set_resources", "cordon_node",
                     "uncordon_node", "honk_advise", "honk_status"}
    assert not any(word in n for n in names for word in ("approve", "resume", "policy"))


async def test_human_approves_in_client_and_the_change_runs() -> None:
    h, cluster = honk()
    async with Client(build(h), mode="legacy", elicitation_callback=answering(True)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
    assert "Approved and ran restart_pod" in out
    assert any(persist for _, persist in cluster.sent)
    assert "Record for this action" in answering.seen[-1]  # type: ignore[attr-defined]


async def test_human_denies_in_client_and_nothing_runs() -> None:
    h, cluster = honk()
    async with Client(build(h), mode="legacy", elicitation_callback=answering(False)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
    assert "Denied" in out and "Do not retry" in out
    assert not any(persist for _, persist in cluster.sent)


async def test_dismissed_prompt_leaves_it_pending() -> None:
    h, cluster = honk()
    async with Client(build(h), mode="legacy", elicitation_callback=answering(None)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
    assert "still pending" in out
    assert len(h.ledger.pending_approvals()) == 1
    assert not any(persist for _, persist in cluster.sent)


async def test_cli_approvals_never_prompt_in_the_client() -> None:
    h, cluster = honk()
    async with Client(build(h, approvals_in_client=False),
                      elicitation_callback=answering(True)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
        pid = h.ledger.pending_approvals()[0].id
        status = text(await client.call_tool("honk_status", {"proposal_id": pid}))
    assert f"honk approve {pid}" in out
    assert "waiting for a human" in status
    assert not any(persist for _, persist in cluster.sent)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_approval_in_client_on_both_protocol_eras(mode: str) -> None:
    """legacy: mid-call request. auto: the 2026-07-28 input-required round trip,
    which re-runs the resolver, so the preview must record nothing."""
    h, cluster = honk()
    async with Client(build(h), mode=mode, elicitation_callback=answering(True)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
        if mode == "auto":
            assert client.protocol_version >= "2026-07-28"
    assert "Approved and ran restart_pod" in out
    assert sum(1 for _, persist in cluster.sent if persist) == 1
    rows = h.ledger._conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    assert rows == 1


async def test_a_client_that_cannot_prompt_falls_back_to_the_terminal() -> None:
    h, cluster = honk()
    async with Client(build(h)) as client:
        out = text(await client.call_tool("restart_pod", ARGS))
    assert "honk approve" in out
    assert len(h.ledger.pending_approvals()) == 1
    assert not any(persist for _, persist in cluster.sent)


async def test_refused_changes_never_prompt() -> None:
    h, cluster = honk()
    async with Client(build(h), mode="auto", elicitation_callback=answering(True)) as client:
        before = len(answering.seen)  # type: ignore[attr-defined]
        out = text(await client.call_tool("scale", {
            "namespace": "shop", "kind": "Deployment", "name": "web", "replicas": 99,
            "reason": "more"}))
    assert "refused" in out
    assert len(answering.seen) == before  # type: ignore[attr-defined]
    assert not any(persist for _, persist in cluster.sent)
