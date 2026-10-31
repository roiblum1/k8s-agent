"""Read-only access to two Kubernetes APIs: the hosted cluster's and the MCE's.

GET only. There is no method here that writes, and the kinds a caller may
name are a fixed allowlist (`HOSTED_KINDS`, `MCE_KINDS`) with no Secret in
either. The agent's identities are its own (AGENT.md #26): on the MCE the
pod's ServiceAccount (a read-only Role on HCP namespaces), on a hosted
cluster a credential mounted from a Secret — never the HostedCluster's admin
kubeconfig, which the workflow's own limb uses.

Nothing a credential holds reaches a log line, an error or the model: errors
carry the HTTP status and the API server's message, nothing else.
"""

from __future__ import annotations

import base64
import os
import ssl
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass(frozen=True)
class Kind:
    group: str  # "" for the core group
    version: str
    plural: str
    namespaced: bool

    def path(self, namespace: str | None, name: str | None = None) -> str:
        base = f"/api/{self.version}" if not self.group else f"/apis/{self.group}/{self.version}"
        if self.namespaced and namespace:
            base += f"/namespaces/{namespace}"
        base += f"/{self.plural}"
        return f"{base}/{name}" if name else base


def _k(group: str, version: str, plural: str, namespaced: bool = True) -> Kind:
    return Kind(group, version, plural, namespaced)


# What the agent may read INSIDE a hosted cluster. Never Secrets.
HOSTED_KINDS: dict[str, Kind] = {
    "Pod": _k("", "v1", "pods"),
    "Node": _k("", "v1", "nodes", False),
    "Event": _k("", "v1", "events"),
    "Namespace": _k("", "v1", "namespaces", False),
    "Service": _k("", "v1", "services"),
    "ConfigMap": _k("", "v1", "configmaps"),
    "PersistentVolumeClaim": _k("", "v1", "persistentvolumeclaims"),
    "Deployment": _k("apps", "v1", "deployments"),
    "StatefulSet": _k("apps", "v1", "statefulsets"),
    "DaemonSet": _k("apps", "v1", "daemonsets"),
    "ReplicaSet": _k("apps", "v1", "replicasets"),
    "Job": _k("batch", "v1", "jobs"),
    "CronJob": _k("batch", "v1", "cronjobs"),
    "PodDisruptionBudget": _k("policy", "v1", "poddisruptionbudgets"),
    "ClusterOperator": _k("config.openshift.io", "v1", "clusteroperators", False),
    "ClusterVersion": _k("config.openshift.io", "v1", "clusterversions", False),
    "ImageDigestMirrorSet": _k("config.openshift.io", "v1", "imagedigestmirrorsets", False),
    "IngressController": _k("operator.openshift.io", "v1", "ingresscontrollers"),
    "ImagePruner": _k("imageregistry.operator.openshift.io", "v1", "imagepruners", False),
    "ImageRegistryConfig": _k("imageregistry.operator.openshift.io", "v1", "configs", False),
    "Route": _k("route.openshift.io", "v1", "routes"),
    "CertificateSigningRequest": _k("certificates.k8s.io", "v1", "certificatesigningrequests", False),
    "ValidatingWebhookConfiguration": _k("admissionregistration.k8s.io", "v1", "validatingwebhookconfigurations", False),
    "MutatingWebhookConfiguration": _k("admissionregistration.k8s.io", "v1", "mutatingwebhookconfigurations", False),
}

_HYPERSHIFT = ("hypershift.openshift.io", "v1beta1")
HOSTED_CLUSTER = _k(*_HYPERSHIFT, "hostedclusters")
NODE_POOL = _k(*_HYPERSHIFT, "nodepools")

# What the agent may read ON THE MCE: these kinds in the cluster's own HCP
# namespace, plus its HostedCluster and NodePools in theirs. Never Secrets.
MCE_KINDS: dict[str, Kind] = {
    "HostedCluster": HOSTED_CLUSTER,
    "NodePool": NODE_POOL,
    "HostedControlPlane": _k(*_HYPERSHIFT, "hostedcontrolplanes"),
    "Pod": _k("", "v1", "pods"),
    "Event": _k("", "v1", "events"),
    "Service": _k("", "v1", "services"),
    "ConfigMap": _k("", "v1", "configmaps"),
    "Deployment": _k("apps", "v1", "deployments"),
    "StatefulSet": _k("apps", "v1", "statefulsets"),
    "ReplicaSet": _k("apps", "v1", "replicasets"),
    "Job": _k("batch", "v1", "jobs"),
    "PodDisruptionBudget": _k("policy", "v1", "poddisruptionbudgets"),
    "Machine": _k("cluster.x-k8s.io", "v1beta1", "machines"),
    "MachineSet": _k("cluster.x-k8s.io", "v1beta1", "machinesets"),
    "MachineDeployment": _k("cluster.x-k8s.io", "v1beta1", "machinedeployments"),
    "AgentMachine": _k("capi-provider.agent-install.openshift.io", "v1beta1", "agentmachines"),
    "KubevirtMachine": _k("infrastructure.cluster.x-k8s.io", "v1alpha1", "kubevirtmachines"),
}

assert not any(k.plural == "secrets" for k in (*HOSTED_KINDS.values(), *MCE_KINDS.values()))


class KubeError(Exception):
    """A read that did not succeed; the message is safe to show the model."""


