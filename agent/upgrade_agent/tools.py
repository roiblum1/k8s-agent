"""The agent's tools: tier 0, READ ONLY (AGENT.md #21-#22).

Typed functions, not kubectl: each takes named arguments, checks them against
an allowlist, and makes GETs only. What a tool returns is shown to the model
as DATA. A tool never raises to the loop; an error is its result, in words.

  evidence        the bundle the workflow sent, one section in full
  hosted_get      one object in the hosted cluster        (HOSTED_KINDS)
  hosted_list     objects in the hosted cluster, summarised
  hosted_logs     the tail of one pod's log in the hosted cluster
  hosted_events   events in a hosted-cluster namespace
  mce_get         one object on the MCE: this cluster's HostedCluster, its
                  NodePools, or a kind in its HCP namespace (MCE_KINDS)
  mce_list        objects of a kind there, summarised
  mce_logs        the tail of one pod's log in the HCP namespace
  mce_events      events in the HCP namespace

On the MCE the namespace is never an argument: it is this cluster's HCP
namespace (or its HostedCluster's namespace), found from the HostedCluster.
There is no write tool. Actions are proposals in the verdict (catalog.py).
"""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from typing import Any

from upgrade_agent import kube
from upgrade_agent.config import Settings
from upgrade_agent.contract import AgentRequest
from upgrade_agent.kube import HOSTED_KINDS, MCE_KINDS, KubeError, KubeReader

SUBMIT_VERDICT = "submit_verdict"
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_SELECTOR = re.compile(r"[A-Za-z0-9_./=!,()\- ]{0,256}")
_MAX_TAIL = 300
_MAX_ITEMS = 200


def _string(description: str, enum: list[str] | None = None) -> dict[str, Any]:
    return {"type": "string", "description": description} | ({"enum": enum} if enum else {})


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }}


