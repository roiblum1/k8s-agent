"""The workflow's agent contract, VENDORED from team-redbull/workflows.

A copy of exactly the models the upgrade-agent activity sends and receives,
from `shared/models/cluster_upgrade.py` there: `AgentRequest` (and what it
holds) in, `AgentVerdict` (and what it holds) out. Nothing here may differ from
the original on the wire. Two guards keep it so:

  * `contract.schema.json` beside this file is the JSON schema of both models;
    this repo's tests/test_contract.py fails when this module stops matching it;
  * the workflows repo's tests/test_upgrade_agent_contract.py renders the
    ORIGINAL models into the same snapshot (and, given the path of this folder,
    compares this module directly), so a change there fails until it is copied
    here too.

To change the contract: change it in the workflows repo first, copy the class
here unchanged, regenerate the snapshot (`UPDATE_CONTRACT=1 pytest
tests/test_contract.py`) and copy the snapshot to the workflows repo.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class StepKind(str, Enum):
    """What a step moves: the HostedCluster, or NodePools."""

    CONTROL_PLANE = "control-plane"
    WORKERS = "workers"


class StepReason(str, Enum):
    """Why the resolver put a step in the plan — shown to the approver."""

    LADDER = "ladder"  # an intermediate rung of the ladder
    TARGET = "target"  # the requested target release
    SKEW = "skew"  # workers must move before the control plane passes the skew limit
    RUNG_REQUIRED = "rung-required"  # the rung has dataPlane: required
    FINAL = "final"  # workers follow the control plane to the target at the end


class UpgradeStep(BaseModel):
    """One step of a plan: move the control plane, or a set of NodePools, to `version`."""

    kind: StepKind
    version: str
    reason: StepReason
    # WORKERS only: every NodePool below `version` when the step runs, in the
    # order the cluster's profile will roll them. Empty for CONTROL_PLANE.
    nodepools: list[str] = Field(default_factory=list)


class ClusterProfile(str, Enum):
    """How a cluster's NodePools roll (DESIGN D7): derived from zone labels."""

    GENERIC = "generic"
    ZONED = "zoned"


class CheckFailure(str, Enum):
    """What a failed check does to the run (the "On failure" column of DESIGN §7)."""

    FAILED = "failed"  # the run fails; nothing was changed
    NEEDS_HUMAN = "needs-human"
    NOTIFY_OWNER = "notify-owner"  # needs-human, and the owner is the one to act
    WAIT = "wait"  # re-checked for a while, then needs-human


class CheckResult(BaseModel):
    name: str
    passed: bool
    expected: str = ""
    observed: str = ""
    on_failure: CheckFailure = CheckFailure.NEEDS_HUMAN


class EvidenceBundle(BaseModel):
    """Read-only evidence for the agent, size-capped, never a Secret.

    `sections` maps a heading ("hostedcluster", "nodepools", "clusteroperators",
    "events", "argo", "data-plane") to its rendered text. For a NodePool, also
    "drain": its cordoned nodes, the pods still on them with their state, the
    PodDisruptionBudgets covering those pods with disruptionsAllowed, and the
    hosted-cluster-config-operator's last `drainer` lines (D17).
    """

    sections: dict[str, str] = Field(default_factory=dict)
    truncated: bool = False


class AgentBudget(BaseModel):
    wall_clock_seconds: int = 900  # judge latency counts against it (AGENT.md #24)
    max_tool_calls: int = 40
    max_tier1_actions: int = 2  # moot while tier1_enabled is False
    max_model_calls: int = 20  # chat completions, repair prompts included


class AgentRequest(BaseModel):
    """One invocation of the upgrade agent, at one failed gate (DESIGN §8.1).

    `invocation_id` is unique per invocation and stable across the activity's
    retries: the tool server keys its budget counters and executed tier-1 calls
    on it, so a retry can neither repeat an action nor reset the budget.
    """

    invocation_id: str
    workflow_id: str
    mce_cluster: str
    cluster: str
    profile: ClusterProfile
    phase: str
    step: UpgradeStep
    from_version: str | None = None
    nodepool: str | None = None
    gate: str
    failing_checks: list[CheckResult] = Field(default_factory=list)
    evidence: EvidenceBundle
    budget: AgentBudget = Field(default_factory=AgentBudget)
    tier1_enabled: bool = False
    # The HostedCluster's own name and namespace, where the agent reads on the
    # MCE (as on HostedClusterRequest); None: the one named `cluster`.
    hosted_cluster: str | None = None
    hosted_cluster_namespace: str | None = None


class ToolCallRecord(BaseModel):
    tool: str
    args: dict[str, str] = Field(default_factory=dict)
    tier: Literal[0, 1]
    judged: Literal["n/a", "approved", "rejected"] = "n/a"
    result: str = ""  # truncated
    at: datetime


class ProposedAction(BaseModel):
    """Tier 2: run by the workflow, with ITS identity, only after a person approves."""

    action_id: str
    kind: str  # from the typed tool catalog
    target: str
    args: dict[str, str] = Field(default_factory=dict)
    rationale: str = ""


class AgentVerdict(BaseModel):
    """What the agent concluded. "continue" and "skip" are deliberately not verdicts."""

    diagnosis: str
    confidence: Literal["verified", "observed", "hypothesis"]
    actions_taken: list[ToolCallRecord] = Field(default_factory=list)
    actions_proposed: list[ProposedAction] = Field(default_factory=list)
    verdict: Literal["retry_gate", "needs_human", "abort_recommended"]


# The two models on the wire, in the order the snapshot lists them.
CONTRACT_MODELS: tuple[type[BaseModel], ...] = (AgentRequest, AgentVerdict)


def contract_schema() -> dict:
    """Both models' JSON schemas, descriptions dropped (docstrings are not the wire)."""
    return {model.__name__: _without_descriptions(model.model_json_schema()) for model in CONTRACT_MODELS}


def _without_descriptions(node):
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node