@dataclass
class ApiTarget:
    """Where an API server is and how to authenticate to it. Never logged."""

    server: str
    token: str | None = None
    ca_pem: str | None = None
    cert_pem: str | None = None
    key_pem: str | None = None
    _tmp: list[str] = field(default_factory=list, repr=False)

    def ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cadata=self.ca_pem) if self.ca_pem else ssl.create_default_context()
        if self.cert_pem and self.key_pem:
            # load_cert_chain reads files only: a private temp dir, removed with this target.
            directory = tempfile.mkdtemp(prefix="hosted-cred-")
            self._tmp.append(directory)
            cert, key = Path(directory, "tls.crt"), Path(directory, "tls.key")
            cert.write_text(self.cert_pem)
            key.write_text(self.key_pem)
            os.chmod(key, 0o600)
            context.load_cert_chain(str(cert), str(key))
        return context

    def cleanup(self) -> None:
        for directory in self._tmp:
            for child in Path(directory).glob("*"):
                child.unlink(missing_ok=True)
            Path(directory).rmdir()
        self._tmp.clear()


def _b64(value: str | None) -> str | None:
    return base64.b64decode(value).decode() if value else None


def target_from_kubeconfig(text: str, server_override: str | None = None) -> ApiTarget:
    """The current context of a kubeconfig, as an ApiTarget. Exec plugins and skip-verify are refused."""
    try:
        config = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise KubeError(f"the hosted-cluster kubeconfig is not YAML ({type(err).__name__})") from None

    def named(section: str, name: str | None) -> dict[str, Any]:
        entries = [e for e in config.get(section) or [] if isinstance(e, dict)]
        found = [e for e in entries if name is None or e.get("name") == name]
        if not found:
            raise KubeError(f"the hosted-cluster kubeconfig has no {section[:-1]} {name!r}")
        return found[0]

    context = named("contexts", config.get("current-context")).get("context") or {}
    cluster = named("clusters", context.get("cluster")).get("cluster") or {}
    user = named("users", context.get("user")).get("user") or {}
    if cluster.get("insecure-skip-tls-verify"):
        raise KubeError("the hosted-cluster kubeconfig turns TLS verification off; refused")
    if "exec" in user or "auth-provider" in user:
        raise KubeError("the hosted-cluster kubeconfig uses an exec or auth-provider plugin; use a token")
    token = user.get("token")
    if not token and user.get("tokenFile"):
        token = Path(user["tokenFile"]).read_text().strip()
    ca = _b64(cluster.get("certificate-authority-data"))
    if not ca and cluster.get("certificate-authority"):
        ca = Path(cluster["certificate-authority"]).read_text()
    target = ApiTarget(
        server=(server_override or cluster.get("server") or "").rstrip("/"),
        token=token,
        ca_pem=ca,
        cert_pem=_b64(user.get("client-certificate-data")),
        key_pem=_b64(user.get("client-key-data")),
    )
    if not target.server:
        raise KubeError("the hosted-cluster kubeconfig names no server")
    if not (target.token or (target.cert_pem and target.key_pem)):
        raise KubeError("the hosted-cluster kubeconfig carries no token or client certificate")
    return target


def hosted_target(credentials_dir: str, cluster: str, server: str | None) -> ApiTarget:
    """The agent's own credential for one hosted cluster, from `<credentials_dir>/<cluster>/`.

    Either a `kubeconfig` key, or a `token` key with an optional `ca.crt` (the
    shape of a ManagedServiceAccount's token Secret). `server`, when given,
    replaces the kubeconfig's: from the MCE's pod network the hosted API server
    is its in-cluster Service, which the kubeconfig's external name may not reach.
    """
    directory = Path(credentials_dir, cluster)
    if not directory.is_dir():
        raise KubeError(f"no hosted-cluster credential is mounted for {cluster} (expected {directory})")
    kubeconfig, token, ca = directory / "kubeconfig", directory / "token", directory / "ca.crt"
    if kubeconfig.is_file():
        return target_from_kubeconfig(kubeconfig.read_text(), server_override=server)
    if token.is_file():
        if not server:
            raise KubeError(f"a token alone for {cluster} needs HOSTED_API_SERVER to say where its API is")
        return ApiTarget(server=server.rstrip("/"), token=token.read_text().strip(),
                         ca_pem=ca.read_text() if ca.is_file() else None)
    raise KubeError(f"the credential for {cluster} has neither a kubeconfig nor a token key")


def in_cluster_target(token_file: str, ca_file: str) -> ApiTarget | None:
    """The pod's own ServiceAccount on the cluster it runs in (the MCE); None outside a cluster."""
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host or not Path(token_file).is_file():
        return None
    host = f"[{host}]" if ":" in host else host
    return ApiTarget(
        server=f"https://{host}:{port}",
        token=Path(token_file).read_text().strip(),  # read per invocation: projected tokens rotate
        ca_pem=Path(ca_file).read_text() if Path(ca_file).is_file() else None,
    )


class KubeReader:
    """GETs against one API server, inside one invocation (`async with`)."""

    def __init__(self, target: ApiTarget, *, timeout: float = 20.0):
        self._target = target
        headers = {"Accept": "application/json"}
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"
        self._client = httpx.AsyncClient(
            base_url=target.server,
            headers=headers,
            timeout=timeout,
            verify=target.ssl_context(),
        )

    async def __aenter__(self) -> KubeReader:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()
        self._target.cleanup()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = await self._client.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        except httpx.HTTPError as err:
            raise KubeError(f"API server unreachable ({type(err).__name__})") from None
        if response.status_code >= 400:
            message = ""
            try:
                message = str(response.json().get("message", ""))[:300]
            except ValueError:
                pass
            raise KubeError(f"HTTP {response.status_code}{': ' + message if message else ''}")
        return response

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._get(path, params)
        try:
            return response.json()
        except ValueError:
            raise KubeError("the API server answered with something that is not JSON") from None

    async def text(self, path: str, params: dict[str, Any] | None = None) -> str:
        return (await self._get(path, params)).text
