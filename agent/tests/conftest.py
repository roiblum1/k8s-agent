"""A fake OpenAI-compatible endpoint, fake API servers, and a request like the workflow sends.

No network: respx answers every HTTP call. The fake model is a script of
replies; each reply may be a function of the request the agent sent, so a
test can assert on what the model was shown.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from upgrade_agent.config import Settings
from upgrade_agent.contract import AgentBudget, AgentRequest, CheckResult, EvidenceBundle

MODEL_URL = "http://model.test/v1"
HOSTED_URL = "https://hosted-api.test:6443"
MCE_HOST = "mce-api.test"

# The pre-flight failure seen live (experiment 3 of the design, 2026-10-31): one
# image-pruner Job failed and left image-registry Degraded.
PRUNER_EVIDENCE = {
    "hostedcluster": "Available=True ClusterVersionSucceeding=True Progressing=False",
    "nodepools": "workers at 4.20.18:\nReady=True UpdatingVersion=False",
    "clusteroperators": "image-registry (Degraded=True: ImagePrunerJobFailed)",
    "argo": "Synced/Healthy at 0123abc",
    "data-plane": "every ingress, image-registry and console Pod Running",
    "events": "2026-10-31T00:05:12Z Job/openshift-image-registry/image-pruner-29890999 BackoffLimitExceeded x1: "
              "Job has reached the specified backoff limit",
}

FAILED_PRUNER_JOB = {
    "metadata": {"name": "image-pruner-29890999", "namespace": "openshift-image-registry", "uid": "5b2c-9a1e",
                 "ownerReferences": [{"kind": "CronJob", "name": "image-pruner"}],
                 "managedFields": [{"manager": "kube-controller-manager"}]},
    "status": {"failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]},
}


def make_request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "invocation_id": "upgrade-cluster-demo-hc/8f14e45f-ceea-4672-9b1a-0a6c7c0e3e0d",
        "workflow_id": "upgrade-cluster-demo-hc",
        "mce_cluster": "ocp4-mce-test",
        "cluster": "demo-hc",
        "profile": "generic",
        "phase": "preflight",
        "step": {"kind": "control-plane", "version": "4.20.18", "reason": "target"},
        "from_version": "4.19.53",
        "gate": "preflight",
        "failing_checks": [CheckResult(name="cluster-operators", passed=False, expected="all Available, none Degraded",
                                       observed="image-registry (Degraded=True: ImagePrunerJobFailed)")],
        "evidence": EvidenceBundle(sections=dict(PRUNER_EVIDENCE)),
        "budget": AgentBudget(),
    }
    fields.update(overrides)
    return AgentRequest.model_validate(fields)


def tool_call(name: str, call_id: str = "call-1", **arguments: Any) -> dict[str, Any]:
    """A chat completion whose message calls one tool, natively."""
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def text_reply(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class FakeModel:
    """Answers POST /chat/completions from a script; records every request body."""

    script: list[Reply] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.script:
            return httpx.Response(500, json={"error": {"message": "script exhausted"}})
        reply = self.script.pop(0)
        return httpx.Response(200, json=reply(body) if callable(reply) else reply)


@pytest.fixture
def mock_http():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def model(mock_http) -> FakeModel:
    fake = FakeModel()
    mock_http.post(f"{MODEL_URL}/chat/completions").mock(side_effect=fake)
    return fake


@pytest.fixture
def hosted_credentials(tmp_path: Path) -> Path:
    """The agent's own (token) kubeconfig for demo-hc, as the chart would mount it."""
    directory = tmp_path / "hosted" / "demo-hc"
    directory.mkdir(parents=True)
    (directory / "kubeconfig").write_text(f"""
apiVersion: v1
kind: Config
current-context: agent
contexts: [{{name: agent, context: {{cluster: hc, user: agent}}}}]
clusters: [{{name: hc, cluster: {{server: {HOSTED_URL}}}}}]
users: [{{name: agent, user: {{token: agent-token}}}}]
""")
    return tmp_path / "hosted"


@pytest.fixture
def mce_in_cluster(tmp_path: Path, monkeypatch) -> Path:
    """The pod's ServiceAccount on the MCE."""
    token = tmp_path / "sa-token"
    token.write_text("mce-sa-token")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", MCE_HOST)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    return token


@pytest.fixture
def settings(tmp_path: Path, hosted_credentials: Path, mce_in_cluster: Path) -> Settings:
    return Settings(
        model_base_url=MODEL_URL,
        model_name="lab-small",
        hosted_credentials_dir=str(hosted_credentials),
        hosted_api_server="",  # the kubeconfig's own server, so no HostedCluster lookup is needed
        mce_token_file=str(mce_in_cluster),
        mce_ca_file=str(tmp_path / "no-ca.crt"),
        wall_clock_margin_seconds=0,
    )
