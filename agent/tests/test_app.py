"""The HTTP API: auth, the contract at the edge, idempotency on invocation_id."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from tests.conftest import make_request, text_reply, tool_call
from upgrade_agent.app import create_app

VERDICT = tool_call("submit_verdict", diagnosis="transient", confidence="observed", verdict="retry_gate")


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent.test")


def _body(**overrides) -> dict:
    return make_request(**overrides).model_dump(mode="json")


async def test_invoke_returns_the_verdict(model, settings):
    model.script = [VERDICT]
    async with _client(create_app(settings)) as client:
        response = await client.post("/v1/invoke", json=_body())
    assert response.status_code == 200
    assert response.json()["verdict"] == "retry_gate"


async def test_a_retry_with_the_same_invocation_id_gets_the_same_verdict_without_a_second_run(model, settings):
    model.script = [VERDICT]
    async with _client(create_app(settings)) as client:
        first = await client.post("/v1/invoke", json=_body())
        second = await client.post("/v1/invoke", json=_body())
    assert first.json() == second.json()
    assert len(model.requests) == 1


async def test_two_concurrent_attempts_share_one_run(model, settings):
    # A retry that arrives while the first attempt still runs joins it.
    model.script = [VERDICT]
    async with _client(create_app(settings)) as client:
        responses = await asyncio.gather(client.post("/v1/invoke", json=_body()),
                                         client.post("/v1/invoke", json=_body()))
    assert [r.status_code for r in responses] == [200, 200]
    assert len(model.requests) == 1


async def test_a_verdict_survives_a_restart_in_the_state_dir(model, settings, tmp_path):
    stateful = replace(settings, state_dir=str(tmp_path / "state"))
    model.script = [VERDICT]
    async with _client(create_app(stateful)) as client:
        first = await client.post("/v1/invoke", json=_body())
    async with _client(create_app(stateful)) as client:  # a new pod, the same volume
        second = await client.post("/v1/invoke", json=_body())
    assert first.json() == second.json()
    assert len(model.requests) == 1


async def test_a_different_invocation_runs_again(model, settings):
    model.script = [VERDICT, text_reply("x"), text_reply("x"), text_reply("x")]
    async with _client(create_app(settings)) as client:
        await client.post("/v1/invoke", json=_body())
        second = await client.post("/v1/invoke", json=_body(invocation_id="upgrade-cluster-demo-hc/other"))
    assert second.json()["verdict"] == "needs_human"


async def test_a_request_outside_the_contract_is_a_422(settings):
    async with _client(create_app(settings)) as client:
        response = await client.post("/v1/invoke", json={"invocation_id": "x"})
    assert response.status_code == 422


@pytest.mark.parametrize("header", [None, "Bearer wrong", "agent-secret"])
async def test_the_token_is_required_when_set(settings, header):
    app = create_app(replace(settings, agent_api_token="agent-secret"))
    headers = {"Authorization": header} if header else {}
    async with _client(app) as client:
        response = await client.post("/v1/invoke", json=_body(), headers=headers)
    assert response.status_code == 401


async def test_the_right_token_is_accepted(model, settings):
    model.script = [VERDICT]
    app = create_app(replace(settings, agent_api_token="agent-secret"))
    async with _client(app) as client:
        response = await client.post("/v1/invoke", json=_body(), headers={"Authorization": "Bearer agent-secret"})
    assert response.status_code == 200


async def test_readyz_never_shows_the_key(settings):
    app = create_app(replace(settings, model_api_key="sk-very-secret"))
    async with _client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert "sk-very-secret" not in response.text
    assert response.json()["model"] == "lab-small"
