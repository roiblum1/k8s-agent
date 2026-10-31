"""The tier-2 catalog: the only actions the agent may PROPOSE (DESIGN §8.3).

The same rules as the workflow's `activities/cluster_upgrade/action_catalog.py`,
which refuses anything else when a person approves it. Checking here first
means a proposal the workflow could never run is dropped, and reported, before
anyone is asked to approve it. The agent never executes any of these: the
workflow does, with its own identity, after approval (AGENT.md #25).

Permissions live in that catalog and in the admission policies, never in a
prompt, a runbook or a proposal (AGENT.md #23). A new kind here without the
same change there would only produce proposals the workflow refuses.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from upgrade_agent.contract import ProposedAction

DELETE_POD = "delete_pod"  # a pod in openshift-* on the hosted cluster
DELETE_HCP_POD = "delete_hcp_pod"  # a pod in this cluster's HCP namespace on the MCE
RERUN_CRONJOB = "rerun_cronjob"  # a Job from a CronJob in openshift-* on the hosted cluster
DELETE_FAILED_JOB = "delete_failed_job"  # a FAILED Job in openshift-* on the hosted cluster
KINDS = (DELETE_POD, DELETE_HCP_POD, RERUN_CRONJOB, DELETE_FAILED_JOB)

DESCRIPTIONS = {
    DELETE_POD: "delete one pod on the hosted cluster; target <namespace>/<pod>, namespace openshift-* "
                "except openshift-ovn-kubernetes and openshift-multus; args.uid = the pod's UID as you read it",
    DELETE_HCP_POD: "delete one pod in this cluster's HCP namespace on the MCE; target <pod>, never etcd-* "
                    "or kube-apiserver-*; args.uid = the pod's UID as you read it",
    RERUN_CRONJOB: "create one Job from a CronJob on the hosted cluster; target <namespace>/<cronjob>, "
                   "namespace openshift-*",
    DELETE_FAILED_JOB: "delete one Job whose Failed condition is True on the hosted cluster; target "
                       "<namespace>/<job>, namespace openshift-*; args.uid = the Job's UID as you read it",
}

_HOSTED_NAMESPACE_PREFIX = "openshift-"
_HOSTED_DENIED_NAMESPACES = ("openshift-ovn-kubernetes", "openshift-multus")
_HCP_DENIED_PODS = ("etcd-", "kube-apiserver-")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
# The only argument any kind reads: a delete's precondition.
ALLOWED_ARGS = ("uid",)


class Refused(ValueError):
    """A proposal outside the catalog; the message says why."""


@dataclass(frozen=True)
class Proposal:
    """A model's proposal before it has an id."""

    kind: str
    target: str
    args: dict[str, str]
    rationale: str


def check(kind: str, target: str) -> None:
    """Raise Refused unless (kind, target) is in the catalog — the workflow's rules, verbatim."""
    if kind in (DELETE_POD, RERUN_CRONJOB, DELETE_FAILED_JOB):
        namespace, _, name = target.partition("/")
        if not (_NAME.fullmatch(namespace) and _NAME.fullmatch(name)):
            raise Refused("the target must be <namespace>/<name>")
        if not namespace.startswith(_HOSTED_NAMESPACE_PREFIX):
            raise Refused(f"only namespaces {_HOSTED_NAMESPACE_PREFIX}* are in the catalog")
        if kind == DELETE_POD and namespace in _HOSTED_DENIED_NAMESPACES:
            raise Refused(f"pods in {namespace} are never deleted by the upgrade")
        return
    if kind == DELETE_HCP_POD:
        if not _NAME.fullmatch(target):
            raise Refused("the target must be the name of a pod in the cluster's HCP namespace")
        if target.startswith(_HCP_DENIED_PODS):
            raise Refused(f"{', '.join(p + '*' for p in _HCP_DENIED_PODS)} pods are never deleted by the upgrade")
        return
    raise Refused(f"kind {kind!r} is not in the catalog ({', '.join(KINDS)})")


def action_id(invocation_id: str, index: int) -> str:
    """A URL-safe id, the same on every retry of one invocation (the API path carries it)."""
    return f"{hashlib.sha256(invocation_id.encode()).hexdigest()[:8]}-{index}"


def accept(invocation_id: str, proposals: list[Proposal]) -> tuple[list[ProposedAction], list[str]]:
    """The proposals the workflow could run, with ids; and one line per proposal dropped, saying why.

    Arguments other than `uid` are dropped from a proposal that is kept (and
    said so): no kind reads them, and nothing a model writes may ride along to
    the approver as if it meant something.
    """
    kept: list[ProposedAction] = []
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        kind, target = proposal.kind.strip(), proposal.target.strip()
        try:
            check(kind, target)
        except Refused as why:
            notes.append(f"dropped proposal {kind} {target!r}: {why}")
            continue
        if (kind, target) in seen:
            continue
        seen.add((kind, target))
        extra = sorted(set(proposal.args) - set(ALLOWED_ARGS))
        if extra:
            notes.append(f"proposal {kind} {target}: ignored args {', '.join(extra)} (only uid is read)")
        kept.append(ProposedAction(
            action_id=action_id(invocation_id, len(kept) + 1),
            kind=kind,
            target=target,
            args={k: v for k, v in proposal.args.items() if k in ALLOWED_ARGS and v},
            rationale=proposal.rationale.strip()[:1000],
        ))
    return kept, notes