def specs(evidence_sections: list[str], catalog_kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """The OpenAI `tools` list: every read tool, and submit_verdict last."""
    hosted_kinds, mce_kinds = sorted(HOSTED_KINDS), sorted(MCE_KINDS)
    tail = {"type": "integer", "description": f"lines from the end, at most {_MAX_TAIL}"}
    return [
        _function("evidence", "One section of the evidence bundle the workflow collected, in full.",
                  {"section": _string("section name", evidence_sections or None)}, ["section"]),
        _function("hosted_get", "Read one object in the hosted cluster (never a Secret).",
                  {"kind": _string("kind", hosted_kinds), "name": _string("object name"),
                   "namespace": _string("namespace; omit for cluster-scoped kinds")}, ["kind", "name"]),
        _function("hosted_list", "List objects of one kind in the hosted cluster, one summary line each.",
                  {"kind": _string("kind", hosted_kinds),
                   "namespace": _string("namespace; omit for all namespaces or a cluster-scoped kind"),
                   "label_selector": _string("e.g. app=image-pruner"),
                   "field_selector": _string("e.g. status.phase!=Running")}, ["kind"]),
        _function("hosted_logs", "The last lines of one pod's log in the hosted cluster.",
                  {"namespace": _string("namespace"), "pod": _string("pod name"),
                   "container": _string("container; omit for a single-container pod"),
                   "previous": {"type": "boolean", "description": "the previous (crashed) container's log"},
                   "tail_lines": tail}, ["namespace", "pod"]),
        _function("hosted_events", "Events in one hosted-cluster namespace, newest first.",
                  {"namespace": _string("namespace"),
                   "warning_only": {"type": "boolean", "description": "only type=Warning (default true)"}},
                  ["namespace"]),
        _function("mce_get", "Read one object on the MCE: this cluster's HostedCluster or a NodePool, or an "
                             "object in its HCP namespace. The namespace is implied.",
                  {"kind": _string("kind", mce_kinds), "name": _string("object name")}, ["kind", "name"]),
        _function("mce_list", "List objects of one kind for this cluster on the MCE (its NodePools, or a kind in "
                              "its HCP namespace), one summary line each.",
                  {"kind": _string("kind", mce_kinds), "label_selector": _string("optional label selector")},
                  ["kind"]),
        _function("mce_logs", "The last lines of one pod's log in this cluster's HCP namespace on the MCE.",
                  {"pod": _string("pod name"), "container": _string("container; omit for a single-container pod"),
                   "previous": {"type": "boolean", "description": "the previous container's log"},
                   "tail_lines": tail}, ["pod"]),
        _function("mce_events", "Events in this cluster's HCP namespace on the MCE, newest first.",
                  {"warning_only": {"type": "boolean", "description": "only type=Warning (default true)"}}, []),
        _function(SUBMIT_VERDICT, "Finish: your diagnosis, verdict and proposed actions. Call exactly once, last.",
                  {"diagnosis": _string("what is wrong and why, with the evidence that shows it"),
                   "confidence": _string("verified: you saw it; observed: strongly indicated; hypothesis: a guess",
                                         ["verified", "observed", "hypothesis"]),
                   "verdict": _string("retry_gate: the cause is gone or transient, re-run the same gate; "
                                      "needs_human: a person must act or approve; abort_recommended: the "
                                      "upgrade should be stopped (a person decides)",
                                      ["retry_gate", "needs_human", "abort_recommended"]),
                   "proposals": {"type": "array", "description": "actions for a person to approve; may be empty",
                                 "items": {"type": "object", "additionalProperties": False,
                                           "required": ["kind", "target", "rationale"],
                                           "properties": {
                                               "kind": _string("catalog kind", list(catalog_kinds)),
                                               "target": _string("exactly as the catalog says for the kind"),
                                               "uid": _string("the object's metadata.uid as you read it"),
                                               "rationale": _string("why this is the smallest safe fix")}}}},
                  ["diagnosis", "confidence", "verdict"]),
    ]


def _pruned(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(obj.get("metadata") or {})
    metadata.pop("managedFields", None)
    annotations = {k: v for k, v in (metadata.get("annotations") or {}).items()
                   if k != "kubectl.kubernetes.io/last-applied-configuration"}
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    return {**obj, "metadata": metadata}


def _conditions(obj: dict[str, Any]) -> str:
    parts = []
    for c in (obj.get("status") or {}).get("conditions") or []:
        reason = f"({c.get('reason')})" if c.get("reason") else ""
        parts.append(f"{c.get('type')}={c.get('status')}{reason}")
    return " ".join(parts)


def summary(kind: str, obj: dict[str, Any]) -> str:
    """One line for one object: who it is and the state a diagnosis needs."""
    metadata, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    who = f"{metadata.get('namespace') + '/' if metadata.get('namespace') else ''}{metadata.get('name')}"
    if kind == "Event":
        involved = obj.get("involvedObject") or {}
        when = obj.get("lastTimestamp") or obj.get("eventTime") or metadata.get("creationTimestamp") or ""
        return (f"{when} {obj.get('type')} {involved.get('kind')}/{involved.get('name')} {obj.get('reason')} "
                f"x{obj.get('count') or 1}: {(obj.get('message') or '')[:300]}")
    parts = [who, f"uid={metadata.get('uid')}"]
    if kind == "Pod":
        statuses = status.get("containerStatuses") or []
        ready = sum(1 for s in statuses if s.get("ready"))
        restarts = sum(int(s.get("restartCount") or 0) for s in statuses)
        reasons = sorted({
            (s.get("state") or {}).get(state, {}).get("reason")
            for s in statuses for state in ("waiting", "terminated")
            if (s.get("state") or {}).get(state, {}).get("reason")
        })
        owners = ",".join(f"{o.get('kind')}/{o.get('name')}" for o in metadata.get("ownerReferences") or [])
        parts += [f"phase={status.get('phase')}", f"ready={ready}/{len(statuses)}", f"restarts={restarts}",
                  f"node={spec.get('nodeName')}"] + ([f"reasons={','.join(reasons)}"] if reasons else []) \
            + ([f"owner={owners}"] if owners else [])
    elif kind == "Job":
        owners = ",".join(f"{o.get('kind')}/{o.get('name')}" for o in metadata.get("ownerReferences") or [])
        parts += [f"active={status.get('active', 0)}", f"succeeded={status.get('succeeded', 0)}",
                  f"failed={status.get('failed', 0)}"] + ([f"owner={owners}"] if owners else [])
    elif kind == "PodDisruptionBudget":
        parts += [f"disruptionsAllowed={status.get('disruptionsAllowed')}",
                  f"currentHealthy={status.get('currentHealthy')}", f"desiredHealthy={status.get('desiredHealthy')}"]
    elif kind == "Node":
        parts += [f"unschedulable={bool(spec.get('unschedulable'))}"]
    elif kind == "CronJob":
        parts += [f"schedule={spec.get('schedule')!r}", f"suspend={bool(spec.get('suspend'))}",
                  f"lastSchedule={status.get('lastScheduleTime')}", f"lastSuccess={status.get('lastSuccessfulTime')}"]
    for field in ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas", "numberReady",
                  "desiredNumberScheduled", "version"):
        if field in status:
            parts.append(f"{field}={status[field]}")
    if kind in ("Deployment", "StatefulSet", "NodePool") and "replicas" in spec:
        parts.append(f"spec.replicas={spec['replicas']}")
    conditions = _conditions(obj)
    return " ".join(parts) + (f" | {conditions}" if conditions else "")


class Toolbox:
    """The tools for ONE invocation: its request, and readers opened with the agent's own identities."""

    def __init__(self, request: AgentRequest, settings: Settings):
        self._request = request
        self._settings = settings
        self._stack = AsyncExitStack()
        self._mce: KubeReader | None = None
        self._mce_error: str | None = None
        self._hosted: KubeReader | None = None
        self._hosted_error: str | None = None
        self._hosted_cluster: dict[str, Any] | None = None

    async def __aenter__(self) -> Toolbox:
        target = kube.in_cluster_target(self._settings.mce_token_file, self._settings.mce_ca_file)
        if target is None:
            self._mce_error = "the MCE API is not reachable from here (no in-cluster ServiceAccount)"
        else:
            self._mce = await self._stack.enter_async_context(
                KubeReader(target, timeout=self._settings.kube_timeout_seconds))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    # --- dispatch -------------------------------------------------------------

    async def call(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool; its result as text, an error included. Never raises."""
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"error: no tool named {name!r}"
        try:
            text = await handler(**args)
        except TypeError as err:
            return f"error: bad arguments for {name}: {err}"
        except (KubeError, ValueError) as err:
            return f"error: {err}"
        limit = self._settings.tool_result_chars
        return text if len(text) <= limit else text[:limit] + f"\n[cut: {len(text) - limit} more characters]"

    # --- the evidence ---------------------------------------------------------

    async def _tool_evidence(self, section: str) -> str:
        sections = self._request.evidence.sections
        if section not in sections:
            return f"error: no section {section!r}; the bundle has {', '.join(sections) or 'none'}"
        return sections[section]

    # --- the hosted cluster ---------------------------------------------------

    async def _hosted_reader(self) -> KubeReader:
        if self._hosted is None and self._hosted_error is None:
            server = None
            if self._settings.hosted_api_server:
                hcp_namespace = await self._hcp_namespace()
                server = self._settings.hosted_api_server.format(hcp_namespace=hcp_namespace)
            try:
                target = kube.hosted_target(self._settings.hosted_credentials_dir, self._request.cluster, server)
                self._hosted = await self._stack.enter_async_context(
                    KubeReader(target, timeout=self._settings.kube_timeout_seconds))
            except KubeError as err:
                self._hosted_error = str(err)
        if self._hosted is None:
            raise KubeError(f"the hosted cluster cannot be read: {self._hosted_error}")
        return self._hosted

    @staticmethod
    def _kind(kinds: dict[str, kube.Kind], kind: str) -> kube.Kind:
        if kind.lower() in ("secret", "secrets"):
            raise ValueError("Secrets are never read")
        if kind not in kinds:
            raise ValueError(f"kind {kind!r} is not readable here; one of {', '.join(sorted(kinds))}")
        return kinds[kind]

    @staticmethod
    def _name(value: str | None, what: str, required: bool = True) -> str | None:
        if value in (None, "") and not required:
            return None
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise ValueError(f"{what} {value!r} is not a Kubernetes name")
        return value

    @staticmethod
    def _selector(value: str | None, what: str) -> str | None:
        if not value:
            return None
        if not _SELECTOR.fullmatch(value):
            raise ValueError(f"{what} {value!r} is not a selector")
        return value

    async def _tool_hosted_get(self, kind: str, name: str, namespace: str | None = None) -> str:
        spec = self._kind(HOSTED_KINDS, kind)
        namespace = self._name(namespace, "namespace", required=spec.namespaced)
        obj = await (await self._hosted_reader()).get(spec.path(namespace, self._name(name, "name")))
        return json.dumps(_pruned(obj), indent=1, default=str)

    async def _list(self, reader: KubeReader, kind: str, path: str, **params: Any) -> str:
        listed = await reader.get(path, {**params, "limit": _MAX_ITEMS})
        items = listed.get("items") or []
        lines = [summary(kind, item) for item in items]
        more = " (more exist)" if (listed.get("metadata") or {}).get("continue") else ""
        return f"{len(items)} {kind}{'' if len(items) == 1 else 's'}{more}\n" + "\n".join(lines)

    async def _tool_hosted_list(self, kind: str, namespace: str | None = None, label_selector: str | None = None,
                                field_selector: str | None = None) -> str:
        spec = self._kind(HOSTED_KINDS, kind)
        namespace = self._name(namespace, "namespace", required=False) if spec.namespaced else None
        return await self._list(await self._hosted_reader(), kind, spec.path(namespace),
                                labelSelector=self._selector(label_selector, "label_selector"),
                                fieldSelector=self._selector(field_selector, "field_selector"))

    @staticmethod
    def _log_params(container: str | None, previous: bool | None, tail_lines: int | None) -> dict[str, Any]:
        tail = max(1, min(int(tail_lines or 100), _MAX_TAIL))
        return {"container": container or None, "previous": "true" if previous else None,
                "tailLines": tail, "limitBytes": 65536}

    async def _tool_hosted_logs(self, namespace: str, pod: str, container: str | None = None,
                                previous: bool | None = None, tail_lines: int | None = None) -> str:
        path = HOSTED_KINDS["Pod"].path(self._name(namespace, "namespace"), self._name(pod, "pod")) + "/log"
        self._name(container, "container", required=False)
        return await (await self._hosted_reader()).text(path, self._log_params(container, previous, tail_lines)) \
            or "(empty log)"

    async def _events(self, reader: KubeReader, namespace: str, warning_only: bool | None) -> str:
        listed = await reader.get(HOSTED_KINDS["Event"].path(namespace),
                                  {"fieldSelector": "type=Warning" if warning_only is not False else None,
                                   "limit": 500})
        items = listed.get("items") or []
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        lines = [summary("Event", e) for e in items[:60]]
        return f"{len(items)} events in {namespace}\n" + "\n".join(lines)

    async def _tool_hosted_events(self, namespace: str, warning_only: bool | None = None) -> str:
        return await self._events(await self._hosted_reader(), self._name(namespace, "namespace"), warning_only)

    # --- the MCE ----------------------------------------------------------------

    def _mce_reader(self) -> KubeReader:
        if self._mce is None:
            raise KubeError(self._mce_error or "the MCE API is not configured")
        return self._mce

    @property
    def _hc_name(self) -> str:
        """The HostedCluster's own name: the request's, else the day1 cluster's (older workflows)."""
        return self._request.hosted_cluster or self._request.cluster

    async def _hosted_cluster_object(self) -> dict[str, Any]:
        """This cluster's HostedCluster on the MCE: by name in the namespace the request gives, else found by
        name in any namespace (one, or an error)."""
        if self._hosted_cluster is None:
            reader, name = self._mce_reader(), self._hc_name
            if self._request.hosted_cluster_namespace:
                self._hosted_cluster = await reader.get(
                    kube.HOSTED_CLUSTER.path(self._request.hosted_cluster_namespace, name))
                return self._hosted_cluster
            listed = await reader.get(kube.HOSTED_CLUSTER.path(None))
            found = [hc for hc in listed.get("items") or [] if (hc.get("metadata") or {}).get("name") == name]
            if len(found) != 1:
                where = ", ".join(sorted((hc.get("metadata") or {}).get("namespace", "?") for hc in found))
                raise KubeError(f"{len(found)} HostedClusters named {name} on the MCE"
                                + (f" (in {where})" if where else ""))
            self._hosted_cluster = found[0]
        return self._hosted_cluster

    async def _hcp_namespace(self) -> str:
        """HyperShift's control-plane namespace: `<namespace>-<name, dots as dashes>`."""
        metadata = (await self._hosted_cluster_object()).get("metadata") or {}
        return f"{metadata.get('namespace')}-{str(metadata.get('name')).replace('.', '-')}"

    async def _mce_namespace(self, kind: str) -> str:
        if kind in ("HostedCluster", "NodePool"):
            return (await self._hosted_cluster_object())["metadata"]["namespace"]
        return await self._hcp_namespace()

    async def _tool_mce_get(self, kind: str, name: str) -> str:
        spec = self._kind(MCE_KINDS, kind)
        name = self._name(name, "name")
        if kind == "HostedCluster" and name != self._hc_name:
            raise ValueError(f"only this cluster's HostedCluster ({self._hc_name}) is readable")
        obj = await self._mce_reader().get(spec.path(await self._mce_namespace(kind), name))
        if kind == "NodePool" and (obj.get("spec") or {}).get("clusterName") != self._hc_name:
            raise ValueError(f"NodePool {name} does not belong to HostedCluster {self._hc_name}")
        return json.dumps(_pruned(obj), indent=1, default=str)

    async def _tool_mce_list(self, kind: str, label_selector: str | None = None) -> str:
        spec = self._kind(MCE_KINDS, kind)
        if kind == "HostedCluster":
            return summary(kind, await self._hosted_cluster_object())
        selector = self._selector(label_selector, "label_selector")
        reader = self._mce_reader()
        if kind == "NodePool":
            listed = await reader.get(spec.path(await self._mce_namespace(kind)), {"labelSelector": selector})
            mine = [np for np in listed.get("items") or []
                    if (np.get("spec") or {}).get("clusterName") == self._hc_name]
            return f"{len(mine)} NodePools\n" + "\n".join(summary(kind, np) for np in mine)
        return await self._list(reader, kind, spec.path(await self._mce_namespace(kind)), labelSelector=selector)

    async def _tool_mce_logs(self, pod: str, container: str | None = None, previous: bool | None = None,
                             tail_lines: int | None = None) -> str:
        path = MCE_KINDS["Pod"].path(await self._hcp_namespace(), self._name(pod, "pod")) + "/log"
        self._name(container, "container", required=False)
        return await self._mce_reader().text(path, self._log_params(container, previous, tail_lines)) or "(empty log)"

    async def _tool_mce_events(self, warning_only: bool | None = None) -> str:
        return await self._events(self._mce_reader(), await self._hcp_namespace(), warning_only)
